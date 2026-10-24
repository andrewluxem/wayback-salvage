"""Spec 3.5 and 3.8: HTML extraction, URL resolution, file naming
(cases 6 and 14)."""

from __future__ import annotations

import os
import tempfile
import unittest

from support import FakeTransport, fixture, fixture_body, read_manifest, run_cli, wb, ws

CLASSIC_URL = "https://example.com/blog/2020/post.html"
A = "https://example.com/assets/"

CLASSIC_EXPECTED = {
    ("https://cdn.example.com/og.jpg", "meta:og:image"),
    ("https://example.com/social/tw.png", "meta:twitter:image"),
    (A + "schema.png", "meta:itemprop"),
    ("https://example.com/favicon.ico", "link:icon"),
    ("https://example.com/favicon2.ico", "link:icon"),
    ("https://example.com/apple.png", "link:icon"),
    ("https://example.com/apple-pre.png", "link:icon"),
    ("https://example.com/mask.svg", "link:icon"),
    ("https://example.com/imgsrc.jpg", "link:image_src"),
    ("https://example.com/hero.webp", "link:preload"),
    ("https://example.com/hero-1x.webp", "link:preload"),
    ("https://example.com/hero-2x.webp", "link:preload"),
    ("https://example.com/bg/double.jpg", "style-block"),
    ("https://example.com/bg/single.png", "style-block"),
    ("https://example.com/bg/none.gif", "style-block"),
    (A + "img/relative.png", "img:src"),
    ("https://example.com/parent.png", "img:src"),
    ("https://static.example.net/proto.png", "img:src"),
    (A + "single.jpg", "img:src"),
    (A + "unquoted.gif", "img:src"),
    ("https://example.com/q.png?a=1&b=2", "img:src"),
    ("data:image/png;base64,iVBORw0KGgo=", "img:src"),
    ("https://example.com/rewritten.png", "img:src"),
    ("http://old.example.com/rel-rewritten.png", "img:src"),
    ("https://res.cloudinary.com/demo/image/upload/w_300,h_200,c_fill/sample.jpg", "img:srcset"),
    ("https://res.cloudinary.com/demo/image/upload/w_600,h_400,c_fill/sample.jpg", "img:srcset"),
    (A + "fallback.jpg", "img:src"),
    (A + "lazy.jpg", "img:data-src"),
    (A + "lazy-1x.jpg", "img:data-srcset"),
    (A + "lazy-2x.jpg", "img:data-srcset"),
    (A + "orig.jpg", "img:data-original"),
    (A + "lazysrc.jpg", "img:data-lazy-src"),
    (A + "lazy2.jpg", "img:data-lazy"),
    (A + "pic.avif", "picture-source:srcset"),
    (A + "pic-lazy.webp", "picture-source:data-srcset"),
    (A + "pic.jpg", "img:src"),
    ("https://example.com/poster.jpg", "video:poster"),
    ("https://example.com/standalone-img.png", "source:srcset"),
    ("https://example.com/button.png", "input-image:src"),
    ("https://example.com/svg-href.png", "svg-image:href"),
    ("https://example.com/svg-xlink.png", "svg-image:href"),
    ("https://example.com/inline-style.jpg", "style-attr"),
}


class TestClassicExtraction(unittest.TestCase):
    """Case 6: every 3.5 source type, exact expected candidate set."""

    @classmethod
    def setUpClass(cls):
        html = fixture_body("page_classic").decode("utf-8")
        cls.extraction = ws.extract(html, CLASSIC_URL)

    def test_exact_candidate_set(self):
        got = {(c.url, c.found_via) for c in self.extraction.candidates}
        self.assertEqual(got - CLASSIC_EXPECTED, set(), "unexpected candidates")
        self.assertEqual(CLASSIC_EXPECTED - got, set(), "missing candidates")
        self.assertEqual(len(self.extraction.candidates), len(CLASSIC_EXPECTED))

    def test_base_href(self):
        self.assertEqual(self.extraction.base_url, A)

    def test_inline_data_flagged(self):
        inline = [c for c in self.extraction.candidates if c.inline]
        self.assertEqual([c.url for c in inline], ["data:image/png;base64,iVBORw0KGgo="])

    def test_skipped_schemes_absent(self):
        urls = " ".join(c.url for c in self.extraction.candidates)
        for bad in ("javascript:", "about:", "blob:", "movie.mp4", "a.woff2", "#svgfilter", "style.css"):
            self.assertNotIn(bad, urls)

    def test_not_js_rendered(self):
        verdict, _ = ws.js_verdict(self.extraction, fixture_body("page_classic").decode("utf-8"))
        self.assertFalse(verdict)


