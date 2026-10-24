"""Build the synthetic test fixtures in tests/fixtures/.

Each fixture is two files: <name>.meta.json (status and headers) and
<name>.body (raw response bytes, exactly as a server would send them).
Output is deterministic (gzip mtime=0, fixed inputs). The bodies are
committed, so tests never depend on running this script; rerun it only
when changing a fixture:

    python3 tests/make_fixtures.py

Responses captured with `wayback_salvage.py --record DIR` use the same
format and can be dropped in beside these.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

HTML_UTF8 = "text/html; charset=utf-8"
CDX_FIELDS = ["timestamp", "original", "statuscode", "mimetype", "digest", "length"]

# Timestamps observed for andrewluxem.com in the 21 Oct sbx run.
SBX_TIMESTAMPS = [
    "20211118160203",
    "20220220124928",
    "20240420013920",
    "20240807154344",
    "20240916025321",
]

FILLER = (
    "<p>This archived page has enough ordinary paragraph text to read as a "
    "normal static document. It talks about marketing systems, lifecycle "
    "programs and a few side projects, and it is long enough that the "
    "visible-text heuristic does not fire on it.</p>"
)


def png(width: int, height: int, rgb: tuple) -> bytes:
    """A valid truecolour PNG filled with one colour."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def raw_deflate(data: bytes) -> bytes:
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    return comp.compress(data) + comp.flush()


def cdx_json(header: list, rows: list, resume_key: str = None) -> bytes:
    """CDX output=json shape: one array per line, header first."""
    lines = [json.dumps(header)] + [json.dumps(r) for r in rows]
    if resume_key is not None:
        lines += ["[]", json.dumps([resume_key])]
    return ("[" + ",\n".join(lines) + "]\n").encode("utf-8")


SIMPLE_HTML = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Simple</title>"
    "</head><body><img src=\"/img/simple.png\" alt=\"\">" + FILLER + "</body></html>\n"
).encode("utf-8")

# Google-Sites-like page: DOCS_timing and atari markers, no <img>, two
# lh3.googleusercontent.com image URLs that exist only inside script
# strings. The first uses JSON-escaped slashes and a literal "=w1280"; the
# second uses / slashes, = and a & in its query.
GSITES_HTML = (
    r"""<!DOCTYPE html><html lang="en-US" itemscope itemtype="http://schema.org/WebPage"><head><meta charset="utf-8">"""
    r"""<script nonce="n1">var DOCS_timing={}; DOCS_timing['sl']=new Date().getTime();</script>"""
    r"""<script nonce="n1">_docs_flag_initialData={"docs-ails":"docs_cold","atari-ebids":false};</script>"""
    r"""<title>Andrew Luxem</title>"""
    r"""<meta property="og:image" content="https://lh3.googleusercontent.com/og/OG000=w16383">"""
    r"""<link rel="icon" href="https://ssl.gstatic.com/atari/images/public/favicon.ico">"""
    r"""<style nonce="n1">@font-face{font-family:'Lato';src:url(https://fonts.gstatic.com/s/lato/v24/S6uyw4BMUTPHjx4wXg.woff2)format('woff2');}.tyJCtd{background-color:#fff}</style>"""
    r"""<script src="https://www.gstatic.com/_/atari/_/js/k=atari.vw.en_US.Xw/am=MBiA/d=1/rs=AGEqA5n/m=view" nonce="n1"></script>"""
    r"""</head><body><div jscontroller="pc62j" jsaction="rcuQ6b:WYd;" class="Xpil1b"><div id="atari-page" class="UtePc">Andrew Luxem</div></div>"""
    r"""<script nonce="n1">AF_initDataCallback({key: 'ds:0', hash: '1', data:[[["https:\/\/lh3.googleusercontent.com\/sitesv\/AAA111=w1280",1280,720]],"""
    r"""[{"src":"https://lh3.googleusercontent.com/sitesv/BBB222?authuser=0&fife=s16383"}],"""
    r""""https:\/\/www.gstatic.com\/atari\/embeds\/913b.js"], sideChannel: {}});</script>"""
    r"""</body></html>"""
    "\n"
).encode("utf-8")

