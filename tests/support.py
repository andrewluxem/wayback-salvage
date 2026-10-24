"""Shared test plumbing: network guard, fake transport, fake clock.

Importing this module installs the guard, so every test module imports it
before anything else. Any attempt to open a socket or resolve a name raises
NetworkBlocked, which makes accidental network use fail loudly.
"""

from __future__ import annotations

import csv
import io
import json
import os
import socket
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("tests must not touch the network: %r" % (args[:2],))


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import wayback_salvage as ws  # noqa: E402

WB = "https://web.archive.org"


def wb(ts: str, url: str, mod: str = "id_") -> str:
    return "%s/web/%s%s/%s" % (WB, ts, mod, url)


def fixture(name: str) -> ws.Response:
    with open(os.path.join(FIXTURES, name + ".meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(FIXTURES, name + ".body"), "rb") as fh:
        body = fh.read()
    return ws.Response("", meta["status"], meta.get("headers", {}), body)


def fixture_body(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name + ".body"), "rb") as fh:
        return fh.read()


def resp(status: int, headers: dict = None, body: bytes = b"") -> ws.Response:
    return ws.Response("", status, headers or {}, body)


def redirect(location: str, status: int = 302) -> ws.Response:
    return resp(status, {"Location": location}, b"")


def cdx_params(url: str) -> dict:
    """Query parameters of a CDX request, or {} for any other URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.path != "/cdx/search/cdx":
        return {}
    return urllib.parse.parse_qs(parts.query, keep_blank_values=True)


def is_cdx(url: str) -> bool:
    return urllib.parse.urlsplit(url).path == "/cdx/search/cdx"


class FakeTransport:
    """Maps request URLs to canned responses.

    A route key is either an exact URL or a predicate taking the URL. Each
    route holds a sequence of responses (or exceptions to raise); the last
    one repeats once the sequence is used up. Unrouted URLs get a 404 and
    are listed in .unrouted so tests can assert on them.
    """

    def __init__(self):
        self.exact = {}
        self.predicates = []
        self.calls = []
        self.unrouted = []

    def add(self, key, *responses):
        if not responses:
            raise ValueError("route needs at least one response")
        entry = [list(responses), 0]
        if callable(key):
            self.predicates.append((key, entry))
        else:
            self.exact[key] = entry
        return self

    def _next(self, entry):
        items, idx = entry
        item = items[min(idx, len(items) - 1)]
        entry[1] = idx + 1
        return item

    def send(self, method, url, headers):
        self.calls.append(url)
        entry = self.exact.get(url)
        if entry is None:
            for pred, candidate in self.predicates:
                if pred(url):
                    entry = candidate
                    break
        if entry is None:
            self.unrouted.append(url)
            return ws.Response(url, 404, {"Content-Type": "text/plain"}, b"unrouted")
        item = self._next(entry)
        if isinstance(item, BaseException):
            raise item
        return ws.Response(url, item.status, dict(item.headers), item.body)

    def count(self, url_or_pred) -> int:
        if callable(url_or_pred):
            return sum(1 for u in self.calls if url_or_pred(u))
        return self.calls.count(url_or_pred)


class FakeClock:
    """Injected clock: sleep() records the call and advances time."""

    def __init__(self, start: float = 1800000000.0):
        self.now = start
        self.sleeps = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ZeroRandom:
    def random(self) -> float:
        return 0.0


def run_cli(args, transport=None, clock=None):
    """Run ws.main with fakes; returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    clock = clock or FakeClock()
    code = ws.main(
        list(args),
        transport=transport if transport is not None else FakeTransport(),
        clock=clock.time,
        sleep=clock.sleep,
        rng=ZeroRandom(),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def make_client(transport, clock=None, **kwargs):
    clock = clock or FakeClock()
    client = ws.Client(
        transport,
        clock=clock.time,
        sleep=clock.sleep,
        rng=ZeroRandom(),
        **kwargs,
    )
    return client, clock


def read_manifest(out_dir: str) -> list:
    path = os.path.join(out_dir, "manifest.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def rows_by_url(out_dir: str) -> dict:
    """Last manifest row per original_url (tests use one timestamp per URL)."""
    return {r["original_url"]: r for r in read_manifest(out_dir)}


def read_requests(out_dir: str) -> list:
    path = os.path.join(out_dir, "requests.jsonl")
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
