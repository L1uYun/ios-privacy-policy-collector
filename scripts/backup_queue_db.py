#!/usr/bin/env python3
"""Create an online SQLite backup of the hot queue database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_event(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"created_at": utc_now(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def backup_database(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
    if tmp_destination.exists():
        tmp_destination.unlink()

    started = time.monotonic()
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    src = sqlite3.connect(source_uri, uri=True, timeout=30)
    dst = sqlite3.connect(tmp_destination, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp_destination.replace(destination)
    elapsed_seconds = time.monotonic() - started
    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": destination.stat().st_size,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def prune_backups(backup_dir: Path, keep: int) -> list[str]:
    if keep <= 0:
        return []
    backups = sorted(
        backup_dir.glob("queue-*.sqlite"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for path in backups[keep:]:
        path.unlink()
        removed.append(str(path))
    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Online backup for the queue SQLite database.")
    parser.add_argument("--db", required=True, help="Source hot queue SQLite path.")
    parser.add_argument("--backup-dir", required=True, help="Destination directory for timestamped backups.")
    parser.add_argument("--event-log", default=None, help="Optional JSONL event log path.")
    parser.add_argument("--keep", type=int, default=12, help="Number of timestamped backups to retain.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.db)
    backup_dir = Path(args.backup_dir)
    event_log = Path(args.event_log) if args.event_log else None

    if not source.exists():
        raise SystemExit(f"source database does not exist: {source}")

    destination = backup_dir / f"queue-{utc_stamp()}.sqlite"
    try:
        report = backup_database(source, destination)
        removed = prune_backups(backup_dir, args.keep)
        report["removed"] = removed
        report["status"] = "ok"
        write_event(event_log, {"event": "backup_complete", **report})
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        write_event(event_log, {"event": "backup_failed", "source": str(source), "error": repr(exc)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
