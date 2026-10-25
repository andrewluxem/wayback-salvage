# wayback-salvage

An agent skill and a stdlib-only Python CLI that recovers archived pages and images from the Internet Archive's Wayback Machine. It lists captures through the CDX API, fetches raw `id_` bodies and decodes them, extracts image URLs from HTML, CSS, meta tags and inline script strings, and downloads them at a pace archive.org tolerates. Every result goes into a resumable CSV manifest with a sha256 per saved file. Plain curl-and-grep recipes find nothing when Wayback replays a gzip body or the page loads its images from JavaScript; this tool handles both cases.

## Install

As a Claude Code skill:

```
mkdir -p ~/.claude/skills/wayback-salvage
cp -R SKILL.md scripts ~/.claude/skills/wayback-salvage/
```

The CLI also runs standalone from a checkout: `python3 scripts/wayback_salvage.py --help`.

Requirements: Python 3.9 or later, standard library only. If `brotli` or `brotlicffi` is already installed it is used for `br` bodies; it is never declared or installed. `zstd` decoding needs Python 3.14 or later (`compression.zstd`).

## Quick start

```
python3 scripts/wayback_salvage.py snapshots andrewluxem.com --limit 5
python3 scripts/wayback_salvage.py page andrewluxem.com -t 20240916025321 -o out
python3 scripts/wayback_salvage.py images https://andrewluxem.com/ --latest --dry-run -o out
python3 scripts/wayback_salvage.py images https://andrewluxem.com/ --latest -o out
python3 scripts/wayback_salvage.py site-images andrewluxem.com --dry-run -o out
python3 scripts/wayback_salvage.py verify -o out
```

In order: list captures, analyse one capture (encoding, JS-rendered verdict, image candidates), plan the image download, run it, plan a domain-wide image listing, re-hash everything saved.

## Subcommands

| Subcommand | Does |
|---|---|
| `snapshots URL [--from YYYYMMDD] [--to YYYYMMDD] [--limit N] [--match exact\|prefix\|host\|domain] [--status 200] [--collapse digest]` | CDX query. Prints timestamp, original, status, mimetype, digest, length and Wayback URL per capture. |
| `fetch URL -t TS [--save FILE]` | One `id_` fetch, decoded. Prints final timestamp, content type, encoding, bytes and sha256. |
| `page URL (-t TS \| --latest)` | Fetches and analyses one archived page: decoded size, encoding, toolbar-injection flag, JS-rendered verdict with reasons, image candidates by source type. Downloads no images. |
| `images URL (-t TS \| --latest) [--fallback auto\|never\|always] [--from YYYYMMDD] [--to YYYYMMDD] [--prefer-live] [--follow-css] [--save-inline]` | Main workflow: fetch the page, extract and resolve image URLs, download each via `id_` at the page timestamp, run fallbacks, write the manifest. `--from`/`--to` bound the fallback's domain listing. |
| `site-images DOMAIN [--from YYYYMMDD] [--to YYYYMMDD] [--loose-mime] [--cdx-limit N]` | CDX listing of the domain's archived images (including `www.` and subdomains), deduped by digest, then downloaded. `--cdx-limit` defaults to 2000 rows. |
| `verify` | Re-hashes every saved file in `--out` against the manifest, prints counts by `status` and `found_via`, exits 5 on a modified or missing file. |

`-t` is short for `--timestamp`. URLs without a scheme are accepted. `page` and `images` take exactly one of `--timestamp` or `--latest`. `--latest` asks CDX for the newest 200 `text/html` capture of the exact URL; if that returns nothing or more than one row, it requests `/web/<now>id_/<url>` and follows the redirect to the newest capture. The output says which path it used.

