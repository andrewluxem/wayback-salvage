"""Outage safety: archive.org HTML where CDX JSON or an image is expected
is retried like a 503, and never recorded as not-archived or
type-mismatch, so a rerun after the outage picks the rows up again."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest

from support import (
    FakeClock,
    FakeTransport,
    fixture,
    fixture_body,
    is_cdx,
    make_client,
    read_manifest,
    read_requests,
    resp,
    rows_by_url,
    run_cli,
    wb,
    ws,
)

TS = "20200101000000"
PAGE = "https://example.com/r/"
IMG = "https://example.com/moved.png"
HTML = {"Content-Type": "text/html; charset=utf-8"}
BACKOFF = [30.0, 60.0, 120.0, 240.0]  # 503 schedule for the default 4 retries


def offline(status: int = 200) -> ws.Response:
    return resp(status, HTML, fixture_body("archive_offline"))


def cdx_rows(*rows) -> ws.Response:
    body = json.dumps([list(ws.CDX_FIELDS)] + [list(r) for r in rows]).encode()
    return resp(200, {"Content-Type": "application/json"}, body)


def image_page(t: FakeTransport) -> FakeTransport:
    t.add(wb(TS, PAGE), fixture("page_one_img"))
    return t


class TestOfflinePageDetection(unittest.TestCase):
    def test_title_forms(self):
        for body in (
            fixture_body("archive_offline"),
            b"<html><head><TITLE lang='en'>Internet Archive: Temporarily  Offline</TITLE></head></html>",
            b"<title>temporarily offline</title>",
        ):
            with self.subTest(body=body[:60]):
                self.assertTrue(ws.is_offline_page(resp(503, HTML, body)))

    def test_gzip_encoded_body(self):
        r = resp(200, dict(HTML, **{"Content-Encoding": "gzip"}), gzip.compress(fixture_body("archive_offline")))
        self.assertTrue(ws.is_offline_page(r))

    def test_only_the_title_counts(self):
        # An archived page that says so in its text is not the outage page.
        body = b"<html><head><title>Our shop</title></head><body><p>We are temporarily offline.</p></body></html>"
        self.assertFalse(ws.is_offline_page(resp(200, HTML, body)))
        self.assertFalse(ws.is_offline_page(fixture("png_red")))

    def test_live_hosts_are_not_checked(self):
        t = FakeTransport().add("https://example.org/a.png", offline())
        client, clock = make_client(t)
        self.assertEqual(client.get("https://example.org/a.png", live=True).response.status, 200)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(clock.sleeps, [])


class TestCdxPath(unittest.TestCase):
    def test_non_json_body_is_retried_like_503(self):
        t = FakeTransport().add(is_cdx, fixture("soft_404"), fixture("soft_404"), fixture("cdx_snapshots_five"))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t, clock)
            log = read_requests(out)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertIn("20240916025321", stdout)
        self.assertEqual(clock.sleeps, BACKOFF[:2])
        self.assertEqual([e["status"] for e in log], [200, 200, 200])  # the wire status is logged
        self.assertEqual([e["attempt"] for e in log], [0, 1, 2])

    def test_sustained_non_json_exits_3(self):
        t = FakeTransport().add(is_cdx, fixture("soft_404"))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t, clock)
        self.assertEqual(code, ws.EXIT_RATE)
        self.assertIn("not JSON", stderr)
        self.assertEqual(len(t.calls), 5)
        self.assertEqual(clock.sleeps, BACKOFF)

    def test_offline_page_with_any_status_is_retried(self):
        for status in (200, 404, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(is_cdx, offline(status), fixture("cdx_snapshots_five"))
                code, stdout, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
                self.assertEqual(code, ws.EXIT_OK, stderr)
                self.assertIn("20240916025321", stdout)
                self.assertEqual(len(t.calls), 2)

    def test_empty_body_still_means_no_captures(self):
        t = FakeTransport().add(is_cdx, fixture("cdx_empty"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, _ = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
        self.assertEqual(code, ws.EXIT_OK)
        self.assertIn("no captures", stdout)
        self.assertEqual(len(t.calls), 1)

    def test_outage_during_nearest_capture_lookup_is_not_not_archived(self):
        # The image 404s, so its one CDX lookup runs, and CDX is down.
        t = image_page(FakeTransport())
        t.add(wb(TS, IMG), resp(404, HTML, b"<html><title>Wayback Machine</title></html>"))
        t.add(is_cdx, offline())
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_RATE, stderr)
            self.assertEqual(rows_by_url(out)[IMG]["status"], "rate-limited")
            self.assertEqual(t.count(is_cdx), 5)

            # Archive back: the rerun retries the row and finds the capture.
            t2 = image_page(FakeTransport())
            t2.add(wb(TS, IMG), resp(404, HTML, b""))
            t2.add(is_cdx, cdx_rows(("20190505050505", IMG, "200", "image/png", "D1", "100")))
            t2.add(wb("20190505050505", IMG), fixture("png_red"))
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t2)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            row = rows_by_url(out)[IMG]
        self.assertEqual((row["status"], row["timestamp"]), ("ok", "20190505050505"))

    def test_failed_lookup_row_is_error_and_resumed(self):
        t = image_page(FakeTransport())
        t.add(wb(TS, IMG), resp(404, HTML, b""))
        t.add(is_cdx, resp(400, {"Content-Type": "text/plain"}, b"bad query"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            row = rows_by_url(out)[IMG]
            self.assertEqual(row["status"], "error")
            self.assertIn("CDX", row["error"])
            self.assertEqual(t.count(is_cdx), 1)  # a 400 is not retried

            t.calls.clear()
            code, _, _ = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(t.count(wb(TS, IMG)), 1)  # resume tried it again


class TestImagePath(unittest.TestCase):
    def test_offline_page_is_retried_then_saved(self):
        t = image_page(FakeTransport()).add(wb(TS, IMG), offline(), offline(), fixture("png_red"))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t, clock)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertEqual(rows_by_url(out)[IMG]["status"], "ok")
        self.assertEqual(clock.sleeps[-2:], BACKOFF[:2])

    def test_sustained_offline_page_is_never_type_mismatch(self):
        for status in (200, 404, 302):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as out:
                t = image_page(FakeTransport()).add(wb(TS, IMG), offline(status))
                code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
                self.assertEqual(code, ws.EXIT_RATE, stderr)
                statuses = {r["status"] for r in read_manifest(out) if r["original_url"] == IMG}
                self.assertEqual(statuses, {"rate-limited"})
                self.assertEqual(t.count(wb(TS, IMG)), 5)
                self.assertFalse(any(is_cdx(u) for u in t.calls))  # no 404 reached the lookup

                t2 = image_page(FakeTransport()).add(wb(TS, IMG), fixture("png_red"))
                code, _, _ = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t2)
                self.assertEqual(code, ws.EXIT_OK)
                self.assertEqual(rows_by_url(out)[IMG]["status"], "ok")

    def test_offline_page_in_place_of_the_page(self):
        t = FakeTransport().add(wb(TS, PAGE), offline())
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
        self.assertEqual(code, ws.EXIT_RATE)
        self.assertIn("Temporarily Offline", stderr)
        self.assertEqual(len(t.calls), 5)


if __name__ == "__main__":
    unittest.main()
