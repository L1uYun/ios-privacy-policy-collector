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

Scale in corpus milestones, not by jumping straight to the million target. Each
stage has a different bottleneck, so do not unlock the next stage only because a
large queue exists:

| Stage | Target accepted policy clusters | Gate before expanding |
| --- | ---: | --- |
| 1 | 10,000 | stable Markdown output, resumable batches, failure categories visible, terminal success rate healthy |
| 2 | 100,000 | enough active seeds, country/source mix selected by observed yield, Common Crawl/app dump quality measured |
| 3 | 500,000 | repeated background batches without stale locks or disk layout issues, dedupe and disk budget verified |
| 4 | 1,000,000 | seed coverage plus policy success rate supports the final run, restart/retry procedure proven |

The milestone unit is an accepted `policy_document` with saved Markdown and
policy-cluster artifacts, not just a seed row or a discovered policy URL. Check
milestone progress with:

```bash
python scripts\milestone_status.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --output-json F:\ios-privacy-policy-collector\data\milestone-status.json
```

The report includes a `stage_plan` object with the current milestone, remaining
accepted policy clusters, active-seed surplus or deficit, expected yield from
pending fetches at the observed terminal success rate, and the recommended next
action. Treat that as the operational switch:

- If 10k still has enough active seeds and expected pending yield, keep policy
  workers running on the best observed sources.
- If 100k shows an active-seed deficit, keep Common Crawl 2026 and Apple live
  lookup running before increasing policy-worker volume.
- If terminal success rate drops below the gate, stop expanding that source mix
  and repair failures on a fixed regression set.
- If a stage completes, freeze `milestone-status.json`, failure classification,
  source/country yield, and the seed dump before opening the next stage.

Create an auditable stage snapshot at any checkpoint:

```bash
python scripts\freeze_stage_snapshot.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --data-root F:\ios-privacy-policy-collector\data \
  --name stage1-10k-checkpoint
```

The snapshot writes a self-contained directory under
`F:\ios-privacy-policy-collector\data\stage-snapshots` with milestone status,
failure classification, Common Crawl shard progress, disk usage, and recent
batch summaries. Use this before widening from 10k to 100k, then again at 500k
and 1M gates.

Classify permanent failures while the 10k stage is running:

```bash
python scripts\classify_policy_failures.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --output-json F:\ios-privacy-policy-collector\data\policy-failure-classification.json
```

Start a resumable background policy-cluster batch for the current 10k stage:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_policy_cluster_batch.ps1 `
  -Name stage1-10k `
  -Workers 8 `
  -LimitPerWorker 250 `
  -Countries "es,it,nl" `
  -Sources "apple-search:*"
```

### 2026-05-25 T430 10k Gate Snapshot

The first T430 stage gate completed on 2026-05-25. The hot queue database was
kept on NVMe, while policy-cluster artifacts were written under `/data`:

```text
Remote repo: /data/xiaolab-research/ios-privacy-policy-collector
Hot DB: /mnt/data-nvme/ios-privacy-policy-collector/state/queue.sqlite
Data root: /data/xiaolab-research/ios-privacy-policy-collector/data
Snapshot: data/stage-snapshots/stage1-10k-auto-stop-reached-10000-20260525-135514
Policy clusters: data/policy-clusters
```

The gate stopped at `policy_documents=10038`, with `running_fetches=0` after
the auto-stop monitor stopped policy workers and cleared running fetch rows.
The snapshot manifest recorded `policy_documents=10022` at freeze time, because
some workers completed additional rows before the final post-stop stats check.

To produce a shareable offline package on the Windows host, run:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\Export-T430Policy10kToF.ps1
```

Default outputs:

```text
F:\ios-privacy-policy-collector-10k
F:\ios-privacy-policy-collector-10k.zip
F:\ios-privacy-policy-collector-10k.sync.log
F:\ios-privacy-policy-collector-10k.sync.err.log
```

The unzipped directory is the easiest way for reviewers to inspect the corpus.
Open its `README.md`, then inspect
`data/stage-snapshots/stage1-10k-auto-stop-reached-10000-20260525-135514/snapshot-manifest.json`
for collection statistics and `data/policy-clusters/` for the Markdown policy
clusters. The zip is only the transport copy of the same directory.

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

For finer Common Crawl coverage, split App Store product slugs by generated
prefix shards. This is resumable and writes one progress JSONL row per shard:

```bash
python scripts/discover_commoncrawl_slug_prefixes.py \
  --countries us,gb,jp,de,fr,cn,in \
  --prefix-lengths 2 \
  --prefix-alphabet abcdefghijklmnopqrstuvwxyz \
  --index CC-MAIN-2026-17 \
  --limit 5000 \
  --server-limit 5000 \
  --fallback-server-limits 1000,500 \
  --backend requests \
  --timeout 120 \
  --sleep 0.05 \
  --retries 2 \
  --resume-progress \
  --progress-jsonl F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.progress.jsonl \
  --summary-json F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.summary.json \
  --output-dir F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite
