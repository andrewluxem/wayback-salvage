# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
