#!/usr/bin/env python3
"""wayback-salvage: recover archived pages and images from the Wayback Machine.

Standard library only, Python 3.9+, single-threaded by design. Subcommands:
snapshots, fetch, page, images, site-images, verify. Run with --help for
options; README.md covers the manifest, exit codes and politeness defaults.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import collections
import csv
import datetime
import email.utils
import gzip
import hashlib
import html.parser
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import tempfile
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib

# Optional decoders. Never declared as dependencies and never installed:
# used only when the interpreter already has them.
try:
    import brotli as _brotli
except ImportError:
    try:
        import brotlicffi as _brotli
    except ImportError:
        _brotli = None

try:
    from compression import zstd as _zstd  # Python 3.14+
except ImportError:
    _zstd = None

VERSION = "0.2.0"
DEFAULT_UA = "wayback-salvage/%s (+https://github.com/andrewluxem/wayback-salvage)" % VERSION

WAYBACK = "https://web.archive.org"
CDX_ENDPOINT = WAYBACK + "/cdx/search/cdx"
CDX_FIELDS = ("timestamp", "original", "statuscode", "mimetype", "digest", "length")
CDX_PAGE_SIZE = 500
LOOSE_MIME_FILTER = r"original:.*\.(png|jpe?g|gif|webp|svg|ico|avif)(\?.*)?$"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CAP = 2
EXIT_RATE = 3
EXIT_TLS = 4
EXIT_VERIFY = 5

RETRY_STATUSES = frozenset((429, 502, 503, 504))
REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
MAX_HOPS = 5
BACKOFF_BASE = 30.0
BACKOFF_CAP = 600.0
STEADY_DELAY_CAP = 60.0
LIVE_DELAY = 1.0
REQUEST_TIMEOUT = 60.0

TS_RE = re.compile(r"^\d{1,14}$")
_WAYBACK_PATH_RE = re.compile(r"^/web/(\d{1,14})([a-z]{2}_)?/+(.*)$", re.S)
_REWRITTEN_RE = re.compile(
    r"^(?:(?:https?:)?//(?:web\.)?archive\.org(?::\d+)?)?/web/(\d{1,14})(?:[a-z]{2}_)?/+(.+)$",
    re.I | re.S,
)
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

TLS_HELP = """\
TLS certificate verification failed for {host}: {reason}
Verification stays on. Fix the trust store this Python uses instead:
  - python.org Python on macOS: run "Install Certificates.command" from the
    /Applications/Python 3.x/ folder, then retry.
  - Or use Homebrew Python or the Command Line Tools /usr/bin/python3.
  - Or pass --cafile /path/to/ca-bundle.pem (SSL_CERT_FILE and SSL_CERT_DIR
    are honoured too).
"""


# ---------------------------------------------------------------- errors


class SalvageError(Exception):
    """An expected failure with a message for the user (exit 1)."""


class CdxError(SalvageError):
    pass


class NetworkError(SalvageError):
    pass


class BadUrl(SalvageError):
    """The URL could not be requested at all; affects one item only."""


class CapReached(Exception):
    """--max-requests reached (exit 2, resumable)."""


class RateLimited(Exception):
    """Retries exhausted on 429/502/503/504, resets or outage responses
    (exit 3, resumable)."""

    def __init__(self, url: str, last: str):
        super().__init__("%s (last: %s)" % (url, last))
        self.url = url
        self.last = last


class TLSError(Exception):
    """Certificate verification failed (exit 4)."""

    def __init__(self, url: str, cause: BaseException):
        super().__init__(str(cause))
        self.url = url
        self.cause = cause

    def help_text(self) -> str:
        host = urllib.parse.urlsplit(self.url).hostname or self.url
        reason = getattr(self.cause, "reason", None) or self.cause
        return TLS_HELP.format(host=host, reason=reason)


# ------------------------------------------------------------ URL helpers


def ts14(epoch: float) -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(epoch))


def iso_utc(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_fragment(url: str) -> str:
    return urllib.parse.urldefrag(url)[0]


def has_scheme(url: str) -> bool:
    return bool(_SCHEME_RE.match(url))


def fetch_target(user_url: str) -> str:
    """URL used for Wayback fetches: scheme-less input becomes http://."""
    url = strip_fragment(user_url.strip())
    return url if has_scheme(url) else "http://" + url


def cdx_target(user_url: str) -> str:
    """URL used for CDX queries: passed as given, minus any fragment."""
    return strip_fragment(user_url.strip())


def is_archive_host(host) -> bool:
    host = (host or "").lower()
    return host == "archive.org" or host.endswith(".archive.org")


def wayback_url(ts: str, original: str, mod: str = "id_") -> str:
    quoted = urllib.parse.quote(original, safe="%/:=&?~#+!$,;'@()*[]")
    return "%s/web/%s%s/%s" % (WAYBACK, ts, mod, quoted)


def _fix_scheme(rest: str) -> str:
    m = re.match(r"^(https?):/+(.*)$", rest, re.I | re.S)
    if m:
        return m.group(1).lower() + "://" + m.group(2)
    return "http://" + rest.lstrip("/")


def parse_wayback_url(url: str):
    """(timestamp, modifier, original) for a /web/<ts><mod>/<url> URL."""
    parts = urllib.parse.urlsplit(url)
    if not is_archive_host(parts.hostname):
        return None
    tail = url.split("://", 1)[-1]
    slash = tail.find("/")
    if slash < 0:
        return None
    m = _WAYBACK_PATH_RE.match(tail[slash:])
    if not m:
        return None
    return m.group(1), m.group(2) or "", _fix_scheme(m.group(3))


def unwrap_wayback(ref: str) -> str:
    """Turn a Wayback-rewritten reference back into the original URL."""
    m = _REWRITTEN_RE.match(ref)
    if not m:
        return ref
    return _fix_scheme(m.group(2))


def _remove_dot_segments(path: str) -> str:
    if "." not in path:
        return path
    segs = path.split("/")
    out = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == ".":
            if last:
                out.append("")
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
            continue
        out.append(seg)
    result = "/".join(out)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


def normalize_url(url: str) -> str:
    """Lowercase scheme and host, drop default ports and fragment, keep query."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = "[%s]" % host if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += ":%d" % port
    path = _remove_dot_segments(parts.path) or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


def own_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def site_domain(value: str) -> str:
    value = value.strip()
    if has_scheme(value):
        host = urllib.parse.urlsplit(value).hostname or ""
    else:
        host = value.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    return own_domain(host)


# A domain, host or prefix CDX listing on one of these would return captures
# for millions of unrelated sites. Only exact-URL lookups are allowed there.
SHARED_HOSTS = (
    "googleusercontent.com", "ggpht.com", "gstatic.com", "googleapis.com",
    "blogger.com", "bp.blogspot.com", "cloudinary.com", "imgix.net",
    "ctfassets.net", "squarespace-cdn.com", "wixstatic.com", "cloudfront.net",
    "amazonaws.com", "akamaihd.net", "akamaized.net", "fastly.net",
    "fbcdn.net", "twimg.com", "wp.com", "githubusercontent.com", "archive.org",
)


def is_shared_host(host: str) -> bool:
    host = own_domain(host)
    return any(host == h or host.endswith("." + h) for h in SHARED_HOSTS)


# ------------------------------------------------------------------- CDX


class CdxPage:
    def __init__(self, rows: list, resume_key=None):
        self.rows = rows
        self.resume_key = resume_key


def build_cdx_query(
    url: str,
    match_type=None,
    filters=(),
    collapse=None,
    from_ts=None,
    to_ts=None,
    limit=None,
    show_resume_key: bool = False,
    resume_key=None,
    fields=CDX_FIELDS,
) -> str:
    """CDX URL with every value percent-encoded; filter may repeat."""
    pairs = [("url", strip_fragment(url)), ("output", "json"), ("fl", ",".join(fields))]
    if match_type:
        pairs.append(("matchType", match_type))
    for f in filters:
        pairs.append(("filter", f))
    if collapse:
        pairs.append(("collapse", collapse))
    if from_ts:
        pairs.append(("from", from_ts))
    if to_ts:
        pairs.append(("to", to_ts))
    if limit is not None:
        pairs.append(("limit", str(int(limit))))
    if show_resume_key:
        pairs.append(("showResumeKey", "true"))
    if resume_key:
        # The server hands the key back already percent-encoded; decode it
        # so urlencode does not encode it twice.
        pairs.append(("resumeKey", urllib.parse.unquote_plus(resume_key)))
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="/:*")
    return CDX_ENDPOINT + "?" + query


def parse_cdx(body) -> CdxPage:
    """Parse output=json. Empty body, [] and header-only all mean no captures."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = text.strip()
    if not text:
        return CdxPage([])
    try:
        data = json.loads(text)
    except ValueError:
        raise CdxError("CDX response is not JSON: %r" % text[:160])
    if not isinstance(data, list):
        raise CdxError("unexpected CDX response shape: %r" % text[:160])
    resume_key = None
    if len(data) >= 2 and data[-2] == [] and isinstance(data[-1], list) and len(data[-1]) == 1:
        resume_key = str(data[-1][0])
        data = data[:-2]
    while data and data[-1] == []:
        data = data[:-1]
    if not data:
        return CdxPage([], resume_key)
    header = [str(h) for h in data[0]]
    rows = []
    for row in data[1:]:
        if isinstance(row, list) and len(row) == len(header):
            rows.append(dict(zip(header, ("" if v is None else str(v) for v in row))))
    return CdxPage(rows, resume_key)


# ------------------------------------------------------------- transport


class Response:
    def __init__(self, url: str, status: int, headers=None, body: bytes = b""):
        self.url = url
        self.status = int(status)
        self.body = body or b""
        self.headers = {}
        items = headers.items() if hasattr(headers, "items") else (headers or [])
        for k, v in items:
            k = str(k).lower()
            self.headers[k] = self.headers[k] + ", " + str(v) if k in self.headers else str(v)

    def header(self, name: str, default=None):
        return self.headers.get(name.lower(), default)

    def media_type(self) -> str:
        return (self.header("content-type") or "").split(";", 1)[0].strip().lower()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Make urllib hand 3xx back as a response; the client follows hops
    itself so each one is paced, counted and logged."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    def __init__(self, cafile=None, timeout: float = REQUEST_TIMEOUT):
        # Default context: CERT_REQUIRED plus hostname checks, honours
        # SSL_CERT_FILE / SSL_CERT_DIR. Verification is never disabled.
        if cafile:
            self.context = ssl.create_default_context(cafile=cafile)
        else:
            self.context = ssl.create_default_context()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.context), _NoRedirect()
        )

    def send(self, method: str, url: str, headers: dict) -> Response:
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return Response(url, r.status, r.headers.items(), r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            hdrs = e.headers.items() if e.headers is not None else []
            return Response(url, e.code, hdrs, body)


_TRANSIENT = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    socket.timeout,
    TimeoutError,
)


