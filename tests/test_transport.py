"""Spec 3.4 transport and TLS, 3.7 rate limiting (cases 7, 8, 15)."""

from __future__ import annotations

import email.utils
import io
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import support
from support import (
    FakeClock,
    FakeTransport,
    fixture,
    make_client,
    read_manifest,
    read_requests,
    redirect,
    resp,
    run_cli,
    wb,
    ws,
)

TS = "20200101000000"


class TestNetworkGuard(unittest.TestCase):
    def test_real_network_is_blocked(self):
        with self.assertRaises(support.NetworkBlocked):
            urllib.request.urlopen("http://example.com/", timeout=1)


class TestRedirects(unittest.TestCase):
    """Case 7: requested vs nearest timestamp, hop counted."""

    def test_manifest_records_both_timestamps(self):
        page = "https://example.com/r/"
        img = "https://example.com/moved.png"
        nearest = wb("20190505050505", img)
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_one_img"))
        t.add(wb(TS, img), redirect(nearest))
        t.add(nearest, fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            row = [r for r in read_manifest(out) if r["original_url"] == img][0]
            self.assertEqual(row["requested_timestamp"], TS)
            self.assertEqual(row["timestamp"], "20190505050505")
            self.assertEqual(row["wayback_url"], nearest)
            self.assertEqual(row["status"], "ok")
            log = read_requests(out)
        self.assertEqual([e["url"] for e in log], [wb(TS, page), wb(TS, img), nearest])
        self.assertEqual([e["status"] for e in log], [200, 302, 200])

    def test_relative_location_and_hop_count(self):
        t = FakeTransport()
        t.add(wb(TS, "https://e.com/a.png"), redirect("/web/2019id_/https://e.com/a.png"))
        t.add(wb("2019", "https://e.com/a.png"), fixture("png_red"))
        client, _ = make_client(t)
        fetched = client.get(wb(TS, "https://e.com/a.png"))
        self.assertEqual(fetched.final_url, wb("2019", "https://e.com/a.png"))
        self.assertEqual(len(fetched.hops), 2)
        self.assertEqual(client.count, 2)

    def test_hop_limit(self):
        t = FakeTransport()
        for i in range(10):
            t.add("https://web.archive.org/x%d" % i, redirect("https://web.archive.org/x%d" % (i + 1)))
        client, _ = make_client(t)
        fetched = client.get("https://web.archive.org/x0")
        self.assertTrue(fetched.too_many_redirects)
        self.assertEqual(client.count, 6)  # the request plus 5 followed hops


class TestBackoff(unittest.TestCase):
    """Case 8."""

    URL = wb(TS, "https://e.com/a.png")

    def test_retry_after_seconds(self):
        t = FakeTransport().add(self.URL, resp(429, {"Retry-After": "7"}), fixture("png_red"))
        client, clock = make_client(t)
        fetched = client.get(self.URL)
        self.assertEqual(fetched.response.status, 200)
        self.assertTrue(any(s >= 7 for s in clock.sleeps), clock.sleeps)
        self.assertEqual(clock.sleeps, [30.0])  # max(7, 30 * 2**0)
        # The steady delay doubles for the rest of the run after a 429.
        self.assertEqual(client.limiter.delay, 12.0)

    def test_retry_after_large_value_wins(self):
        t = FakeTransport().add(self.URL, resp(429, {"Retry-After": "90"}), fixture("png_red"))
        client, clock = make_client(t)
        client.get(self.URL)
        self.assertEqual(clock.sleeps, [90.0])

    def test_retry_after_http_date(self):
        clock = FakeClock()
        date = email.utils.formatdate(clock.time() + 45, usegmt=True)
        t = FakeTransport().add(self.URL, resp(429, {"Retry-After": date}), fixture("png_red"))
        client, clock = make_client(t, clock=clock)
        client.get(self.URL)
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 45.0, delta=1.0)

    def test_503_without_header_is_exponential(self):
        t = FakeTransport().add(self.URL, resp(503), resp(503), resp(502), fixture("png_red"))
        client, clock = make_client(t)
        self.assertEqual(client.get(self.URL).response.status, 200)
        self.assertEqual(clock.sleeps, [30.0, 60.0, 120.0])
        self.assertEqual(client.limiter.delay, 6.0)  # only 429 doubles the delay

    def test_backoff_capped_at_600(self):
        t = FakeTransport().add(self.URL, resp(429, {"Retry-After": "86400"}), fixture("png_red"))
        client, clock = make_client(t)
        client.get(self.URL)
        self.assertEqual(clock.sleeps, [600.0])

    def test_steady_delay_cap(self):
        t = FakeTransport().add(self.URL, *([resp(429)] * 4 + [fixture("png_red")]))
        client, _ = make_client(t)
        client.get(self.URL)
        self.assertEqual(client.limiter.delay, 60.0)

    def test_connection_reset_retried(self):
        t = FakeTransport().add(self.URL, ConnectionResetError(54, "reset"), fixture("png_red"))
        client, clock = make_client(t)
        self.assertEqual(client.get(self.URL).response.status, 200)
        self.assertEqual(clock.sleeps, [30.0])

    def test_steady_pacing(self):
        a, b = wb(TS, "https://e.com/a.png"), wb(TS, "https://e.com/b.png")
        t = FakeTransport().add(a, fixture("png_red")).add(b, fixture("png_red"))
        client, clock = make_client(t)
        client.get(a)
        client.get(b)
        self.assertEqual(clock.sleeps, [6.0])

    def test_five_429s_exit_3_manifest_intact(self):
        page = "https://example.net/"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_four"))
        t.add(wb(TS, "https://example.net/i1.png"), fixture("png_red"))
        t.add(wb(TS, "https://example.net/i2.png"), resp(429))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t, clock)
            self.assertEqual(code, ws.EXIT_RATE, stderr)
            rows = {r["original_url"]: r for r in read_manifest(out)}
            self.assertEqual(rows[page]["status"], "ok")
            self.assertEqual(rows["https://example.net/i1.png"]["status"], "ok")
            self.assertEqual(rows["https://example.net/i2.png"]["status"], "rate-limited")
            self.assertTrue(os.path.isfile(os.path.join(out, rows["https://example.net/i1.png"]["saved_path"])))
            with open(os.path.join(out, "requests.jsonl"), encoding="utf-8") as fh:
                self.assertEqual(fh.read().count('"status": 429'), 5)
        self.assertEqual(t.count(wb(TS, "https://example.net/i2.png")), 5)  # 1 + 4 retries
        self.assertEqual(t.count(wb(TS, "https://example.net/i3.png")), 0)
        self.assertLessEqual(max(clock.sleeps), 600.0)


