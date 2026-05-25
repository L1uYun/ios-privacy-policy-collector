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
- `policy-cluster/cluster.json`: optional manifest for the root policy plus
  linked user agreements, terms, cookie policies, data/children notices,
  permission statements, and other related legal documents.
- `policy-cluster/nodes/*.{html,md,txt,links.jsonl}`: archived pages in the
  protocol cluster. Markdown keeps absolute links so downstream readers can
  follow the original references.
- `results.jsonl`: one metadata row per attempted app/country pair.

The script rejects Apple-owned App Store privacy/cookie helper pages as policy
targets, because those pages are not the developer's privacy policy.

## Install

Python 3.11+ is recommended.

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The script still runs with the Python standard library only, but installs better
HTML-to-Markdown extraction when these optional packages are present. Playwright
is only needed when using `--js-fallback`.

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

## Queue Database

For large runs, keep runtime data outside the repository. On the Windows machine
used for development, the recommended data root is:

```text
F:\ios-privacy-policy-collector\data
```

Initialize a queue database:

```bash
python scripts/queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  init
```

Import seeds from CSV or JSONL:

```bash
python scripts/queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  import-seeds \
  --file seeds.csv \
  --source public-dataset \
  --country us
```

Seed CSV files can include:

```text
app_id,bundle_id,app_store_url,country,provenance_url,license_note
```

Check queue statistics:

```bash
python scripts/queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  stats
```

Claim one pending fetch task for a worker:

```bash
python scripts/queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  claim \
  --worker-id worker-1
```

Run queued collection tasks:

```bash
python scripts/queue_worker.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --worker-id worker-1 \
  --limit 10 \
  --output-dir F:\ios-privacy-policy-collector\data\out \
  --jsonl F:\ios-privacy-policy-collector\data\worker-results.jsonl \
  --proxy http://127.0.0.1:7890 \
  --try-common-paths \
  --enrich-lookup \
  --js-fallback \
  --collect-cluster \
  --cluster-probe-common-paths
```

For larger batches, use the multi-worker runner. It keeps worker JSONL logs
separate, writes a live summary JSON, and uses bounded JS rendering defaults so
one slow site does not stall the whole run:

```bash
python scripts/run_batch.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --output-dir F:\ios-privacy-policy-collector\data\out \
  --log-dir F:\ios-privacy-policy-collector\data\logs \
  --workers 8 \
  --limit-per-worker 1000 \
  --proxy http://127.0.0.1:7890
```

For million-scale collection, run repeated batches against the same queue until
`pending_fetches` reaches zero. Keep `policy_url_attempt` rows: they record each
candidate URL, fetch method, extracted length, quality flag, and error, which is
the audit trail needed to repair failures without re-running successful apps.

## Policy Cluster Mode

The collector can archive more than the privacy-policy entry page. Policy
cluster mode starts from the resolved developer privacy policy URL, saves that
root document, then follows same-domain legal/privacy links such as terms,
user agreements, cookie policies, children/privacy notices, data policies,
permission statements, third-party sharing lists, and related notices.

Run it directly for a single known policy URL:

```bash
python scripts/policy_cluster.py \
  https://www.doubao.com/legal/privacy \
  --output-dir F:\ios-privacy-policy-collector\data\smoke\doubao-policy-cluster \
  --proxy http://127.0.0.1:7890 \
  --max-depth 1 \
  --max-docs 12 \
  --js-fallback
```

For higher recall, add common same-domain path probing:

```bash
python scripts/policy_cluster.py \
  https://www.doubao.com/legal/privacy \
  --output-dir F:\ios-privacy-policy-collector\data\smoke\doubao-policy-cluster-probe \
  --proxy http://127.0.0.1:7890 \
  --max-depth 1 \
  --max-docs 30 \
  --probe-common-paths \
  --js-fallback
```

Common-path probing tries routes such as `/terms`, `/legal/terms`,
`/cookie-policy`, `/children-privacy`, `/data-policy`, and `/permissions`.
Short shell pages and failed probes are recorded as manifest errors rather than
accepted as cluster nodes. Explicit links found inside the root policy are kept
as evidence even when their quality flag is weak, because they were referenced
by the policy itself.

With `--js-fallback`, static HTTP is still tried first. If extracted text is too
short or appears blocked, Chromium renders the page and the rendered HTML is
archived instead when it improves the extracted policy text.

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
- Use policy cluster mode for high-quality runs so privacy policies, user
  agreements, cookie/data notices, and linked legal documents stay connected.
