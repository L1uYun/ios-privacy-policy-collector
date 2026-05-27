#!/usr/bin/env python3
"""Freeze a stage progress snapshot for long-running corpus collection."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
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
milestone_status = load_script_module("milestone_status", SCRIPT_DIR / "milestone_status.py")
classify_policy_failures = load_script_module("classify_policy_failures", SCRIPT_DIR / "classify_policy_failures.py")
summarize_commoncrawl_progress = load_script_module(
    "summarize_commoncrawl_progress",
    SCRIPT_DIR / "summarize_commoncrawl_progress.py",
)


DEFAULT_MILESTONES = [10_000, 100_000, 500_000, 1_000_000]


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file_path in path.rglob("*"):
        if file_path.is_file():
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def newest_files(path: Path, pattern: str, limit: int) -> list[Path]:
    if not path.exists():
        return []
    files = [file_path for file_path in path.glob(pattern) if file_path.is_file()]
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)[:limit]


def copy_if_exists(source: Path, target_dir: Path) -> str | None:
    if not source.exists() or not source.is_file():
        return None
    target = target_dir / source.name
    shutil.copy2(source, target)
    return str(target)


def build_snapshot(args: argparse.Namespace, snapshot_dir: Path) -> dict:
    milestones = [int(item.strip()) for item in args.milestones.split(",") if item.strip()] or DEFAULT_MILESTONES
    milestone = milestone_status.collect_status(args.db, milestones)
    failures = classify_policy_failures.build_report(
        args.db,
        args.sample_limit,
        countries=[item.strip().lower() for item in args.countries.split(",") if item.strip()] if args.countries else None,
        sources=[item.strip() for item in args.sources.split(",") if item.strip()] if args.sources else None,
    )

    copied_files: list[str] = []
    cc_progress = None
    progress_path = Path(args.commoncrawl_progress_jsonl) if args.commoncrawl_progress_jsonl else None
    if progress_path and progress_path.exists():
        events = summarize_commoncrawl_progress.load_events(progress_path)
        cc_progress = summarize_commoncrawl_progress.summarize(events, args.commoncrawl_total_shards)
        (snapshot_dir / "commoncrawl-progress-summary.json").write_text(
            json.dumps(cc_progress, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        copied = copy_if_exists(progress_path, snapshot_dir)
        if copied:
            copied_files.append(copied)

    log_dir = Path(args.policy_batch_log_dir)
    for source in newest_files(log_dir, "*.summary.json", args.copy_recent_summaries):
        copied = copy_if_exists(source, snapshot_dir / "batch-summaries")
        if copied:
            copied_files.append(copied)

    data_root = Path(args.data_root)
    disk = {
        "data_root": str(data_root),
        "data_root_bytes": directory_size(data_root),
        "policy_clusters_bytes": directory_size(data_root / "policy-clusters"),
        "policy_batch_logs_bytes": directory_size(data_root / "policy-batch-bg"),
        "commoncrawl_prefix_bytes": directory_size(data_root / "commoncrawl-prefix-bg"),
        "queue_sqlite_bytes": Path(args.db).stat().st_size if Path(args.db).exists() else 0,
    }

    (snapshot_dir / "milestone-status.json").write_text(
        json.dumps(milestone, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (snapshot_dir / "policy-failure-classification.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (snapshot_dir / "disk-usage.json").write_text(
        json.dumps(disk, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot_dir": str(snapshot_dir),
        "milestone": milestone["stage_plan"],
        "stats": milestone["stats"],
        "failure_category_counts": failures["category_counts"],
        "failure_seed_source_counts": failures["seed_source_counts"],
        "commoncrawl_progress": cc_progress,
        "disk_usage": disk,
        "copied_files": copied_files,
    }


def build_parser() -> argparse.ArgumentParser:
    data_root = queue_store.default_data_root()
    parser = argparse.ArgumentParser(description="Freeze current staged corpus progress into an auditable snapshot.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--data-root", default=str(data_root))
    parser.add_argument("--snapshot-root", default=str(data_root / "stage-snapshots"))
    parser.add_argument("--name", default=None)
    parser.add_argument("--milestones", default="10000,100000,500000,1000000")
    parser.add_argument("--sample-limit", type=int, default=30)
    parser.add_argument("--countries", default=None)
    parser.add_argument("--sources", default=None)
    parser.add_argument(
        "--commoncrawl-progress-jsonl",
        default=str(data_root / "commoncrawl-prefix-bg" / "wave1-us-gb-jp-de-fr-cn-in-2char.progress.jsonl"),
    )
    parser.add_argument("--commoncrawl-total-shards", type=int, default=4732)
    parser.add_argument("--policy-batch-log-dir", default=str(data_root / "policy-batch-bg"))
    parser.add_argument("--copy-recent-summaries", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot_name = args.name or f"stage-{utc_stamp()}"
    snapshot_dir = Path(args.snapshot_root) / snapshot_name
    (snapshot_dir / "batch-summaries").mkdir(parents=True, exist_ok=True)
    report = build_snapshot(args, snapshot_dir)
    manifest_path = snapshot_dir / "snapshot-manifest.json"
    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
