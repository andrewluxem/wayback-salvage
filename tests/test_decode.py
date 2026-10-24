"""Spec 3.4: decoding by Content-Encoding, sniffing, image validation
(cases 3, 4)."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import zlib
from unittest import mock

from support import FakeTransport, fixture, fixture_body, run_cli, wb, ws

SIMPLE = fixture_body("page_plain")
TS = "20240916025321"
PAGE = "https://andrewluxem.com/"


class TestGzip(unittest.TestCase):
    """Case 3."""

    def test_gzip_header_decodes(self):
        d = ws.decode_body(fixture_body("page_gzip"), "gzip")
        self.assertEqual(d.data, SIMPLE)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.encoding, "gzip")
        self.assertEqual(d.flags, [])

    def test_x_gzip_alias(self):
        d = ws.decode_body(fixture_body("page_gzip"), "x-gzip")
        self.assertEqual(d.data, SIMPLE)

    def test_gzip_without_header_is_sniffed_and_flagged(self):
        d = ws.decode_body(fixture_body("page_gzip_noheader"), None, sniff=True)
        self.assertEqual(d.data, SIMPLE)
        self.assertEqual(d.status, "ok")
        self.assertIn("sniffed-gzip", d.flags)

    def test_no_sniff_when_not_requested(self):
        body = fixture_body("page_gzip_noheader")
        d = ws.decode_body(body, None)
        self.assertEqual(d.data, body)
        self.assertEqual(d.flags, [])

    def test_bad_gzip_keeps_raw_bytes(self):
        d = ws.decode_body(fixture_body("page_bad_gzip"), "gzip")
        self.assertEqual(d.status, "encoding-mismatch")
        self.assertEqual(d.data, SIMPLE)

    def test_page_command_reports_gzip(self):
        t = FakeTransport().add(wb(TS, PAGE), fixture("page_gzip"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertIn("content_encoding=gzip", stdout)
        self.assertIn("decoded_bytes=%d" % len(SIMPLE), stdout)
        self.assertIn("toolbar_injected=false", stdout)

    def test_page_command_reports_sniffed_gzip(self):
        t = FakeTransport().add(wb(TS, PAGE), fixture("page_gzip_noheader"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        info = json.loads(stdout)
        self.assertEqual(info["decoded_bytes"], len(SIMPLE))
        self.assertIn("sniffed-gzip", info["flags"])


class TestDeflateBrZstd(unittest.TestCase):
    """Case 4."""

    def test_zlib_wrapped_deflate(self):
        d = ws.decode_body(fixture_body("body_zlib"), "deflate")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))

    def test_raw_deflate(self):
        d = ws.decode_body(fixture_body("body_raw_deflate"), "deflate")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))

    def test_bad_deflate_is_mismatch(self):
        d = ws.decode_body(b"plain text, not deflate", "deflate")
        self.assertEqual(d.status, "encoding-mismatch")
        self.assertEqual(d.data, b"plain text, not deflate")

    def test_stacked_encodings_decode_in_reverse_order(self):
        # "deflate, gzip": deflate was applied first, so gzip is undone first.
        body = gzip.compress(zlib.compress(SIMPLE), mtime=0)
        d = ws.decode_body(body, "deflate, gzip")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))

    def test_identity(self):
        d = ws.decode_body(SIMPLE, "identity")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))

    def test_unknown_encoding_unsupported(self):
        d = ws.decode_body(SIMPLE, "compress")
        self.assertEqual(d.status, "encoding-unsupported")

    def test_br_unsupported_without_module(self):
        with mock.patch.object(ws, "_brotli", None):
            d = ws.decode_body(fixture_body("body_br"), "br")
        self.assertEqual(d.status, "encoding-unsupported")
        self.assertEqual(d.data, fixture_body("body_br"))

    def test_br_unsupported_with_real_import_state(self):
        if ws._brotli is not None:
            self.skipTest("a brotli module is importable, so br decodes here")
        d = ws.decode_body(fixture_body("body_br"), "br")
        self.assertEqual(d.status, "encoding-unsupported")

    def test_br_decodes_when_module_present(self):
        if ws._brotli is None:
            self.skipTest("no brotli or brotlicffi installed")
        d = ws.decode_body(ws._brotli.compress(SIMPLE), "br")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))

    def test_zstd(self):
        if ws._zstd is None:
            self.skipTest("compression.zstd needs Python 3.14+")
        d = ws.decode_body(ws._zstd.compress(SIMPLE), "zstd")
        self.assertEqual((d.status, d.data), ("ok", SIMPLE))
        with mock.patch.object(ws, "_zstd", None):
            self.assertEqual(ws.decode_body(b"x", "zstd").status, "encoding-unsupported")

    def test_br_html_page_triggers_one_rewrite_mode_retry(self):
        rewrite = wb(TS, PAGE, mod="")
        t = FakeTransport()
        t.add(wb(TS, PAGE), fixture("body_br"))
        t.add(rewrite, fixture("page_plain"))
        with mock.patch.object(ws, "_brotli", None), tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertEqual(t.count(rewrite), 1)
        self.assertEqual(t.count(wb(TS, PAGE)), 1)
        info = json.loads(stdout)
        self.assertIn("rewrite-mode", info["flags"])
        self.assertEqual(info["status"], "ok")
        self.assertIn("https://andrewluxem.com/img/simple.png", [c["url"] for c in info["candidates"]])

    def test_br_retry_also_br_stays_unsupported(self):
        rewrite = wb(TS, PAGE, mod="")
        t = FakeTransport()
        t.add(wb(TS, PAGE), fixture("body_br"))
        t.add(rewrite, fixture("body_br"))
        with mock.patch.object(ws, "_brotli", None), tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(["page", PAGE, "-t", TS, "-o", out, "--json"], t)
        self.assertEqual(code, ws.EXIT_OK, stderr)
        self.assertEqual(t.count(rewrite), 1)
        self.assertEqual(json.loads(stdout)["status"], "encoding-unsupported")


class TestImageSniff(unittest.TestCase):
    def test_magic_bytes(self):
        cases = {
            "png": fixture_body("png_red"),
            "jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01",
            "gif": b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
            "webp": b"RIFF\x24\x00\x00\x00WEBPVP8 ",
            "ico": b"\x00\x00\x01\x00\x01\x00\x10\x10",
            "bmp": b"BM\x3a\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00",
            "avif": b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00",
            "heic": b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00",
            "svg": b"<?xml version=\"1.0\"?>\n<!-- c -->\n<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
        }
        for ext, data in cases.items():
            with self.subTest(ext=ext):
                self.assertEqual(ws.sniff_image(data), ext)

    def test_bare_svg_and_doctype(self):
        self.assertEqual(ws.sniff_image(b"  <svg width='1'></svg>"), "svg")
        self.assertEqual(
            ws.sniff_image(b"<!DOCTYPE svg PUBLIC \"-//W3C//DTD SVG 1.1//EN\" \"x\">\n<svg/>"), "svg"
        )

    def test_html_and_unknown_are_not_images(self):
        self.assertIsNone(ws.sniff_image(fixture_body("soft_404")))
        self.assertIsNone(ws.sniff_image(b"<!DOCTYPE html><html><body><svg></svg></body></html>"))
        self.assertIsNone(ws.sniff_image(b"BMW is a car maker, not a bitmap"))
        self.assertIsNone(ws.sniff_image(b""))


if __name__ == "__main__":
    unittest.main()