The JS fallback has two steps. A page shows signs of script building (`js_reasons`) when it has a known marker (`DOCS_timing`, `atari`, an empty `root` or `app` div, `ng-version`), inline script over half the document, or under 200 characters of visible text. It is judged JS-rendered when it shows a sign and has no content images (icons, `meta` images and CSS backgrounds do not count). `page` prints both. With `--fallback auto` (the default), any sign runs the script scan, even beside content images: inline scripts, ld+json and other data blocks included, are searched for absolute image URLs, JSON- and JS-escaped forms decoded. On known image hosts a path ending in `.html`, `.js`, `.css`, `.json` or a font extension is not taken as an image. Only a JS-rendered verdict also runs the CDX listing of the domain's own images, bounded to captures within 365 days either side of the page's capture; `--from` and `--to` replace either bound (`--from 1996` reaches back to the start). `always` runs both steps, `never` neither. The `images` summary reports `js_scan_ran`, `listing_ran` and `listing_window`; `page` prints `js_scan_ran` as well, true when `images --fallback auto` would scan. `page` and `images` also print a third-party host report in two parts, most frequent first: `script_src_host.*` for hosts of the `<script src>` JavaScript the page loads, and `script_text_host.*` for hosts only mentioned inside inline JavaScript (a loaded host is not repeated there). Data blocks such as ld+json are left out of both, but an inline copy of one still counts as mentioned: Next.js pages repeat their ld+json in the RSC flight data (`self.__next_f.push`), so schema.org and social profile hosts show up under `script_text_host`. `--loose-mime` matches images by file extension instead of CDX mimetype, for captures stored as `application/octet-stream`.

Shared options:

| Option | Default | Meaning |
|---|---|---|
| `-o/--out DIR` | `./wayback-out` | Output directory; holds the manifest, so reuse it to resume. |
| `--delay SECONDS` | `6` | Minimum interval between archive.org requests. |
| `-n/--max-requests N` | `300` | Request cap per run, redirect hops and CSS fetches included. |
| `--max-retries N` | `4` | Retries per request on 429, 502, 503, 504, network errors and outage pages. |
| `--user-agent STR` | `wayback-salvage/<version> (+https://github.com/andrewluxem/wayback-salvage)` | User-Agent header. |
| `--cafile PATH` | none | CA bundle to trust instead of the default store. |
| `--dry-run` | off | CDX queries and the page fetch run; image downloads do not; the manifest gets `planned` rows. |
| `--record DIR` | off | Saves every response (status, headers, body) in the test-fixture format. |
| `--quiet` | off | No progress messages on stderr; results still print on stdout. |
| `--json` | off | Print the result as one JSON object (`snapshots`: a list) instead of `key=value` lines. |

## Manifest

`<out>/manifest.csv` is appended to during a run, so a crash loses nothing, and compacted atomically at clean exit. On load the last row per key wins. The key is `(original_url, requested_timestamp)`.

| Column | Meaning |
|---|---|
| `original_url` | Resolved, normalised URL of the page or image (Wayback rewriting removed). |
| `requested_timestamp` | Timestamp asked for: the `-t` value for a page (the capture used, with `--latest`); for page images, the page's actual capture timestamp; for `cdx-listing` rows, the CDX row's timestamp. |
| `timestamp` | Actual capture timestamp after redirects (blank for `source=live`). |
| `wayback_url` | Final Wayback URL fetched, or the planned one for `planned` rows (blank for `source=live`). |
| `status` | Outcome, see below. |
| `sha256` | Hash of the saved bytes. |
| `saved_path` | File path relative to `--out`, with forward slashes; for `duplicate`, the first copy. |
| `kind` | `page` or `image`. |
| `found_via` | Where the URL was found, see below; `page` for page rows. |
| `source` | `wayback`; `live` when `--prefer-live` got a valid image from the live host; `inline` for a `data:` URI. |
| `mimetype` | Media type from the final response's `Content-Type` (the CDX mimetype for rows not fetched). |
| `cdx_digest` | CDX content digest; a digest already `ok` in the manifest is not fetched again. |
| `bytes` | Size of the saved (decoded) body. |
| `content_encoding` | `Content-Encoding` the body arrived with: `identity` when absent, `sniffed-gzip` when an HTML body was gzip without a header. |
| `error` | Detail for failed rows, plus page flags such as `rewrite-mode`. |
| `fetched_at` | When the row was written. |
| `memento_datetime` | `Memento-Datetime` header of the final response, as sent (`Mon, 16 Sep 2024 02:53:21 GMT`); blank when it had none or nothing was fetched. A manifest from 0.2.0 or earlier gains the column the first time a run writes to it. |

Status values:

