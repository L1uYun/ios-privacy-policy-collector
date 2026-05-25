#!/usr/bin/env python3
"""Validate queued App Store seeds through the iTunes lookup endpoint."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ios_privacy_policy_collector as collector
import queue_store


def utc_now() -> str:
    return queue_store.utc_now()


def load_seed_rows(
    db_path: str | Path,
    limit: int | None,
    country: str | None,
    seed_source: str | None,
    include_validated: bool,
    start_after_seed_id: int | None = None,
) -> list[sqlite3.Row]:
    queue_store.init_db(db_path)
    clauses = []
    params: list[object] = []
    join_validation = ""
    if not include_validated:
        join_validation = "left join seed_validation v on v.seed_id = s.seed_id"
        clauses.append("v.seed_id is null")
    if country:
        clauses.append("s.country = ?")
        params.append(country.lower())
    if seed_source:
        clauses.append("s.seed_source = ?")
        params.append(seed_source)
    if start_after_seed_id is not None:
        clauses.append("s.seed_id > ?")
        params.append(start_after_seed_id)
    where_sql = f"where {' and '.join(clauses)}" if clauses else ""
    limit_sql = "limit ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    query = f"""
        select s.seed_id, s.seed_source, s.app_id, s.bundle_id, s.app_store_url, s.country
        from app_seed s
        {join_validation}
        {where_sql}
        order by s.seed_id
        {limit_sql}
    """
    with closing(queue_store.connect(db_path)) as conn:
        return conn.execute(query, params).fetchall()


def classify_payload(seed: sqlite3.Row | dict, payload: dict, lookup_url: str, zero_result_status: str) -> dict:
    app_id = str(seed["app_id"] or "").strip()
    result_count = int(payload.get("resultCount") or 0)
    results = payload.get("results") or []
    if not app_id:
        return {
            "status": "missing",
            "result_count": result_count,
            "lookup_url": lookup_url,
            "raw_json": payload,
            "error_class": "MissingAppId",
            "error_message": "seed row has no app_id",
        }
    if result_count <= 0 or not results:
        return {
            "status": zero_result_status,
            "result_count": result_count,
            "lookup_url": lookup_url,
            "raw_json": payload,
        }
    record = next(collector.records_from_itunes_payload(payload, "seed-validation"), None)
    if record is None:
        return {
            "status": "lookup_error",
            "result_count": result_count,
            "lookup_url": lookup_url,
            "raw_json": payload,
            "error_class": "UnexpectedPayload",
            "error_message": "lookup response had results but no trackId",
        }
    return {
        "status": "active",
        "result_count": result_count,
        "track_id": record.app_id,
        "bundle_id": record.bundle_id,
        "name": record.name,
        "seller_name": record.seller_name,
        "app_store_url": record.app_store_url,
        "lookup_url": lookup_url,
        "raw_json": payload,
    }


def validate_seed(seed: sqlite3.Row | dict, args: argparse.Namespace) -> dict:
    app_id = str(seed["app_id"] or "").strip()
    country = str(seed["country"] or args.country or "us").lower()
    lookup_url = collector.itunes_lookup_url(app_id=app_id, country=country)
    if not app_id:
        return classify_payload(seed, {"resultCount": 0, "results": []}, lookup_url, args.zero_result_status)
    try:
        payload = collector.request_json_with_proxy(
            lookup_url,
            timeout=args.timeout,
            user_agent=args.user_agent,
            proxy=args.proxy,
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        http_status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        return {
            "status": "lookup_error",
            "result_count": None,
            "http_status": http_status,
            "lookup_url": lookup_url,
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
        }
    return classify_payload(seed, payload, lookup_url, args.zero_result_status)


def batched_lookup_url(app_ids: list[str], country: str) -> str:
    params = {
        "id": ",".join(app_ids),
        "country": country,
        "entity": "software",
    }
    return f"{collector.ITUNES_LOOKUP_URL}?{urllib.parse.urlencode(params)}"


def chunks(rows: list[sqlite3.Row], size: int) -> Iterable[list[sqlite3.Row]]:
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def validate_seed_batch(seeds: list[sqlite3.Row], args: argparse.Namespace) -> list[dict]:
    if not seeds:
        return []
    country = str(seeds[0]["country"] or args.country or "us").lower()
    valid_pairs = [
        (index, seed, str(seed["app_id"] or "").strip())
        for index, seed in enumerate(seeds)
        if str(seed["app_id"] or "").strip()
    ]
    results: list[dict | None] = [None] * len(seeds)
    for index, seed in enumerate(seeds):
        if not str(seed["app_id"] or "").strip():
            lookup_url = collector.itunes_lookup_url(app_id="", country=country)
            results[index] = classify_payload(seed, {"resultCount": 0, "results": []}, lookup_url, args.zero_result_status)
    if not valid_pairs:
        return [result for result in results if result is not None]

    app_ids = [app_id for _index, _seed, app_id in valid_pairs]
    lookup_url = batched_lookup_url(app_ids, country)
    try:
        payload = collector.request_json_with_proxy(
            lookup_url,
            timeout=args.timeout,
            user_agent=args.user_agent,
            proxy=args.proxy,
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        http_status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        for index, _seed, _app_id in valid_pairs:
            results[index] = {
                "status": "lookup_error",
                "result_count": None,
                "http_status": http_status,
                "lookup_url": lookup_url,
                "error_class": exc.__class__.__name__,
                "error_message": str(exc),
            }
        return [result for result in results if result is not None]

    records_by_id = {
        record.app_id: record
        for record in collector.records_from_itunes_payload(payload, "seed-validation")
    }
    result_count = int(payload.get("resultCount") or 0)
    for index, _seed, app_id in valid_pairs:
        record = records_by_id.get(app_id)
        if record is None:
            results[index] = {
                "status": args.zero_result_status,
                "result_count": result_count,
                "lookup_url": lookup_url,
                "raw_json": {"resultCount": result_count},
            }
            continue
        results[index] = {
            "status": "active",
            "result_count": result_count,
            "track_id": record.app_id,
            "bundle_id": record.bundle_id,
            "name": record.name,
            "seller_name": record.seller_name,
            "app_store_url": record.app_store_url,
            "lookup_url": lookup_url,
            "raw_json": record.raw,
        }
    return [result for result in results if result is not None]


def save_validation(conn: sqlite3.Connection, seed: sqlite3.Row | dict, result: dict, validated_at: str) -> None:
    raw_json = result.get("raw_json")
    conn.execute(
        """
        insert into seed_validation(
            seed_id, seed_source, app_id, country, status, result_count,
            http_status, track_id, bundle_id, name, seller_name, app_store_url,
            lookup_url, error_class, error_message, raw_json, validated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(seed_id) do update set
            seed_source = excluded.seed_source,
            app_id = excluded.app_id,
            country = excluded.country,
            status = excluded.status,
            result_count = excluded.result_count,
            http_status = excluded.http_status,
            track_id = excluded.track_id,
            bundle_id = excluded.bundle_id,
            name = excluded.name,
            seller_name = excluded.seller_name,
            app_store_url = excluded.app_store_url,
            lookup_url = excluded.lookup_url,
            error_class = excluded.error_class,
            error_message = excluded.error_message,
            raw_json = excluded.raw_json,
            validated_at = excluded.validated_at
        """,
        (
            seed["seed_id"],
            seed["seed_source"],
            seed["app_id"],
            seed["country"],
            result["status"],
            result.get("result_count"),
            result.get("http_status"),
            result.get("track_id"),
            result.get("bundle_id"),
            result.get("name"),
            result.get("seller_name"),
            result.get("app_store_url"),
            result.get("lookup_url"),
            result.get("error_class"),
            result.get("error_message"),
            json.dumps(raw_json, ensure_ascii=False, sort_keys=True) if raw_json is not None else None,
            validated_at,
        ),
    )
    if result["status"] == "active":
        conn.execute(
            """
            insert into app_metadata(
                app_id, country, bundle_id, name, seller_name, app_store_url,
                seller_url, raw_json, fetched_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(country, app_id) do update set
                bundle_id = excluded.bundle_id,
                name = excluded.name,
                seller_name = excluded.seller_name,
                app_store_url = excluded.app_store_url,
                raw_json = excluded.raw_json,
                fetched_at = excluded.fetched_at
            """,
            (
                seed["app_id"],
                seed["country"],
                result.get("bundle_id"),
                result.get("name"),
                result.get("seller_name"),
                result.get("app_store_url"),
                None,
                json.dumps(raw_json, ensure_ascii=False, sort_keys=True) if raw_json is not None else None,
                validated_at,
            ),
        )


def summarize(results: Iterable[dict]) -> dict:
    summary = {
        "total": 0,
        "active": 0,
        "inactive": 0,
        "missing": 0,
        "lookup_error": 0,
        "by_country": {},
        "by_source": {},
    }
    for row in results:
        status = row["status"]
        country = row["country"]
        source = row["seed_source"]
        summary["total"] += 1
        summary[status] = summary.get(status, 0) + 1
        summary["by_country"].setdefault(country, {"total": 0, "active": 0, "inactive": 0, "missing": 0, "lookup_error": 0})
        summary["by_country"][country]["total"] += 1
        summary["by_country"][country][status] = summary["by_country"][country].get(status, 0) + 1
        summary["by_source"].setdefault(source, {"total": 0, "active": 0, "inactive": 0, "missing": 0, "lookup_error": 0})
        summary["by_source"][source]["total"] += 1
        summary["by_source"][source][status] = summary["by_source"][source].get(status, 0) + 1
    if summary["total"]:
        summary["active_rate"] = summary["active"] / summary["total"]
    else:
        summary["active_rate"] = 0.0
    return summary


def write_csv(path: str | Path, rows: list[dict]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "seed_id",
        "seed_source",
        "country",
        "app_id",
        "status",
        "result_count",
        "http_status",
        "track_id",
        "bundle_id",
        "name",
        "seller_name",
        "app_store_url",
        "lookup_url",
        "error_class",
        "error_message",
    ]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def run(args: argparse.Namespace) -> dict:
    seeds = load_seed_rows(
        db_path=args.db,
        limit=args.limit,
        country=args.country,
        seed_source=args.source,
        include_validated=args.include_validated,
        start_after_seed_id=args.start_after_seed_id,
    )
    validated_at = utc_now()
    result_rows: list[dict] = []
    with closing(queue_store.connect(args.db)) as conn:
        processed = 0
        grouped: dict[str, list[sqlite3.Row]] = {}
        for seed in seeds:
            grouped.setdefault(str(seed["country"]).lower(), []).append(seed)
        for country_rows in grouped.values():
            for batch in chunks(country_rows, max(1, args.batch_size)):
                batch_results = (
                    [validate_seed(seed, args) for seed in batch]
                    if args.batch_size == 1
                    else validate_seed_batch(batch, args)
                )
                for seed, result in zip(batch, batch_results):
                    row = {
                        "seed_id": seed["seed_id"],
                        "seed_source": seed["seed_source"],
                        "country": seed["country"],
                        "app_id": seed["app_id"],
                        **result,
                    }
                    result_rows.append(row)
                    save_validation(conn, seed, result, validated_at)
                conn.commit()
                processed += len(batch)
                if args.progress_every_batch or (
                    args.progress_every and (processed % args.progress_every == 0 or processed == len(seeds))
                ):
                    partial = summarize(result_rows)
                    print(json.dumps({"validated": processed, **partial}, ensure_ascii=False, sort_keys=True), flush=True)
                if args.sleep > 0 and processed < len(seeds):
                    time.sleep(args.sleep)
    summary = summarize(result_rows)
    summary["db"] = str(args.db)
    summary["validated_at"] = validated_at
    if args.output_csv:
        write_csv(args.output_csv, result_rows)
        summary["output_csv"] = str(args.output_csv)
    if args.output_json:
        output_json = Path(args.output_json)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps({"summary": summary, "rows": result_rows}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        summary["output_json"] = str(output_json)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate queued App Store seeds with iTunes lookup.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--country", default=None, help="Validate only one queued country/storefront.")
    parser.add_argument("--source", default=None, help="Validate only one seed_source.")
    parser.add_argument("--include-validated", action="store_true", help="Revalidate seeds already present in seed_validation.")
    parser.add_argument("--start-after-seed-id", type=int, default=None)
    parser.add_argument("--zero-result-status", choices=["inactive", "missing"], default="inactive")
    parser.add_argument("--batch-size", type=int, default=100, help="Number of app IDs per iTunes lookup request. Use 1 for per-seed lookup.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between lookup requests.")
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--progress-every-batch", action="store_true")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-csv", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
