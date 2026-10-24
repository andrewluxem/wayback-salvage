"""Spec 3.3: CDX query builder and parser (cases 1, 2, 12)."""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.parse

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

SBX = ["20211118160203", "20220220124928", "20240420013920", "20240807154344", "20240916025321"]


class TestCdxParse(unittest.TestCase):
    """Case 1: header plus five rows; empty body; []; header-only."""

    def test_five_rows_mapped_by_header_name(self):
        page = ws.parse_cdx(fixture_body("cdx_snapshots_five"))
        self.assertEqual([r["timestamp"] for r in page.rows], SBX)
        # The fixture header is in a different order from fl=; mapping must
        # follow the header, not position.
        first = page.rows[0]
        self.assertEqual(first["original"], "https://andrewluxem.com/")
        self.assertEqual(first["statuscode"], "200")
        self.assertEqual(first["mimetype"], "text/html")
        self.assertEqual(first["digest"], "DIGEST00")
        self.assertIsNone(page.resume_key)

    def test_no_captures_shapes(self):
        for name in ("cdx_empty", "cdx_brackets", "cdx_header_only"):
            with self.subTest(name=name):
                page = ws.parse_cdx(fixture_body(name))
                self.assertEqual(page.rows, [])
                self.assertIsNone(page.resume_key)

    def test_whitespace_body_is_no_captures(self):
        self.assertEqual(ws.parse_cdx(b"  \n").rows, [])

    def test_resume_key_row(self):
        page = ws.parse_cdx(fixture_body("cdx_listing_page1"))
        self.assertEqual(len(page.rows), 2)
        self.assertEqual(page.resume_key, "com%2Cexample%29%2Flogo.png+20200101000000")

    def test_snapshots_command_prints_five_rows(self):
        t = FakeTransport().add(is_cdx, fixture("cdx_snapshots_five"))
        with tempfile.TemporaryDirectory() as out:
            code, stdout, _ = run_cli(["snapshots", "andrewluxem.com", "--limit", "5", "-o", out], t)
        self.assertEqual(code, ws.EXIT_OK)
        for ts in SBX:
            self.assertIn(ts, stdout)
            self.assertIn("https://web.archive.org/web/%s/https://andrewluxem.com/" % ts, stdout)
        params = cdx_params(t.calls[0])
        self.assertEqual(params["url"], ["andrewluxem.com"])  # scheme-less input passes as given
        self.assertEqual(params["limit"], ["5"])

    def test_snapshots_empty_is_ok(self):
        for name in ("cdx_empty", "cdx_brackets", "cdx_header_only"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as out:
                t = FakeTransport().add(is_cdx, fixture(name))
                code, stdout, stderr = run_cli(["snapshots", "andrewluxem.com", "-o", out], t)
                self.assertEqual(code, ws.EXIT_OK)
                self.assertIn("no captures", (stdout + stderr).lower())


class TestCdxQueryBuilder(unittest.TestCase):
    """Case 2: special characters, repeated filter, * wildcard, fragment."""

    def _query(self, url):
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.scheme + "://" + parts.netloc + parts.path, ws.CDX_ENDPOINT)
        return parts.query

    def test_special_characters_are_encoded(self):
        url = ws.build_cdx_query("https://example.com/a b?a=1&b=2#frag")
        query = self._query(url)
        self.assertIn("url=https://example.com/a%20b%3Fa%3D1%26b%3D2", query)
        self.assertNotIn("frag", query)
        self.assertNotIn("#", url)
        params = urllib.parse.parse_qs(query)
        self.assertEqual(params["url"], ["https://example.com/a b?a=1&b=2"])
        self.assertEqual(params["output"], ["json"])
        self.assertEqual(params["fl"], ["timestamp,original,statuscode,mimetype,digest,length"])

    def test_repeated_filter(self):
        url = ws.build_cdx_query(
            "example.com",
            filters=["statuscode:200", "mimetype:image/.*"],
        )
        query = self._query(url)
        self.assertEqual(query.count("filter="), 2)
        self.assertEqual(
            urllib.parse.parse_qs(query)["filter"],
            ["statuscode:200", "mimetype:image/.*"],
        )

    def test_wildcard_preserved(self):
        query = self._query(ws.build_cdx_query("andrewluxem.com/*"))
        self.assertIn("url=andrewluxem.com/*", query)

    def test_options(self):
        url = ws.build_cdx_query(
            "example.com",
            match_type="domain",
            collapse="digest",
            from_ts="2020",
            to_ts="2021",
            limit=-1,
            show_resume_key=True,
            resume_key="com%2Cexample%29%2F+2020",
        )
        params = urllib.parse.parse_qs(self._query(url))
        self.assertEqual(params["matchType"], ["domain"])
        self.assertEqual(params["collapse"], ["digest"])
        self.assertEqual(params["from"], ["2020"])
        self.assertEqual(params["to"], ["2021"])
        self.assertEqual(params["limit"], ["-1"])
        self.assertEqual(params["showResumeKey"], ["true"])
        self.assertEqual(params["resumeKey"], ["com,example)/ 2020"])