class TestSrcset(unittest.TestCase):
    def test_cloudinary_commas_survive(self):
        value = (
            "https://res.cloudinary.com/x/image/upload/w_300,h_200/a.jpg 300w,"
            "https://res.cloudinary.com/x/image/upload/w_600,h_400/a.jpg 600w"
        )
        self.assertEqual(
            ws.parse_srcset(value),
            [
                "https://res.cloudinary.com/x/image/upload/w_300,h_200/a.jpg",
                "https://res.cloudinary.com/x/image/upload/w_600,h_400/a.jpg",
            ],
        )

    def test_descriptors_and_trailing_commas(self):
        self.assertEqual(ws.parse_srcset("a.png 1x, b.png 2x"), ["a.png", "b.png"])
        self.assertEqual(ws.parse_srcset("a.png,b.png 2x"), ["a.png", "b.png"])
        self.assertEqual(ws.parse_srcset("  a.png  "), ["a.png"])
        self.assertEqual(ws.parse_srcset(""), [])
        self.assertEqual(ws.parse_srcset("a.png (max-width: 1px, x) 1x, b.png"), ["a.png", "b.png"])


class TestUrlHandling(unittest.TestCase):
    def test_unwrap_wayback(self):
        cases = {
            "https://web.archive.org/web/20200101000000im_/https://e.com/a.png": "https://e.com/a.png",
            "http://web.archive.org/web/2020id_/http://e.com/a.png?x=1": "http://e.com/a.png?x=1",
            "/web/20200101000000cs_/https://e.com/s.css": "https://e.com/s.css",
            "//web.archive.org/web/20200101000000/https://e.com/": "https://e.com/",
            "/web/20200101000000js_/http:/e.com/a.js": "http://e.com/a.js",
            "/web/20200101000000if_/e.com/frame": "http://e.com/frame",
            "https://e.com/web/page": "https://e.com/web/page",
            "img/a.png": "img/a.png",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ws.unwrap_wayback(raw), want)

    def test_normalize(self):
        self.assertEqual(
            ws.normalize_url("HTTPS://Example.COM:443/a/./b/../c.png?q=1#frag"),
            "https://example.com/a/c.png?q=1",
        )
        self.assertEqual(ws.normalize_url("http://e.com:80/x"), "http://e.com/x")
        self.assertEqual(ws.normalize_url("http://e.com:8080/x"), "http://e.com:8080/x")
        self.assertEqual(ws.normalize_url("http://e.com"), "http://e.com/")

    def test_protocol_relative_takes_page_scheme(self):
        ex = ws.extract('<img src="//cdn.e.com/a.png"><p>x</p>', "http://e.com/")
        self.assertEqual([c.url for c in ex.candidates], ["http://cdn.e.com/a.png"])

    def test_relative_against_page_without_base(self):
        ex = ws.extract('<img src="../x/a.png">', "https://e.com/one/two/page.html")
        self.assertEqual([c.url for c in ex.candidates], ["https://e.com/one/x/a.png"])

    def test_source_outside_picture_needs_image_type(self):
        html = '<audio><source src="/a.mp3" type="audio/mpeg"></audio><source srcset="/n.png">'
        self.assertEqual(ws.extract(html, "https://e.com/").candidates, [])