- Keep `policy_url_attempt` logs for every candidate URL so failed app rows can
  be repaired by URL-discovery rules instead of being guessed from coarse
  worker errors.
- Apply per-domain rate limits and keep failure reasons auditable.

## Roadmap For 1M App Store Apps

The first scaling problem is seed acquisition, not page parsing. Apple provides
public lookup/search/chart interfaces, but it does not publish a complete public
App Store catalog dump. Treat the current script as a policy-collection worker,
then build a queue and seed-ingestion layer around it.

### Target Definition

Before a 1M run, define which target is meant:

- 1M unique App Store app IDs.
- 1M app-country rows, where the same app can appear in multiple storefronts.
- 1M policy fetch attempts.
- 1M unique privacy policy documents.

These are different workloads. Many apps share the same company policy URL, so
1M app rows will usually produce far fewer unique policy texts.

### Phase 0: Worker Hardening

- Keep Markdown as the primary artifact.
- Preserve absolute links in Markdown.
- Archive policy clusters, not only a single privacy-policy page.
- Store raw App Store HTML and raw policy HTML as evidence.
- Reject Apple-owned App Store privacy/cookie helper pages as developer policy
  targets.
- Classify short, blocked, JS-only, non-policy, and retryable pages separately.
- Add Playwright fallback for JS-heavy policy pages.

### Phase 1: Seed Ingestion

Define a seed table with:

```text
seed_source
app_id
bundle_id
app_store_url
country
observed_at
provenance_url
license_note
```

Then import seeds from multiple sources, deduplicate by `app_id` and
`(country, app_id)`, and validate a stratified sample through iTunes lookup.

### Phase 2: Queue Database

Move from JSONL-only execution to SQLite or Postgres.

Recommended tables:

- `app_seed`
- `app_metadata`
- `policy_url_candidate`
- `policy_url_attempt`
- `policy_fetch`
- `policy_document`
- `policy_link`
- `run_event`

Recommended statuses:

```text
pending
running
ok
not_found
blocked
js_required
too_short
not_policy
retryable_error
permanent_error
```

### Phase 3: URL Discovery

- Resolve app metadata by iTunes lookup.
- Fetch App Store pages per country/storefront.
- Extract developer privacy policy URLs.
- Filter Apple helper links, cookie notices, and privacy-label pages.
- Canonicalize URLs.
- Deduplicate policy URLs before fetching policy text.

### Phase 4: Policy Fetch Workers

- Use static HTTP first.
- Convert HTML to Markdown with `trafilatura`; fall back to `markdownify`, then
  the internal parser.
- Use a browser-rendered worker for JS-only, empty, and blocked pages.
- Enforce per-domain concurrency and rate limits.
- Save raw HTML, Markdown, plain text, extracted links, hashes, and quality
  flags.
- Save cluster manifests with nodes and edges for linked legal documents.

### Phase 5: Quality And Deduplication

- Exact dedupe by `policy_text_sha256`.
- Near-duplicate detection with SimHash or MinHash.
- Language detection.
- Policy-vs-non-policy classifier.
- Detection for cookie pages, terms pages, Apple helper pages, and redirect
  shells.
- Manual review queue for ambiguous cases.

### Phase 6: Measurement

Run staged pilots before the 1M run:

- 1k pilot: prove seed ingestion, Markdown output, absolute links, and failure
  taxonomy.
- 10k pilot: measure unique-policy ratio, success rate, median fetch time, and
  storage per app.
- 100k pilot: validate queue concurrency, retry/resume behavior, and cost
  projection.
- 1M run: fetch unique policy URLs first, then link results back to app/country
  rows.

## 1M App Store Seed Sources

No single public Apple endpoint gives a full current catalog, so use a layered
seed strategy.

At million scale, the expected yield is:

```text
usable policy clusters = seed rows * active-app validation rate * policy success rate
```

So the work splits into two tracks: collect far more than 1M candidate seed rows,
then keep reducing policy-fetch failure on a fixed regression set.

### 1. Public App Store Metadata Dumps

Start here if the license permits reuse.

- Large public app metadata datasets, for example
  `gauthamp10/apple-appstore-apps`.
- AppGoblin-style public app metadata exports with iOS `store_id` values.
- Research datasets from App Store privacy-label studies that report large
  App Store snapshots.