class TestCdxListingPagination(unittest.TestCase):
    """Case 12: two pages via resume key, duplicate digests across
    non-adjacent rows, one download per digest."""

    def _transport(self):
        t = FakeTransport()
        t.add(lambda u: is_cdx(u) and "resumeKey" not in cdx_params(u), fixture("cdx_listing_page1"))
        t.add(lambda u: is_cdx(u) and "resumeKey" in cdx_params(u), fixture("cdx_listing_page2"))
        t.add(wb("20200101000000", "https://example.com/logo.png"), fixture("png_red"))
        t.add(wb("20200101000000", "https://example.com/hero.jpg"), fixture("png_green"))
        t.add(wb("20210101000000", "https://www.example.com/banner.gif"), fixture("png_blue"))
        return t

    def test_two_pages_one_download_per_digest(self):
        t = self._transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["site-images", "example.com", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            rows = read_manifest(out)
            ok = [r for r in rows if r["status"] == "ok"]
            self.assertEqual(len(ok), 3)
            self.assertEqual(len({r["cdx_digest"] for r in ok}), 3)
            for r in ok:
                self.assertEqual(r["found_via"], "cdx-listing")
                self.assertTrue(os.path.isfile(os.path.join(out, r["saved_path"])))
        cdx_calls = [u for u in t.calls if is_cdx(u)]
        self.assertEqual(len(cdx_calls), 2)
        first, second = cdx_params(cdx_calls[0]), cdx_params(cdx_calls[1])
        for params in (first, second):
            self.assertEqual(params["url"], ["example.com"])
            self.assertEqual(params["matchType"], ["domain"])
            self.assertEqual(params["collapse"], ["digest"])
            self.assertEqual(params["showResumeKey"], ["true"])
            self.assertIn("statuscode:200", params["filter"])
            self.assertIn("mimetype:image/.*", params["filter"])
        self.assertEqual(second["resumeKey"], ["com,example)/logo.png 20200101000000"])
        # The duplicate-digest logo capture from 2021 is never fetched.
        self.assertEqual(t.count(wb("20210101000000", "https://example.com/logo.png")), 0)
        self.assertEqual(t.unrouted, [])

    def test_dry_run_plans_one_row_per_digest(self):
        # No ok rows exist in a dry run, so this pins the client-side
        # digest dedupe of the listing itself.
        t = self._transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["site-images", "example.com", "--dry-run", "-o", out, "--quiet"], t)
            self.assertEqual(code, ws.EXIT_OK, stderr)
            planned = [r for r in read_manifest(out) if r["status"] == "planned"]
        self.assertEqual(sorted(r["cdx_digest"] for r in planned), ["DIGESTBANNER", "DIGESTHERO", "DIGESTLOGO"])
        self.assertFalse(any(not is_cdx(u) for u in t.calls))

    def test_loose_mime_filter(self):
        t = self._transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(
                ["site-images", "example.com", "--loose-mime", "--dry-run", "-o", out, "--quiet"], t
            )
            self.assertEqual(code, ws.EXIT_OK, stderr)
        params = cdx_params(t.calls[0])
        self.assertNotIn("mimetype:image/.*", params["filter"])
        self.assertIn(r"original:.*\.(png|jpe?g|gif|webp|svg|ico|avif)(\?.*)?$", params["filter"])

    def test_cdx_limit_caps_rows(self):
        t = self._transport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(
                ["site-images", "example.com", "--cdx-limit", "2", "--dry-run", "-o", out, "--quiet"], t
            )
            self.assertEqual(code, ws.EXIT_OK, stderr)
            planned = [r for r in read_manifest(out) if r["status"] == "planned"]
        self.assertEqual(len([u for u in t.calls if is_cdx(u)]), 1)
        self.assertEqual(cdx_params(t.calls[0])["limit"], ["2"])
        self.assertEqual(len(planned), 2)

    def test_shared_cdn_host_listing_refused(self):
        t = FakeTransport()
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["site-images", "lh3.googleusercontent.com", "-o", out], t)
        self.assertEqual(code, ws.EXIT_ERROR)
        self.assertIn("shared", stderr.lower())
        self.assertEqual(t.calls, [])


if __name__ == "__main__":
    unittest.main()
