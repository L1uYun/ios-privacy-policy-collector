#!/usr/bin/env python3
"""Export validated active App Store seeds into an auditable seed dump."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import queue_store


def parse_sources(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_rows(db_path: str | Path, sources: list[str], status: str, limit: int | None = None) -> list[sqlite3.Row]:
    queue_store.init_db(db_path)
    clauses = ["v.status = ?"]
    params: list[object] = [status]
    if sources:
        placeholders = ",".join("?" for _source in sources)
        clauses.append(f"s.seed_source in ({placeholders})")
        params.extend(sources)
    limit_sql = "limit ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    query = f"""
        select
            s.seed_id,
            s.seed_source,
            s.app_id,
            s.bundle_id as seed_bundle_id,
            s.app_store_url as seed_app_store_url,
            s.country,
            s.observed_at,
            s.provenance_url,
            s.license_note,
            v.status as validation_status,
            v.result_count,
            v.track_id,
            v.bundle_id as lookup_bundle_id,
            v.name,
            v.seller_name,
            v.app_store_url as lookup_app_store_url,
            v.lookup_url,
            v.validated_at
        from seed_validation v
        join app_seed s on s.seed_id = v.seed_id
        where {' and '.join(clauses)}
        order by s.seed_source, s.country, cast(s.app_id as integer), s.seed_id
        {limit_sql}
    """
    with closing(queue_store.connect(db_path)) as conn:
        return conn.execute(query, params).fetchall()


def dump_row(row: sqlite3.Row) -> dict:
    return {
        "seed_id": row["seed_id"],
        "seed_source": row["seed_source"],
        "country": row["country"],
        "app_id": row["app_id"],
        "track_id": row["track_id"] or row["app_id"],
        "bundle_id": row["lookup_bundle_id"] or row["seed_bundle_id"] or "",
        "name": row["name"] or "",
        "seller_name": row["seller_name"] or "",
        "app_store_url": row["lookup_app_store_url"] or row["seed_app_store_url"] or "",
        "validation_status": row["validation_status"],
        "result_count": row["result_count"],
        "observed_at": row["observed_at"],
        "validated_at": row["validated_at"],
        "provenance_url": row["provenance_url"] or "",
        "lookup_url": row["lookup_url"] or "",
        "license_note": row["license_note"] or "",
    }


def write_csv(path: str | Path, rows: list[dict]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "seed_source",
        "country",
        "app_id",
        "track_id",
        "bundle_id",
        "name",
        "seller_name",
        "app_store_url",
        "validation_status",
        "result_count",
        "observed_at",
        "validated_at",
        "provenance_url",
        "lookup_url",
        "license_note",
        "seed_id",
    ]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def summarize(rows: list[dict]) -> dict:
    summary = {
        "rows": len(rows),
        "sources": {},
        "countries": {},
    }
    unique_apps = set()
    for row in rows:
        unique_apps.add(row["app_id"])
        source = row["seed_source"]
        country = row["country"]
        summary["sources"][source] = summary["sources"].get(source, 0) + 1
        summary["countries"][country] = summary["countries"].get(country, 0) + 1
    summary["unique_app_ids"] = len(unique_apps)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export validated active App Store seed dump.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--sources", default="commoncrawl-appstore-url")
    parser.add_argument("--status", default="active")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--summary-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = [dump_row(row) for row in load_rows(args.db, parse_sources(args.sources), args.status, args.limit)]
    write_csv(args.output_csv, rows)
    summary = summarize(rows)
    summary.update(
        {
            "db": args.db,
            "sources_filter": parse_sources(args.sources),
            "status_filter": args.status,
            "output_csv": args.output_csv,
        }
    )
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
