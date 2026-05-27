#!/usr/bin/env python3
"""Run Common Crawl App Store discovery across app slug prefix shards."""

from __future__ import annotations

import argparse
import itertools
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


DEFAULT_PREFIXES = "0,1,2,3,4,5,6,7,8,9,a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z"
DEFAULT_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def parse_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def safe_prefix_name(prefix: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in prefix) or "root"


def parse_int_csv(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def generate_prefixes(alphabet: str, lengths: str) -> list[str]:
    chars = [char.lower() for char in alphabet if char.strip()]
    prefixes: list[str] = []
    for length in parse_int_csv(lengths):
        for combo in itertools.product(chars, repeat=length):
            prefixes.append("".join(combo))
    return prefixes


def load_completed_progress_shards(path: str | Path | None) -> set[tuple[str, str]]:
    if not path:
        return set()
    progress_path = Path(path)
    if not progress_path.exists():
        return set()
    completed: set[tuple[str, str]] = set()
    with progress_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("status") != "ok":
                continue
            country = event.get("country")
            prefix = event.get("prefix")
            if country is not None and prefix is not None:
                completed.add((str(country).lower(), str(prefix).lower()))
    return completed


def emit_progress(path: str | Path | None, event: dict) -> None:
    if not path:
        return
    progress_path = Path(path)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def discover_slug_prefix(args: argparse.Namespace, index: str, country: str, prefix: str, server_limit: int) -> dict:
    shard = f"{country}-{safe_prefix_name(prefix)}"
    shard_args = argparse.Namespace(
        index=index,
        url_pattern=f"apps.apple.com/{country}/app/{prefix}",
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
        output_csv=str(Path(args.output_dir) / f"commoncrawl-appstore-{shard}-{args.limit}.csv"),
        db=args.db,
    )
    query_url, rows = discovery.discover_rows(shard_args)
    result = {
        "country": country,
        "prefix": prefix,
        "status": "ok",
        "index": index,
        "server_limit": server_limit,
        "query_url": query_url,
        "rows": len(rows),
        "output_csv": shard_args.output_csv,
    }
    result["output_csv_rows"] = discovery.write_seed_csv(shard_args.output_csv, rows)
    if args.db:
        result["db_import"] = queue_store.import_seeds(args.db, rows)
    return result


def run(args: argparse.Namespace) -> dict:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    index = args.index
    if index == "latest":
        index = discovery.latest_index(args.timeout, args.user_agent, args.proxy)
    countries = parse_csv(args.countries)
    prefixes = generate_prefixes(args.prefix_alphabet, args.prefix_lengths) if args.prefix_lengths else parse_csv(args.prefixes)
    completed_shards = load_completed_progress_shards(args.progress_jsonl) if args.resume_progress else set()
    fallback_limits = [int(value) for value in str(args.fallback_server_limits).split(",") if value.strip()]
    server_limits = [args.server_limit or args.limit] + fallback_limits
    results = []
    for country in countries:
        for prefix in prefixes:
            if args.max_shards is not None and len(results) >= args.max_shards:
                break
            if (country, prefix) in completed_shards:
                shard_result = {
                    "country": country,
                    "prefix": prefix,
                    "status": "skipped",
                    "index": index,
                    "reason": "already completed in progress log",
                }
                results.append(shard_result)
                continue
            shard_result = None
            for attempt, server_limit in enumerate(server_limits[: args.retries + 1], start=1):
                try:
                    shard_result = discover_slug_prefix(args, index, country, prefix, server_limit)
                    shard_result["attempt"] = attempt
                    break
                except Exception as exc:  # noqa: BLE001 - discovery should continue across shards.
                    shard_result = {
                        "country": country,
                        "prefix": prefix,
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
            results.append(shard_result)
            emit_progress(args.progress_jsonl, shard_result)
            print(json.dumps(shard_result, ensure_ascii=False, sort_keys=True), flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)
        if args.max_shards is not None and len(results) >= args.max_shards:
            break

    summary = {
        "index": index,
        "countries": len(countries),
        "prefixes": len(prefixes),
        "shards": len(results),
        "ok_shards": sum(1 for row in results if row["status"] == "ok"),
        "skipped_shards": sum(1 for row in results if row["status"] == "skipped"),
        "error_shards": sum(1 for row in results if row["status"] == "error"),
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
    parser = argparse.ArgumentParser(description="Discover App Store seeds from Common Crawl app slug prefix shards.")
    parser.add_argument("--countries", required=True)
    parser.add_argument("--prefixes", default=DEFAULT_PREFIXES)
    parser.add_argument("--prefix-alphabet", default=DEFAULT_ALPHABET)
    parser.add_argument("--prefix-lengths", default=None, help="Generate prefix shards from --prefix-alphabet, for example '1,2'. Overrides --prefixes.")
    parser.add_argument("--max-shards", type=int, default=None)
    parser.add_argument("--index", default="latest")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--server-limit", type=int, default=None)
    parser.add_argument("--fallback-server-limits", default="1000,500")
    parser.add_argument("--source", default="commoncrawl-appstore-url")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--output-dir", default=str(queue_store.default_data_root()))
    parser.add_argument("--summary-json", default=None)
    parser.add_argument("--progress-jsonl", default=None)
    parser.add_argument("--resume-progress", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--backend", choices=["urllib", "requests"], default="requests")
    parser.add_argument("--user-agent", default=discovery.collector.DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--sleep", type=float, default=0.05)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, sort_keys=True))
    return 0 if summary["error_shards"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
