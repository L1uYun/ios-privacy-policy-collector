# iOS Privacy Policy Collector

Collect iOS App Store privacy policy URLs and convert policy pages into Markdown
with usable absolute links.

The collector is designed as a worker prototype for larger privacy-policy corpus
construction. It resolves iOS apps from App Store IDs, App Store URLs, bundle
IDs, search terms, or Apple public RSS charts, then fetches the developer
privacy policy linked from the App Store page.

## What It Saves

For each app/country pair, the script can save:

- `app-store.html`: raw App Store page used as URL evidence.
- `app-privacy-label.txt`: App Store privacy label text when visible.
- `privacy-policy.html`: raw developer privacy policy page.
- `privacy-policy.md`: primary corpus artifact, with Markdown links normalized
  to absolute URLs.
- `privacy-policy.txt`: plain text fallback for length and quality checks.
- `privacy-policy.links.jsonl`: links extracted from the Markdown/HTML.
- `results.jsonl`: one metadata row per attempted app/country pair.

The script rejects Apple-owned App Store privacy/cookie helper pages as policy
targets, because those pages are not the developer's privacy policy.

## Install

Python 3.11+ is recommended.

```bash
python -m pip install -r requirements.txt
```

The script still runs with the Python standard library only, but installs better
HTML-to-Markdown extraction when these optional packages are present.

## Quick Start

Collect one known app:

```bash
python scripts/ios_privacy_policy_collector.py \
  --app-id 284882215 \
  --country us \
  --output-dir out/ios \
  --jsonl out/ios/results.jsonl
```

Use a local HTTP proxy:

```bash
python scripts/ios_privacy_policy_collector.py \
  --app-id 284882215 \
  --country us \
  --proxy http://127.0.0.1:7890 \
  --output-dir out/ios \
  --jsonl out/ios/results.jsonl
```

Run a chart-based sample across countries:

```bash
python scripts/ios_privacy_policy_collector.py \
  --chart top-free --chart top-paid \
  --country us --country cn --country jp --country gb \
  --search-limit 100 \
  --timeout 45 --fallback-timeout 8 --sleep 0.05 \
  --try-common-paths --enrich-lookup --resume \
  --output-dir out/ios-chart \
  --jsonl out/ios-chart/results.jsonl
```

## Inputs

- `--app-id`: App Store numeric ID, `id...` value, or full App Store URL.
- `--bundle-id`: iOS bundle ID.
- `--search`: App Store search term.
- `--chart`: Apple public RSS chart seed, currently `top-free` or `top-paid`.
- `--input`: UTF-8 text file with one ID, bundle ID, URL, or search term per line.
- `--country`: repeatable App Store country code.

## Scaling Notes

This is not a full million-app crawler by itself. For large runs:

- First obtain a reliable seed list of app IDs or bundle IDs.
- Resolve App Store metadata first.
- Deduplicate by canonical `policy_url` before fetching policy pages.
- Deduplicate extracted policies by `policy_text_sha256`.
- Add a queue database and multiple workers.
- Add Playwright or another browser-rendering fallback for JS-heavy or blocked
  pages.
- Apply per-domain rate limits and keep failure reasons auditable.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## License

MIT