BACKOFF = [30.0, 60.0, 120.0, 240.0]  # 503 schedule for the default 4 retries


def network_errors() -> list:
    """(label, raised, cause) for each transport error that is retried like
    a 503, bare and wrapped in URLError the way urllib raises it. cause is
    the error the messages should name. A fresh instance per call."""
    causes = [
        ConnectionRefusedError(61, "Connection refused"),
        ConnectionResetError(54, "Connection reset by peer"),
        ConnectionAbortedError(53, "Software caused connection abort"),
        TimeoutError("timed out"),
        socket.timeout("timed out"),  # TimeoutError itself on 3.10+, distinct on 3.9
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
    ]
    out = []
    for cause in causes:
        out.append(("%s bare" % type(cause).__name__, cause, cause))
    for cause in [type(c)(*c.args) for c in causes]:
        out.append(("%s wrapped" % type(cause).__name__, urllib.error.URLError(reason=cause), cause))
    return out


def named(cause: BaseException) -> str:
    return "%s: %s" % (type(cause).__name__, cause)


class TestNetworkErrors(unittest.TestCase):
    """Refused, reset and aborted connections, timeouts and EAI_AGAIN from
    archive.org back off like a 503 and end in exit 3."""

    URL = wb(TS, "https://e.com/a.png")

    def test_retried_with_503_schedule(self):
        for label, err, _ in network_errors():
            with self.subTest(error=label):
                t = FakeTransport().add(self.URL, err, err, fixture("png_red"))
                client, clock = make_client(t)
                self.assertEqual(client.get(self.URL).response.status, 200)
                self.assertEqual(clock.sleeps, [30.0, 60.0])
                self.assertEqual(t.count(self.URL), 3)

    def test_sustained_exits_3_naming_the_error(self):
        for label, err, cause in network_errors():
            with self.subTest(error=label), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(support.is_cdx, err)
                clock = FakeClock()
                code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t, clock)
                self.assertEqual(code, ws.EXIT_RATE, stderr)
                self.assertEqual(len(t.calls), 5)  # 1 + 4 retries
                self.assertEqual(clock.sleeps, BACKOFF)
                self.assertIn("stopped: archive.org", stderr)
                self.assertIn(named(cause), stderr)

    def test_image_refused_exits_3_manifest_flushed(self):
        page = "https://example.net/"
        img2 = "https://example.net/i2.png"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_four"))
        t.add(wb(TS, "https://example.net/i1.png"), fixture("png_red"))
        t.add(wb(TS, img2), urllib.error.URLError(reason=ConnectionRefusedError(61, "Connection refused")))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-o", out], t)
            self.assertEqual(code, ws.EXIT_RATE, stderr)
            self.assertIn("ConnectionRefusedError: [Errno 61] Connection refused", stderr)
            rows = {r["original_url"]: r for r in read_manifest(out)}
            self.assertEqual(rows[page]["status"], "ok")
            self.assertEqual(rows["https://example.net/i1.png"]["status"], "ok")
            self.assertEqual(rows[img2]["status"], "rate-limited")
            self.assertIn("ConnectionRefusedError: [Errno 61] Connection refused", rows[img2]["error"])
        self.assertEqual(t.count(wb(TS, img2)), 5)
        self.assertEqual(t.count(wb(TS, "https://example.net/i3.png")), 0)

    def test_live_requests_are_not_retried(self):
        live = "https://example.org/a.png"
        for label, err, cause in network_errors():
            with self.subTest(error=label):
                t = FakeTransport().add(live, err)
                client, clock = make_client(t)
                with self.assertRaises(ws.NetworkError) as cm:
                    client.get(live, live=True)
                self.assertIn(str(cause), str(cm.exception))
                self.assertEqual(t.count(live), 1)
                self.assertEqual(clock.sleeps, [])

    def test_prefer_live_refused_falls_back_to_wayback_once(self):
        page = "https://example.org/"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_one_img"))
        t.add(lambda u: not u.startswith(support.WB), ConnectionRefusedError(61, "Connection refused"))
        t.add(lambda u: u.startswith(wb(TS, "")), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "--prefer-live", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = [r for r in read_manifest(out) if r["kind"] == "image"]
        self.assertEqual([r["source"] for r in rows], ["wayback"])
        self.assertEqual(t.count(lambda u: not u.startswith(support.WB)), 1)

    def test_error_text(self):
        refused = ConnectionRefusedError(61, "Connection refused")
        self.assertEqual(ws.error_text(refused), "ConnectionRefusedError: [Errno 61] Connection refused")
        self.assertEqual(ws.error_text(urllib.error.URLError(reason=refused)),
                         "ConnectionRefusedError: [Errno 61] Connection refused")
        self.assertEqual(ws.error_text(urllib.error.URLError(reason="unknown url type: htp")),
                         "URLError: unknown url type: htp")
        self.assertEqual(ws.error_text(TimeoutError()), "TimeoutError")

    def test_other_dns_failures_stay_fatal(self):
        for form in ("bare", "wrapped"):
            cause = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
            err = cause if form == "bare" else urllib.error.URLError(reason=cause)
            with self.subTest(form=form), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(lambda u: True, err)
                code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
                self.assertEqual(code, ws.EXIT_ERROR, stderr)
                self.assertIn("nodename nor servname provided", stderr)
                self.assertEqual(len(t.calls), 1)


class TestTls(unittest.TestCase):
    """Case 15: bare SSLCertVerificationError and the URLError wrapping
    that urllib actually raises."""

    def _errors(self):
        bare = ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate")
        wrapped = urllib.error.URLError(
            reason=ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate")
        )
        return {"bare": bare, "wrapped": wrapped}

    def test_exit_4_with_fix_message(self):
        for label, err in self._errors().items():
            with self.subTest(form=label), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(lambda u: True, err)
                code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
                self.assertEqual(code, ws.EXIT_TLS)
                self.assertIn("Install Certificates.command", stderr)
                self.assertIn("--cafile", stderr)
                self.assertIn("Homebrew", stderr)
                self.assertEqual(len(t.calls), 1)  # TLS failure is not retried

    def test_other_url_error_is_not_tls(self):
        t = FakeTransport().add(lambda u: True, urllib.error.URLError(reason="nodename nor servname provided"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
        self.assertEqual(code, ws.EXIT_ERROR)
        self.assertNotIn("Install Certificates.command", stderr)


class TestUrllibTransport(unittest.TestCase):
    def test_verification_stays_on(self):
        tr = ws.UrllibTransport()
        self.assertEqual(tr.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tr.context.check_hostname)

    def test_cafile_is_loaded(self):
        with mock.patch.object(ssl, "create_default_context", wraps=ssl.create_default_context) as cdc:
            with self.assertRaises((FileNotFoundError, ssl.SSLError, OSError)):
                ws.UrllibTransport(cafile="/nonexistent/ca.pem")
        cdc.assert_called_with(cafile="/nonexistent/ca.pem")

    def test_http_error_becomes_response_and_redirects_are_not_followed(self):
        tr = ws.UrllibTransport()
        err = urllib.error.HTTPError(
            "https://web.archive.org/x", 302, "Found", {"Location": "https://web.archive.org/y"}, io.BytesIO(b"")
        )
        with mock.patch.object(tr.opener, "open", side_effect=err):
            r = tr.send("GET", "https://web.archive.org/x", {"User-Agent": "t"})
        self.assertEqual(r.status, 302)
        self.assertEqual(r.header("location"), "https://web.archive.org/y")
        handlers = [type(h).__name__ for h in tr.opener.handlers]
        self.assertNotIn("HTTPRedirectHandler", handlers)

    def test_wrapped_tls_error_propagates_from_urllib(self):
        tr = ws.UrllibTransport()
        err = urllib.error.URLError(reason=ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch.object(tr.opener, "open", side_effect=err):
            with tempfile.TemporaryDirectory() as out:
                code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], tr)
        self.assertEqual(code, ws.EXIT_TLS)
        self.assertIn("Install Certificates.command", stderr)


class TestRequestLog(unittest.TestCase):
    def test_log_fields(self):
        t = FakeTransport().add(wb(TS, "https://e.com/a.png"), resp(429), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["fetch", "https://e.com/a.png", "-t", TS, "-o", out], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            log = read_requests(out)
        self.assertEqual([e["status"] for e in log], [429, 200])
        self.assertEqual([e["attempt"] for e in log], [0, 1])
        self.assertEqual(log[1]["wait"], 30.0)
        for e in log:
            self.assertEqual(set(e), {"time", "method", "url", "status", "bytes", "wait", "attempt"})
            self.assertIsInstance(e["status"], int)

    def test_each_failed_attempt_logs_status_0_and_the_error(self):
        refused = "ConnectionRefusedError: [Errno 61] Connection refused"
        for label, err, cause in network_errors():
            with self.subTest(error=label), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(support.is_cdx, err, err, fixture("cdx_snapshots_five"))
                code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
                self.assertEqual(code, ws.EXIT_OK, stderr)
                log = read_requests(out)
                self.assertEqual([e["status"] for e in log], [0, 0, 200])
                self.assertEqual([e["attempt"] for e in log], [0, 1, 2])
                self.assertEqual([e.get("error") for e in log], [named(cause), named(cause), None])
                self.assertEqual([e["bytes"] for e in log[:2]], [0, 0])
        with tempfile.TemporaryDirectory() as out:
            t = FakeTransport().add(support.is_cdx, ConnectionRefusedError(61, "Connection refused"))
            code, _, _ = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
            self.assertEqual(code, ws.EXIT_RATE)
            log = read_requests(out)
        self.assertEqual([(e["status"], e["error"]) for e in log], [(0, refused)] * 5)

    def test_failures_that_are_not_retried_log_the_error_too(self):
        cases = [
            ("live", urllib.error.URLError(reason=ConnectionRefusedError(61, "Connection refused")), True,
             ws.NetworkError, "ConnectionRefusedError: [Errno 61] Connection refused"),
            ("fatal", urllib.error.URLError(reason="nodename nor servname provided"), False,
             ws.NetworkError, "URLError: nodename nor servname provided"),
            ("tls", urllib.error.URLError(reason=ssl.SSLCertVerificationError(1, "certificate verify failed")),
             False, ws.TLSError, "SSLCertVerificationError: certificate verify failed"),
            ("bad url", ValueError("unknown url type: 'htp'"), False,
             ws.BadUrl, "ValueError: unknown url type: 'htp'"),
        ]
        for label, err, live, raised, text in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as out:
                path = os.path.join(out, "requests.jsonl")
                client, _ = make_client(FakeTransport().add(lambda u: True, err), log_path=path)
                with self.assertRaises(raised):
                    client.get("https://web.archive.org/x", live=live)
                log = read_requests(out)
                self.assertEqual([(e["status"], e["bytes"], e["error"]) for e in log], [(0, 0, text)])


if __name__ == "__main__":
    unittest.main()
