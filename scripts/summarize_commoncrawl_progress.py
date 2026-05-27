#!/usr/bin/env python3
"""Summarize Common Crawl prefix discovery progress JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_events(path: str | Path) -> list[dict]:
    events: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def summarize(events: list[dict], total_shards: int | None = None) -> dict:
    latest_by_shard: dict[tuple[str, str], dict] = {}
    for event in events:
        country = event.get("country")
        prefix = event.get("prefix")
        if country is not None and prefix is not None:
            latest_by_shard[(str(country), str(prefix))] = event
    current_events = list(latest_by_shard.values())
    rows = sum(int(event.get("rows") or 0) for event in current_events if event.get("status") == "ok")
    inserted = sum(int((event.get("db_import") or {}).get("inserted") or 0) for event in current_events)
    duplicates = sum(int((event.get("db_import") or {}).get("duplicates") or 0) for event in current_events)
    by_country: dict[str, dict[str, int]] = {}
    for event in current_events:
        country = str(event.get("country") or "")
        if not country:
            continue
        bucket = by_country.setdefault(country, {"shards": 0, "rows": 0, "inserted": 0, "errors": 0})
        bucket["shards"] += 1
        bucket["rows"] += int(event.get("rows") or 0)
        bucket["inserted"] += int((event.get("db_import") or {}).get("inserted") or 0)
        if event.get("status") == "error":
            bucket["errors"] += 1
    summary = {
        "events": len(events),
        "unique_shards": len(current_events),
        "ok_shards": sum(1 for event in current_events if event.get("status") == "ok"),
        "error_shards": sum(1 for event in current_events if event.get("status") == "error"),
        "skipped_shards": sum(1 for event in current_events if event.get("status") == "skipped"),
        "rows": rows,
        "inserted": inserted,
        "duplicates": duplicates,
        "by_country": by_country,
        "last_event": events[-1] if events else None,
    }
    if total_shards:
        summary["total_shards"] = total_shards
        summary["remaining_shards"] = max(total_shards - len(events), 0)
        summary["completion_rate"] = len(events) / total_shards
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize Common Crawl prefix progress JSONL.")
    parser.add_argument("--progress-jsonl", required=True)
    parser.add_argument("--total-shards", type=int, default=None)
    parser.add_argument("--output-json", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = summarize(load_events(args.progress_jsonl), args.total_shards)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        Path(args.output_json).write_text(text + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