# Classic static page covering every source type in spec 3.5. The page URL
# used by the tests is https://example.com/blog/2020/post.html and the
# <base href> moves relative resolution to https://example.com/assets/.
LONG_TEXT = " ".join(["Plain archived prose for the visible text count."] * 8)
CLASSIC_HTML = (
    """<!DOCTYPE html>
<HTML>
<HEAD>
<BASE HREF="/assets/">
<META property="og:image" content="https://cdn.example.com/og.jpg">
<meta name="twitter:image" content="/social/tw.png">
<meta itemprop="image" content="schema.png">
<link rel="icon" href="/favicon.ico">
<link rel="shortcut icon" href="/favicon2.ico">
<link rel="apple-touch-icon" href="/apple.png">
<link rel="apple-touch-icon-precomposed" href="/apple-pre.png">
<link rel="mask-icon" href="/mask.svg" color="#000000">
<link rel="image_src" href="/imgsrc.jpg">
<link rel="preload" as="image" href="/hero.webp" imagesrcset="/hero-1x.webp 1x, /hero-2x.webp 2x">
<link rel="stylesheet" href="/style.css">
<STYLE>
.hero { background-image: url("/bg/double.jpg"); }
.x { background: url('/bg/single.png') no-repeat; }
.y { background: URL(/bg/none.gif); }
@font-face { font-family: X; src: url(/fonts/a.woff2) format("woff2"); }
.z { filter: url(#svgfilter); }
</STYLE>
</HEAD>
<BODY>
<IMG SRC="img/relative.png" ALT="relative">
<img src="../parent.png">
<img src="//static.example.net/proto.png">
<img src='single.jpg'>
<img src=unquoted.gif>
<img src="/q.png?a=1&amp;b=2">
<img src="data:image/png;base64,iVBORw0KGgo=">
<img src="https://web.archive.org/web/20200101000000im_/https://example.com/rewritten.png">
<img src="/web/20190101000000im_/http://old.example.com/rel-rewritten.png">
<img srcset="https://res.cloudinary.com/demo/image/upload/w_300,h_200,c_fill/sample.jpg 300w, https://res.cloudinary.com/demo/image/upload/w_600,h_400,c_fill/sample.jpg 600w" src="fallback.jpg">
<img data-src="lazy.jpg" data-srcset="lazy-1x.jpg 1x, lazy-2x.jpg 2x" data-original="orig.jpg" data-lazy-src="lazysrc.jpg" data-lazy="lazy2.jpg">
<picture><source srcset="pic.avif" type="image/avif"><source data-srcset="pic-lazy.webp"><img src="pic.jpg"></picture>
<video poster="/poster.jpg"><source src="/movie.mp4" type="video/mp4"></video>
<source srcset="/standalone-img.png" type="image/png">
<input type="image" src="/button.png">
<svg width="10" height="10"><image href="/svg-href.png"/><image xlink:href="/svg-xlink.png"/></svg>
<div style="background-image:url(&quot;/inline-style.jpg&quot;)"></div>
<a href="#top">top</a>
<img src="javascript:void(0)">
<img src="about:blank">
<img src="">
<img src="#">
<img src="blob:https://example.com/0f1e2d">
<P>"""
    + LONG_TEXT
    + """</P>
</BODY>
</HTML>
"""
).encode("utf-8")

