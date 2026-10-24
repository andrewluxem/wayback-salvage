"""Spec 3.4 toolbar check and 3.6 JS-rendered detection and fallback
(cases 5 and 11)."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest

from support import (
    FakeTransport,
    cdx_params,
    fixture,
    fixture_body,
    is_cdx,
    read_manifest,
    run_cli,
    wb,
    ws,
)

TS = "20240916025321"
PAGE = "https://andrewluxem.com/"
JS_A = "https://lh3.googleusercontent.com/sitesv/AAA111=w1280"
JS_B = "https://lh3.googleusercontent.com/sitesv/BBB222?authuser=0&fife=s16383"
OG = "https://lh3.googleusercontent.com/og/OG000=w16383"
ICON = "https://ssl.gstatic.com/atari/images/public/favicon.ico"


def gsites_html() -> str:
    return gzip.decompress(fixture_body("page_gsites")).decode("utf-8")


def gsites_transport() -> FakeTransport:
    t = FakeTransport()
    t.add(wb(TS, PAGE), fixture("page_gsites"))
    t.add(
        lambda u: is_cdx(u) and cdx_params(u).get("matchType") == ["domain"],
        fixture("cdx_header_only"),
    )
    t.add(wb(TS, JS_A), fixture("png_red"))
    t.add(wb(TS, JS_B), fixture("png_green"))
    t.add(wb(TS, OG), fixture("png_blue"))
    t.add(wb(TS, ICON), fixture("png_white"))
    return t


class TestGoogleSitesPage(unittest.TestCase):
    """Case 5."""

    def test_verdict_and_reasons(self):
        html = gsites_html()
        ex = ws.extract(html, PAGE)
        verdict, reasons = ws.js_verdict(ex, html)
        self.assertTrue(verdict)
        joined = " ".join(reasons)
        self.assertIn("DOCS_timing", joined)
        self.assertIn("atari", joined)
        # Icons, og:image and @font-face urls are not content images.
        self.assertEqual(ws.content_image_count(ex), 0)

    def test_js_string_scan_decodes_escapes_exactly(self):
        ex = ws.extract(gsites_html(), PAGE)
        self.assertEqual(ws.scan_js_strings(ex.scripts), [JS_A, JS_B])

    def test_scan_hex_and_unicode_escape_forms(self):
        script = (
            r'a="https:\x2f\x2fimages.squarespace-cdn.com\x2fcontent\x2fpic?format\x3d750w";'
            r'b="https://res.cloudinary.com/d/image/upload/w_300,h_200/s.jpg";'
            r'c="https://example.com/not-an-image.js";'
            r'd="https://example.com/photo.JPG?x=1&y=2";'
        )
        self.assertEqual(
            ws.scan_js_strings([script]),
            [
                "https://images.squarespace-cdn.com/content/pic?format=750w",
                "https://res.cloudinary.com/d/image/upload/w_300,h_200/s.jpg",
                "https://example.com/photo.JPG?x=1&y=2",
            ],
        )

    def test_images_fallback_end_to_end(self):
        t = gsites_transport()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["images", PAGE, "-t", TS, "-o", out, "--json"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            summary = json.loads(stdout)
            self.assertTrue(summary["js_rendered"])
            self.assertTrue(summary["fallback_ran"])
            rows = read_manifest(out)
        js_rows = {r["original_url"]: r for r in rows if r["found_via"] == "js-string"}
        self.assertEqual(set(js_rows), {JS_A, JS_B})
        for r in js_rows.values():
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["requested_timestamp"], TS)
        page_rows = [r for r in rows if r["kind"] == "page"]
        self.assertEqual(page_rows[0]["content_encoding"], "gzip")
        # The domain listing ran for the site's own domain only.
        listings = [cdx_params(u) for u in t.calls if is_cdx(u)]
        self.assertEqual([p["url"] for p in listings], [["andrewluxem.com"]])
        for u in t.calls:
            if is_cdx(u) and "googleusercontent" in u:
                self.assertNotIn(cdx_params(u).get("matchType"), (["domain"], ["prefix"], ["host"]))
        hosts = dict((h, n) for h, n in summary["host_report"])
        self.assertIn("lh3.googleusercontent.com", hosts)
        self.assertIn("www.gstatic.com", hosts)
        self.assertNotIn("andrewluxem.com", hosts)
        self.assertEqual(t.unrouted, [])

    def test_fallback_never(self):
        t = gsites_transport()
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(
                ["images", PAGE, "-t", TS, "-o", out, "--json", "--fallback", "never"], t
            )
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertFalse(json.loads(stdout)["fallback_ran"])
            self.assertFalse(any(r["found_via"] == "js-string" for r in read_manifest(out)))
        self.assertFalse(any(is_cdx(u) for u in t.calls))

    def test_fallback_always_on_static_page(self):
        t = FakeTransport()
        t.add(wb(TS, PAGE), fixture("page_plain"))
        t.add(lambda u: is_cdx(u), fixture("cdx_header_only"))
        t.add(wb(TS, "https://andrewluxem.com/img/simple.png"), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(
                ["images", PAGE, "-t", TS, "-o", out, "--json", "--fallback", "always"], t
            )
            self.assertEqual(code, ws.EXIT_OK, stderr)
            summary = json.loads(stdout)
        self.assertFalse(summary["js_rendered"])
        self.assertTrue(summary["fallback_ran"])
        self.assertEqual(len([u for u in t.calls if is_cdx(u)]), 1)

    def test_no_domain_listing_when_page_is_on_a_shared_host(self):
        page = "https://storage.googleapis.com/some-bucket/index.html"
        t = FakeTransport().add(wb(TS, page), fixture("page_gsites"))
        t.add(lambda u: not is_cdx(u) and u != wb(TS, page), fixture("png_red"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(
                ["images", page, "-t", TS, "-o", out, "--json", "--fallback", "always"], t
            )
            self.assertEqual(code, ws.EXIT_OK, stderr)
            self.assertTrue(json.loads(stdout)["fallback_ran"])
        self.assertFalse(any(is_cdx(u) for u in t.calls))

    def test_page_command_prints_verdict(self):
        t = FakeTransport().add(wb(TS, PAGE), fixture("page_gsites"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertIn("js_rendered=true", stdout)
        self.assertIn("DOCS_timing", stdout)
        self.assertIn("content_images=0", stdout)
        self.assertIn("content_encoding=gzip", stdout)
        # page never downloads images or runs the fallback listing
        self.assertEqual(t.calls, [wb(TS, PAGE)])


class TestToolbar(unittest.TestCase):
    """Case 11."""

    def test_markers(self):
        self.assertTrue(ws.is_toolbar_injected(fixture_body("page_toolbar_static").decode()))
        self.assertTrue(ws.is_toolbar_injected(fixture_body("page_toolbar_wm").decode()))
        self.assertFalse(ws.is_toolbar_injected(fixture_body("page_wombat_mention").decode()))
        self.assertFalse(ws.is_toolbar_injected("__wm.init only, no second marker"))

    def test_page_command_flags(self):
        for name, want in (
            ("page_toolbar_static", "true"),
            ("page_toolbar_wm", "true"),
            ("page_wombat_mention", "false"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(wb(TS, PAGE), fixture(name))
                code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
                self.assertEqual(code, ws.EXIT_OK, stderr)
                self.assertIn("toolbar_injected=" + want, stdout)
                status = read_manifest(out)[0]["status"]
                self.assertEqual(status, "toolbar-injected" if want == "true" else "ok")


if __name__ == "__main__":
    unittest.main()