- Other public App Store ID dumps with clear provenance.

Use these as candidate seed lists, then refresh each app through iTunes lookup.
Do not assume old datasets still represent active apps.

For AppGoblin-style TSV/CSV dumps, import iOS rows with:

```bash
python scripts/import_seed_dump.py \
  --input F:\ios-privacy-policy-collector\data\seed-dumps\live_store_apps.tsv.xz \
  --format appgoblin \
  --source appgoblin-live-store-apps \
  --country us \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite
```

The importer accepts `.tsv`, `.csv`, `.tsv.xz`, and `.csv.xz`, extracts numeric
App Store IDs from `store_id`, `app_id`, `id`, or App Store URLs, filters iOS
rows when a `store/platform` column exists, and writes queue-ready seeds.

For appstoredb-style SQLite dumps, import directly from the `apps` table and,
when present, join `stores` for storefront-specific canonical URLs:

```bash
python scripts/import_seed_dump.py \
  --input F:\ios-privacy-policy-collector\data\seed-dumps\appstoredb\appstore_sample.sqlite \
  --format appstoredb-sqlite \
  --source appstoredb-sample-2025-11 \
  --country us \
  --output-csv F:\ios-privacy-policy-collector\data\appstoredb-sample-seeds.csv \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite
```

The public appstoredb Hugging Face repository currently exposes a 211 MB sample
SQLite file, not the full database described in the dataset card. The sample
contains 1,000 apps and 17,227 storefront rows; importing and validating it
added 17,227 seed rows in the local queue, with 17,115 active, 112 inactive,
and zero lookup errors. If the full appstoredb SQLite database is obtained
later, the same importer can ingest it without a schema change.

After importing a dump, validate seed liveness before spending policy-fetch
budget:

```bash
python scripts/validate_itunes_seeds.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --limit 100000 \
  --batch-size 100 \
  --sleep 0.05 \
  --proxy http://127.0.0.1:7890 \
  --output-json F:\ios-privacy-policy-collector\data\seed-validation-100k.json \
  --output-csv F:\ios-privacy-policy-collector\data\seed-validation-100k.csv
```

The validator writes one `seed_validation` row per `app_seed` row and separates:

- `active`: iTunes lookup returned a software result.
- `inactive`: lookup succeeded but returned zero results for that country.
- `missing`: reserved for unusable seed rows or `--zero-result-status missing`.
- `lookup_error`: HTTP, timeout, proxy, or malformed JSON failure.

`python scripts/queue_store.py --db F:\ios-privacy-policy-collector\data\queue.sqlite stats`
includes these validation counters, so dump quality can be measured before the
full crawler run.

### 2. Apple Public Interfaces

Use official Apple surfaces for validation and enrichment.

- iTunes Search API lookup by `id`.
- iTunes Search API lookup by `bundleId`.
- iTunes Search API search by keyword.
- Apple public RSS chart feeds by country and chart type.
- App Store product HTML as evidence for policy URL and privacy label.

These are reliable for known IDs and fresh popular apps, but they are not enough
for full-catalog discovery.

### 3. Common Crawl And Web Index Discovery

Use web indexes to discover App Store product URLs at scale.

Search for URL patterns such as:

```text
https://apps.apple.com/*/app/*/id*
```

Then extract numeric app IDs, deduplicate, and validate with iTunes lookup.
Keep the Common Crawl index path and timestamp as provenance.

Run a bounded discovery slice first:

```bash
python scripts/discover_commoncrawl_appstore_urls.py \
  --index latest \
  --url-pattern "apps.apple.com/us/app/" \
  --match-type prefix \
  --limit 10000 \
  --proxy http://127.0.0.1:7890 \
  --output-csv F:\ios-privacy-policy-collector\data\commoncrawl-appstore-10k.csv \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite
```

The script streams Common Crawl CDX JSON lines, keeps only
`apps.apple.com/<country>/app/.../id<digits>` product URLs, deduplicates by
`(country, app_id)`, stores the CDX index and timestamp in `provenance_url`, and
can import directly into `app_seed`. Prefix queries such as
`apps.apple.com/us/app/` are more reliable than one broad wildcard query. Scale
it by running multiple recent indexes and storefront prefixes, then measure the
marginal active rate with `scripts/validate_itunes_seeds.py`.

