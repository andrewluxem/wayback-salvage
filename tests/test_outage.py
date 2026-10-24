"""Outage safety: archive.org HTML where CDX JSON or an image is expected
is retried like a 503, and never recorded as not-archived or
type-mismatch, so a rerun after the outage picks the rows up again. A
replay that carries Memento-Datetime is a real capture, even when its
title says Temporarily Offline."""

from __future__ import annotations

import csv
import gzip
import json
import os
import tempfile
import unittest
from unittest import mock

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
# As sent by a real id_ replay in the live run.
MEMENTO_VALUE = "Mon, 16 Sep 2024 02:53:21 GMT"
MEMENTO = {"Memento-Datetime": MEMENTO_VALUE}
CDX_URL = "https://web.archive.org/cdx/search/cdx?url=example.com&output=json"


def offline(status: int = 200) -> ws.Response:
    return resp(status, HTML, fixture_body("archive_offline"))


def offline_capture(status: int = 200) -> ws.Response:
    """An archived page whose own title says Temporarily Offline."""
    return resp(status, dict(HTML, **MEMENTO), fixture_body("archive_offline"))


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


class TestMementoDatetimeRule(unittest.TestCase):
    """On replay (/web/) responses the title counts as an outage only
    without Memento-Datetime; CDX and other archive.org responses go by
    the title alone."""

    def test_unit(self):
        replay = wb(TS, PAGE)
        page = resp(200, dict(HTML, **MEMENTO), b"<html><title>Home</title></html>")
        for url, r, want in (
            (replay, offline(), True),
            (replay, offline_capture(), False),
            (replay, offline_capture(404), False),
            (replay, resp(200, dict(HTML, **{"Memento-Datetime": "  "}), fixture_body("archive_offline")), True),
            (replay, page, False),
            (wb(TS, PAGE, mod=""), offline(), True),
            (CDX_URL, offline(), True),
            (CDX_URL, offline_capture(), True),
            ("https://web.archive.org/", offline_capture(), True),
        ):
            with self.subTest(url=url, headers=r.headers, status=r.status):
                self.assertIs(ws.is_outage(url, r), want)

    def test_archived_page_with_the_title_is_kept(self):
        t = FakeTransport().add(wb(TS, PAGE), offline_capture())
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t, clock)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            [row] = read_manifest(out)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("status=ok", stdout)
        self.assertEqual((row["status"], row["memento_datetime"]), ("ok", MEMENTO_VALUE))

    def test_archived_image_url_with_the_title_is_final(self):
        # A real capture of HTML at an image URL: type-mismatch, not retried.
        t = image_page(FakeTransport()).add(wb(TS, IMG), offline_capture())
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertEqual(rows_by_url(out)[IMG]["status"], "type-mismatch")
            self.assertEqual(t.count(wb(TS, IMG)), 1)
            code, _, _ = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(t.count(wb(TS, IMG)), 1)  # resume skips it

    def test_cdx_keeps_the_title_rule(self):
        t = FakeTransport().add(is_cdx, offline_capture(), fixture("cdx_snapshots_five"))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t, clock)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertIn("20240916025321", stdout)
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(clock.sleeps, BACKOFF[:1])


class TestMementoDatetimeRecorded(unittest.TestCase):
    def _page(self, headers=None):
        page = resp(200, dict(HTML, **(headers or {})), fixture_body("page_one_img"))
        t = FakeTransport().add(wb(TS, PAGE), page)
        t.add(wb(TS, IMG), resp(200, {"Content-Type": "image/png", "Memento-Datetime": "Tue, 01 Jan 2019 00:00:00 GMT"},
                                fixture_body("png_red")))
        return t

    def test_page_output_and_manifest(self):
        t = self._page(headers=MEMENTO)
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            code, jout, _ = run_cli(["page", PAGE, "-t", TS, "-o", out, "--json"], t)
            [row] = read_manifest(out)
        self.assertIn("\nmemento_datetime=%s\n" % MEMENTO_VALUE, stdout)
        self.assertEqual(json.loads(jout)["memento_datetime"], MEMENTO_VALUE)
        self.assertEqual(row["memento_datetime"], MEMENTO_VALUE)

    def test_blank_without_the_header(self):
        t = self._page()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            [row] = read_manifest(out)
        self.assertIn("\nmemento_datetime=\n", stdout)
        self.assertEqual(row["memento_datetime"], "")

    def test_image_rows(self):
        t = self._page(headers=MEMENTO)
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = rows_by_url(out)
        self.assertEqual(rows[PAGE]["memento_datetime"], MEMENTO_VALUE)
        self.assertEqual(rows[IMG]["memento_datetime"], "Tue, 01 Jan 2019 00:00:00 GMT")

    def test_rewrite_mode_retry_takes_the_retry_header(self):
        t = FakeTransport().add(wb(TS, PAGE), fixture("body_br"))
        t.add(wb(TS, PAGE, mod=""), resp(200, dict(HTML, **MEMENTO), fixture_body("page_plain")))
        with mock.patch.object(ws, "_brotli", None), tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        d = json.loads(stdout)
        self.assertIn("rewrite-mode", d["flags"])
        self.assertEqual(d["memento_datetime"], MEMENTO_VALUE)


class TestManifestHeaderUpgrade(unittest.TestCase):
    """A manifest from 0.2.0 or earlier lacks memento_datetime. It is
    rewritten with the current header before the first append, so rows
    written by a run that then crashes still line up with the header."""

    OLD = ["original_url", "requested_timestamp", "timestamp", "wayback_url", "status",
           "sha256", "saved_path", "kind", "found_via", "source", "mimetype",
           "cdx_digest", "bytes", "content_encoding", "error", "fetched_at"]

    def test_old_manifest_upgraded_on_append(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "manifest.csv"), "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.OLD, lineterminator="\n")
                w.writeheader()
                w.writerow({"original_url": "u1", "requested_timestamp": "1", "status": "ok", "fetched_at": "t1"})
            m = ws.Manifest(out)
            m.append({"original_url": "u2", "requested_timestamp": "2", "status": "ok",
                      "memento_datetime": MEMENTO_VALUE, "fetched_at": "t2"})
            m.close()  # no compact: as after a crash
            with open(os.path.join(out, "manifest.csv"), newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ws.MANIFEST_COLUMNS)
        self.assertEqual({len(r) for r in rows}, {len(ws.MANIFEST_COLUMNS)})
        by_url = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
        self.assertEqual((by_url["u1"]["fetched_at"], by_url["u1"]["memento_datetime"]), ("t1", ""))
        self.assertEqual((by_url["u2"]["fetched_at"], by_url["u2"]["memento_datetime"]), ("t2", MEMENTO_VALUE))

    def test_current_manifest_is_appended_in_place(self):
        with tempfile.TemporaryDirectory() as out:
            m = ws.Manifest(out)
            m.append({"original_url": "u", "requested_timestamp": "1", "status": "http-503"})
            m.append({"original_url": "u", "requested_timestamp": "1", "status": "http-502"})
            m.close()
            m = ws.Manifest(out)
            m.append({"original_url": "u", "requested_timestamp": "1", "status": "ok"})
            m.close()
            # Not rewritten: a rewrite would have compacted the first two.
            self.assertEqual([r["status"] for r in read_manifest(out)], ["http-503", "http-502", "ok"])


if __name__ == "__main__":
    unittest.main()
