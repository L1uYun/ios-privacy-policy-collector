#!/usr/bin/env python3
"""Ensure every accepted policy document has at least its source policy URL as an auditable link."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def ensure_links(db_path: str | Path, dry_run: bool) -> dict:
    added: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select d.document_id, d.app_id, d.country, d.policy_url, count(l.link_id) as links_count
            from policy_document d
            left join policy_link l on l.document_id = d.document_id
            group by d.document_id
            having links_count = 0
            order by d.document_id
            """
        ).fetchall()
        for row in rows:
            item = {
                "document_id": row["document_id"],
                "app_id": row["app_id"],
                "country": row["country"],
                "policy_url": row["policy_url"],
                "dry_run": dry_run,
            }
            added.append(item)
            if not dry_run:
                conn.execute(
                    "insert into policy_link(document_id, text, url) values (?, ?, ?)",
                    (row["document_id"], row["policy_url"], row["policy_url"]),
                )
        if not dry_run:
            conn.commit()
    return {
        "db": str(db_path),
        "dry_run": dry_run,
        "added_count": len(added),
        "added": added,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure accepted policies retain a source policy URL link.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    result = ensure_links(args.db, args.dry_run)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