```

Summarize long-running shard progress without reading the full worker log:

```bash
python scripts\summarize_commoncrawl_progress.py \
  --progress-jsonl F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.progress.jsonl \
  --total-shards 4732 \
  --output-json F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.progress-summary.json
```

On 2026-05-25, wave1 was restarted in direct mode from:

```text
F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\run-prefix-2026-17-wave1-direct.ps1
```

The Common Crawl CDX endpoint was more reliable without the local proxy in this
run. The discovery client disables inherited environment proxies for direct
runs, so a machine-wide `http_proxy` does not accidentally route CDX traffic
through `127.0.0.1:7890`.

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
- Apple platform/legal pages are rejected as developer policy URLs. This covers
  `apps.apple.com` and country Apple legal/privacy domains such as
  `apple.com`, `apple.com.cn`, `apple.fr`, and other `apple.*` hosts when the
  path is under `/legal/`, `/privacy/`, `/cookies`, or a privacy endpoint.
- App Store URL discovery uses layered fallbacks:
  1. explicit App Store privacy-policy links and JSON privacy fields;
  2. App Store external links such as `Developer Website`, `App Support`, and
     JSON fields like `developerWebsite` or `appSupportUrl`;
  3. iTunes `sellerUrl` and App Store external links as seller/support bases;
  4. short-link expansion for common hosts such as `on.fb.me`, `bit.ly`,
     `t.co`, and `tinyurl.com`;
  5. seller/support homepage static parsing, then optional `--js-fallback`;
  6. common legal paths, `robots.txt` sitemaps, and `sitemap.xml` /
     `sitemap_index.xml` privacy-like URL ranking;
  7. cross-country lookup for the same `app_id` when one storefront lacks a
     usable developer policy link.
- `scripts/run_batch.py` runs multi-worker batches and writes live summaries.
- `scripts/queue_store.py requeue-running` can safely return stale `running`
  fetches to `pending` by worker prefix or lock timestamp after a stopped batch.
- `scripts/run_batch.py --active-only --countries us,gb,jp,de` claims only
  iTunes-validated active seeds for higher-quality pilot batches.
- `config/domain_rules.json` stores auditable domain-specific policy URL rules.
  Rules can mark a domain as `browser_first` for sites that return 403/406 or
  require JavaScript. The default rules currently cover OpenAI, Uber, Meta,
  Spotify, Ryanair, and Tesco.

The first domain-rule regression on the remaining 20 failed pilot rows recovered
8 rows, all through auditable `domain-rule:*` attempts. The report is written to:

```text
F:\ios-privacy-policy-collector\data\failed-20-domain-rules\failed-20-domain-rules-summary.json
```

### 2026-05-26 Apple Platform Rediscovery Repair

The 10k review pack contained rows whose root URL resolved to Apple platform
pages, including Apple Internet Services, App Store pages, and Apple privacy
legal pages. Those are not developer privacy policies. On 2026-05-26 the
Windows host rebuilt this subset with stricter Apple-domain rejection and the
fallback stack above.

Audited output files:

```text
F:\ios-privacy-policy-collector-10k\apple-platform-policy-rediscovery.csv
F:\ios-privacy-policy-collector-10k\apple-platform-policy-rediscovery-final-audited-summary.json
F:\ios-privacy-policy-collector-10k\apple-platform-policy-rediscovery-final-nonrecovered-classified.csv
F:\ios-privacy-policy-collector-10k\apple-platform-policy-rediscovery-apple-rejected.csv
F:\ios-privacy-policy-collector-10k\clean\apple-platform-policy-rediscovery-clean.csv
```

Final audited subset status:

```text
Apple-platform subset rows: 8074
Developer policy recovered: 7874
Apple/platform rejected: 97
Network/error tail: 77
Unrecovered: 25
iTunes lookup missing: 1
Developer-policy recovery rate in this subset: 97.523%
Unique recovered apps: 5094
Unique recovered policy URLs: 4718
```

The `clean/` directory contains the reviewer-facing CSVs generated from the
working audit file:

- `apple-platform-policy-rediscovery-clean.csv`: all rows with normalized
  `audit_class`, `confidence`, `source_type`, `policy_url`, and
  `policy_domain` columns.
- `apple-platform-policy-rediscovery-recovered-clean.csv`: recovered developer
  policy rows only.
- `apple-platform-policy-rediscovery-nonrecovered-clean.csv`: rejected,
  unrecovered, error, and lookup-missing rows only.
- `apple-platform-policy-rediscovery-clean-summary.json`: machine-readable
  status, audit-class, confidence, and source-type counts.

The 2026-05-27 clean pass also merged one manually verified recovery:
WeatherRadar Basic (`app_id=1187807450`, `country=nz`) now maps to
`https://www.sparklingapps.com/mobile/privacy.html` with
`recovery_method=manual-audit:web-search:developer-site`.

