#!/usr/bin/env python3
"""SQLite queue storage for large iOS privacy-policy collection runs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_data_root() -> Path:
    if os.name == "nt":
        return Path(r"F:\ios-privacy-policy-collector\data")
    return Path("data")


def default_db_path() -> Path:
    return default_data_root() / "queue.sqlite"


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 30000")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        init_db(path)
        return connect(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 30000")
    return conn


def init_db(db_path: str | Path) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute("pragma journal_mode = wal")
        conn.executescript(
            """
            create table if not exists schema_meta (
                key text primary key,
                value text not null
            );

            create table if not exists app_seed (
                seed_id integer primary key,
                seed_source text not null,
                app_id text not null,
                bundle_id text,
                app_store_url text,
                country text not null default 'us',
                observed_at text not null,
                provenance_url text,
                license_note text,
                created_at text not null,
                unique(seed_source, country, app_id)
            );

            create table if not exists app_metadata (
                metadata_id integer primary key,
                app_id text not null,
                country text not null,
                bundle_id text,
                name text,
                seller_name text,
                app_store_url text,
                seller_url text,
                raw_json text,
                fetched_at text not null,
                unique(country, app_id)
            );

            create table if not exists seed_validation (
                validation_id integer primary key,
                seed_id integer not null references app_seed(seed_id) on delete cascade,
                seed_source text not null,
                app_id text not null,
                country text not null,
                status text not null,
                result_count integer,
                http_status integer,
                track_id text,
                bundle_id text,
                name text,
                seller_name text,
                app_store_url text,
                lookup_url text,
                error_class text,
                error_message text,
                raw_json text,
                validated_at text not null,
                unique(seed_id)
            );

            create table if not exists policy_url_candidate (
                candidate_id integer primary key,
                app_id text not null,
                country text not null,
                policy_url text not null,
                canonical_policy_url text,
                method text,
                evidence text,
                discovered_at text not null,
                unique(country, app_id, policy_url)
            );

            create table if not exists policy_fetch (
                fetch_id integer primary key,
                seed_id integer not null references app_seed(seed_id) on delete cascade,
                app_id text not null,
                country text not null,
                status text not null default 'pending',
                attempts integer not null default 0,
                worker_id text,
                locked_at text,
                next_attempt_at text,
                last_error_class text,
                last_error_message text,
                created_at text not null,
                updated_at text not null,
                unique(seed_id)
            );

            create table if not exists policy_document (
                document_id integer primary key,
                fetch_id integer not null references policy_fetch(fetch_id) on delete cascade,
                app_id text not null,
                country text not null,
                policy_url text not null,
                canonical_policy_url text,
                policy_text_sha256 text,
                policy_text_chars integer,
                policy_markdown_path text,
                policy_html_path text,
                policy_text_path text,
                policy_fetch_method text,
                policy_cluster_manifest_path text,
                policy_cluster_nodes_count integer,
                policy_cluster_edges_count integer,
                policy_cluster_errors_count integer,
                created_at text not null,
                unique(fetch_id)
            );

            create table if not exists policy_link (
                link_id integer primary key,
                document_id integer not null references policy_document(document_id) on delete cascade,
                text text,
                url text not null
            );

            create table if not exists policy_url_attempt (
                attempt_id integer primary key,
                fetch_id integer not null references policy_fetch(fetch_id) on delete cascade,
                app_id text not null,
                country text not null,
                attempt_index integer not null,
                policy_url text not null,
                canonical_policy_url text,
                source text,
                status text not null,
                fetch_method text,
                text_chars integer,
                quality text,
                quality_reason text,
                error_class text,
                error_message text,
                created_at text not null,
                unique(fetch_id, attempt_index, policy_url)
            );

            create table if not exists run_event (
                event_id integer primary key,
                event_type text not null,
                message text,
                payload_json text,
                created_at text not null
            );

            create index if not exists idx_policy_fetch_status_next
                on policy_fetch(status, next_attempt_at, fetch_id);
            create index if not exists idx_policy_document_hash
                on policy_document(policy_text_sha256);
            create index if not exists idx_seed_validation_status
                on seed_validation(status, country, seed_source);
            create index if not exists idx_policy_url_candidate_url
                on policy_url_candidate(canonical_policy_url);
            create index if not exists idx_policy_url_attempt_fetch
                on policy_url_attempt(fetch_id, attempt_index);
            """
        )
        conn.execute(
            "insert or replace into schema_meta(key, value) values('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        for column_name, column_type in {
            "policy_cluster_manifest_path": "text",
            "policy_cluster_nodes_count": "integer",
            "policy_cluster_edges_count": "integer",
            "policy_cluster_errors_count": "integer",
            "policy_fetch_method": "text",
        }.items():
            existing_columns = {
                row["name"]
                for row in conn.execute("pragma table_info(policy_document)").fetchall()
            }
            if column_name not in existing_columns:
                conn.execute(f"alter table policy_document add column {column_name} {column_type}")
        conn.commit()


def save_policy_url_attempts(conn: sqlite3.Connection, fetch_id: int, attempts: Iterable[dict], now: str) -> None:
    fetch = conn.execute(
        "select app_id, country from policy_fetch where fetch_id = ?",
        (fetch_id,),
    ).fetchone()
    if fetch is None:
        raise ValueError(f"unknown fetch_id: {fetch_id}")
    conn.execute("delete from policy_url_attempt where fetch_id = ?", (fetch_id,))
    for index, attempt in enumerate(attempts):
        policy_url = attempt.get("policy_url")
        if not policy_url:
            continue
        conn.execute(
            """
            insert or replace into policy_url_attempt(
                fetch_id, app_id, country, attempt_index, policy_url,
                canonical_policy_url, source, status, fetch_method, text_chars,
                quality, quality_reason, error_class, error_message, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fetch_id,
                fetch["app_id"],
                fetch["country"],
                int(attempt.get("attempt_index", index)),
                policy_url,
                attempt.get("canonical_policy_url") or policy_url,
                attempt.get("source"),
                attempt.get("status") or "unknown",
                attempt.get("fetch_method"),
                attempt.get("text_chars"),
                attempt.get("quality"),
                attempt.get("quality_reason"),
                attempt.get("error_class"),
                attempt.get("error_message"),
                now,
            ),
        )