| Status | Meaning |
|---|---|
| `ok` | Saved, and the magic bytes match an image (PNG, JPEG, GIF, WEBP, ICO, BMP, AVIF/HEIC, SVG) or the page decoded. |
| `duplicate` | Same sha256 as an earlier file, or a CDX digest already saved (not fetched again); `saved_path` points at that file. |
| `not-archived` | No capture exists: Wayback answered 404 and the exact-URL CDX lookup found nothing. A lookup that failed is `error` instead. |
| `http-<code>` | Final response had that HTTP status. |
| `type-mismatch` | A 200 whose body is HTML or an unknown type (usually a soft 404). Not counted as recovered. archive.org's own outage page never lands here; it is retried as a 503. |
| `encoding-unsupported` | `br` or `zstd` body with no decoder available. |
| `encoding-mismatch` | Header claims an encoding the body does not decode with; raw bytes kept. |
| `toolbar-injected` | An `id_` HTML response contains Wayback toolbar markup, so the `id_` was lost (usually through a redirect). |
| `inline-data` | A `data:` URI on the page; recorded, not saved unless `--save-inline`. |
| `rate-limited` | Retries ran out on 429, 502, 503, 504, network errors or archive.org outage responses. |
| `skipped-cap` | Not attempted because `--max-requests` was reached. |
| `planned` | Written by `--dry-run`. |
| `error` | Anything else, such as a failed nearest-capture CDX lookup; see `error`. |

Resume: rerunning the same command with the same `--out` skips rows whose status is `ok`, `duplicate`, `not-archived`, `type-mismatch` or `inline-data` (except `inline-data` under `--save-inline`), and retries every other status, `planned` included. The page itself is fetched again on every run, since its links are needed to know what to resume.

`found_via` values:

| Values | Source |
|---|---|
| `img:src`, `img:srcset`, `img:data-src`, `img:data-srcset`, `img:data-original`, `img:data-lazy-src`, `img:data-lazy` | `<img>` attributes, lazy-load variants included |
| `picture-source:srcset`, `picture-source:data-srcset` | `<source>` inside `<picture>` |
| `source:src`, `source:srcset` | `<source type="image/...">` outside `<picture>` |
| `input-image:src`, `video:poster`, `svg-image:href` | `<input type=image>`, `<video poster>`, SVG `<image href>` or `xlink:href` |
| `style-attr`, `style-block`, `css-link` | CSS `url()` in a `style` attribute, a `<style>` block, or a linked stylesheet (`--follow-css`) |
| `meta:og:image`, `meta:og:image:url`, `meta:og:image:secure_url`, `meta:twitter:image`, `meta:itemprop` | `<meta>` tags |
| `link:icon`, `link:image_src`, `link:preload` | `<link>` icon variants, `rel=image_src`, `rel=preload as=image` |
| `js-string` | Absolute image URL inside an inline script (JS fallback) |
| `cdx-listing` | CDX listing of the domain's images (`site-images` or JS fallback) |

## File layout

```
<out>/
  manifest.csv
  requests.jsonl
  pages/<ts>/<host>/<path>.html
  images/<host>/<path>
```

- Pages are saved decoded. All files are written as bytes.
- A query string becomes a short hash suffix on the file name.
- A different capture at the same path gets `__<ts>` before the extension.
- When the URL has no extension, the extension comes from the magic-byte sniff.
- Path segments are capped at 120 characters, `..` and absolute segments are neutralised, and every final path is checked to be inside `--out`.
- `requests.jsonl` has one JSON object per HTTP request, keys `time`, `method`, `url`, `status`, `bytes`, `wait` (seconds waited before it) and `attempt`. `grep -c '"status": 429' out/requests.jsonl` counts throttles. `status` is the status on the wire, so an outage page served as a 200 logs 200 even though it was retried.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | Usage or unexpected error. |
| 2 | `--max-requests` reached. Rerun the same command to resume. |
| 3 | Aborted on sustained 429/503 or an archive.org outage. Rerun the same command later to resume. |
| 4 | TLS verification failed. The message names the fix. |
| 5 | `verify` found a modified or missing file. |

## Encoding

urllib sends no `Accept-Encoding` and decodes nothing, and Wayback replays `id_` captures with their stored encoding anyway. Decoding is driven by the response's `Content-Encoding`, applied in reverse order when it lists several.

| Encoding | Stdlib | Method |
|---|---|---|
| `gzip`, `x-gzip` | yes | `gzip.decompress` |
| `deflate`, zlib-wrapped | yes | `zlib.decompress` |
| `deflate`, raw | yes | retry with `zlib.decompress(b, -15)` |
| `br` | no | Installed `brotli` or `brotlicffi`, else `encoding-unsupported`. For HTML pages only, one retry in rewrite mode (`/web/<ts>/<url>`, no `id_`); the extractor unwraps the rewritten URLs. |
| `zstd` | Python 3.14+ only | `compression.zstd` when importable, else `encoding-unsupported` |
| `identity` or absent | n/a | as is |