Important interpretation: `apple_platform_rejected` rows are not successful
developer-policy recoveries. They are rows where discovery attempted to use an
Apple country legal/privacy URL, such as `www.apple.fr/fr/legal/privacy/`, and
the audit rejected it. Keep them out of third-party developer-policy success
metrics or label them separately as Apple/platform policy rows.

The tail file
`apple-platform-policy-rediscovery-final-nonrecovered-classified.csv`
classifies the previously audited non-recovered rows. After the WeatherRadar
manual merge, the current clean non-recovered count is 200:

- `apple_platform_or_first_party`: 97 rows, exclude or label separately.
- `network_timeout_retryable`: 74 rows in the clean audit class, retry later
  with lower concurrency and
  direct/proxy A-B testing.
- `no_seller_url_no_external_link`: remaining rows require web search or Common
  Crawl by app name plus app ID because App Store metadata did not expose a
  deterministic developer URL.
- `seller_site_no_policy_found`: 4 rows, add domain rules or mark no visible
  policy after manual review.
- `deep_landing_seller_needs_search`: 2 rows, try origin search, sitemap, and
  domain-specific rules.
- `itunes_lookup_missing`: 1 row, validate in other storefronts or refresh the
  seed.

For targeted rediscovery of Apple-platform rows on another machine:

```powershell
python scripts\rediscover_apple_platform_policies.py `
  --sample-index F:\ios-privacy-policy-collector-10k\sample-index.csv `
  --output-dir F:\ios-privacy-policy-collector-10k `
  --timeout 20 `
  --hard-timeout 45 `
  --proxy http://127.0.0.1:7890 `
  --resume `
  --workers 4 `
  --sleep 0 `
  --js-fallback
```

When syncing to T430, copy the repo changes first, then copy or regenerate the
audited CSVs under the T430 data root. The T430 data root used for the 10k gate
was:

```text
/data/xiaolab-research/ios-privacy-policy-collector/data
```

Suggested code sync from Windows, after committing the local changes:

```powershell
git push origin master
ssh t430 "cd /data/xiaolab-research/ios-privacy-policy-collector && git pull --ff-only"
```

For the remaining `no_seller_url_no_external_link` rows, use the classified
tail CSV directly and enable the App Store external-link plus web-search
fallback path:

```powershell
python scripts\rediscover_apple_platform_policies.py `
  --sample-index F:\ios-privacy-policy-collector-10k\apple-platform-policy-rediscovery-tail-104-classified.csv `
  --output-dir F:\ios-privacy-policy-collector-10k `
  --output-name apple-platform-policy-rediscovery-no-seller-websearch `
  --all-rows `
  --failure-class-filter no_seller_url_no_external_link `
  --timeout 8 `
  --hard-timeout 30 `
  --proxy http://127.0.0.1:7890 `
  --workers 1 `
  --sleep 0 `
  --web-search-fallback `
  --web-search-results 3 `
  --skip-alternate-countries