def import_seeds(db_path: str | Path, rows: Iterable[dict]) -> dict[str, int]:
    init_db(db_path)
    inserted = 0
    duplicates = 0
    now = utc_now()
    with closing(connect(db_path)) as conn:
        for row in rows:
            app_id = str(row.get("app_id") or "").strip()
            if not app_id:
                continue
            seed_source = str(row.get("seed_source") or "manual").strip()
            country = str(row.get("country") or "us").strip().lower()
            observed_at = str(row.get("observed_at") or now)
            params = (
                seed_source,
                app_id,
                row.get("bundle_id"),
                row.get("app_store_url"),
                country,
                observed_at,
                row.get("provenance_url"),
                row.get("license_note"),
                now,
            )
            cursor = conn.execute(
                """
                insert or ignore into app_seed(
                    seed_source, app_id, bundle_id, app_store_url, country,
                    observed_at, provenance_url, license_note, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            if cursor.rowcount:
                inserted += 1
                seed_id = cursor.lastrowid
                conn.execute(
                    """
                    insert or ignore into policy_fetch(
                        seed_id, app_id, country, status, created_at, updated_at
                    ) values (?, ?, ?, 'pending', ?, ?)
                    """,
                    (seed_id, app_id, country, now, now),
                )
            else:
                duplicates += 1
        conn.commit()
    return {"inserted": inserted, "duplicates": duplicates}


def claim_next_fetch(
    db_path: str | Path,
    worker_id: str,
    active_only: bool = False,
    countries: list[str] | None = None,
    sources: list[str] | None = None,
    claim_order: str = "oldest",
) -> dict | None:
    now = utc_now()
    clauses = ["f.status = 'pending'", "(f.next_attempt_at is null or f.next_attempt_at <= ?)"]
    params: list[object] = [now]
    join_validation = ""
    if active_only:
        join_validation = "join seed_validation v on v.seed_id = f.seed_id and v.status = 'active'"
    if countries:
        placeholders = ",".join("?" for _country in countries)
        clauses.append(f"f.country in ({placeholders})")
        params.extend(country.lower() for country in countries)
    if sources:
        source_clauses = []
        for source in sources:
            if source.endswith("*"):
                source_clauses.append("s.seed_source like ?")
                params.append(source[:-1] + "%")
            else:
                source_clauses.append("s.seed_source = ?")
                params.append(source)
        clauses.append("(" + " or ".join(source_clauses) + ")")
    order_sql = "f.fetch_id desc" if claim_order == "newest" else "f.fetch_id"
    with closing(connect(db_path)) as conn:
        conn.execute("begin immediate")
        row = conn.execute(
            f"""
            select f.fetch_id, f.seed_id, f.app_id, f.country, f.attempts,
                   s.bundle_id, s.app_store_url, s.seed_source
            from policy_fetch f
            join app_seed s on s.seed_id = f.seed_id
            {join_validation}
            where {' and '.join(clauses)}
            order by {order_sql}
            limit 1
            """,
            params,
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            update policy_fetch
            set status = 'running',
                worker_id = ?,
                locked_at = ?,
                attempts = attempts + 1,
                updated_at = ?
            where fetch_id = ?
            """,
            (worker_id, now, now, row["fetch_id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["attempts"] = int(row["attempts"]) + 1
        return claimed


def claim_fetch_by_id(db_path: str | Path, fetch_id: int, worker_id: str) -> dict | None:
    now = utc_now()
    with closing(connect(db_path)) as conn:
        conn.execute("begin immediate")
        row = conn.execute(
            """
            select f.fetch_id, f.seed_id, f.app_id, f.country, f.attempts,
                   s.bundle_id, s.app_store_url, s.seed_source
            from policy_fetch f
            join app_seed s on s.seed_id = f.seed_id
            where f.fetch_id = ?
              and f.status = 'pending'
              and (f.next_attempt_at is null or f.next_attempt_at <= ?)
            """,
            (fetch_id, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            update policy_fetch
            set status = 'running',
                worker_id = ?,
                locked_at = ?,
                attempts = attempts + 1,
                updated_at = ?
            where fetch_id = ?
            """,
            (worker_id, now, now, row["fetch_id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["attempts"] = int(row["attempts"]) + 1
        return claimed


def complete_fetch(db_path: str | Path, fetch_id: int, result: dict) -> None:
    now = utc_now()
    with closing(connect(db_path)) as conn:
        fetch = conn.execute(
            "select app_id, country from policy_fetch where fetch_id = ?",
            (fetch_id,),
        ).fetchone()
        if fetch is None:
            raise ValueError(f"unknown fetch_id: {fetch_id}")
        conn.execute(
            """
            update policy_fetch
            set status = ?, updated_at = ?, worker_id = null, locked_at = null
            where fetch_id = ?
            """,
            (result.get("status") or "ok", now, fetch_id),
        )
        if result.get("policy_url"):
            conn.execute(
                """
                insert or ignore into policy_url_candidate(
                    app_id, country, policy_url, canonical_policy_url,
                    method, evidence, discovered_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch["app_id"],
                    fetch["country"],
                    result["policy_url"],
                    result.get("canonical_policy_url") or result["policy_url"],
                    result.get("policy_url_method"),
                    result.get("policy_url_evidence"),
                    now,
                ),
            )
            cursor = conn.execute(
                """
                insert or replace into policy_document(
                    fetch_id, app_id, country, policy_url, canonical_policy_url,
                    policy_text_sha256, policy_text_chars, policy_markdown_path,
                    policy_html_path, policy_text_path, policy_fetch_method,
                    policy_cluster_manifest_path,
                    policy_cluster_nodes_count, policy_cluster_edges_count,
                    policy_cluster_errors_count, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch_id,
                    fetch["app_id"],
                    fetch["country"],
                    result["policy_url"],
                    result.get("canonical_policy_url") or result["policy_url"],
                    result.get("policy_text_sha256"),
                    result.get("policy_text_chars"),
                    result.get("policy_markdown_path"),
                    result.get("policy_html_path"),
                    result.get("policy_text_path"),
                    result.get("policy_fetch_method"),
                    result.get("policy_cluster_manifest_path"),
                    result.get("policy_cluster_nodes_count"),
                    result.get("policy_cluster_edges_count"),
                    result.get("policy_cluster_errors_count"),
                    now,
                ),
            )
            document_id = cursor.lastrowid
            conn.execute("delete from policy_link where document_id = ?", (document_id,))
            for link in result.get("policy_links") or []:
                if link.get("url"):
                    conn.execute(
                        "insert into policy_link(document_id, text, url) values (?, ?, ?)",
                        (document_id, link.get("text"), link["url"]),
                    )
        save_policy_url_attempts(conn, fetch_id, result.get("policy_url_attempts") or [], now)
        conn.commit()


def fail_fetch(
    db_path: str | Path,
    fetch_id: int,
    error_class: str,
    error_message: str,
    retryable: bool,
    max_attempts: int = 3,
    policy_url_attempts: Iterable[dict] | None = None,
) -> None:
    now = utc_now()
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "select attempts from policy_fetch where fetch_id = ?",
            (fetch_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown fetch_id: {fetch_id}")
        attempts = int(row["attempts"])
        status = "pending" if retryable and attempts < max_attempts else "permanent_error"
        conn.execute(
            """
            update policy_fetch
            set status = ?,
                worker_id = null,
                locked_at = null,
                next_attempt_at = ?,
                last_error_class = ?,
                last_error_message = ?,
                updated_at = ?
            where fetch_id = ?
            """,
            (status, now if status == "pending" else None, error_class, error_message, now, fetch_id),
        )
        save_policy_url_attempts(conn, fetch_id, policy_url_attempts or [], now)
        conn.commit()


def requeue_running_fetches(
    db_path: str | Path,
    worker_prefix: str | None = None,
    locked_before: str | None = None,
) -> dict[str, int]:
    init_db(db_path)
    clauses = ["status = 'running'"]
    params: list[object] = []
    if worker_prefix:
        clauses.append("worker_id like ?")
        params.append(f"{worker_prefix}%")
    if locked_before:
        clauses.append("locked_at < ?")
        params.append(locked_before)
    now = utc_now()
    with closing(connect(db_path)) as conn:
        cursor = conn.execute(
            f"""
            update policy_fetch
            set status = 'pending',
                worker_id = null,
                locked_at = null,
                next_attempt_at = ?,
                updated_at = ?
            where {' and '.join(clauses)}
            """,
            [now, now, *params],
        )
        conn.commit()
        return {"requeued": int(cursor.rowcount or 0)}


def requeue_fetch(db_path: str | Path, fetch_id: int) -> dict[str, int]:
    now = utc_now()
    with closing(connect(db_path)) as conn:
        cursor = conn.execute(
            """
            update policy_fetch
            set status = 'pending',
                worker_id = null,
                locked_at = null,
                next_attempt_at = ?,
                updated_at = ?
            where fetch_id = ?
              and status in ('permanent_error', 'failed', 'running')
            """,
            (now, now, fetch_id),
        )
        conn.commit()
        return {"requeued": int(cursor.rowcount or 0), "fetch_id": int(fetch_id)}


def stats(db_path: str | Path) -> dict[str, int]:
    with closing(connect_readonly(db_path)) as conn:
        values = {
            "seed_rows": conn.execute("select count(*) from app_seed").fetchone()[0],
            "seed_validations": conn.execute("select count(*) from seed_validation").fetchone()[0],
            "policy_documents": conn.execute("select count(*) from policy_document").fetchone()[0],
            "policy_links": conn.execute("select count(*) from policy_link").fetchone()[0],
            "policy_url_attempts": conn.execute("select count(*) from policy_url_attempt").fetchone()[0],
        }
        for status_name in ["active", "inactive", "missing", "lookup_error"]:
            values[f"{status_name}_seed_validations"] = conn.execute(
                "select count(*) from seed_validation where status = ?",
                (status_name,),
            ).fetchone()[0]
        for status_name, key in [
            ("pending", "pending_fetches"),
            ("running", "running_fetches"),
            ("ok", "ok_fetches"),
            ("permanent_error", "permanent_error_fetches"),
        ]:
            values[key] = conn.execute(
                "select count(*) from policy_fetch where status = ?",
                (status_name,),
            ).fetchone()[0]
        return values


def iter_seed_file(path: str | Path, seed_source: str, country: str) -> Iterable[dict]:
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    row.setdefault("seed_source", seed_source)
                    row.setdefault("country", country)
                    yield row
        return
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row.setdefault("seed_source", seed_source)
            row.setdefault("country", country)
            yield row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the SQLite collection queue.")
    parser.add_argument("--db", default=str(default_db_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    import_parser = sub.add_parser("import-seeds")
    import_parser.add_argument("--file", required=True)
    import_parser.add_argument("--source", default="file")
    import_parser.add_argument("--country", default="us")
    sub.add_parser("stats")
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument("--worker-id", default="manual")
    claim_parser.add_argument("--active-only", action="store_true")
    claim_parser.add_argument("--countries", default=None)
    claim_parser.add_argument("--sources", default=None)
    claim_parser.add_argument("--claim-order", choices=["oldest", "newest"], default="oldest")
    requeue_parser = sub.add_parser("requeue-running")
    requeue_parser.add_argument("--worker-prefix", default=None)
    requeue_parser.add_argument("--locked-before", default=None)
    requeue_fetch_parser = sub.add_parser("requeue-fetch")
    requeue_fetch_parser.add_argument("--fetch-id", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        init_db(args.db)
        print(json.dumps({"db": args.db, "status": "ok"}, sort_keys=True))
        return 0
    if args.command == "import-seeds":
        result = import_seeds(args.db, iter_seed_file(args.file, args.source, args.country))
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "stats":
        print(json.dumps(stats(args.db), sort_keys=True))
        return 0
    if args.command == "claim":
        countries = [item.strip().lower() for item in args.countries.split(",") if item.strip()] if args.countries else None
        sources = [item.strip() for item in args.sources.split(",") if item.strip()] if args.sources else None
        print(json.dumps(
            claim_next_fetch(
                args.db,
                args.worker_id,
                active_only=args.active_only,
                countries=countries,
                sources=sources,
                claim_order=args.claim_order,
            ),
            sort_keys=True,
        ))
        return 0
    if args.command == "requeue-running":
        print(json.dumps(requeue_running_fetches(args.db, args.worker_prefix, args.locked_before), sort_keys=True))
        return 0
    if args.command == "requeue-fetch":
        print(json.dumps(requeue_fetch(args.db, args.fetch_id), sort_keys=True))
        return 0
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
