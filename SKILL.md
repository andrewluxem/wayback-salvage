---
name: "wayback-salvage"
description: Recover archived pages and images from the Wayback Machine through a bundled stdlib-only Python CLI. Lists captures, decodes gzip and deflate bodies, flags JS-rendered pages, extracts images from HTML, CSS, meta tags and script strings, downloads them at a pace archive.org tolerates, and records every result in a resumable manifest. Use when asked to recover an old version of a site, find archived images, or salvage content from archive.org.
---

# Wayback Salvage

Recover archived pages and images from the Wayback Machine by running the bundled CLI. Never write curl loops, wget calls or ad hoc fetch code (Python, shell, JS) against archive.org, not even for one URL. The CLI handles decoding, pacing, backoff, TLS and the manifest; hand-rolled code skips all of that and gets the user's IP throttled.

## When to use

- The user wants an old version of their site, a page that no longer exists, or images the live site no longer serves.
- The user asks what the Wayback Machine holds for a URL or domain.

## How to run it

The skill lives at `~/.claude/skills/wayback-salvage/`, with `scripts/` beside this file. Every call has this form:

```
python3 <skill dir>/scripts/wayback_salvage.py <subcommand> [options]
```

`<skill dir>` is the directory that holds this SKILL.md. Use one `-o DIR` for every step of a job: resume and dedupe read the manifest in that directory. Output is `key=value` lines; add `--json` when you need to parse it. URLs may omit the scheme (`example.com/about`). `-t` is short for `--timestamp`; take timestamps from `snapshots` output (for example `20240916025321`).

## Workflow

1. `snapshots URL --collapse digest` lists captures, oldest first (`--from`/`--to` narrow the window; `--limit N` keeps the first N). Pick a timestamp with the user, or plan to use `--latest`.
2. `page URL -t TS -o DIR` (or `--latest`) reports encoding, decoded size, the toolbar-injection flag, the JS-rendered verdict with its reasons, and image candidates by source type. It downloads no images. Tell the user the verdict. Any `js_reasons` means `images` scans the page's scripts for image URLs; `page` then prints `js_scan_ran=true` and already lists them as `js-string` candidates. `js_rendered=true` also adds a CDX listing of the domain's images, and the result may still be only icons.
3. `images URL -t TS -o DIR` (or `--latest`) for one page, or `site-images DOMAIN -o DIR` for every archived image on a domain.
   - Run with `--dry-run` first whenever the count could exceed about 50: `page` listed more than that, the page is JS-rendered (the fallback's count is unknown until it runs), or the command is `site-images`.
   - Report the planned count to the user (the dry run prints it; the manifest holds `planned` rows). At the default pace the real run makes about 10 requests a minute.
   - The `images` domain listing covers captures within 365 days either side of the page capture; the summary prints `listing_window`. Pass `--from`/`--to` only when the user wants images from other periods of the site.
   - The real run is the same command without `--dry-run`.
4. `verify -o DIR` re-hashes saved files against the manifest and prints counts by `status` and `found_via`.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done | Summarise (below). |
| 1 | Usage or unexpected error | Read the message and fix the arguments. Do not fall back to curl. |
| 2 | Request cap reached (`-n`, default 300); unfinished rows are `skipped-cap` | Tell the user to rerun the same command later; it resumes. Raise `-n` only if the user asks. |
| 3 | Aborted on sustained 429/503, network errors or an outage at archive.org | Tell the user to rerun the same command later; it resumes. Do not lower `--delay` and do not retry in a loop. |
| 4 | TLS certificate verification failed | Relay the printed fix to the user. Never disable verification or switch tools to get around it. |
| 5 | `verify` found a modified or missing file | Relay the `verify` output. Do not edit the manifest. |

## Pacing

- `--delay` defaults to 6 seconds. Never pass a value below 3 unless the user asks for it.
- Run one instance at a time against archive.org. The rate limiter is per process.

## Summarising results

Read `<out>/manifest.csv`. After a run that did not exit cleanly a key can appear more than once; the last row per `(original_url, requested_timestamp)` wins. Report:

- counts by `status` and by `found_via`;
- every `type-mismatch` row (a 200 whose body was HTML or an unknown type, usually a soft 404) and every `encoding-unsupported` row (a `br` or `zstd` body with no decoder available), each with its `original_url`;
- every `error` and `rate-limited` row with its `error` text; rerunning the same command later retries them;
- where the files are: `<out>/images/` and `<out>/pages/`;
- for a JS-rendered page, the host report the CLI printed, as the place to look next: `script_src_host` hosts serve the scripts the page loads; `script_text_host` hosts are only mentioned in inline scripts and are weaker leads.

Only `ok` rows are recovered files; `duplicate` rows point at an earlier `ok` copy. For `encoding-unsupported`, installing `brotli` is the user's decision; never pip install it yourself.
