#!/usr/bin/env python3
"""Print a compact stage-1 crawl status for operator check-ins."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import queue_store


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0] or 0)


def live_wave_progress(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    completed = 0
    total = None
    errors = 0
    last_event = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            last_event = event
            completed = max(completed, int(event.get("completed") or 0))
            if event.get("total_tasks") is not None:
                total = int(event["total_tasks"])
            errors = max(errors, int(event.get("errors") or 0))
    return {
        "exists": True,
        "completed_tasks": completed,
        "total_tasks": total,
        "errors": errors,
        "last_country": last_event.get("country") if last_event else None,
        "last_value": last_event.get("value") if last_event else None,
    }


def source_yield(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        select
            s.seed_source,
            sum(case when f.status = 'ok' then 1 else 0 end) as ok_fetches,
            sum(case when f.status = 'permanent_error' then 1 else 0 end) as permanent_error_fetches,
            sum(case when f.status = 'pending' then 1 else 0 end) as pending_fetches,
            sum(case when f.status = 'running' then 1 else 0 end) as running_fetches
        from policy_fetch f
        join app_seed s on s.seed_id = f.seed_id
        group by s.seed_source
        order by ok_fetches desc, pending_fetches desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        terminal = int(row["ok_fetches"] or 0) + int(row["permanent_error_fetches"] or 0)
        result.append(
            {
                "seed_source": row["seed_source"],
                "ok": int(row["ok_fetches"] or 0),
                "permanent_error": int(row["permanent_error_fetches"] or 0),
                "pending": int(row["pending_fetches"] or 0),
                "running": int(row["running_fetches"] or 0),
                "terminal_success_rate": round(int(row["ok_fetches"] or 0) / terminal, 4) if terminal else None,
            }
        )
    return result


def build_status(args: argparse.Namespace) -> dict:
    queue_store.init_db(args.db)
    with queue_store.connect(args.db) as conn:
        policy_documents = fetch_one(conn, "select count(*) from policy_document")
        status = {
            "policy_documents": policy_documents,
            "target_policy_documents": args.target,
            "remaining_policy_documents": max(0, args.target - policy_documents),
            "fetch_status": {
                "pending": fetch_one(conn, "select count(*) from policy_fetch where status = 'pending'"),
                "running": fetch_one(conn, "select count(*) from policy_fetch where status = 'running'"),
                "ok": fetch_one(conn, "select count(*) from policy_fetch where status = 'ok'"),
                "permanent_error": fetch_one(conn, "select count(*) from policy_fetch where status = 'permanent_error'"),
            },
            "seed_status": {
                "seed_rows": fetch_one(conn, "select count(*) from app_seed"),
                "active_validations": fetch_one(conn, "select count(*) from seed_validation where status = 'active'"),
                "inactive_validations": fetch_one(conn, "select count(*) from seed_validation where status = 'inactive'"),
                "lookup_errors": fetch_one(conn, "select count(*) from seed_validation where status = 'lookup_error'"),
            },
            "top_source_yield": source_yield(conn, args.source_limit),
        }
    if args.live_progress:
        status["live_wave_progress"] = live_wave_progress(Path(args.live_progress))
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print compact stage-1 crawl status.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--target", type=int, default=10000)
    parser.add_argument("--source-limit", type=int, default=12)
    parser.add_argument("--live-progress", default=None)
    args = parser.parse_args(argv)
    print(json.dumps(build_status(args), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
