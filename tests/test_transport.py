"""Spec 3.4 transport and TLS, 3.7 rate limiting (cases 7, 8, 15)."""

from __future__ import annotations

import email.utils
import io
import os
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


if __name__ == "__main__":
    unittest.main()
