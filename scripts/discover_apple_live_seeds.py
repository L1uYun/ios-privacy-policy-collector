#!/usr/bin/env python3
"""Discover fresh App Store seeds from Apple public RSS and search APIs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ios_privacy_policy_collector as collector
import queue_store


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def app_record_to_seed(record: collector.AppRecord, country: str, seed_source: str, provenance_url: str) -> dict:
    return {
        "seed_source": seed_source,
        "app_id": record.app_id,
        "bundle_id": record.bundle_id or "",
        "app_store_url": record.app_store_url or f"https://apps.apple.com/{country}/app/id{record.app_id}",
        "country": country,
        "provenance_url": provenance_url,
        "license_note": "Discovered from Apple public live RSS/search API; validate with iTunes lookup before policy fetching.",
    }


def discover_chart(country: str, chart: str, limit: int, args: argparse.Namespace) -> list[dict]:
    url = collector.apple_rss_url(chart, country, limit)
    payload = collector.request_json_with_proxy(url, args.timeout, args.user_agent, args.proxy)
    return [
        app_record_to_seed(record, country, f"apple-rss-{chart}", url)
        for record in collector.records_from_apple_rss_payload(payload, f"apple-rss:{chart}")
    ]


def discover_search(country: str, term: str, limit: int, args: argparse.Namespace) -> list[dict]:
    url = collector.itunes_search_url(term, country=country, limit=limit)
    payload = collector.request_json_with_proxy(url, args.timeout, args.user_agent, args.proxy)
    return [
        app_record_to_seed(record, country, f"apple-search:{term}", url)
        for record in collector.records_from_itunes_payload(payload, f"apple-search:{term}")
    ]


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for row in rows:
        key = (row["seed_source"], row["country"], row["app_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write_seed_csv(path: str | Path, rows: list[dict]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["app_id", "bundle_id", "app_store_url", "country", "provenance_url", "license_note", "seed_source"]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def run(args: argparse.Namespace) -> dict:
    countries = parse_csv(args.countries)
    charts = parse_csv(args.charts)
    terms = [term.strip() for term in (args.term or []) if term.strip()]
    rows: list[dict] = []
    errors: list[dict] = []
    completed = 0
    total_tasks = len(countries) * (len(charts) + len(terms))
    progress_handle = None
    if args.progress_jsonl:
        progress_path = Path(args.progress_jsonl)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_handle = progress_path.open("a", encoding="utf-8")
    def emit_progress(event: dict) -> None:
        if progress_handle:
            progress_handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            progress_handle.flush()
        if args.progress_every and event.get("completed", 0) % args.progress_every == 0:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)

    for country in countries:
        for chart in charts:
            try:
                discovered = discover_chart(country, chart, args.chart_limit, args)
                rows.extend(discovered)
                completed += 1
                emit_progress({"completed": completed, "total_tasks": total_tasks, "country": country, "kind": "chart", "value": chart, "rows": len(discovered), "errors": len(errors)})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                errors.append({"country": country, "kind": "chart", "value": chart, "error_class": exc.__class__.__name__, "error_message": str(exc)})
                completed += 1
                emit_progress({"completed": completed, "total_tasks": total_tasks, "country": country, "kind": "chart", "value": chart, "rows": 0, "errors": len(errors)})
            if args.sleep:
                time.sleep(args.sleep)
        for term in terms:
            try:
                discovered = discover_search(country, term, args.search_limit, args)
                rows.extend(discovered)
                completed += 1
                emit_progress({"completed": completed, "total_tasks": total_tasks, "country": country, "kind": "search", "value": term, "rows": len(discovered), "errors": len(errors)})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                errors.append({"country": country, "kind": "search", "value": term, "error_class": exc.__class__.__name__, "error_message": str(exc)})
                completed += 1
                emit_progress({"completed": completed, "total_tasks": total_tasks, "country": country, "kind": "search", "value": term, "rows": 0, "errors": len(errors)})
            if args.sleep:
                time.sleep(args.sleep)
    if progress_handle:
        progress_handle.close()
    rows = dedupe_rows(rows)
    summary = {
        "rows": len(rows),
        "countries": countries,
        "charts": charts,
        "terms": terms,
        "errors": errors,
    }
    if args.output_csv:
        write_seed_csv(args.output_csv, rows)
        summary["output_csv"] = args.output_csv
    if args.db:
        summary["db_import"] = queue_store.import_seeds(args.db, rows)
        summary["db"] = args.db
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover fresh App Store seeds from Apple public APIs.")
    parser.add_argument("--countries", required=True)
    parser.add_argument("--charts", default="top-free,top-paid")
    parser.add_argument("--chart-limit", type=int, default=200)
    parser.add_argument("--term", action="append", default=[])
    parser.add_argument("--search-limit", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--progress-every", type=int, default=0)
    parser.add_argument("--progress-jsonl", default=None)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--summary-json", default=None)
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
