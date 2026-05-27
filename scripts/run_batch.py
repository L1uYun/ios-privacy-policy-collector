#!/usr/bin/env python3
"""Launch multiple queue workers for high-volume policy-cluster collection."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


queue_store = load_script_module("queue_store", SCRIPT_DIR / "queue_store.py")


def build_worker_command(args: argparse.Namespace, worker_index: int, limit: int) -> list[str]:
    worker_id = f"{args.worker_prefix}-{worker_index:03d}"
    command = [
        sys.executable,
        str(SCRIPT_DIR / "queue_worker.py"),
        "--db",
        args.db,
        "--worker-id",
        worker_id,
        "--limit",
        str(limit),
        "--min-policy-chars",
        str(args.min_policy_chars),
        "--output-dir",
        args.output_dir,
        "--jsonl",
        str(Path(args.log_dir) / f"{worker_id}.jsonl"),
        "--timeout",
        str(args.timeout),
        "--fallback-timeout",
        str(args.fallback_timeout),
        "--max-attempts",
        str(args.max_attempts),
        "--cluster-max-depth",
        str(args.cluster_max_depth),
        "--cluster-max-docs",
        str(args.cluster_max_docs),
        "--cluster-min-chars",
        str(args.cluster_min_chars),
        "--js-timeout",
        str(args.js_timeout),
        "--js-wait-ms",
        str(args.js_wait_ms),
    ]
    if args.proxy:
        command.extend(["--proxy", args.proxy])
    if args.active_only:
        command.append("--active-only")
    if args.countries:
        command.extend(["--countries", args.countries])
    if args.sources:
        command.extend(["--sources", args.sources])
    if args.claim_order:
        command.extend(["--claim-order", args.claim_order])
    if args.try_common_paths:
        command.append("--try-common-paths")
    if args.enrich_lookup:
        command.append("--enrich-lookup")
    if args.collect_cluster:
        command.append("--collect-cluster")
    if args.cluster_probe_common_paths:
        command.append("--cluster-probe-common-paths")
    if args.js_fallback:
        command.append("--js-fallback")
    return command


def summarize(db_path: str | Path) -> dict:
    stats = queue_store.stats(db_path)
    with queue_store.connect(db_path) as conn:
        root_methods = {
            row["policy_fetch_method"] or "unknown": row["count"]
            for row in conn.execute(
                """
                select policy_fetch_method, count(*) as count
                from policy_document
                group by policy_fetch_method
                """
            )
        }
        attempt_statuses = {
            row["status"] or "unknown": row["count"]
            for row in conn.execute(
                """
                select status, count(*) as count
                from policy_url_attempt
                group by status
                """
            )
        }
        countries = {
            f"{row['country']}:{row['status']}": row["count"]
            for row in conn.execute(
                """
                select country, status, count(*) as count
                from policy_fetch
                group by country, status
                order by country, status
                """
            )
        }
    return {
        "stats": stats,
        "root_policy_fetch_method": root_methods,
        "policy_url_attempt_status": attempt_statuses,
        "country_status": countries,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multiple queue workers for policy-cluster collection.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--output-dir", default=str(queue_store.default_data_root() / "out"))
    parser.add_argument("--log-dir", default=str(queue_store.default_data_root() / "logs"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit-per-worker", type=int, default=250)
    parser.add_argument("--worker-prefix", default="batch")
    parser.add_argument("--active-only", action="store_true", help="Claim only seeds validated as active by iTunes lookup.")
    parser.add_argument("--countries", default=None, help="Comma-separated storefront countries to claim.")
    parser.add_argument("--sources", default=None, help="Comma-separated seed sources to claim. A trailing * means prefix match.")
    parser.add_argument("--claim-order", choices=["oldest", "newest"], default="oldest")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--fallback-timeout", type=int, default=6)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--min-policy-chars", type=int, default=1000)
    parser.add_argument("--try-common-paths", action="store_true", default=True)
    parser.add_argument("--enrich-lookup", action="store_true", default=True)
    parser.add_argument("--collect-cluster", action="store_true", default=True)
    parser.add_argument("--cluster-probe-common-paths", action="store_true", default=True)
    parser.add_argument("--cluster-max-depth", type=int, default=1)
    parser.add_argument("--cluster-max-docs", type=int, default=8)
    parser.add_argument("--cluster-min-chars", type=int, default=200)
    parser.add_argument("--js-fallback", action="store_true", default=True)
    parser.add_argument("--js-timeout", type=int, default=15)
    parser.add_argument("--js-wait-ms", type=int, default=1000)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--summary-json", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue_store.init_db(args.db)
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary_json) if args.summary_json else Path(args.log_dir) / "batch-summary.json"
    commands = [build_worker_command(args, index + 1, args.limit_per_worker) for index in range(args.workers)]
    if args.dry_run:
        print(json.dumps({"commands": commands, "summary": summarize(args.db)}, ensure_ascii=False, indent=2))
        return 0

    started_at = time.time()
    processes = [subprocess.Popen(command) for command in commands]
    while any(process.poll() is None for process in processes):
        report = summarize(args.db)
        report["elapsed_seconds"] = round(time.time() - started_at, 1)
        summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        time.sleep(args.poll_seconds)
    exit_codes = [process.wait() for process in processes]
    report = summarize(args.db)
    report["elapsed_seconds"] = round(time.time() - started_at, 1)
    report["worker_exit_codes"] = exit_codes
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if all(code == 0 for code in exit_codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