class TestFileNaming(unittest.TestCase):
    """Case 14 (unit level) plus the naming rules in 3.8."""

    def test_traversal_neutralised(self):
        with tempfile.TemporaryDirectory() as out:
            for url in (
                "https://example.com/../../etc/passwd.png",
                "https://example.com/%2E%2E/%2E%2E/etc/passwd.png",
                "https://example.com/a/%2e%2e%2f%2e%2e%2fescape.png",
                "https://example.com//etc/passwd.png",
            ):
                with self.subTest(url=url):
                    rel = ws.image_relpath(url, "png")
                    full = ws.resolve_inside(out, rel)
                    real_out = os.path.realpath(out)
                    self.assertTrue(os.path.realpath(full).startswith(real_out + os.sep))
                    self.assertNotIn("..", rel.split("/"))

    def test_resolve_inside_rejects_escape(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(ValueError):
                ws.resolve_inside(out, "../outside.png")
            with self.assertRaises(ValueError):
                ws.resolve_inside(out, "/etc/passwd")

    def test_overlong_segments_capped(self):
        rel = ws.image_relpath("https://example.com/" + "b" * 300 + "/" + "a" * 300 + ".png", "png")
        for seg in rel.split("/"):
            self.assertLessEqual(len(seg), 120)
        self.assertTrue(rel.endswith(".png"))

    def test_query_becomes_hash_suffix(self):
        a = ws.image_relpath("https://cdn.e.com/img/a.jpg?w=300", "jpg")
        b = ws.image_relpath("https://cdn.e.com/img/a.jpg?w=600", "jpg")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("images/cdn.e.com/img/a__"))
        self.assertTrue(a.endswith(".jpg"))
        self.assertEqual(ws.image_relpath("https://cdn.e.com/img/a.jpg", "jpg"), "images/cdn.e.com/img/a.jpg")

    def test_extension_from_sniff_when_url_has_none(self):
        rel = ws.image_relpath("https://lh3.googleusercontent.com/sitesv/AAA111=w1280", "png")
        self.assertTrue(rel.endswith(".png"))
        self.assertTrue(rel.startswith("images/lh3.googleusercontent.com/sitesv/"))

    def test_page_relpath(self):
        self.assertEqual(
            ws.page_relpath("20240916025321", "https://andrewluxem.com/"),
            "pages/20240916025321/andrewluxem.com/index.html",
        )
        self.assertEqual(
            ws.page_relpath("2024", "https://e.com/blog/"),
            "pages/2024/e.com/blog/index.html",
        )
        self.assertEqual(ws.page_relpath("2024", "https://e.com/about"), "pages/2024/e.com/about.html")
        self.assertEqual(ws.page_relpath("2024", "https://e.com/x.html"), "pages/2024/e.com/x.html")


class TestTraversalEndToEnd(unittest.TestCase):
    """Case 14 through the images workflow."""

    def test_saved_files_stay_inside_out(self):
        ts, page = "20200101000000", "https://example.com/"
        evil = "https://example.com/%2E%2E/%2E%2E/etc/passwd.png"
        long_url = "https://example.com/" + "a" * 300 + ".png"
        t = FakeTransport()
        t.add(wb(ts, page), fixture("page_traversal"))
        t.add(wb(ts, evil), fixture("png_red"))
        t.add(wb(ts, long_url), fixture("png_green"))
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["images", page, "-t", ts, "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = [r for r in read_manifest(out) if r["kind"] == "image"]
            self.assertEqual(len(rows), 2)
            real_out = os.path.realpath(out)
            for r in rows:
                self.assertEqual(r["status"], "ok")
                full = os.path.realpath(os.path.join(out, r["saved_path"]))
                self.assertTrue(full.startswith(real_out + os.sep), full)
                self.assertTrue(os.path.isfile(full))
                for seg in r["saved_path"].split("/"):
                    self.assertLessEqual(len(seg), 120)
                    self.assertNotEqual(seg, "..")


if __name__ == "__main__":
    unittest.main()
