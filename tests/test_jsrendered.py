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
    resp,
    run_cli,
    wb,
    ws,
)
from make_fixtures import BACKSLASH, js_escape  # noqa: E402

TS = "20240916025321"
PAGE = "https://andrewluxem.com/"
JS_A = "https://lh3.googleusercontent.com/sitesv/AAA111=w1280"
JS_B = "https://lh3.googleusercontent.com/sitesv/BBB222?authuser=0&fife=s16383"
OG = "https://lh3.googleusercontent.com/og/OG000=w16383"
ICON = "https://ssl.gstatic.com/atari/images/public/favicon.ico"


def gsites_html() -> str:
    return gzip.decompress(fixture_body("page_gsites")).decode("utf-8")


def gsites_transport(ts: str = TS) -> FakeTransport:
    t = FakeTransport()
    t.add(wb(ts, PAGE), fixture("page_gsites"))
    t.add(
        lambda u: is_cdx(u) and cdx_params(u).get("matchType") == ["domain"],
        fixture("cdx_header_only"),
    )
    t.add(wb(ts, JS_A), fixture("png_red"))
    t.add(wb(ts, JS_B), fixture("png_green"))
    t.add(wb(ts, OG), fixture("png_blue"))
    t.add(wb(ts, ICON), fixture("png_white"))
    return t


def listing_params(t: FakeTransport) -> list:
    return [cdx_params(u) for u in t.calls if is_cdx(u) and cdx_params(u).get("matchType") == ["domain"]]


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
        html = gsites_html()
        # Pin that the fixture really carries the escaped forms; they were
        # once lost in transit and the test passed without them.
        for code in ("u002f", "u003d", "u0026"):
            self.assertIn(BACKSLASH + code, html)
        self.assertNotIn("BBB222?authuser=0", html)
        # The atari-embeds frame (HTML on an image host) is present but not kept.
        self.assertIn("atari-embeds.googleusercontent.com", html)
        ex = ws.extract(html, PAGE)
        self.assertEqual(ws.scan_js_strings(ex.scripts), [JS_A, JS_B])

    def test_non_image_paths_on_image_hosts_rejected(self):
        # Live run: this matched only because the host ends in
        # googleusercontent.com.
        embed = ("https://1230016118-atari-embeds.googleusercontent.com/embeds/"
                 "16cb204cf3a9d4d223a0a3fd8b0eec5d/inner-frame-minified.html")
        keep_a = "https://lh3.googleusercontent.com/sitesv/KEEP1=w800"
        keep_b = "https://res.cloudinary.com/d/image/upload/keep2.png"
        script = ",".join('"%s"' % u for u in (
            embed,
            embed.replace("/", BACKSLASH + "/"),
            embed + "?jsh=m",
            "https://lh3.googleusercontent.com/static/app.js",
            "https://res.cloudinary.com/d/raw/upload/site.css",
            "https://images.ctfassets.net/space/data.json",
            keep_a,
            keep_b,
        ))
        self.assertEqual(ws.scan_js_strings([script]), [keep_a, keep_b])

    def test_scan_hex_and_unicode_escape_forms(self):
        script = (
            r'a="https:\x2f\x2fimages.squarespace-cdn.com\x2fcontent\x2fpic?format\x3d750w";'
            + 'b="' + js_escape("https://res.cloudinary.com/d/image/upload/w_300,h_200/s.jpg", "/", upper=True) + '";'
            + r'c="https://example.com/not-an-image.js";'
            + 'd="' + js_escape("https://example.com/photo.JPG?x=1&y=2", "&") + '";'
        )
        self.assertIn(BACKSLASH + "u002F", script)
        self.assertIn(BACKSLASH + "u0026", script)
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


