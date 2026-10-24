"""Spec 3.2 and 3.8: the images workflow, manifest, resume, dedupe,
verify, scheme-less input and --latest (cases 9, 10, 13, 16, 17)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest

from support import (
    FakeClock,
    FakeTransport,
    cdx_params,
    fixture,
    fixture_body,
    is_cdx,
    read_manifest,
    redirect,
    rows_by_url,
    run_cli,
    wb,
    ws,
)

TS = "20200101000000"


def four_transport():
    page = "https://example.net/"
    t = FakeTransport().add(wb(TS, page), fixture("page_four"))
    for name, colour in zip(("i1", "i2", "i3", "i4"), ("png_red", "png_green", "png_blue", "png_white")):
        t.add(wb(TS, "https://example.net/%s.png" % name), fixture(colour))
    return page, t


def mixed_transport():
    page = "https://example.org/"
    t = FakeTransport()
    t.add(wb(TS, page), fixture("page_mixed"))
    t.add(wb(TS, "https://example.org/a.png"), fixture("png_red"))
    t.add(wb(TS, "https://example.org/b.png"), fixture("png_red"))  # same bytes, other URL
    t.add(wb(TS, "https://example.org/c.png"), fixture("soft_404"))  # 200 HTML body
    return page, t


class TestManifestColumns(unittest.TestCase):
    def test_header(self):
        self.assertEqual(
            ws.MANIFEST_COLUMNS,
            [
                "original_url", "requested_timestamp", "timestamp", "wayback_url", "status",
                "sha256", "saved_path", "kind", "found_via", "source", "mimetype",
                "cdx_digest", "bytes", "content_encoding", "error", "fetched_at",
            ],
        )


class TestCapAndResume(unittest.TestCase):
    """Case 9."""

    def test_cap_then_resume(self):
        page, t = four_transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-n", "3", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_CAP, stderr)
            rows = rows_by_url(out)
            self.assertEqual(rows["https://example.net/i1.png"]["status"], "ok")
            self.assertEqual(rows["https://example.net/i2.png"]["status"], "ok")
            self.assertEqual(rows["https://example.net/i3.png"]["status"], "skipped-cap")
            self.assertEqual(rows["https://example.net/i4.png"]["status"], "skipped-cap")
            self.assertEqual(len(t.calls), 3)

            t.calls.clear()
            code, _, stderr = run_cli(["images", page, "-t", TS, "-n", "3", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertEqual(t.count(wb(TS, "https://example.net/i1.png")), 0)
            self.assertEqual(t.count(wb(TS, "https://example.net/i2.png")), 0)
            self.assertEqual(t.count(wb(TS, "https://example.net/i3.png")), 1)
            self.assertEqual(t.count(wb(TS, "https://example.net/i4.png")), 1)
            rows = read_manifest(out)
            # Compacted at clean exit: one row per key.
            keys = [(r["original_url"], r["requested_timestamp"]) for r in rows]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual({r["status"] for r in rows}, {"ok"})

    def test_append_only_last_row_wins(self):
        with tempfile.TemporaryDirectory() as out:
            m = ws.Manifest(out)
            m.append({"original_url": "u", "requested_timestamp": "1", "status": "http-503"})
            m.append({"original_url": "u", "requested_timestamp": "1", "status": "ok", "sha256": "abc"})
            m.close()
            self.assertEqual(len(read_manifest(out)), 2)  # not compacted yet
            m2 = ws.Manifest(out)
            self.assertEqual(m2.get(("u", "1"))["status"], "ok")
            m2.compact()
            m2.close()
            self.assertEqual(len(read_manifest(out)), 1)


class TestTypeMismatchAndDuplicates(unittest.TestCase):
    """Cases 10 and 13."""

    def test_statuses(self):
        page, t = mixed_transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = rows_by_url(out)
            a, b, c = (rows["https://example.org/%s.png" % n] for n in "abc")
            self.assertEqual(a["status"], "ok")
            self.assertEqual(b["status"], "duplicate")
            self.assertEqual(b["saved_path"], a["saved_path"])
            self.assertEqual(b["sha256"], a["sha256"])
            self.assertEqual(c["status"], "type-mismatch")
            self.assertEqual(c["saved_path"], "")
            self.assertEqual(a["sha256"], hashlib.sha256(fixture_body("png_red")).hexdigest())
            images = os.listdir(os.path.join(out, "images", "example.org"))
            self.assertEqual(images, ["a.png"])

    def test_resume_skips_type_mismatch(self):
        page, t = mixed_transport()
        with tempfile.TemporaryDirectory() as out:
            run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            t.calls.clear()
            code, _, _ = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(t.calls, [wb(TS, page)])

    def test_known_digest_not_refetched(self):
        t = FakeTransport()
        t.add(lambda u: is_cdx(u) and "resumeKey" not in cdx_params(u), fixture("cdx_listing_page1"))
        t.add(lambda u: is_cdx(u) and "resumeKey" in cdx_params(u), fixture("cdx_listing_page2"))
        t.add(wb(TS, "https://example.com/logo.png"), fixture("png_red"))
        t.add(wb(TS, "https://example.com/hero.jpg"), fixture("png_green"))
        t.add(wb("20210101000000", "https://www.example.com/banner.gif"), fixture("png_blue"))
        with tempfile.TemporaryDirectory() as out:
            # Seed the manifest with an ok row carrying the hero digest under
            # another URL; the listing row must then be skipped as duplicate.
            m = ws.Manifest(out)
            m.append({
                "original_url": "https://example.com/hero-old.jpg", "requested_timestamp": "2019",
                "status": "ok", "cdx_digest": "DIGESTHERO", "saved_path": "images/example.com/hero-old.jpg",
                "kind": "image",
            })
            m.close()
            code, _, stderr = run_cli(["site-images", "example.com", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            hero = rows_by_url(out)["https://example.com/hero.jpg"]
        self.assertEqual(hero["status"], "duplicate")
        self.assertEqual(hero["saved_path"], "images/example.com/hero-old.jpg")
        self.assertEqual(t.count(wb(TS, "https://example.com/hero.jpg")), 0)

    def test_same_path_different_capture_gets_ts_suffix(self):
        t = FakeTransport()
        t.add(lambda u: is_cdx(u), fixture("cdx_listing_page2"))
        t.add(wb("20210101000000", "https://example.com/logo.png"), fixture("png_red"))
        t.add(wb("20210101000000", "https://www.example.com/banner.gif"), fixture("png_blue"))
        with tempfile.TemporaryDirectory() as out:
            target = os.path.join(out, "images", "example.com")
            os.makedirs(target)
            with open(os.path.join(target, "logo.png"), "wb") as fh:
                fh.write(fixture_body("png_green"))
            code, _, stderr = run_cli(["site-images", "example.com", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            logo = rows_by_url(out)["https://example.com/logo.png"]
        self.assertEqual(logo["saved_path"], "images/example.com/logo__20210101000000.png")


class TestVerify(unittest.TestCase):
    """Case 16."""

    def test_clean_modified_missing(self):
        page, t = mixed_transport()
        with tempfile.TemporaryDirectory() as out:
            run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            code, stdout, _ = run_cli(["verify", "-o", out])
            self.assertEqual(code, ws.EXIT_OK, stdout)
            self.assertIn("ok", stdout)
            self.assertIn("img:src", stdout)  # counts by found_via

            rows = rows_by_url(out)
            with open(os.path.join(out, rows["https://example.org/a.png"]["saved_path"]), "ab") as fh:
                fh.write(b"tampered")
            os.remove(os.path.join(out, rows[page]["saved_path"]))

            code, stdout, _ = run_cli(["verify", "-o", out, "--json"])
            self.assertEqual(code, ws.EXIT_VERIFY)
            report = json.loads(stdout)
            self.assertEqual(report["modified"], [rows["https://example.org/a.png"]["saved_path"]])
            self.assertEqual(report["missing"], [rows[page]["saved_path"]])
            self.assertEqual(report["by_status"]["ok"], 2)
            self.assertEqual(report["by_status"]["duplicate"], 1)
            self.assertEqual(report["by_status"]["type-mismatch"], 1)

    def test_no_manifest(self):
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["verify", "-o", out])
        self.assertEqual(code, ws.EXIT_ERROR)
        self.assertIn("manifest", stderr)


class TestSchemeLessAndLatest(unittest.TestCase):
    """Case 17."""

    PAGE_HTTPS = "https://andrewluxem.com/"
    LTS = "20240916025321"

    def test_scheme_less_page_with_timestamp(self):
        t = FakeTransport()
        t.add(wb(self.LTS, "http://andrewluxem.com"), redirect(wb(self.LTS, self.PAGE_HTTPS)))
        t.add(wb(self.LTS, self.PAGE_HTTPS), fixture("page_scheme_less"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", "andrewluxem.com", "-t", self.LTS, "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertEqual(t.calls[0], "https://web.archive.org/web/%sid_/http://andrewluxem.com" % self.LTS)
        info = json.loads(stdout)
        self.assertEqual(info["base_url"], self.PAGE_HTTPS)
        self.assertEqual([c["url"] for c in info["candidates"]], ["https://andrewluxem.com/img/a.png"])

    def _latest_transport(self, cdx_fixture):
        t = FakeTransport()
        t.add(
            lambda u: is_cdx(u) and cdx_params(u).get("limit") == ["-1"],
            fixture(cdx_fixture),
        )
        t.add(wb(self.LTS, self.PAGE_HTTPS), fixture("page_scheme_less"))
        return t

    def test_latest_one_row(self):
        t = self._latest_transport("cdx_latest_one")
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", "andrewluxem.com", "--latest", "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        info = json.loads(stdout)
        self.assertEqual(info["latest_path"], "cdx-negative-limit")
        self.assertEqual(info["timestamp"], self.LTS)
        self.assertEqual(info["base_url"], self.PAGE_HTTPS)
        self.assertEqual([c["url"] for c in info["candidates"]], ["https://andrewluxem.com/img/a.png"])
        params = cdx_params(t.calls[0])
        self.assertEqual(params["url"], ["andrewluxem.com"])
        self.assertIn("statuscode:200", params["filter"])
        self.assertIn("mimetype:text/html", params["filter"])
        self.assertEqual(t.calls[1], wb(self.LTS, self.PAGE_HTTPS))

    def test_latest_multi_row_falls_back_to_now_redirect(self):
        clock = FakeClock()
        now14 = time.strftime("%Y%m%d%H%M%S", time.gmtime(clock.time()))
        t = self._latest_transport("cdx_latest_multi")
        t.add(wb(now14, "http://andrewluxem.com"), redirect(wb(self.LTS, self.PAGE_HTTPS)))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", "andrewluxem.com", "--latest", "-o", out, "--json"], t, clock)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        info = json.loads(stdout)
        self.assertEqual(info["latest_path"], "now-redirect")
        self.assertEqual(info["timestamp"], self.LTS)
        self.assertEqual(info["base_url"], self.PAGE_HTTPS)
        self.assertEqual(t.calls[1], wb(now14, "http://andrewluxem.com"))

    def test_latest_empty_falls_back(self):
        clock = FakeClock()
        now14 = time.strftime("%Y%m%d%H%M%S", time.gmtime(clock.time()))
        t = self._latest_transport("cdx_empty")
        t.add(wb(now14, "http://andrewluxem.com"), redirect(wb(self.LTS, self.PAGE_HTTPS)))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", "andrewluxem.com", "--latest", "-o", out, "--json"], t, clock)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertEqual(json.loads(stdout)["latest_path"], "now-redirect")

    def test_timestamp_and_latest_are_exclusive_and_required(self):
        for args in (["page", "andrewluxem.com"], ["page", "andrewluxem.com", "-t", "2020", "--latest"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as out:
                code, _, _ = run_cli(args + ["-o", out])
                self.assertEqual(code, ws.EXIT_ERROR)


class TestOptions(unittest.TestCase):
    def test_dry_run_plans_without_downloading(self):
        page, t = four_transport()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["images", page, "-t", TS, "--dry-run", "-o", out], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = [r for r in read_manifest(out) if r["kind"] == "image"]
            self.assertEqual({r["status"] for r in rows}, {"planned"})
            self.assertEqual(len(rows), 4)
            self.assertIn("img:src", stdout)
        self.assertEqual(t.calls, [wb(TS, page)])

    def test_dry_run_rows_are_fetched_on_real_run(self):
        page, t = four_transport()
        with tempfile.TemporaryDirectory() as out:
            run_cli(["images", page, "-t", TS, "--dry-run", "-o", out, "--quiet"], t)
            code, _, _ = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
            self.assertEqual({r["status"] for r in read_manifest(out)}, {"ok"})

    def test_prefer_live(self):
        page = "https://example.org/"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_mixed"))
        t.add("https://example.org/a.png", fixture("png_red"))
        t.add("https://example.org/b.png", fixture("soft_404"))  # live soft 404, falls back
        t.add(wb(TS, "https://example.org/b.png"), fixture("png_green"))
        t.add("https://example.org/c.png", fixture("png_blue"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "--prefer-live", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = rows_by_url(out)
        self.assertEqual(rows["https://example.org/a.png"]["source"], "live")
        self.assertEqual(rows["https://example.org/b.png"]["source"], "wayback")
        self.assertEqual(rows["https://example.org/c.png"]["source"], "live")
        self.assertEqual(t.count(wb(TS, "https://example.org/a.png")), 0)

    def test_follow_css(self):
        page = "https://example.com/p/"
        css = "https://example.com/css/site.css"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_css"))
        t.add(wb(TS, css), fixture("site_css"))
        t.add(wb(TS, "https://example.com/img/banner.png"), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "--follow-css", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            row = rows_by_url(out)["https://example.com/img/banner.png"]
        self.assertEqual(row["found_via"], "css-link")
        self.assertEqual(row["status"], "ok")

    def test_inline_data(self):
        page = "https://example.com/i/"
        t = FakeTransport().add(wb(TS, page), fixture("page_inline"))
        with tempfile.TemporaryDirectory() as out:
            code, _, _ = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
            row = [r for r in read_manifest(out) if r["kind"] == "image"][0]
            self.assertEqual(row["status"], "inline-data")
            self.assertEqual(row["saved_path"], "")
            code, _, _ = run_cli(["images", page, "-t", TS, "--save-inline", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK)
            row = [r for r in read_manifest(out) if r["kind"] == "image"][0]
            self.assertEqual(row["status"], "ok")
            with open(os.path.join(out, row["saved_path"]), "rb") as fh:
                self.assertEqual(ws.sniff_image(fh.read()), "png")

    def test_record_writes_fixture_format(self):
        url = "https://e.com/a.png"
        t = FakeTransport().add(wb(TS, url), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as rec:
            code, _, stderr = run_cli(["fetch", url, "-t", TS, "-o", out, "--record", rec], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            metas = sorted(f for f in os.listdir(rec) if f.endswith(".meta.json"))
            self.assertEqual(len(metas), 1)
            with open(os.path.join(rec, metas[0]), encoding="utf-8") as fh:
                meta = json.load(fh)
            self.assertEqual(meta["status"], 200)
            self.assertEqual(meta["url"], wb(TS, url))
            self.assertIn("headers", meta)
            with open(os.path.join(rec, metas[0][: -len(".meta.json")] + ".body"), "rb") as fh:
                self.assertEqual(fh.read(), fixture_body("png_red"))

    def test_fetch_save(self):
        url = "https://andrewluxem.com/"
        t = FakeTransport().add(wb(TS, url), fixture("page_gzip"))
        with tempfile.TemporaryDirectory() as out:
            dest = os.path.join(out, "saved.html")
            code, stdout, stderr = run_cli(["fetch", url, "-t", TS, "--save", dest, "-o", out], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            with open(dest, "rb") as fh:
                data = fh.read()
        self.assertEqual(data, fixture_body("page_plain"))
        self.assertIn("timestamp=%s" % TS, stdout)
        self.assertIn("content_encoding=gzip", stdout)
        self.assertIn("sha256=%s" % hashlib.sha256(data).hexdigest(), stdout)
        self.assertIn("bytes=%d" % len(data), stdout)

    def test_image_404_falls_back_to_one_exact_cdx_lookup(self):
        page = "https://example.com/r/"
        img = "https://example.com/moved.png"
        t = FakeTransport()
        t.add(wb(TS, page), fixture("page_one_img"))
        t.add(wb(TS, img), ws.Response("", 404, {}, b""))
        t.add(lambda u: is_cdx(u), fixture("cdx_header_only"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", TS, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertEqual(rows_by_url(out)[img]["status"], "not-archived")
        cdx = [cdx_params(u) for u in t.calls if is_cdx(u)]
        self.assertEqual(len(cdx), 1)
        self.assertEqual(cdx[0]["url"], [img])
        self.assertNotIn("matchType", cdx[0])


class TestUsage(unittest.TestCase):
    def test_bad_arguments_exit_1(self):
        for args in (["nonsense"], ["fetch", "https://e.com/"], ["snapshots"], ["page", "x", "-t", "notdigits"]):
            with self.subTest(args=args):
                code, _, _ = run_cli(args)
                self.assertEqual(code, ws.EXIT_ERROR)

    def test_help_exits_0(self):
        for args in (["--help"], ["snapshots", "--help"], ["fetch", "--help"], ["page", "--help"],
                     ["images", "--help"], ["site-images", "--help"], ["verify", "--help"]):
            with self.subTest(args=args):
                code, stdout, _ = run_cli(args)
                self.assertEqual(code, ws.EXIT_OK)
                self.assertIn("usage", stdout.lower())

    def test_version(self):
        code, stdout, _ = run_cli(["--version"])
        self.assertEqual(code, ws.EXIT_OK)
        self.assertIn(ws.VERSION, stdout)

    def test_inline_data_png_is_valid_base64(self):
        # Guards the fixture itself: the inline image decodes to a PNG.
        html = fixture_body("page_inline").decode("ascii")
        data = html.split("base64,", 1)[1].split('"', 1)[0]
        self.assertEqual(ws.sniff_image(base64.b64decode(data)), "png")


if __name__ == "__main__":
    unittest.main()
