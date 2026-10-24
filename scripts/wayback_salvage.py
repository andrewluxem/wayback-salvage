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

VERSION = "0.1.0"
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
    """Retries exhausted on 429/502/503/504 or resets (exit 3, resumable)."""

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

    def request(self, url: str, live: bool = False) -> Response:
        """One request with retries. Live requests are not retried."""
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

    def get(self, url: str, live: bool = False) -> Fetched:
        """GET following up to MAX_HOPS redirects, each a counted request."""
        hops = []
        current = url
        while True:
            resp = self.request(current, live=live)
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