class TestHostReport(unittest.TestCase):
    """ld+json, templates and other non-JavaScript script types stay out of
    the host report, but the JS-string scan still reads them."""

    HTML = (
        '<html><head>'
        '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Person",'
        '"sameAs":["https://www.linkedin.com/in/example"],'
        '"image":"https://cdn.ldjson-example.net/brand/logo.png"}</script>'
        '<script type="text/x-template"><a href="https://templates.example.org/card.png"></a></script>'
        '<script type="importmap">{"imports":{"app":"https://modules.example.org/app.js"}}</script>'
        '<script type="application/ld+json" src="https://data.example.org/site.json"></script>'
        '<script src="https://www.gstatic.com/a.js"></script>'
        '<script type="module" src="https://esm.example.net/m.js"></script>'
        '<script type="text/javascript; charset=utf-8">x="https://lh3.googleusercontent.com/sitesv/CCC333=w1280"</script>'
        '<script>y="https://ads.example.com/pixel.gif"</script>'
        '<SCRIPT TYPE=" Text/JavaScript ">z="https://lh3.googleusercontent.com/z.png"</SCRIPT>'
        '</head><body><p>' + "Ordinary visible text. " * 12 + '</p></body></html>'
    )

    def test_data_blocks_excluded(self):
        ex = ws.extract(self.HTML, PAGE)
        self.assertEqual(
            ws.host_report(ex, "andrewluxem.com"),
            [("lh3.googleusercontent.com", 2), ("ads.example.com", 1),
             ("esm.example.net", 1), ("www.gstatic.com", 1)],
        )

    def test_data_blocks_still_scanned(self):
        ex = ws.extract(self.HTML, PAGE)
        self.assertEqual(
            ws.scan_js_strings(ex.scripts),
            [
                "https://cdn.ldjson-example.net/brand/logo.png",
                "https://templates.example.org/card.png",
                "https://lh3.googleusercontent.com/sitesv/CCC333=w1280",
                "https://ads.example.com/pixel.gif",
                "https://lh3.googleusercontent.com/z.png",
            ],
        )

    def test_script_types(self):
        for value in (None, "", "module", "text/javascript", " TEXT/JAVASCRIPT ",
                      "text/javascript; charset=utf-8", "application/javascript", "text/ecmascript"):
            with self.subTest(value=value):
                self.assertTrue(ws.is_js_script_type(value))
        for value in ("application/ld+json", "application/json", "text/template", "text/x-template",
                      "importmap", "speculationrules", "text/html", "text/plain"):
            with self.subTest(value=value):
                self.assertFalse(ws.is_js_script_type(value))

    def test_page_command_reports_js_hosts_and_scans_data_blocks(self):
        t = FakeTransport().add(wb(TS, PAGE), resp(200, {"Content-Type": "text/html"}, self.HTML.encode()))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertIn("script_host.www.gstatic.com=1", stdout)
        for host in ("schema.org", "www.linkedin.com", "cdn.ldjson-example.net", "data.example.org"):
            self.assertNotIn("script_host." + host, stdout)
        self.assertIn("candidate=js-string https://cdn.ldjson-example.net/brand/logo.png", stdout)


class TestListingWindow(unittest.TestCase):
    """The fallback's domain listing is bounded to the page capture +/- 365
    days unless --from/--to say otherwise. Without a window, a 2021 page
    pulled 36 images from the 2026 site in the live run."""

    def _images(self, ts, *extra):
        t = gsites_transport(ts)
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["images", PAGE, "-t", ts, "-o", out, "--quiet"] + list(extra), t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        return t, stdout

    def test_default_window_around_2021_capture(self):
        t, stdout = self._images("20211118160203", "--json")
        [params] = listing_params(t)
        self.assertEqual(params["from"], ["20201118160203"])
        self.assertEqual(params["to"], ["20221118160203"])
        summary = json.loads(stdout)
        self.assertTrue(summary["listing_ran"])
        self.assertEqual(summary["listing_window"], {"from": "20201118160203", "to": "20221118160203"})

    def test_default_window_spans_a_leap_day(self):
        t, _ = self._images(TS, "--json")
        [params] = listing_params(t)
        self.assertEqual((params["from"], params["to"]), (["20230917025321"], ["20250916025321"]))

    def test_flags_override_each_bound(self):
        t, stdout = self._images(TS, "--json", "--from", "2019", "--to", "20230101")
        [params] = listing_params(t)
        self.assertEqual((params["from"], params["to"]), (["2019"], ["20230101"]))
        self.assertEqual(json.loads(stdout)["listing_window"], {"from": "2019", "to": "20230101"})
        # One flag given: the other bound keeps its default.
        t, stdout = self._images(TS, "--json", "--from", "2010")
        [params] = listing_params(t)
        self.assertEqual((params["from"], params["to"]), (["2010"], ["20250916025321"]))

    def test_text_summary_prints_window(self):
        _, stdout = self._images(TS)
        self.assertIn("listing_window.from=20230917025321", stdout)
        self.assertIn("listing_window.to=20250916025321", stdout)

    def test_no_window_when_listing_did_not_run(self):
        _, stdout = self._images(TS, "--json", "--fallback", "never")
        summary = json.loads(stdout)
        self.assertFalse(summary["listing_ran"])
        self.assertIsNone(summary["listing_window"])

    def test_listing_window_unit(self):
        self.assertEqual(ws.listing_window("20240229120000"), ("20230301120000", "20250228120000"))
        self.assertEqual(ws.listing_window("2020"), ("20190101000000", "20201231000000"))
        self.assertEqual(ws.listing_window("", "2019", None), ("2019", None))
        self.assertEqual(ws.listing_window("notdigits"), (None, None))
        self.assertEqual(ws.listing_window("20201301000000"), (None, None))  # month 13


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
