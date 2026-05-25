#!/usr/bin/env python3
"""Run Common Crawl App Store discovery across multiple storefront countries."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import discover_commoncrawl_appstore_urls as discovery
import queue_store


def parse_countries(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def discover_country(args: argparse.Namespace, index: str, country: str, server_limit: int) -> dict:
    country_args = argparse.Namespace(
        index=index,
        url_pattern=f"apps.apple.com/{country}/app/",
        match_type="prefix",
        country=country,
        source=args.source,
        limit=args.limit,
        server_limit=server_limit,
        collapse_urlkey=True,
        timeout=args.timeout,
        proxy=args.proxy,
        user_agent=args.user_agent,
        backend=args.backend,
        output_csv=str(Path(args.output_dir) / f"commoncrawl-appstore-{country}-{args.limit}.csv"),
        db=args.db,
    )
    query_url, rows = discovery.discover_rows(country_args)
    result = {
        "country": country,
        "status": "ok",
        "index": index,
        "server_limit": server_limit,
        "query_url": query_url,
        "rows": len(rows),
        "output_csv": country_args.output_csv,
    }
    result["output_csv_rows"] = discovery.write_seed_csv(country_args.output_csv, rows)
    if args.db:
        result["db_import"] = queue_store.import_seeds(args.db, rows)
    return result


def run(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    index = args.index
    if index == "latest":
        index = discovery.latest_index(args.timeout, args.user_agent, args.proxy)
    results = []
    fallback_limits = [int(value) for value in str(args.fallback_server_limits).split(",") if value.strip()]
    for country in parse_countries(args.countries):
        country_result = None
        server_limits = [args.server_limit or args.limit] + fallback_limits
        for attempt, server_limit in enumerate(server_limits[: args.retries + 1], start=1):
            try:
                country_result = discover_country(args, index, country, server_limit)
                country_result["attempt"] = attempt
                break
            except Exception as exc:  # noqa: BLE001 - operational runner must keep the batch moving.
                country_result = {
                    "country": country,
                    "status": "error",
                    "index": index,
                    "attempt": attempt,
                    "server_limit": server_limit,
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                    "traceback": traceback.format_exc(limit=5),
                }
                if attempt <= args.retries:
                    time.sleep(args.retry_sleep)
        results.append(country_result)
        print(json.dumps(country_result, ensure_ascii=False, sort_keys=True), flush=True)
        if args.sleep > 0:
            time.sleep(args.sleep)

    summary = {
        "index": index,
        "countries": len(results),
        "ok_countries": sum(1 for row in results if row["status"] == "ok"),
        "error_countries": sum(1 for row in results if row["status"] != "ok"),
        "rows": sum(int(row.get("rows") or 0) for row in results),
        "inserted": sum(int((row.get("db_import") or {}).get("inserted") or 0) for row in results),
        "duplicates": sum(int((row.get("db_import") or {}).get("duplicates") or 0) for row in results),
        "results": results,
    }
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover App Store seeds across Common Crawl storefront prefixes.")
    parser.add_argument("--countries", required=True, help="Comma-separated storefronts, for example us,gb,de,fr,jp.")
    parser.add_argument("--index", default="latest")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--server-limit", type=int, default=None)
    parser.add_argument("--fallback-server-limits", default="5000,2000,1000", help="Comma-separated lower CDX limits to try after failures.")
    parser.add_argument("--source", default="commoncrawl-appstore-url")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--output-dir", default=str(queue_store.default_data_root()))
    parser.add_argument("--summary-json", default=None)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--backend", choices=["urllib", "requests"], default="requests")
    parser.add_argument("--user-agent", default=discovery.collector.DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=3.0)
    parser.add_argument("--sleep", type=float, default=0.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, sort_keys=True))
    return 0 if summary["error_countries"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