```

The `--skip-alternate-countries` flag is intentional for this repair class:
the classified CSV already carries `live_app_store_url`, so the repair should
try that App Store page's external links, then web search by app name and app
ID, before any expensive cross-country lookup. Search results are only treated
as candidates; the collector still probes privacy/common paths and rejects
Apple, App Store, DuckDuckGo, Google, Bing, and other platform/noise hosts.

Pilot checks on 2026-05-26 found that a naive search fallback can otherwise
misclassify the search engine's own `/privacy` page as a recovered policy. That
case is now filtered and covered by tests. A fixed first-5 pilot recovered
`0/5` rows, while the earlier unfiltered pilot's apparent `5/5` was rejected as
false positive. Treat web-search recovery as an audited candidate source, not a
blind success source.

Next useful failure reducers:

- domain rules for high-volume sites with stable legal URLs.
- Common Crawl / web-index URL discovery for apps whose iTunes `sellerUrl` is
  missing.
- browser-first mode for domains that consistently return 403/406 to static
  HTTP.
- cross-country reuse when the same `app_id` or `bundle_id` succeeds in another
  storefront.

### Current Background Runs

As of 2026-05-25 09:42 Asia/Shanghai, these local background jobs were active:

- Common Crawl 2026 prefix seed discovery, direct/no-proxy CDX mode:
  `F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\run-prefix-2026-17-wave1-direct.ps1`
- Apple live lookup for newly discovered queue rows:
  `F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\run-wave1-live-lookup-export.ps1`
- Policy-cluster batch4, 4 workers x 50 rows:
  `F:\ios-privacy-policy-collector\data\policy-batch-bg\run-policy-cluster-batch4-apple-search-es-it-nl.ps1`
- Policy-cluster batch5, 8 workers x 250 rows:
  `F:\ios-privacy-policy-collector\data\policy-batch-bg\cluster-batch5-apple-search-es-it-nl-2000.*.log`
- Stage 1 Common Crawl US pilot, 4 workers x 100 rows:
  `F:\ios-privacy-policy-collector\data\policy-batch-bg\cluster-stage1-cc-us-pilot-400-20260525-093815.*.log`
- Stage 1 Apple-search broad batch, 8 workers x 250 rows:
  `F:\ios-privacy-policy-collector\data\policy-batch-bg\cluster-stage1-apple-search-broad-2000-20260525-094222.*.log`

Batch4 and batch5 intentionally target `--active-only --countries es,it,nl
--sources "apple-search:*" --claim-order newest --min-policy-chars 500`. This
keeps early corpus growth on high-quality, recently discovered Apple live search
seeds instead of burning time on old cold US rows.

The Stage 1 Common Crawl pilot tests whether `commoncrawl-appstore-url` can
carry the 10k target. Early terminal yield was much lower than Apple-search
rows, with failures dominated by 404 and weak policy signal. Treat it as a
long-tail source until its failure modes are repaired. The Apple-search broad
batch consumes all active `apple-search:*` rows across storefronts first because
observed terminal success was above 90% for the active travel/video slices.

Useful status commands:

```bash
python scripts\queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  stats

python scripts\summarize_commoncrawl_progress.py \
  --progress-jsonl F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.progress.jsonl \
  --total-shards 4732 \
  --output-json F:\ios-privacy-policy-collector\data\commoncrawl-prefix-bg\wave1-us-gb-jp-de-fr-cn-in-2char.progress-summary.json
```

If a stopped batch leaves stale `running` rows, requeue by worker prefix:

```bash
python scripts\queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  requeue-running \
  --worker-prefix cluster-batch5-apple-search-es-it-nl
```

For targeted repair after a collector bug or domain-rule fix, requeue and retry
one failed fetch without disturbing the rest of the batch:

```bash
python scripts\queue_store.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  requeue-fetch \
  --fetch-id 42780

python scripts\queue_worker.py \
  --db F:\ios-privacy-policy-collector\data\queue.sqlite \
  --worker-id retry-42780 \
  --fetch-id 42780 \
  --limit 1 \
  --output-dir F:\ios-privacy-policy-collector\data\policy-clusters \
  --jsonl F:\ios-privacy-policy-collector\data\policy-batch-bg\retry-42780.jsonl \
  --proxy http://127.0.0.1:7890 \
  --try-common-paths \
  --enrich-lookup \
  --collect-cluster \
  --cluster-probe-common-paths \
  --js-fallback
```

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## License

MIT