TOOLBAR_STATIC_HTML = (
    b"<html><head><script src=\"//web-static.archive.org/_static/js/bundle-playback.js\">"
    b"</script></head><body><p>Archived body.</p></body></html>\n"
)
TOOLBAR_WM_HTML = (
    b"<html><head><script>__wm.init(\"https://web.archive.org/web\");</script>"
    b"<script src=\"/_static/js/wombat.js\"></script></head><body><p>Archived body.</p></body></html>\n"
)
WOMBAT_MENTION_HTML = (
    b"<html><body><p>The common wombat is a short-legged marsupial. "
    b"This page is about wombats and nothing else.</p></body></html>\n"
)

SCHEME_LESS_HTML = (
    "<html><head><title>Home</title></head><body><img src=\"/img/a.png\">"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")

PAGE_FOUR_HTML = (
    "<html><body>"
    "<img src=\"/i1.png\"><img src=\"/i2.png\"><img src=\"/i3.png\"><img src=\"/i4.png\">"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")

PAGE_ONE_IMG_HTML = (
    "<html><body><img src=\"/moved.png\">" + FILLER + "</body></html>\n"
).encode("utf-8")

PAGE_MIXED_HTML = (
    "<html><body><img src=\"/a.png\"><img src=\"/b.png\"><img src=\"/c.png\">"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")

LONG_SEGMENT = "a" * 300
PAGE_TRAVERSAL_HTML = (
    "<html><body>"
    "<img src=\"https://example.com/%2E%2E/%2E%2E/etc/passwd.png\">"
    "<img src=\"/" + LONG_SEGMENT + ".png\">"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")

PAGE_CSS_HTML = (
    "<html><head><link rel=\"stylesheet\" href=\"/css/site.css\"></head><body>"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")
SITE_CSS = b".banner { background: url(../img/banner.png); }\n"

PAGE_INLINE_HTML = (
    "<html><body><img src=\"data:image/png;base64,"
    + base64.b64encode(png(1, 1, (9, 9, 9))).decode("ascii")
    + "\">"
    + FILLER
    + "</body></html>\n"
).encode("utf-8")

SOFT_404_HTML = b"<!DOCTYPE html><html><body><h1>Not Found</h1></body></html>\n"


def fixtures() -> dict:
    snapshot_header = ["original", "timestamp", "mimetype", "statuscode", "digest", "length"]
    snapshot_rows = [
        ["https://andrewluxem.com/", ts, "text/html", "200", "DIGEST%02d" % i, str(15000 + i)]
        for i, ts in enumerate(SBX_TIMESTAMPS)
    ]
    listing_p1 = [
        ["20200101000000", "https://example.com/logo.png", "200", "image/png", "DIGESTLOGO", "100"],
        ["20200101000000", "https://example.com/hero.jpg", "200", "image/jpeg", "DIGESTHERO", "200"],
    ]
    listing_p2 = [
        ["20210101000000", "https://example.com/logo.png", "200", "image/png", "DIGESTLOGO", "100"],
        ["20210101000000", "https://www.example.com/banner.gif", "200", "image/gif", "DIGESTBANNER", "300"],
    ]
    latest_one = [["20240916025321", "https://andrewluxem.com/", "200", "text/html", "DIGESTL", "15003"]]
    latest_multi = [
        [ts, "https://andrewluxem.com/", "200", "text/html", "DIGESTM%d" % i, "15003"]
        for i, ts in enumerate(SBX_TIMESTAMPS[:3])
    ]
    cdx_meta = {"Content-Type": "application/json"}
    png_meta = {"Content-Type": "image/png"}
    html_meta = {"Content-Type": HTML_UTF8}

    return {
        # Case 1: CDX snapshots.
        "cdx_snapshots_five": (200, cdx_meta, cdx_json(snapshot_header, snapshot_rows)),
        "cdx_empty": (200, cdx_meta, b""),
        "cdx_brackets": (200, cdx_meta, b"[]\n"),
        "cdx_header_only": (200, cdx_meta, cdx_json(CDX_FIELDS, [])),
        # Case 12: listing over two pages with a resume key.
        "cdx_listing_page1": (
            200,
            cdx_meta,
            cdx_json(CDX_FIELDS, listing_p1, resume_key="com%2Cexample%29%2Flogo.png+20200101000000"),
        ),
        "cdx_listing_page2": (200, cdx_meta, cdx_json(CDX_FIELDS, listing_p2)),
        # Case 17: --latest.
        "cdx_latest_one": (200, cdx_meta, cdx_json(CDX_FIELDS, latest_one)),
        "cdx_latest_multi": (200, cdx_meta, cdx_json(CDX_FIELDS, latest_multi)),
        # Case 3: gzip.
        "page_gzip": (
            200,
            {"Content-Type": HTML_UTF8, "Content-Encoding": "gzip"},
            gzip.compress(SIMPLE_HTML, mtime=0),
        ),
        "page_gzip_noheader": (200, html_meta, gzip.compress(SIMPLE_HTML, mtime=0)),
        "page_bad_gzip": (200, {"Content-Type": HTML_UTF8, "Content-Encoding": "gzip"}, SIMPLE_HTML),
        "page_plain": (200, html_meta, SIMPLE_HTML),
        # Case 4: deflate and br.
        "body_zlib": (200, {"Content-Type": HTML_UTF8, "Content-Encoding": "deflate"}, zlib.compress(SIMPLE_HTML, 9)),
        "body_raw_deflate": (
            200,
            {"Content-Type": HTML_UTF8, "Content-Encoding": "deflate"},
            raw_deflate(SIMPLE_HTML),
        ),
        "body_br": (
            200,
            {"Content-Type": HTML_UTF8, "Content-Encoding": "br"},
            b"\x1b\x4a\x00\xf8\x0f\x00not-really-brotli",
        ),
        # Case 5: Google-Sites-like page, served gzip.
        "page_gsites": (
            200,
            {"Content-Type": HTML_UTF8, "Content-Encoding": "gzip"},
            gzip.compress(GSITES_HTML, mtime=0),
        ),
        # Case 6: classic page.
        "page_classic": (200, html_meta, CLASSIC_HTML),
        # Case 11: toolbar markers.
        "page_toolbar_static": (200, html_meta, TOOLBAR_STATIC_HTML),
        "page_toolbar_wm": (200, html_meta, TOOLBAR_WM_HTML),
        "page_wombat_mention": (200, html_meta, WOMBAT_MENTION_HTML),
        # Cases 7, 9, 10, 13, 14, 16, 17 and option coverage.
        "page_scheme_less": (200, html_meta, SCHEME_LESS_HTML),
        "page_four": (200, html_meta, PAGE_FOUR_HTML),
        "page_one_img": (200, html_meta, PAGE_ONE_IMG_HTML),
        "page_mixed": (200, html_meta, PAGE_MIXED_HTML),
        "page_traversal": (200, html_meta, PAGE_TRAVERSAL_HTML),
        "page_css": (200, html_meta, PAGE_CSS_HTML),
        "site_css": (200, {"Content-Type": "text/css"}, SITE_CSS),
        "page_inline": (200, html_meta, PAGE_INLINE_HTML),
        "soft_404": (200, html_meta, SOFT_404_HTML),
        "png_red": (200, png_meta, png(2, 2, (255, 0, 0))),
        "png_green": (200, png_meta, png(2, 2, (0, 255, 0))),
        "png_blue": (200, png_meta, png(2, 2, (0, 0, 255))),
        "png_white": (200, png_meta, png(2, 2, (255, 255, 255))),
    }


def main() -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    for name, (status, headers, body) in sorted(fixtures().items()):
        meta = {"status": status, "headers": headers}
        with open(os.path.join(FIXTURES, name + ".meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
            fh.write("\n")
        with open(os.path.join(FIXTURES, name + ".body"), "wb") as fh:
            fh.write(body)
    print("wrote %d fixtures to %s" % (len(fixtures()), FIXTURES))


if __name__ == "__main__":
    main()