For multi-storefront discovery, use the runner so transient CDX failures are
captured per country and retried with smaller server-side limits:

```bash
python scripts/discover_commoncrawl_countries.py \
  --countries us,gb,ca,au,de,fr,jp,kr,cn,in,br,mx,es,it,nl \
  --index CC-MAIN-2026-17 \
  --limit 5000 \
  --backend requests \
  --proxy http://127.0.0.1:7890 \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --output-dir F:\ios-privacy-policy-collector\data \
  --summary-json F:\ios-privacy-policy-collector\data\commoncrawl-multicountry-summary.json
```

In the first verified run against `CC-MAIN-2026-17`, 15 storefront prefixes
produced 20,424 seed rows. iTunes batch lookup validated 20,190 active and 234
inactive rows, with no lookup errors. Older 2026 indexes tested with the same
prefixes had zero marginal rows, so the next scale step should split the latest
index more finely before spending time on older indexes.

Export the active rows as the project-owned 2026 seed dump:

```bash
python scripts/export_seed_dump.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --sources commoncrawl-appstore-url \
  --status active \
  --output-csv F:\ios-privacy-policy-collector\data\seed-dumps\ios-appstore-2026-commoncrawl-live-lookup-active-seeds.csv \
  --summary-json F:\ios-privacy-policy-collector\data\seed-dumps\ios-appstore-2026-commoncrawl-live-lookup-active-seeds.summary.json
```

The first exported dump contains 20,190 active app-country rows from Common
Crawl 2026 URLs validated by Apple iTunes lookup, covering 19,282 unique App
Store IDs. Public 2023/2024/2025 dumps should be treated as supplemental seed
sources unless their freshness and license are verified.

### 4. Commercial App Metadata APIs

Commercial APIs may be the cleanest path to current 1M-scale coverage, but they
add cost and licensing constraints.

Candidates to evaluate:

- 42matters
- Appfigures
- AppTweak
- Sensor Tower

Required checks:

- Does the contract allow research crawling of linked privacy policies?
- Does it provide App Store numeric IDs or canonical URLs?
- Does it expose country/storefront dimensions?
- Is batch export or sufficient quota available?

### 5. Keyword And Category Expansion

Use this only as a gap filler because recall is hard to prove.

Approach:

- Build multilingual keyword lists from categories, brands, common app nouns,
  and app descriptions.
- Search by country and language.
- Add discovered IDs to the seed queue.
- Track query provenance and marginal new-ID yield.

Do not claim catalog completeness from keyword expansion alone.

### Recommended Seed Plan

1. Keep the primary 2026 seed dump as Common Crawl 2026 App Store URLs validated
   through Apple live iTunes lookup.
2. Import license-compatible public large app-ID datasets only as supplemental
   sources because old dumps will include inactive apps.
3. Validate and refresh every source through iTunes lookup, keeping inactive and
   missing IDs as separate seed-quality metrics.
4. Add Apple RSS/search for freshness and targeted gap filling.
5. Add commercial metadata only if public seed quality is not enough.

### Failure-Rate Track

Seed volume is not sufficient if policy fetch failure stays high. Keep a
regression set of failed apps and re-run it after URL-discovery changes.

Current failure-repair hooks:

- `policy_url_attempt` records every candidate URL, status, fetch method,
  extracted length, quality flag, and error.
- `--js-fallback` uses Chromium for short or blocked pages.
- candidate failures continue to the next URL instead of failing the entire app.
- `scripts/run_batch.py` runs multi-worker batches and writes live summaries.
- `config/domain_rules.json` stores auditable domain-specific policy URL rules.
  Rules can mark a domain as `browser_first` for sites that return 403/406 or
  require JavaScript. The default rules currently cover OpenAI, Uber, Meta,
  Spotify, Ryanair, and Tesco.

The first domain-rule regression on the remaining 20 failed pilot rows recovered
8 rows, all through auditable `domain-rule:*` attempts. The report is written to:

```text
F:\ios-privacy-policy-collector\data\failed-20-domain-rules\failed-20-domain-rules-summary.json
```

Next useful failure reducers:

- domain rules for high-volume sites with stable legal URLs.
- Common Crawl / web-index URL discovery for apps whose iTunes `sellerUrl` is
  missing.
- browser-first mode for domains that consistently return 403/406 to static
  HTTP.
- cross-country reuse when the same `app_id` or `bundle_id` succeeds in another
  storefront.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## License

MIT