- HTML that starts with `1f 8b` but has no `Content-Encoding` header is decoded and flagged `sniffed-gzip`.
- A header that claims gzip but fails to decode keeps the raw bytes and records `encoding-mismatch`.
- HTML text decoding uses the `Content-Type` charset, then `<meta charset>` or `http-equiv`, then UTF-8 with replacement characters.

## Politeness defaults

- One rate limiter covers all archive.org hosts (`web.archive.org`, `archive.org`): at least `--delay` seconds (default 6, about 10 requests a minute) between requests, plus 0 to 1 s of random jitter.
- Redirects are followed by the client, up to 5 hops, and each hop goes through the limiter.
- On 429, 502, 503, 504 and network errors: wait `max(Retry-After, 30 * 2**attempt)` seconds plus jitter, capped at 600 s, for up to `--max-retries` (default 4) retries. `Retry-After` is accepted as seconds or an HTTP date.
- Network errors are refused, reset or aborted connections, timeouts and temporary DNS failures (`EAI_AGAIN`), raised bare or inside urllib's `URLError`. Other DNS failures and other transport errors exit 1, and a TLS verification failure exits 4. `--prefer-live` requests are never retried.
- After any 429 the steady delay doubles for the rest of the run, capped at 60 s.
- When retries run out the run stops, flushes the manifest and exits 3. The message names the last error, such as `HTTP 503` or `ConnectionRefusedError: [Errno 61] Connection refused`.
- From archive.org, a response whose `<title>` contains "Temporarily Offline" is handled as a 503 whatever its status code. On a replay (`/web/...`) that holds only when the `Memento-Datetime` header is absent: every real replay carries one, so a replay with it is an archived page that happens to have that title. CDX responses go by the title alone. A 200 CDX body that is not JSON is retried the same way (an empty body still means no captures). Neither becomes `not-archived` or `type-mismatch`, so a rerun after the outage picks the rows up again.
- `--max-requests` (default 300) counts every HTTP request, redirect hops and `--follow-css` fetches included. At the cap, remaining items get `skipped-cap` and the run exits 2.
- The availability API is never used; CDX covers it.
- An image's nearest capture comes from requesting `/web/<page_ts>id_/<image_url>` and following Wayback's redirect. A per-image CDX lookup happens only after a 404.
- `--prefer-live` (off by default) tries the live host first, with its own 1 s per-host pacing, and keeps the response only if the magic bytes match an image.
- No domain or prefix CDX listing is ever run on a shared CDN host such as `lh3.googleusercontent.com`. URLs found on those hosts get exact-URL lookups only.

## TLS

Certificate verification is never disabled. The CLI uses `ssl.create_default_context()`, which honours `SSL_CERT_FILE` and `SSL_CERT_DIR`, and `--cafile PATH` overrides it. On a verification failure it exits 4 with the common macOS fixes: run `Install Certificates.command` from the python.org installer's folder in `/Applications`, use Homebrew or Command Line Tools Python instead, or pass `--cafile`.

## Testing

```
python3 -m unittest discover -s tests -v
```

The suite is offline. A module-level guard makes any socket connection fail, and a fake transport serves committed fixtures (`tests/fixtures/<name>.meta.json` for status and headers, `<name>.body` for raw bytes). Fixture bodies are synthetic; `python3 tests/make_fixtures.py` regenerates them deterministically. `--record DIR` on any subcommand saves real responses in the same format so they can become fixtures. CI runs the suite on Python 3.9 and 3.13 on Ubuntu and 3.13 on macOS.

## Limitations

- Single-threaded by design. A full 300-request run at the default pace takes more than half an hour.
- JS-rendered sites such as Google Sites may yield only icons: images loaded by JavaScript are found only if their URLs appear in inline script strings or in the domain's CDX listing (within its date window). The host report says where to look next.
- An archived page whose own `<title>` contains "Temporarily Offline" is told apart from archive.org's outage page only by its `Memento-Datetime` header. The outage page's real headers have not been observed: if it ever carried that header, it would be kept as a page or recorded as `type-mismatch` instead of retried.

## License

MIT. See [LICENSE](LICENSE).

Prior art: [wayback-machine-CDX-image-recovery-skill](https://github.com/Suganthan-Mohanadasan/wayback-machine-CDX-image-recovery-skill) by Suganthan Mohanadasan.