def classify_error(exc: BaseException) -> str:
    """'tls', 'transient' or 'fatal'. urllib wraps the SSL error in
    URLError.reason, so both the bare and the wrapped form are checked."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    for candidate in (exc, reason):
        if isinstance(candidate, ssl.SSLCertVerificationError):
            return "tls"
        if isinstance(candidate, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(candidate):
            return "tls"
    if isinstance(reason, _TRANSIENT) or isinstance(exc, _TRANSIENT):
        return "transient"
    return "fatal"


def parse_retry_after(value, now: float):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, dt.timestamp() - now)


class Limiter:
    """Minimum interval between requests plus 0 to 1 s jitter."""

    def __init__(self, delay: float, clock, sleep, rng, max_delay: float = STEADY_DELAY_CAP):
        self.delay = float(delay)
        self.max_delay = max(max_delay, self.delay)
        self.clock = clock
        self.sleep = sleep
        self.rng = rng
        self.last = None

    def wait(self) -> float:
        waited = 0.0
        if self.last is not None:
            target = self.last + self.delay + self.rng.random()
            gap = target - self.clock()
            if gap > 0:
                self.sleep(gap)
                waited = gap
        self.last = self.clock()
        return waited

    def note_429(self) -> None:
        self.delay = min(max(self.delay * 2, 1.0), self.max_delay)


class Fetched:
    def __init__(self, final_url: str, response: Response, hops: list, too_many_redirects: bool = False):
        self.final_url = final_url
        self.response = response
        self.hops = hops
        self.too_many_redirects = too_many_redirects


class Client:
    """Every HTTP request goes through here: cap, pacing, backoff, log."""

    def __init__(
        self,
        transport,
        *,
        clock=time.time,
        sleep=time.sleep,
        rng=None,
        delay: float = 6.0,
        max_requests: int = 300,
        max_retries: int = 4,
        user_agent: str = DEFAULT_UA,
        log_path=None,
        record_dir=None,
    ):
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.limiter = Limiter(delay, clock, sleep, self.rng)
        self.live_limiters = {}
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.log_path = log_path
        self.record_dir = record_dir
        self.count = 0
        self._record_seq = None

    def _limiter_for(self, host: str) -> Limiter:
        if is_archive_host(host):
            return self.limiter
        lim = self.live_limiters.get(host)
        if lim is None:
            lim = self.live_limiters[host] = Limiter(LIVE_DELAY, self.clock, self.sleep, self.rng)
        return lim

    def request(self, url: str, live: bool = False, validate=None) -> Response:
        """One request with retries. Live requests are not retried. From
        archive.org, the outage page (see is_outage) counts as a 503
        whatever its status, and validate(resp) may return another reason
        to retry like one."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        limiter = self._limiter_for(host)
        attempt = 0
        backoff = 0.0
        while True:
            if self.count >= self.max_requests:
                raise CapReached(url)
            waited = limiter.wait() + backoff
            self.count += 1
            started = self.clock()
            resp = None
            try:
                resp = self.transport.send(
                    "GET", url, {"User-Agent": self.user_agent, "Accept": "*/*"}
                )
            except (OSError, http.client.HTTPException) as exc:
                self._log(started, url, 0, 0, waited, attempt)
                kind = classify_error(exc)
                if kind == "tls":
                    raise TLSError(url, exc)
                if kind == "fatal" or live:
                    reason = getattr(exc, "reason", None) or exc
                    raise NetworkError("%s: %s" % (url, reason))
                last = type(exc).__name__
            except ValueError as exc:
                self._log(started, url, 0, 0, waited, attempt)
                raise BadUrl("%s: %s" % (url, exc))
            else:
                self._log(started, url, resp.status, len(resp.body), waited, attempt)
                self._record(url, resp)
                problem = None
                if not live and is_archive_host(host):
                    if is_outage(url, resp):
                        problem = "archive.org Temporarily Offline page (HTTP %d)" % resp.status
                    elif validate is not None:
                        problem = validate(resp)
                if problem:
                    last = problem
                else:
                    if resp.status not in RETRY_STATUSES or live:
                        return resp
                    if resp.status == 429:
                        limiter.note_429()
                    last = "HTTP %d" % resp.status
            if attempt >= self.max_retries:
                raise RateLimited(url, last)
            backoff = self._backoff(attempt, resp)
            self.sleep(backoff)
            attempt += 1

    def _backoff(self, attempt: int, resp) -> float:
        wait = BACKOFF_BASE * (2 ** attempt)
        if resp is not None:
            retry_after = parse_retry_after(resp.header("retry-after"), self.clock())
            if retry_after is not None:
                wait = max(wait, retry_after)
        return min(wait + self.rng.random(), BACKOFF_CAP)

    def get(self, url: str, live: bool = False, validate=None) -> Fetched:
        """GET following up to MAX_HOPS redirects, each a counted request."""
        hops = []
        current = url
        while True:
            resp = self.request(current, live=live, validate=validate)
            hops.append(current)
            location = resp.header("location")
            if resp.status in REDIRECT_STATUSES and location:
                if len(hops) > MAX_HOPS:
                    return Fetched(current, resp, hops, too_many_redirects=True)
                current = urllib.parse.urljoin(current, location.strip())
                continue
            return Fetched(current, resp, hops)

    def _log(self, started, url, status, nbytes, waited, attempt) -> None:
        if not self.log_path:
            return
        entry = {
            "time": iso_utc(started),
            "method": "GET",
            "url": url,
            "status": int(status),
            "bytes": int(nbytes),
            "wait": round(waited, 3),
            "attempt": attempt,
        }
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _record(self, url: str, resp: Response) -> None:
        """--record: save the raw response in the test-fixture format."""
        if not self.record_dir:
            return
        os.makedirs(self.record_dir, exist_ok=True)
        if self._record_seq is None:
            self._record_seq = sum(1 for f in os.listdir(self.record_dir) if f.endswith(".meta.json"))
        self._record_seq += 1
        parts = urllib.parse.urlsplit(url)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", parts.netloc + parts.path).strip("_")[:80] or "response"
        name = "%04d-%s" % (self._record_seq, slug)
        meta = {"url": url, "method": "GET", "status": resp.status, "headers": resp.headers}
        with open(os.path.join(self.record_dir, name + ".meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
            fh.write("\n")
        with open(os.path.join(self.record_dir, name + ".body"), "wb") as fh:
            fh.write(resp.body)


# --------------------------------------------------------------- decoding


class Decoded:
    def __init__(self, data: bytes, encoding: str, status: str = "ok", flags=None):
        self.data = data
        self.encoding = encoding
        self.status = status
        self.flags = flags or []


def _gunzip(data: bytes) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        # Tolerate trailing bytes after a complete member.
        d = zlib.decompressobj(31)
        out = d.decompress(data)
        if not d.eof:
            raise
        return out


def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)  # zlib-wrapped, as the RFC says
    except zlib.error:
        return zlib.decompress(data, -15)  # raw deflate, as some servers send


def decode_body(body: bytes, content_encoding, sniff: bool = False) -> Decoded:
    """Undo Content-Encoding (last applied first). On failure the raw bytes
    are kept and the status says why. sniff=True also decodes a gzip body
    sent without a header and flags it sniffed-gzip."""
    encodings = [e.strip().lower() for e in (content_encoding or "").split(",") if e.strip()]
    real = [e for e in encodings if e != "identity"]
    label = ", ".join(real) if real else "identity"
    data = body
    for enc in reversed(real):
        if enc in ("gzip", "x-gzip"):
            decoder = _gunzip
        elif enc == "deflate":
            decoder = _inflate
        elif enc == "br" and _brotli is not None:
            decoder = _brotli.decompress
        elif enc == "zstd" and _zstd is not None:
            decoder = _zstd.decompress
        else:
            return Decoded(body, label, "encoding-unsupported")
        try:
            data = decoder(data)
        except Exception:
            return Decoded(body, label, "encoding-mismatch")
    flags = []
    if sniff and not real and data[:2] == b"\x1f\x8b":
        try:
            data = _gunzip(data)
            flags.append("sniffed-gzip")
            label = "sniffed-gzip"
        except (OSError, EOFError, zlib.error):
            pass
    return Decoded(data, label, "ok", flags)


def is_textual(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return (
        not mt
        or mt.startswith("text/")
        or mt.endswith("+xml")
        or mt.endswith("+json")
        or mt in ("application/xml", "application/json", "application/javascript", "application/octet-stream")
    )


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)
_CT_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([^\s;"']+)""", re.I)


def decode_html(data: bytes, content_type) -> tuple:
    """(text, charset): Content-Type charset, then <meta>, then UTF-8."""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", "replace"), "utf-8-sig"
    candidates = []
    m = _CT_CHARSET_RE.search(content_type or "")
    if m:
        candidates.append(m.group(1))
    m = _META_CHARSET_RE.search(data[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "replace"))
    for cs in candidates:
        try:
            codecs.lookup(cs)
            return data.decode(cs, "replace"), cs.lower()
        except LookupError:
            continue
    return data.decode("utf-8", "replace"), "utf-8"


def is_toolbar_injected(text: str) -> bool:
    """An id_ body carrying Wayback's replay toolbar: the id_ was lost.
    A bare 'wombat' is not enough, an archived page can mention one."""
    return "web-static.archive.org" in text or ("__wm." in text and "wombat" in text.lower())


# archive.org's outage page. Matched in <title> only, so an archived page
# that says "temporarily offline" in its text is not taken for it.
_OFFLINE_TITLE_RE = re.compile(rb"<title[^>]*>[^<]*Temporarily\s+Offline", re.I)
OFFLINE_SCAN_BYTES = 65536


def is_offline_page(resp) -> bool:
    dec = decode_body(resp.body, resp.header("content-encoding"), sniff=True)
    return bool(_OFFLINE_TITLE_RE.search(dec.data[:OFFLINE_SCAN_BYTES]))


def is_outage(url: str, resp) -> bool:
    """archive.org's outage page in place of the response to url. A real
    replay (/web/) carries Memento-Datetime, so a replay that has one is
    an archived page, whatever its title. CDX and other archive.org
    responses go by the title alone."""
    if not is_offline_page(resp):
        return False
    if parse_wayback_url(url) is None:
        return True
    return not (resp.header("memento-datetime") or "").strip()


_HEIF_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")


def _looks_like_svg(head: bytes) -> bool:
    s = head.lstrip(b"\xef\xbb\xbf").lstrip()
    while True:
        if s.startswith(b"<?"):
            end = s.find(b"?>")
            skip = 2
        elif s.startswith(b"<!--"):
            end = s.find(b"-->")
            skip = 3
        elif s[:9].upper() == b"<!DOCTYPE":
            end = s.find(b">")
            skip = 1
        else:
            break
        if end < 0:
            return False
        s = s[end + skip:].lstrip()
    return s[:4].lower() == b"<svg" and (len(s) == 4 or s[4:5] in (b" ", b"\t", b"\r", b"\n", b"/", b">"))


def sniff_image(data: bytes):
    """File extension from magic bytes, or None when it is not an image."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:2] == b"BM" and len(data) >= 14 and data[6:10] == b"\x00\x00\x00\x00":
        return "bmp"
    if data[4:8] == b"ftyp":
        size = int.from_bytes(data[0:4], "big")
        brands = data[8:max(16, min(size, 64))]
        if b"avif" in brands or b"avis" in brands:
            return "avif"
        if any(b in brands for b in _HEIF_BRANDS):
            return "heic"
    if _looks_like_svg(data[:1024]):
        return "svg"
    return None


# ------------------------------------------------------- HTML extraction

# img attributes checked, in this order; lazy-loading variants included.
IMG_ATTRS = ("src", "srcset", "data-src", "data-srcset", "data-original", "data-lazy-src", "data-lazy")
SRCSET_ATTRS = frozenset(("srcset", "data-srcset", "imagesrcset"))
OG_PROPERTIES = ("og:image", "og:image:url", "og:image:secure_url")
SKIP_SCHEMES = ("javascript:", "about:", "blob:")
# url() references in CSS that are certainly not images.
NON_IMAGE_EXTS = frozenset(("woff", "woff2", "ttf", "otf", "eot", "css", "js", "htc", "json", "html", "htm"))
# found_via prefixes that count as content images for the JS-rendered
# verdict. Icons, meta images and CSS backgrounds do not.
CONTENT_VIA = ("img:", "picture-source:", "source:", "input-image:", "video:", "svg-image:", "link:preload")
HIDDEN_TEXT_TAGS = frozenset(("noscript", "template", "title"))
# <script type> values a browser runs as JavaScript ("" stands for an absent
# or empty type). Anything else (ld+json, templates, importmap) is data.
JS_SCRIPT_TYPES = frozenset((
    "", "module", "text/javascript", "application/javascript", "text/ecmascript",
    "application/ecmascript", "application/x-javascript", "application/x-ecmascript",
    "text/x-javascript", "text/x-ecmascript", "text/jscript", "text/livescript",
    "text/javascript1.0", "text/javascript1.1", "text/javascript1.2",
    "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
))


def is_js_script_type(value) -> bool:
    """Lenient about case, whitespace and parameters (charset=...)."""
    return (value or "").split(";", 1)[0].strip().lower() in JS_SCRIPT_TYPES

_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.I)


def css_urls(text: str) -> list:
    return [a or b or c for a, b, c in _CSS_URL_RE.findall(text or "")]


def parse_srcset(value: str) -> list:
    """URLs from a srcset, per the WHATWG candidate algorithm: a URL is a
    run of non-whitespace (inner commas kept), trailing commas stripped,
    descriptors skipped up to the next comma outside parentheses."""
    s = value or ""
    n = len(s)
    i = 0
    urls = []
    while True:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not s[j].isspace():
            j += 1
        url = s[i:j]
        i = j
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            depth = 0
            while i < n:
                c = s[i]
                i += 1
                if c == "(":
                    depth += 1
                elif c == ")" and depth:
                    depth -= 1
                elif c == "," and not depth:
                    break
        if url:
            urls.append(url)
    return urls


class Candidate:
    __slots__ = ("url", "found_via", "inline")

    def __init__(self, url: str, found_via: str, inline: bool = False):
        self.url = url
        self.found_via = found_via
        self.inline = inline

    def __repr__(self):
        return "Candidate(%r, %r)" % (self.url, self.found_via)

    def __eq__(self, other):
        return isinstance(other, Candidate) and (self.url, self.found_via) == (other.url, other.found_via)

    def __hash__(self):
        return hash((self.url, self.found_via))


class Extraction:
    def __init__(self):
        self.candidates = []
        self.base_url = ""
        self.scripts = []  # every inline script, data blocks included
        self.js_scripts = []  # the inline scripts that are JavaScript
        self.script_srcs = []  # external JavaScript only
        self.stylesheets = []
        self.visible_text_chars = 0
        self.script_bytes = 0
        self.doc_bytes = 0


class _Collector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []
        self.base_href = None
        self.picture_depth = 0
        self.hidden_depth = 0
        self.script_buf = None
        self.script_is_js = True
        self.style_buf = None
        self.scripts = []
        self.js_scripts = []
        self.script_srcs = []
        self.stylesheet_hrefs = []
        self.text = []
        self.script_bytes = 0

    def _add(self, value, via):
        if value is not None:
            self.refs.append((value, via))

    def handle_starttag(self, tag, attrs):
        a = {}
        ordered = []
        for k, v in attrs:
            if k not in a:
                a[k] = v if v is not None else ""
                ordered.append(k)
        if "style" in a:
            for u in css_urls(a["style"]):
                self._add(u, "style-attr")

        if tag == "img":
            for k in ordered:
                if k in IMG_ATTRS:
                    if k in SRCSET_ATTRS:
                        for u in parse_srcset(a[k]):
                            self._add(u, "img:" + k)
                    else:
                        self._add(a[k], "img:" + k)
        elif tag == "picture":
            self.picture_depth += 1
        elif tag == "source":
            if self.picture_depth:
                for k in ("srcset", "data-srcset"):
                    for u in parse_srcset(a.get(k, "")):
                        self._add(u, "picture-source:" + k)
            elif a.get("type", "").strip().lower().startswith("image/"):
                if "src" in a:
                    self._add(a["src"], "source:src")
                for k in ("srcset", "data-srcset"):
                    for u in parse_srcset(a.get(k, "")):
                        self._add(u, "source:" + k)
        elif tag == "input":
            if a.get("type", "").strip().lower() == "image":
                self._add(a.get("src"), "input-image:src")
        elif tag == "video":
            self._add(a.get("poster"), "video:poster")
        elif tag == "image":
            for k in ("href", "xlink:href"):
                self._add(a.get(k), "svg-image:href")
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content")
            if prop in OG_PROPERTIES:
                self._add(content, "meta:" + prop)
            elif prop in ("twitter:image", "twitter:image:src"):
                self._add(content, "meta:twitter:image")
            if a.get("itemprop", "").strip().lower() == "image":
                self._add(content, "meta:itemprop")
        elif tag == "link":
            rels = a.get("rel", "").lower().split()
            href = a.get("href")
            if any("icon" in r for r in rels):
                self._add(href, "link:icon")
            if "image_src" in rels:
                self._add(href, "link:image_src")
            if "preload" in rels and a.get("as", "").strip().lower() == "image":
                self._add(href, "link:preload")
                for u in parse_srcset(a.get("imagesrcset", "")):
                    self._add(u, "link:preload")
            if "stylesheet" in rels and href:
                self.stylesheet_hrefs.append(href)
        elif tag == "base":
            if self.base_href is None and a.get("href"):
                self.base_href = a["href"]
        elif tag == "script":
            self.script_is_js = is_js_script_type(a.get("type"))
            if a.get("src") and self.script_is_js:
                self.script_srcs.append(a["src"])
            self.script_bytes += len(self.get_starttag_text() or "")
            self.script_buf = []
        elif tag == "style":
            self.style_buf = []
        elif tag in HIDDEN_TEXT_TAGS:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag == "picture":
            self.picture_depth = max(0, self.picture_depth - 1)
        elif tag == "script" and self.script_buf is not None:
            self._finish_script()
        elif tag == "style" and self.style_buf is not None:
            self._finish_style()
        elif tag in HIDDEN_TEXT_TAGS:
            self.hidden_depth = max(0, self.hidden_depth - 1)

    def _finish_script(self):
        text = "".join(self.script_buf)
        self.script_buf = None
        if text.strip():
            self.scripts.append(text)
            if self.script_is_js:
                self.js_scripts.append(text)
        self.script_bytes += len(text)

    def _finish_style(self):
        text = "".join(self.style_buf)
        self.style_buf = None
        for u in css_urls(text):
            self._add(u, "style-block")

    def handle_data(self, data):
        if self.script_buf is not None:
            self.script_buf.append(data)
        elif self.style_buf is not None:
            self.style_buf.append(data)
        elif not self.hidden_depth:
            self.text.append(data)

    def finish(self):
        self.close()
        if self.script_buf is not None:
            self._finish_script()
        if self.style_buf is not None:
            self._finish_style()


def _url_ext(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def resolve_ref(raw: str, base: str):
    """(url, inline) for an attribute value, or None to skip it."""
    ref = (raw or "").strip()
    if not ref or ref.startswith("#"):
        return None
    low = ref.lower()
    if low.startswith("data:"):
        return ref, True
    if low.startswith(SKIP_SCHEMES):
        return None
    ref = unwrap_wayback(ref)
    try:
        url = urllib.parse.urljoin(base, ref)
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            return None
        return normalize_url(url), False
    except ValueError:
        return None


def extract(html_text: str, page_url: str) -> Extraction:
    """Image candidates and page facts. Relative references resolve against
    <base href> if present, else page_url (the original URL, never the
    Wayback URL)."""
    p = _Collector()
    try:
        p.feed(html_text)
        p.finish()
    except Exception:
        # HTMLParser tolerates nearly everything; keep what was collected.
        pass
    ex = Extraction()
    base = page_url
    if p.base_href:
        joined = resolve_ref(p.base_href, page_url)
        if joined and not joined[1]:
            base = joined[0]
    ex.base_url = base
    seen = set()
    for raw, via in p.refs:
        r = resolve_ref(raw, base)
        if r is None:
            continue
        url, inline = r
        if via in ("style-attr", "style-block") and not inline and _url_ext(url) in NON_IMAGE_EXTS:
            continue
        if url in seen:
            continue
        seen.add(url)
        ex.candidates.append(Candidate(url, via, inline))
    for src in p.script_srcs:
        r = resolve_ref(src, base)
        if r and not r[1]:
            ex.script_srcs.append(r[0])
    for href in p.stylesheet_hrefs:
        r = resolve_ref(href, base)
        if r and not r[1]:
            ex.stylesheets.append(r[0])
    ex.scripts = p.scripts
    ex.js_scripts = p.js_scripts
    ex.visible_text_chars = len(" ".join(" ".join(p.text).split()))
    ex.script_bytes = p.script_bytes
    ex.doc_bytes = len(html_text)
    return ex


# ------------------------------------------- JS-rendered detection, fallback

_MARKERS = (
    ("DOCS_timing", re.compile(r"DOCS_timing")),
    ("atari", re.compile(r"\batari\b")),
    ('id="root"></div>', re.compile(r"""id=["']root["']\s*>\s*</div>""", re.I)),
    ('id="app"></div>', re.compile(r"""id=["']app["']\s*>\s*</div>""", re.I)),
    ("ng-version", re.compile(r"\bng-version\b")),
)
VISIBLE_TEXT_MIN = 200


def content_image_count(ex: Extraction) -> int:
    return sum(1 for c in ex.candidates if not c.inline and c.found_via.startswith(CONTENT_VIA))


def js_verdict(ex: Extraction, html_text: str) -> tuple:
    """(js_rendered, reasons). True when there are no content images and at
    least one sign of a script-built page."""
    reasons = []
    for label, rx in _MARKERS:
        if rx.search(html_text):
            reasons.append("marker:" + label)
    if ex.doc_bytes and ex.script_bytes * 2 > ex.doc_bytes:
        reasons.append("script-bytes:%d%%" % (100 * ex.script_bytes // ex.doc_bytes))
    if ex.visible_text_chars < VISIBLE_TEXT_MIN:
        reasons.append("visible-text:%d" % ex.visible_text_chars)
    return content_image_count(ex) == 0 and bool(reasons), reasons


# Escapes decoded inside script text: JSON \/ plus \u00XX and \xXX forms of
# URL punctuation. Quotes and backslashes stay escaped so string ends hold.
_JS_ESCAPE_RE = re.compile(r"\\(?:u00([0-9a-fA-F]{2})|x([0-9a-fA-F]{2})|(/))")
_JS_ESCAPE_OK = frozenset("/=&:?%+,;~@-._#")
_ABS_URL_RE = re.compile(r"""https?://[^\s"'`<>\\()\[\]{}|^;]+""", re.I)
_IMAGE_PATH_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico|avif|bmp)$", re.I)
IMAGE_HOSTS = (
    "googleusercontent.com", "ggpht.com", "res.cloudinary.com", "imgix.net",
    "images.ctfassets.net", "images.squarespace-cdn.com", "static.wixstatic.com",
)


def unescape_js(text: str) -> str:
    def rep(m):
        if m.group(3):
            return "/"
        ch = chr(int(m.group(1) or m.group(2), 16))
        return ch if ch in _JS_ESCAPE_OK else m.group(0)

    return _JS_ESCAPE_RE.sub(rep, text)


def _script_urls(texts) -> list:
    out = []
    for text in texts:
        for m in _ABS_URL_RE.finditer(unescape_js(text)):
            out.append(m.group(0).rstrip(".,:"))
    return out


def _is_image_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if _IMAGE_PATH_RE.search(parts.path):
        return True
    # Image hosts also serve frames and scripts (atari-embeds .html, .js).
    if _url_ext(url) in NON_IMAGE_EXTS:
        return False
    return any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS)


def scan_js_strings(texts) -> list:
    """Absolute image URLs inside inline script text, escapes decoded."""
    found = []
    seen = set()
    for raw in _script_urls(texts):
        url = unwrap_wayback(raw)
        try:
            host = urllib.parse.urlsplit(url).hostname
            url = normalize_url(url)
        except ValueError:
            continue
        if is_archive_host(host) or not _is_image_url(url) or url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def host_report(ex: Extraction, page_host: str) -> tuple:
    """(loaded, mentioned): third-party hosts of the <script src>
    JavaScript the page loads, and hosts only mentioned inside inline
    JavaScript, each most frequent first. A loaded host is not listed again
    as mentioned. ld+json and other data blocks are left out: they name
    schema.org and social profiles, not where the page loads things from.
    Inline copies of them (Next.js flight data) still count as mentioned."""
    own = own_domain(page_host)

    def count(urls):
        counts = collections.Counter()
        for url in urls:
            try:
                host = (urllib.parse.urlsplit(unwrap_wayback(url)).hostname or "").lower()
            except ValueError:
                continue
            if not host or is_archive_host(host) or host == own or host.endswith("." + own):
                continue
            counts[host] += 1
        return counts

    def ranked(counts):
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    loaded = count(ex.script_srcs)
    mentioned = count(_script_urls(ex.js_scripts))
    return ranked(loaded), ranked({h: n for h, n in mentioned.items() if h not in loaded})


# ------------------------------------------------------ files and naming

SEGMENT_MAX = 120
_UNSAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._~+=,@-]")


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def _split_ext(name: str) -> tuple:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 1 <= len(ext) <= 5 and ext.isalnum():
        return stem, "." + ext
    return name, ""


def _cap_segment(seg: str) -> str:
    if len(seg) <= SEGMENT_MAX:
        return seg
    stem, ext = _split_ext(seg)
    return stem[: SEGMENT_MAX - len(ext) - 9] + "-" + _short_hash(seg) + ext


def _safe_segment(seg: str) -> str:
    """Percent-decode, replace anything unusual, neutralise . and .."""
    seg = _UNSAFE_SEGMENT_RE.sub("_", urllib.parse.unquote(seg))
    if seg in (".", ".."):
        seg = "_" * len(seg)
    return seg


def _host_segment(parts) -> str:
    host = (parts.hostname or "").lower() or "unknown-host"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host += "_%d" % port
    return _cap_segment(_safe_segment(host))


def _url_name_parts(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    segs = [_safe_segment(s) for s in parts.path.split("/") if s]
    name = "index" if not segs or parts.path.endswith("/") else segs.pop()
    return parts, [_cap_segment(s) for s in segs], name


def image_relpath(url: str, sniffed_ext=None) -> str:
    """images/<host>/<path>; query becomes a short hash suffix; the
    extension comes from the sniff when the URL has none."""
    parts, dirs, name = _url_name_parts(url)
    stem, ext = _split_ext(name)
    if not ext and sniffed_ext:
        ext = "." + sniffed_ext
    if parts.query:
        stem += "__" + _short_hash(parts.query)
    return "/".join(["images", _host_segment(parts)] + dirs + [_cap_segment(stem + ext)])


def page_relpath(ts: str, url: str) -> str:
    """pages/<ts>/<host>/<path>.html"""
    parts, dirs, name = _url_name_parts(url)
    stem, ext = _split_ext(name)
    if ext.lower() not in (".html", ".htm"):
        stem, ext = name, ".html"
    if parts.query:
        stem += "__" + _short_hash(parts.query)
    return "/".join(["pages", _safe_segment(ts), _host_segment(parts)] + dirs + [_cap_segment(stem + ext)])


def resolve_inside(out_dir: str, relpath: str) -> str:
    """Absolute path for relpath, asserted to be inside out_dir."""
    if not relpath or os.path.isabs(relpath) or relpath.startswith(("/", "\\")):
        raise ValueError("unsafe output path: %r" % relpath)
    root = os.path.realpath(out_dir)
    full = os.path.realpath(os.path.join(root, *relpath.split("/")))
    if not full.startswith(root + os.sep):
        raise ValueError("output path escapes --out: %r" % relpath)
    return full


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path: str, data: bytes) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _with_suffix(relpath: str, suffix: str) -> str:
    head, _, name = relpath.rpartition("/")
    stem, ext = _split_ext(name)
    return head + "/" + _cap_segment(stem + suffix + ext)


def place_file(out_dir: str, relpath: str, data: bytes, sha: str, ts: str) -> str:
    """Write data at relpath, or reuse it when identical bytes are already
    there. A different file at the same path gets __<ts> (then -2, -3)."""
    candidate = relpath
    n = 1
    while True:
        full = resolve_inside(out_dir, candidate)
        if not os.path.exists(full):
            write_atomic(full, data)
            return candidate
        if os.path.isfile(full) and sha256_file(full) == sha:
            return candidate
        n += 1
        candidate = _with_suffix(relpath, "__%s" % ts if n == 2 else "__%s-%d" % (ts, n - 1))


def parse_data_uri(uri: str):
    head, sep, payload = uri[5:].partition(",")
    if not sep:
        return None
    params = [p.strip().lower() for p in head.split(";")]
    mime = params[0] or "text/plain"
    try:
        if "base64" in params[1:]:
            payload = re.sub(r"\s+", "", urllib.parse.unquote(payload))
            data = base64.b64decode(payload + "=" * (-len(payload) % 4))
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except ValueError:
        return None
    return mime, data


def inline_key(uri: str) -> str:
    """Short stable manifest key for a data: URI (which can be megabytes)."""
    mime = uri[5:].split(",", 1)[0].split(";", 1)[0] or "text/plain"
    return "data:%s;sha256=%s" % (mime, hashlib.sha256(uri.encode("utf-8", "surrogateescape")).hexdigest()[:16])


# --------------------------------------------------------------- manifest

MANIFEST_COLUMNS = [
    "original_url", "requested_timestamp", "timestamp", "wayback_url", "status",
    "sha256", "saved_path", "kind", "found_via", "source", "mimetype",
    "cdx_digest", "bytes", "content_encoding", "error", "fetched_at",
    "memento_datetime",
]
# Rows in these states are final: a rerun skips them.
RESUME_SKIP = frozenset(("ok", "duplicate", "not-archived", "type-mismatch", "inline-data"))


class Manifest:
    """<out>/manifest.csv: append-only while running (each row flushed),
    last row per (original_url, requested_timestamp) wins on load,
    compacted atomically at a clean exit."""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.path = os.path.join(out_dir, "manifest.csv")
        self.rows = {}
        self.sha_paths = {}
        self.digest_paths = {}
        self._fh = None
        self._writer = None
        self._dirty = False
        self._old_header = False
        if os.path.exists(self.path):
            with open(self.path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    self._index(self._clean(row))
                self._old_header = reader.fieldnames is not None and reader.fieldnames != MANIFEST_COLUMNS

    @staticmethod
    def _clean(row: dict) -> dict:
        return {c: "" if row.get(c) is None else str(row.get(c)) for c in MANIFEST_COLUMNS}

    def _index(self, row: dict) -> None:
        self.rows[(row["original_url"], row["requested_timestamp"])] = row
        if row["kind"] == "page" or not row["saved_path"]:
            return
        if row["status"] == "ok" and row["sha256"]:
            self.sha_paths.setdefault(row["sha256"], row["saved_path"])
        if row["status"] in ("ok", "duplicate") and row["cdx_digest"]:
            self.digest_paths.setdefault(row["cdx_digest"], row["saved_path"])

    def get(self, key: tuple):
        return self.rows.get(key)

    def append(self, row: dict) -> dict:
        row = self._clean(row)
        if self._fh is None:
            if self._old_header:
                # Written by an older version with other columns: rewrite
                # it first so appended rows line up with the header.
                self._rewrite()
                self._old_header = False
            os.makedirs(self.out_dir, exist_ok=True)
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            self._fh = open(self.path, "a", newline="", encoding="utf-8")
            if size:
                with open(self.path, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        self._fh.write("\n")  # a crash left a partial line
            self._writer = csv.DictWriter(self._fh, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
            if not size:
                self._writer.writeheader()
        self._writer.writerow(row)
        self._fh.flush()
        self._dirty = True
        self._index(row)
        return row

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def compact(self) -> None:
        self.close()
        if not self._dirty and not os.path.exists(self.path):
            return
        self._rewrite()

    def _rewrite(self) -> None:
        """Replace the file atomically with one row per key."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for row in self.rows.values():
                writer.writerow(row)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


# -------------------------------------------------------------- workflows


class Run:
    """Shared state for one CLI invocation."""

    def __init__(self, args, client, stdout, stderr, clock):
        self.args = args
        self.client = client
        self.out_dir = args.out
        self.stdout = stdout
        self.stderr = stderr
        self.clock = clock
        self.exit_code = EXIT_OK
        self._manifest = None

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest(self.out_dir)
        return self._manifest

    def info(self, message: str) -> None:
        if not self.args.quiet:
            self.stderr.write(message + "\n")

    def say(self, line: str = "") -> None:
        self.stdout.write(line + "\n")

    def emit_json(self, obj) -> None:
        self.stdout.write(json.dumps(obj, indent=2) + "\n")

    def row(self, fields: dict) -> dict:
        fields = dict(fields)
        fields.setdefault("fetched_at", iso_utc(self.clock()))
        return self.manifest.append(fields)

    def close(self, compact: bool) -> None:
        if self._manifest is not None:
            if compact:
                self._manifest.compact()
            else:
                self._manifest.close()


def cdx_body_problem(resp: Response):
    """Retry reason for a 200 CDX response whose body is not JSON (an error
    or outage page in place of results), else None. An empty body is a
    valid "no captures"; other statuses are left to cdx_request."""
    if resp.status != 200:
        return None
    dec = decode_body(resp.body, resp.header("content-encoding"), sniff=True)
    text = dec.data.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        json.loads(text)
    except ValueError:
        return "CDX body is not JSON: %s" % " ".join(text[:120].split())
    return None


def cdx_request(run: Run, url: str) -> CdxPage:
    fetched = run.client.get(url, validate=cdx_body_problem)
    resp = fetched.response
    if resp.status != 200:
        snippet = " ".join(resp.body[:200].decode("utf-8", "replace").split())
        raise CdxError("CDX query returned HTTP %d %s" % (resp.status, snippet))
    dec = decode_body(resp.body, resp.header("content-encoding"), sniff=True)
    if dec.status != "ok":
        raise CdxError("CDX response could not be decoded (%s, %s)" % (dec.status, dec.encoding))
    return parse_cdx(dec.data)


def cdx_listing(run: Run, domain: str, from_ts=None, to_ts=None, loose_mime=False, cdx_limit=2000) -> list:
    """Archived images of a domain and its subdomains, paginated with the
    resume key and capped at cdx_limit rows."""
    filters = [LOOSE_MIME_FILTER if loose_mime else "mimetype:image/.*", "statuscode:200"]
    rows = []
    resume = None
    while len(rows) < cdx_limit:
        want = min(CDX_PAGE_SIZE, cdx_limit - len(rows))
        query = build_cdx_query(
            domain, match_type="domain", filters=filters, collapse="digest",
            from_ts=from_ts, to_ts=to_ts, limit=want, show_resume_key=True, resume_key=resume,
        )
        try:
            page = cdx_request(run, query)
        except CapReached:
            run.info("request cap reached during the CDX listing; continuing with %d rows" % len(rows))
            break
        rows.extend(page.rows[:want])
        if not page.resume_key or not page.rows:
            break
        resume = page.resume_key
    return rows


# Default reach of the images fallback's domain listing on either side of
# the page capture. Unbounded, a 2021 page pulled in the 2026 site's images.
LISTING_WINDOW_DAYS = 365


def ts_datetime(ts):
    """datetime for a 4 to 14 digit timestamp (missing month and day are
    1, missing time is midnight), or None when it is not a date."""
    if not ts or not ts.isdigit() or not 4 <= len(ts) <= 14:
        return None
    s = ts.ljust(14, "0")
    try:
        return datetime.datetime(int(s[0:4]), int(s[4:6]) or 1, int(s[6:8]) or 1,
                                 int(s[8:10]), int(s[10:12]), int(s[12:14]))
    except ValueError:
        return None


def listing_window(capture_ts, from_ts=None, to_ts=None) -> tuple:
    """(from, to) for the fallback listing. Each bound is the flag when
    given, else the capture +/- LISTING_WINDOW_DAYS, else None (open)."""
    dt = ts_datetime(capture_ts)
    span = datetime.timedelta(days=LISTING_WINDOW_DAYS)

    def bound(given, sign):
        if given or dt is None:
            return given or None
        try:
            return (dt + sign * span).strftime("%Y%m%d%H%M%S")
        except OverflowError:
            return None

    return bound(from_ts, -1), bound(to_ts, 1)


class Item:
    __slots__ = ("url", "requested_ts", "found_via", "digest", "mimetype", "inline")

    def __init__(self, url, requested_ts, found_via, digest="", mimetype="", inline=False):
        self.url = url
        self.requested_ts = requested_ts
        self.found_via = found_via
        self.digest = digest
        self.mimetype = mimetype
        self.inline = inline

    def key(self) -> tuple:
        return (inline_key(self.url) if self.inline else self.url, self.requested_ts)


def listing_items(rows: list) -> list:
    """One item per CDX digest (collapse=digest only drops adjacent rows)."""
    items = []
    digests = set()
    keys = set()
    for r in rows:
        digest = r.get("digest", "")
        if digest and digest in digests:
            continue
        original = r.get("original", "")
        if not original:
            continue
        try:
            url = normalize_url(original if has_scheme(original) else "http://" + original)
        except ValueError:
            continue
        ts = r.get("timestamp", "")
        if (url, ts) in keys:
            continue
        digests.add(digest)
        keys.add((url, ts))
        items.append(Item(url, ts, "cdx-listing", digest=digest, mimetype=r.get("mimetype", "")))
    return items


class PageResult:
    def __init__(self, url: str):
        self.url = url
        self.base_url = ""
        self.wayback_url = ""
        self.requested_timestamp = ""
        self.timestamp = ""
        self.memento_datetime = ""
        self.latest_path = None
        self.http_status = 0
        self.status = "error"
        self.content_type = ""
        self.content_encoding = ""
        self.raw_bytes = 0
        self.decoded_bytes = 0
        self.charset = ""
        self.flags = []
        self.toolbar_injected = False
        self.js_rendered = False
        self.js_reasons = []
        self.content_images = 0
        self.extraction = None
        self.js_strings = []
        self.script_src_hosts = []
        self.script_text_hosts = []
        self.saved_path = ""
        self.sha256 = ""
        self.error = ""
        self.decoded = None

    def candidates(self) -> list:
        if self.extraction is None:
            return []
        out = [(c.url, c.found_via) for c in self.extraction.candidates]
        if self.js_reasons:  # what images --fallback auto would scan
            known = {u for u, _ in out}
            out += [(u, "js-string") for u in self.js_strings if u not in known]
        return out

    def as_dict(self) -> dict:
        cands = self.candidates()
        return {
            "url": self.url,
            "base_url": self.base_url,
            "wayback_url": self.wayback_url,
            "requested_timestamp": self.requested_timestamp,
            "timestamp": self.timestamp,
            "memento_datetime": self.memento_datetime,
            "latest_path": self.latest_path,
            "http_status": self.http_status,
            "status": self.status,
            "content_type": self.content_type,
            "content_encoding": self.content_encoding,
            "raw_bytes": self.raw_bytes,
            "decoded_bytes": self.decoded_bytes,
            "charset": self.charset,
            "flags": self.flags,
            "toolbar_injected": self.toolbar_injected,
            "js_rendered": self.js_rendered,
            "js_reasons": self.js_reasons,
            "content_images": self.content_images,
            "candidates": [{"url": _short_uri(u), "found_via": v} for u, v in cands],
            "found_via": dict(collections.Counter(v for _, v in cands)),
            "script_src_host": [list(kv) for kv in self.script_src_hosts],
            "script_text_host": [list(kv) for kv in self.script_text_hosts],
            "saved_path": self.saved_path,
            "error": self.error,
        }


def _short_uri(url: str) -> str:
    return url if not url.startswith("data:") or len(url) <= 100 else url[:96] + "..."


def analyse_page(run: Run, user_url: str, timestamp=None, latest: bool = False) -> PageResult:
    """Resolve the capture, fetch it via id_, decode, check, extract, save."""
    pr = PageResult(user_url)
    target = fetch_target(user_url)
    cdx_original = None
    if latest:
        rows = []
        query = build_cdx_query(
            cdx_target(user_url), filters=["statuscode:200", "mimetype:text/html"], limit=-1
        )
        try:
            rows = cdx_request(run, query).rows
        except CdxError as e:
            run.info("latest: CDX lookup failed (%s)" % e)
        if len(rows) == 1:
            pr.latest_path = "cdx-negative-limit"
            ts = rows[0].get("timestamp", "")
            cdx_original = rows[0].get("original") or target
            fetch_url = wayback_url(ts, cdx_original)
        else:
            if len(rows) > 1:
                run.info("latest: CDX returned %d rows for limit=-1, so negative limit is not honoured" % len(rows))
            pr.latest_path = "now-redirect"
            ts = ts14(run.clock())
            fetch_url = wayback_url(ts, target)
        run.info("latest: using the %s path" % pr.latest_path)
    else:
        ts = timestamp
        fetch_url = wayback_url(ts, target)

    fetched = run.client.get(fetch_url)
    resp = fetched.response
    pr.wayback_url = fetched.final_url
    parsed = parse_wayback_url(fetched.final_url)
    pr.timestamp = parsed[0] if parsed else ts
    # --latest keys the page on the capture actually used, so reruns match.
    pr.requested_timestamp = pr.timestamp if latest else ts
    original = cdx_original or (parsed[2] if parsed else target)
    pr.base_url = normalize_url(original)
    pr.http_status = resp.status
    pr.content_type = resp.header("content-type") or ""
    pr.memento_datetime = resp.header("memento-datetime") or ""
    pr.raw_bytes = len(resp.body)
    if fetched.too_many_redirects:
        pr.error = "more than %d redirects" % MAX_HOPS
    elif resp.status == 404:
        pr.status = "not-archived"
    elif resp.status != 200:
        pr.status = "http-%d" % resp.status
    else:
        _decode_page(run, pr, resp, original)
    _save_page(run, pr)
    return pr


def _decode_page(run: Run, pr: PageResult, resp: Response, original: str) -> None:
    dec = decode_body(resp.body, resp.header("content-encoding"), sniff=True)
    if dec.status == "encoding-unsupported":
        # Rewrite mode (no id_) came back as plain text in the sbx run; the
        # extractor unwraps the rewritten URLs.
        run.info("page body is %s, which this Python cannot decode; retrying once in rewrite mode" % dec.encoding)
        retry = run.client.get(wayback_url(pr.timestamp, original, mod=""))
        r2 = retry.response
        if r2.status == 200 and not retry.too_many_redirects:
            dec2 = decode_body(r2.body, r2.header("content-encoding"), sniff=True)
            if dec2.status == "ok":
                pr.error = "id_ body was %s; used rewrite mode" % dec.encoding
                dec = dec2
                dec.flags.append("rewrite-mode")
                pr.wayback_url = retry.final_url
                pr.content_type = r2.header("content-type") or pr.content_type
                pr.memento_datetime = r2.header("memento-datetime") or ""
                pr.raw_bytes = len(r2.body)
        if dec.status == "encoding-unsupported":
            pr.status = "encoding-unsupported"
            pr.content_encoding = dec.encoding
            pr.error = "Content-Encoding %s not decodable without an optional module" % dec.encoding
            return
    pr.content_encoding = dec.encoding
    pr.flags = list(dec.flags)
    pr.decoded = dec.data
    pr.decoded_bytes = len(dec.data)
    text, pr.charset = decode_html(dec.data, pr.content_type)
    pr.toolbar_injected = "rewrite-mode" not in pr.flags and is_toolbar_injected(text)
    ex = extract(text, pr.base_url)
    pr.extraction = ex
    pr.js_rendered, pr.js_reasons = js_verdict(ex, text)
    pr.content_images = content_image_count(ex)
    pr.js_strings = scan_js_strings(ex.scripts)
    pr.script_src_hosts, pr.script_text_hosts = host_report(ex, urllib.parse.urlsplit(pr.base_url).hostname or "")
    if dec.status == "encoding-mismatch":
        pr.status = "encoding-mismatch"
        pr.error = "Content-Encoding %s did not decode; raw bytes kept" % dec.encoding
    elif pr.toolbar_injected:
        pr.status = "toolbar-injected"
        pr.error = "Wayback toolbar markup in an id_ body: the raw modifier was lost"
    else:
        pr.status = "ok"


def _save_page(run: Run, pr: PageResult) -> None:
    if pr.decoded is not None:
        pr.sha256 = sha256_bytes(pr.decoded)
        rel = page_relpath(pr.timestamp, pr.base_url)
        pr.saved_path = place_file(run.out_dir, rel, pr.decoded, pr.sha256, pr.timestamp)
    run.row({
        "original_url": pr.base_url,
        "requested_timestamp": pr.requested_timestamp,
        "timestamp": pr.timestamp,
        "wayback_url": pr.wayback_url,
        "status": pr.status,
        "sha256": pr.sha256,
        "saved_path": pr.saved_path,
        "kind": "page",
        "found_via": "page",
        "source": "wayback",
        "mimetype": (pr.content_type.split(";", 1)[0].strip().lower()),
        "bytes": pr.decoded_bytes if pr.decoded is not None else pr.raw_bytes,
        "content_encoding": pr.content_encoding,
        "error": "; ".join(x for x in (pr.error, ",".join(pr.flags)) if x),
        "memento_datetime": pr.memento_datetime,
    })


def follow_css(run: Run, pr: PageResult) -> list:
    """--follow-css: fetch linked stylesheets via id_ and resolve their
    url() references against the stylesheet URL."""
    items = []
    known = {c.url for c in pr.extraction.candidates}
    for css_url in pr.extraction.stylesheets:
        try:
            fetched = run.client.get(wayback_url(pr.timestamp, css_url))
        except CapReached:
            run.info("request cap reached while following stylesheets")
            break
        except BadUrl:
            continue
        resp = fetched.response
        if resp.status != 200 or fetched.too_many_redirects:
            continue
        dec = decode_body(resp.body, resp.header("content-encoding"), sniff=True)
        if dec.status != "ok":
            continue
        parsed = parse_wayback_url(fetched.final_url)
        css_base = normalize_url(parsed[2]) if parsed else css_url
        for raw in css_urls(dec.data.decode("utf-8", "replace")):
            r = resolve_ref(raw, css_base)
            if not r or r[1] or _url_ext(r[0]) in NON_IMAGE_EXTS or r[0] in known:
                continue
            known.add(r[0])
            items.append(Item(r[0], pr.timestamp, "css-link"))
    return items


def nearest_capture(run: Run, item: Item):
    """After a 404 on the direct id_ request: one exact-URL CDX lookup.
    CdxError propagates: a failed lookup is not proof of no capture."""
    query = build_cdx_query(item.url, filters=["statuscode:200"], collapse="digest", limit=200)
    rows = cdx_request(run, query).rows
    if not rows:
        return None

    def distance(row):
        ts = row.get("timestamp", "")
        return abs(int(ts.ljust(14, "0")[:14] or 0) - int(item.requested_ts.ljust(14, "0")[:14] or 0))

    best = min((r for r in rows if r.get("timestamp", "").isdigit()), key=distance, default=None)
    if best is None:
        return None
    return best["timestamp"], best.get("original") or item.url


def _image_row(run: Run, item: Item, base: dict, fetched: Fetched, source: str) -> dict:
    resp = fetched.response
    parsed = parse_wayback_url(fetched.final_url) if source == "wayback" else None
    row = dict(base)
    row.update({
        "wayback_url": fetched.final_url if source == "wayback" else "",
        "timestamp": parsed[0] if parsed else "",
        "source": source,
        "mimetype": resp.media_type() or base.get("mimetype", ""),
        "memento_datetime": resp.header("memento-datetime") or "",
    })
    if fetched.too_many_redirects:
        row.update(status="error", error="more than %d redirects" % MAX_HOPS)
        return run.row(row)
    if resp.status == 404:
        row["status"] = "not-archived"
        return run.row(row)
    if resp.status != 200:
        row["status"] = "http-%d" % resp.status
        return run.row(row)
    dec = decode_body(resp.body, resp.header("content-encoding"))
    row["content_encoding"] = dec.encoding
    if dec.status != "ok":
        row.update(status=dec.status, bytes=len(resp.body), error="Content-Encoding %s" % dec.encoding)
        return run.row(row)
    row["bytes"] = len(dec.data)
    ext = sniff_image(dec.data)
    if ext is None:
        row.update(status="type-mismatch", error="200 body is not an image (%s)" % (resp.media_type() or "no type"))
        return run.row(row)
    sha = sha256_bytes(dec.data)
    row["sha256"] = sha
    first = run.manifest.sha_paths.get(sha)
    if first:
        row.update(status="duplicate", saved_path=first)
        return run.row(row)
    rel = image_relpath(item.url, ext)
    row.update(status="ok", saved_path=place_file(run.out_dir, rel, dec.data, sha, row["timestamp"] or ts14(run.clock())))
    return run.row(row)


def _try_live(run: Run, item: Item, base: dict):
    """--prefer-live: the original host first; None means use Wayback."""
    try:
        fetched = run.client.get(item.url, live=True)
    except (NetworkError, BadUrl, TLSError) as e:
        run.info("live fetch failed for %s (%s); using Wayback" % (item.url, e))
        return None
    resp = fetched.response
    if fetched.too_many_redirects or resp.status != 200:
        return None
    dec = decode_body(resp.body, resp.header("content-encoding"))
    if dec.status != "ok" or sniff_image(dec.data) is None:
        return None
    return _image_row(run, item, base, fetched, "live")


def _inline_row(run: Run, item: Item, base: dict) -> dict:
    row = dict(base, source="inline")
    if not run.args.save_inline:
        row.update(status="inline-data", error="data: URI; pass --save-inline to save it")
        return run.row(row)
    parsed = parse_data_uri(item.url)
    if parsed is None:
        row.update(status="error", error="malformed data: URI")
        return run.row(row)
    mime, data = parsed
    row.update(mimetype=mime, bytes=len(data))
    ext = sniff_image(data)
    if ext is None:
        row.update(status="type-mismatch", error="data: URI is not an image")
        return run.row(row)
    sha = sha256_bytes(data)
    row["sha256"] = sha
    first = run.manifest.sha_paths.get(sha)
    if first:
        row.update(status="duplicate", saved_path=first)
        return run.row(row)
    rel = "images/inline/%s.%s" % (sha[:16], ext)
    row.update(status="ok", saved_path=place_file(run.out_dir, rel, data, sha, item.requested_ts))
    return run.row(row)


def handle_item(run: Run, item: Item) -> dict:
    key = item.key()
    prev = run.manifest.get(key)
    save_inline = getattr(run.args, "save_inline", False)
    if prev and prev["status"] in RESUME_SKIP and not (save_inline and prev["status"] == "inline-data"):
        return prev
    base = {
        "original_url": key[0],
        "requested_timestamp": item.requested_ts,
        "kind": "image",
        "found_via": item.found_via,
        "cdx_digest": item.digest,
        "mimetype": item.mimetype,
    }
    if item.inline:
        return _inline_row(run, item, base)
    if item.digest and item.digest in run.manifest.digest_paths:
        return run.row(dict(base, status="duplicate", saved_path=run.manifest.digest_paths[item.digest],
                            error="CDX digest already saved"))
    target = wayback_url(item.requested_ts, item.url)
    if run.args.dry_run:
        return run.row(dict(base, status="planned", wayback_url=target))
    if getattr(run.args, "prefer_live", False):
        row = _try_live(run, item, base)
        if row is not None:
            return row
    fetched = run.client.get(target)
    if fetched.response.status == 404 and item.found_via != "cdx-listing" and not fetched.too_many_redirects:
        try:
            alt = nearest_capture(run, item)
        except CdxError as e:
            # error, not not-archived, so a rerun tries the lookup again.
            return run.row(dict(base, status="error", wayback_url=fetched.final_url,
                                error="CDX lookup failed: %s" % e))
        if alt is not None:
            fetched = run.client.get(wayback_url(alt[0], alt[1]))
    return _image_row(run, item, base, fetched, "wayback")


def process_items(run: Run, items: list) -> collections.Counter:
    """Fetch every item, resumably. Returns counts by status."""
    items = sorted(items, key=lambda i: not i.inline)  # inline needs no requests
    statuses = collections.Counter()
    total = len(items)
    for idx, item in enumerate(items):
        try:
            row = handle_item(run, item)
        except CapReached:
            run.info("request cap (--max-requests %d) reached; rerun the same command to resume"
                     % run.client.max_requests)
            for rest in items[idx:]:
                prev = run.manifest.get(rest.key())
                if prev and prev["status"] in RESUME_SKIP:
                    statuses[prev["status"]] += 1
                    continue
                run.row({"original_url": rest.key()[0], "requested_timestamp": rest.requested_ts,
                         "kind": "image", "found_via": rest.found_via, "cdx_digest": rest.digest,
                         "mimetype": rest.mimetype, "status": "skipped-cap"})
                statuses["skipped-cap"] += 1
            run.exit_code = EXIT_CAP
            break
        except RateLimited as e:
            run.row({"original_url": item.key()[0], "requested_timestamp": item.requested_ts,
                     "kind": "image", "found_via": item.found_via, "cdx_digest": item.digest,
                     "status": "rate-limited", "error": str(e)})
            statuses["rate-limited"] += 1
            run.info("archive.org kept throttling or failing (%s). Stopped; rerun the same command later "
                     "(it resumes) rather than lowering --delay." % e)
            run.exit_code = EXIT_RATE
            break
        except BadUrl as e:
            row = run.row({"original_url": item.key()[0], "requested_timestamp": item.requested_ts,
                           "kind": "image", "found_via": item.found_via, "status": "error", "error": str(e)})
        statuses[row["status"]] += 1
        run.info("[%d/%d] %-14s %s" % (idx + 1, total, row["status"], _short_uri(item.url)))
    return statuses


# --------------------------------------------------------------- commands

# The host report's output keys, loaded hosts first.
HOST_KEYS = ("script_src_host", "script_text_host")


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) if value else "none"
    if value is None:
        return ""
    return str(value)


def cmd_snapshots(run: Run) -> int:
    a = run.args
    if a.match in ("prefix", "host", "domain") and is_shared_host(site_domain(a.url)):
        raise SalvageError("refusing a %s listing on shared host %s: it holds captures for unrelated "
                           "sites. Query exact URLs instead." % (a.match, site_domain(a.url)))
    filters = ["statuscode:%s" % a.status] if a.status else []
    query = build_cdx_query(
        cdx_target(a.url), match_type=a.match, filters=filters, collapse=a.collapse,
        from_ts=a.from_ts, to_ts=a.to_ts, limit=a.limit,
    )
    rows = []
    for r in cdx_request(run, query).rows:
        ts, original = r.get("timestamp", ""), r.get("original", "")
        rows.append({
            "timestamp": ts, "original": original, "status": r.get("statuscode", ""),
            "mimetype": r.get("mimetype", ""), "digest": r.get("digest", ""),
            "length": r.get("length", ""), "wayback_url": wayback_url(ts, original, mod=""),
        })
    if a.json:
        run.emit_json(rows)
    elif not rows:
        run.say("no captures for %s" % a.url)
    else:
        cols = ["timestamp", "original", "status", "mimetype", "digest", "length", "wayback_url"]
        run.say("\t".join(cols))
        for r in rows:
            run.say("\t".join(r[c] for c in cols))
    return EXIT_OK


def cmd_fetch(run: Run) -> int:
    a = run.args
    target = fetch_target(a.url)
    url = wayback_url(a.timestamp, target)
    if a.dry_run:
        info = {"dry_run": True, "would_fetch": url}
    else:
        fetched = run.client.get(url)
        resp = fetched.response
        dec = decode_body(resp.body, resp.header("content-encoding"), sniff=is_textual(resp.media_type()))
        parsed = parse_wayback_url(fetched.final_url)
        info = {
            "url": target,
            "wayback_url": fetched.final_url,
            "requested_timestamp": a.timestamp,
            "timestamp": parsed[0] if parsed else "",
            "hops": len(fetched.hops),
            "http_status": resp.status,
            "content_type": resp.header("content-type") or "",
            "content_encoding": dec.encoding,
            "status": dec.status,
            "flags": dec.flags,
            "bytes": len(dec.data),
            "sha256": sha256_bytes(dec.data),
            "image_type": sniff_image(dec.data) or "",
        }
        if "html" in resp.media_type():
            info["toolbar_injected"] = is_toolbar_injected(decode_html(dec.data, info["content_type"])[0])
        if fetched.too_many_redirects:
            info["error"] = "more than %d redirects" % MAX_HOPS
        if a.save:
            write_atomic(os.path.abspath(a.save), dec.data)
            info["saved"] = a.save
    if a.json:
        run.emit_json(info)
    else:
        for k, v in info.items():
            run.say("%s=%s" % (k, _fmt(v)))
    return EXIT_OK


def _print_page(run: Run, pr: PageResult) -> None:
    d = pr.as_dict()
    if run.args.json:
        run.emit_json(d)
        return
    for k in ("url", "base_url", "wayback_url", "requested_timestamp", "timestamp", "memento_datetime",
              "latest_path", "http_status", "status", "content_type", "content_encoding", "raw_bytes",
              "decoded_bytes", "charset", "flags", "toolbar_injected", "js_rendered", "js_reasons",
              "content_images", "saved_path", "error"):
        if k == "latest_path" and d[k] is None:
            continue
        run.say("%s=%s" % (k, _fmt(d[k])))
    run.say("candidates=%d" % len(d["candidates"]))
    for via, n in sorted(d["found_via"].items()):
        run.say("found_via.%s=%d" % (via, n))
    for k in HOST_KEYS:
        for host, n in d[k]:
            run.say("%s.%s=%d" % (k, host, n))
    for c in d["candidates"]:
        run.say("candidate=%s %s" % (c["found_via"], c["url"]))


def cmd_page(run: Run) -> int:
    a = run.args
    pr = analyse_page(run, a.url, a.timestamp, a.latest)
    _print_page(run, pr)
    return EXIT_OK


def _print_summary(run: Run, summary: dict) -> None:
    if run.args.json:
        run.emit_json(summary)
        return
    for k, v in summary.items():
        if isinstance(v, dict):
            for sub, n in sorted(v.items()):
                run.say("%s.%s=%s" % (k, sub, _fmt(n)))
        elif k in HOST_KEYS:
            for host, n in v:
                run.say("%s.%s=%d" % (k, host, n))
        elif v is not None:
            run.say("%s=%s" % (k, _fmt(v)))


def cmd_images(run: Run) -> int:
    a = run.args
    pr = analyse_page(run, a.url, a.timestamp, a.latest)
    run.info("page %s @ %s: %s (js_rendered=%s)" % (pr.base_url, pr.timestamp, pr.status, _fmt(pr.js_rendered)))
    items = []
    if pr.extraction is not None:
        items = [Item(c.url, pr.timestamp, c.found_via, inline=c.inline) for c in pr.extraction.candidates]
        if a.follow_css:
            items += follow_css(run, pr)
    # auto: the script scan on any sign of script building, even beside
    # content images; the domain listing only for a JS-rendered verdict.
    scan = a.fallback == "always" or (a.fallback == "auto" and bool(pr.js_reasons))
    listing = a.fallback == "always" or (a.fallback == "auto" and pr.js_rendered)
    window = None
    known = {i.url for i in items}
    if scan:
        for url in pr.js_strings:
            if url not in known:
                known.add(url)
                items.append(Item(url, pr.timestamp, "js-string"))
    if listing:
        domain = own_domain(urllib.parse.urlsplit(pr.base_url).hostname or "")
        if not domain or is_shared_host(domain):
            run.info("fallback: no CDX listing for %s (shared or empty host)" % (domain or "?"))
        else:
            window = listing_window(pr.timestamp, a.from_ts, a.to_ts)
            run.info("fallback: CDX listing of %s, captures from %s to %s"
                     % (domain, window[0] or "the start", window[1] or "now"))
            try:
                rows = cdx_listing(run, domain, window[0], window[1])
            except CdxError as e:
                run.info("fallback: CDX listing failed (%s)" % e)
                rows = []
            for it in listing_items(rows):
                if it.url not in known:
                    known.add(it.url)
                    items.append(it)
    found_via = dict(collections.Counter(i.found_via for i in items))
    run.info("candidates: %d (%s)" % (len(items), ", ".join("%s %d" % kv for kv in sorted(found_via.items())) or "none"))
    if pr.script_src_hosts or pr.script_text_hosts:
        run.info("third-party script hosts loaded: %s; only mentioned in inline scripts: %s"
                 % tuple(", ".join("%s %d" % kv for kv in hosts) or "none"
                         for hosts in (pr.script_src_hosts, pr.script_text_hosts)))
    statuses = process_items(run, items)
    _print_summary(run, {
        "page": pr.base_url,
        "page_status": pr.status,
        "requested_timestamp": pr.requested_timestamp,
        "timestamp": pr.timestamp,
        "latest_path": pr.latest_path,
        "content_encoding": pr.content_encoding,
        "toolbar_injected": pr.toolbar_injected,
        "js_rendered": pr.js_rendered,
        "js_reasons": pr.js_reasons,
        "fallback_ran": scan or listing,
        "js_scan_ran": scan,
        "listing_ran": window is not None,
        "listing_window": {"from": window[0], "to": window[1]} if window else None,
        "dry_run": bool(a.dry_run),
        "candidates": len(items),
        "found_via": found_via,
        "script_src_host": [list(kv) for kv in pr.script_src_hosts],
        "script_text_host": [list(kv) for kv in pr.script_text_hosts],
        "status": dict(statuses),
        "requests": run.client.count,
        "exit_code": run.exit_code,
    })
    return run.exit_code


def cmd_site_images(run: Run) -> int:
    a = run.args
    domain = site_domain(a.domain)
    if not domain:
        raise SalvageError("no domain in %r" % a.domain)
    if is_shared_host(domain):
        raise SalvageError("refusing a domain listing on shared host %s: it holds captures for unrelated "
                           "sites. Use images or fetch with exact URLs instead." % domain)
    rows = cdx_listing(run, domain, a.from_ts, a.to_ts, a.loose_mime, a.cdx_limit)
    items = listing_items(rows)
    run.info("CDX listing: %d rows, %d unique digests" % (len(rows), len(items)))
    statuses = process_items(run, items)
    _print_summary(run, {
        "domain": domain,
        "cdx_rows": len(rows),
        "candidates": len(items),
        "dry_run": bool(a.dry_run),
        "status": dict(statuses),
        "requests": run.client.count,
        "exit_code": run.exit_code,
    })
    return run.exit_code


def cmd_verify(args, stdout, stderr) -> int:
    path = os.path.join(args.out, "manifest.csv")
    if not os.path.exists(path):
        stderr.write("error: no manifest.csv in %s\n" % args.out)
        return EXIT_ERROR
    manifest = Manifest(args.out)
    rows = list(manifest.rows.values())
    by_status = collections.Counter(r["status"] for r in rows)
    by_found_via = collections.Counter(r["found_via"] or "-" for r in rows)
    checked, modified, missing = [], [], []
    for r in rows:
        rel = r["saved_path"]
        if not rel or not r["sha256"] or r["status"] not in ("ok", "duplicate") or rel in checked:
            continue
        checked.append(rel)
        try:
            full = resolve_inside(args.out, rel)
        except ValueError:
            missing.append(rel)
            continue
        if not os.path.isfile(full):
            missing.append(rel)
        elif sha256_file(full) != r["sha256"]:
            modified.append(rel)
    report = {
        "manifest": path,
        "rows": len(rows),
        "files_checked": len(checked),
        "files_ok": len(checked) - len(modified) - len(missing),
        "modified": modified,
        "missing": missing,
        "by_status": dict(by_status),
        "by_found_via": dict(by_found_via),
    }
    if args.json:
        stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        for k in ("rows", "files_checked", "files_ok"):
            stdout.write("%s=%d\n" % (k, report[k]))
        stdout.write("modified=%d\nmissing=%d\n" % (len(modified), len(missing)))
        for s, n in sorted(by_status.items()):
            stdout.write("status.%s=%d\n" % (s, n))
        for v, n in sorted(by_found_via.items()):
            stdout.write("found_via.%s=%d\n" % (v, n))
        for rel in modified:
            stdout.write("modified %s\n" % rel)
        for rel in missing:
            stdout.write("missing %s\n" % rel)
    return EXIT_VERIFY if modified or missing else EXIT_OK


# --------------------------------------------------------------------- CLI

EXIT_CODES_HELP = """\
exit codes:
  0  done
  1  usage or unexpected error
  2  --max-requests reached (resumable: rerun the same command)
  3  stopped on sustained 429/503 or an archive.org outage (resumable: rerun
     later, do not lower --delay)
  4  TLS verification failed (the message names the fix)
  5  verify found a modified or missing file
"""


class _ParserExit(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def _ts_arg(value: str) -> str:
    if not TS_RE.match(value):
        raise argparse.ArgumentTypeError("expected 1 to 14 digits (YYYYMMDDhhmmss), got %r" % value)
    return value


def _int_arg(minimum: int):
    def parse(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected an integer, got %r" % value)
        if n < minimum:
            raise argparse.ArgumentTypeError("must be at least %d" % minimum)
        return n

    return parse


def _delay_arg(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected seconds, got %r" % value)
    if not f >= 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return f


def build_parser(stdout, stderr) -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def _print_message(self, message, file=None):
            if not message:
                return
            if file is None or file is sys.stdout:
                stdout.write(message)
            elif file is sys.stderr:
                stderr.write(message)
            else:
                file.write(message)

        def exit(self, status=0, message=None):
            if message:
                stderr.write(message)
            raise _ParserExit(status)

        def error(self, message):
            self.print_usage(stderr)
            stderr.write("%s: error: %s\n" % (self.prog, message))
            raise _ParserExit(EXIT_ERROR)

    common = Parser(add_help=False)
    g = common.add_argument_group("common options")
    g.add_argument("-o", "--out", metavar="DIR", default="./wayback-out",
                   help="output directory for files, manifest.csv and requests.jsonl (default: ./wayback-out)")
    g.add_argument("--delay", metavar="SECONDS", type=_delay_arg, default=6.0,
                   help="minimum seconds between archive.org requests, plus 0-1 s jitter (default: 6)")
    g.add_argument("-n", "--max-requests", metavar="N", type=_int_arg(1), default=300,
                   help="stop after N HTTP requests, redirect hops included; exit 2, resumable (default: 300)")
    g.add_argument("--max-retries", metavar="N", type=_int_arg(0), default=4,
                   help="retries on 429/502/503/504, resets and outage pages before exit 3 (default: 4)")
    g.add_argument("--user-agent", metavar="STR", default=DEFAULT_UA, help="User-Agent header (default: %(default)s)")
    g.add_argument("--cafile", metavar="PATH", help="CA bundle for TLS verification (verification is never disabled)")
    g.add_argument("--dry-run", action="store_true",
                   help="run CDX queries and the page fetch, download no images; manifest gets planned rows")
    g.add_argument("--record", metavar="DIR", help="save every raw response (status, headers, body) in the test-fixture format")
    g.add_argument("--quiet", action="store_true", help="no progress messages on stderr")
    g.add_argument("--json", action="store_true", help="print the result as JSON on stdout")

    parser = Parser(
        description="Recover archived pages and images from the Wayback Machine (stdlib only, polite by default).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="wayback-salvage " + VERSION)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def add(name, help_text, description):
        return sub.add_parser(name, parents=[common], help=help_text, description=textwrap.fill(description, 78),
                              epilog=EXIT_CODES_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)

    def capture_choice(p):
        grp = p.add_mutually_exclusive_group(required=True)
        grp.add_argument("-t", "--timestamp", metavar="TS", type=_ts_arg,
                         help="capture timestamp, 1 to 14 digits; Wayback redirects to the nearest capture")
        grp.add_argument("--latest", action="store_true",
                         help="newest 200 text/html capture (CDX limit=-1, else the now-timestamp redirect)")

    p = add("snapshots", "list captures of a URL (CDX)",
            "CDX query. Prints timestamp, original, status, mimetype, digest, length and the Wayback URL.")
    p.add_argument("url", metavar="URL", help="URL or pattern as CDX accepts it (andrewluxem.com, example.com/*)")
    p.add_argument("--from", dest="from_ts", metavar="YYYYMMDD", type=_ts_arg, help="earliest timestamp")
    p.add_argument("--to", dest="to_ts", metavar="YYYYMMDD", type=_ts_arg, help="latest timestamp")
    p.add_argument("--limit", metavar="N", type=int, help="CDX limit (negative returns the newest rows)")
    p.add_argument("--match", choices=("exact", "prefix", "host", "domain"), help="CDX matchType")
    p.add_argument("--status", metavar="CODE", help="only captures with this HTTP status, e.g. 200")
    p.add_argument("--collapse", metavar="FIELD", help="CDX collapse, e.g. digest (drops adjacent duplicates only)")

    p = add("fetch", "fetch one capture raw (id_), decoded",
            "One id_ fetch, decoded. Prints final timestamp, content-type, encoding, bytes and sha256.")
    p.add_argument("url", metavar="URL")
    p.add_argument("-t", "--timestamp", metavar="TS", type=_ts_arg, required=True, help="capture timestamp")
    p.add_argument("--save", metavar="FILE", help="write the decoded body to FILE")

    p = add("page", "analyse one archived page (no image downloads)",
            "Fetch and analyse one archived page: decoded size, encoding, toolbar-injection flag, "
            "JS-rendered verdict with reasons, image candidates by source type.")
    p.add_argument("url", metavar="URL", help="page URL; a scheme-less input is fetched as http://")
    capture_choice(p)

    p = add("images", "recover the images of one archived page",
            "Main workflow: fetch the page, extract and resolve image URLs, download each via id_ at the "
            "page timestamp, run the JS-rendered fallback when needed, record everything in manifest.csv.")
    p.add_argument("url", metavar="URL", help="page URL; a scheme-less input is fetched as http://")
    capture_choice(p)
    p.add_argument("--fallback", choices=("auto", "never", "always"), default="auto",
                   help="JS-string scan plus the site's own CDX image listing: auto runs the scan when the "
                        "page shows any JS-rendered sign and the listing when it looks JS-rendered "
                        "(default: auto)")
    p.add_argument("--from", dest="from_ts", metavar="YYYYMMDD", type=_ts_arg,
                   help="earliest capture for the fallback's domain listing (default: page capture - 365 days)")
    p.add_argument("--to", dest="to_ts", metavar="YYYYMMDD", type=_ts_arg,
                   help="latest capture for the fallback's domain listing (default: page capture + 365 days)")
    p.add_argument("--prefer-live", action="store_true",
                   help="try each image on its live host first (1 s per-host pacing, magic-byte checked)")
    p.add_argument("--follow-css", action="store_true",
                   help="also fetch linked stylesheets via id_ and collect their url() images")
    p.add_argument("--save-inline", action="store_true", help="save data: URI images instead of recording them")

    p = add("site-images", "recover every archived image of a domain (CDX listing)",
            "CDX listing of archived images for a domain and its subdomains, deduplicated by digest, "
            "then downloaded via id_. Refused for shared CDN hosts.")
    p.add_argument("domain", metavar="DOMAIN", help="site domain, e.g. andrewluxem.com (www. is dropped)")
    p.add_argument("--from", dest="from_ts", metavar="YYYYMMDD", type=_ts_arg, help="earliest timestamp")
    p.add_argument("--to", dest="to_ts", metavar="YYYYMMDD", type=_ts_arg, help="latest timestamp")
    p.add_argument("--loose-mime", action="store_true",
                   help="match image file extensions instead of image/* mimetypes (catches octet-stream captures)")
    p.add_argument("--cdx-limit", metavar="N", type=_int_arg(1), default=2000,
                   help="maximum CDX rows to read, across resume-key pages (default: 2000)")

    add("verify", "re-hash saved files against manifest.csv",
        "Re-hash every saved file in --out against the manifest; print counts by status and found_via. "
        "Exit 5 on a modified or missing file.")
    return parser


def main(argv=None, *, transport=None, clock=None, sleep=None, rng=None, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser(stdout, stderr)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except _ParserExit as e:
        return e.status

    if args.command == "verify":
        return cmd_verify(args, stdout, stderr)

    clock = clock or time.time
    sleep = sleep or time.sleep
    rng = rng or random.Random()
    if transport is None:
        try:
            transport = UrllibTransport(cafile=args.cafile)
        except (OSError, ssl.SSLError) as e:
            stderr.write("error: cannot load --cafile %s: %s\n" % (args.cafile, e))
            return EXIT_ERROR
    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as e:
        stderr.write("error: cannot create --out %s: %s\n" % (args.out, e))
        return EXIT_ERROR
    client = Client(
        transport, clock=clock, sleep=sleep, rng=rng, delay=args.delay,
        max_requests=args.max_requests, max_retries=args.max_retries, user_agent=args.user_agent,
        log_path=os.path.join(args.out, "requests.jsonl"), record_dir=args.record,
    )
    run = Run(args, client, stdout, stderr, clock)
    handler = {
        "snapshots": cmd_snapshots,
        "fetch": cmd_fetch,
        "page": cmd_page,
        "images": cmd_images,
        "site-images": cmd_site_images,
    }[args.command]
    clean = False
    try:
        code = handler(run)
        clean = True
    except CapReached:
        stderr.write("stopped: request cap (--max-requests %d) reached; rerun the same command to resume\n"
                     % args.max_requests)
        code, clean = EXIT_CAP, True
    except RateLimited as e:
        stderr.write("stopped: archive.org kept throttling or failing (%s). Rerun the same command later; it resumes. "
                     "Do not lower --delay.\n" % e)
        code, clean = EXIT_RATE, True
    except TLSError as e:
        stderr.write(e.help_text())
        code, clean = EXIT_TLS, True
    except SalvageError as e:
        stderr.write("error: %s\n" % e)
        code, clean = EXIT_ERROR, True
    except KeyboardInterrupt:
        stderr.write("interrupted; manifest rows so far are saved, rerun the same command to resume\n")
        code = 130
    except Exception:
        stderr.write("unexpected error:\n" + traceback.format_exc())
        code = EXIT_ERROR
    finally:
        run.close(compact=clean)
    return code


if __name__ == "__main__":
    sys.exit(main())
