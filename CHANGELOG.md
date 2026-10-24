# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-10-24

Fixes from the live recheck of 0.2.0.

### Added

- `page` and the manifest record `memento_datetime`, the `Memento-Datetime` header of the final response (blank when absent). The manifest column comes last; a manifest from 0.2.0 or earlier is rewritten with the new header before the first row is appended to it.
- `page` prints `js_scan_ran` in text and JSON output: true when `images --fallback auto` would scan the scripts (any `js_reasons`).
- Tests: the `page_script_hosts` fixture, with hosts that are loaded, mentioned, or both, and a Next.js flight-data copy of its ld+json block.

### Changed

- The host report is split in two. `script_src_host.*` lists hosts of the `<script src>` JavaScript the page loads; `script_text_host.*` lists hosts only mentioned inside inline JavaScript, leaving out any host that is also loaded. Loaded hosts come first in the `images` progress line and in the `page` and `images` output. The JSON keys `script_src_host` and `script_text_host` replace `host_report`, and the `script_host.*` lines are gone. ld+json and other data blocks stay out of both.

### Fixed

- After the ld+json exclusion, the live andrewluxem.com report still counted schema.org 5 times and each social host twice. The second copy is the Next.js RSC flight data (`self.__next_f.push`, untyped inline scripts that repeat each ld+json block as a string). Those hosts now appear only under `script_text_host`, apart from the hosts the page actually loads.
- An archived page whose own `<title>` says "Temporarily Offline" was retried as an outage on every run, until exit 3. On replay (`/web/`) responses the title now means an outage only when `Memento-Datetime` is absent, since a real replay always sends it. CDX responses keep the title-only rule.

## [0.2.0] - 2026-10-24

Fixes from the first live acceptance run.

### Added

- `images --from YYYYMMDD --to YYYYMMDD` bound the fallback's CDX listing of the domain. Each unset bound defaults to the page's actual capture timestamp minus or plus 365 days.
- The `images` summary and `--json` output report `js_scan_ran`, `listing_ran` and `listing_window`.
- Outage handling: from archive.org, a response whose `<title>` contains "Temporarily Offline" is retried as a 503 whatever its status code, and a 200 CDX body that is not valid JSON is retried with the same backoff. When retries run out the run exits 3, resumable.
- Tests: `tests/test_outage.py` with the synthetic `archive_offline` fixture, and the `page_gsites_img` fixture.

### Changed

- `--fallback auto` runs the JS-string scan whenever `js_reasons` is non-empty, even when the page has content images. The domain CDX listing still needs a `js_rendered` verdict. `page` lists `js-string` candidates under the same rule, and `fallback_ran` now means either step ran.
- The host report counts only JavaScript: `<script>` with no type, `module` or a JavaScript MIME type. ld+json, templates, importmap and other data blocks are left out of it; the JS-string scan still reads them.
- Exit 3 messages and help name archive.org outages as well as throttling.

### Fixed

- The fallback's domain listing had no date window, so a 2021 capture pulled 36 images from the 2026 site.
- The JS-string scan took non-image URLs on known image hosts as images, such as `https://<n>-atari-embeds.googleusercontent.com/embeds/<id>/inner-frame-minified.html`. On those hosts a path with an extension from `NON_IMAGE_EXTS` (`.html`, `.js`, `.css`, `.json`, fonts) is now rejected.
- An outage page at an image URL was recorded as `type-mismatch`, and a failed nearest-capture CDX lookup as `not-archived`. Resume skips both, so the rows were never retried. A failed lookup is now `error`, which resume retries.

## [0.1.0] - 2026-10-23

### Added

- CLI `scripts/wayback_salvage.py` with six subcommands: `snapshots`, `fetch`, `page`, `images`, `site-images` and `verify`.
- Decoding of `gzip`, `x-gzip`, zlib-wrapped and raw `deflate` bodies, gzip sniffing for HTML sent without a header, `br` through an already-installed brotli module, and `zstd` on Python 3.14+.
- HTML extractor for `img` and lazy-load attributes, `picture` and `source`, `srcset`, CSS `url()`, `og:image`, `twitter:image`, icons and image preloads, with Wayback URL unwrapping and relative URL resolution.
- JS-rendered page detection with a fallback: inline script string scan, CDX listing of the domain's images, and a third-party host report.
- Rate limiting: one limiter for archive.org hosts (6 s plus jitter), `Retry-After`-aware exponential backoff, a doubled delay after any 429, and a per-run request cap.
- Append-only CSV manifest with resume, digest and sha256 dedupe, contained file paths, and `verify`.
- Offline unittest suite with a socket guard, a fake transport and committed fixtures.
- GitHub Actions CI on Python 3.9 and 3.13 (Ubuntu) and 3.13 (macOS).
