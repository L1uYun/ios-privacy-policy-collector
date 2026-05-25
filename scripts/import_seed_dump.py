#!/usr/bin/env python3
"""Import large public app-store seed dumps into the collection queue."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import lzma
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, TextIO


SCRIPT_DIR = Path(__file__).resolve().parent
APP_ID_RE = re.compile(r"(?:^|[/\?&])id(?P<id>\d{5,})(?:[/?&#]|$)")


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


queue_store = load_script_module("queue_store", SCRIPT_DIR / "queue_store.py")


def open_text(path: str | Path) -> TextIO:
    path = Path(path)
    if path.suffix.lower() == ".xz":
        return lzma.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def parse_app_id(value: str | None) -> str | None:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{5,}", value):
        return value
    match = APP_ID_RE.search(value)
    if match:
        return match.group("id")
    return None


def looks_like_ios_store(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"ios", "apple", "apple_app_store", "app_store", "itunes", "iphone", "ipad"}


def app_store_url(app_id: str, country: str) -> str:
    return f"https://apps.apple.com/{country}/app/id{app_id}"


def iter_appgoblin_rows(path: str | Path, seed_source: str, country: str, limit: int | None = None) -> Iterable[dict]:
    seen: set[tuple[str, str]] = set()
    with open_text(path) as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        reader = csv.DictReader(handle, dialect=dialect)
        count = 0
        for row in reader:
            store = row.get("store") or row.get("appstore") or row.get("platform")
            store_id = row.get("store_id") or row.get("app_id") or row.get("id") or row.get("canonical_url")
            app_id = parse_app_id(store_id)
            if not app_id:
                continue
            if store and not looks_like_ios_store(store):
                continue
            key = (country, app_id)
            if key in seen:
                continue
            seen.add(key)
            yield {
                "seed_source": seed_source,
                "app_id": app_id,
                "bundle_id": row.get("bundle_id") or "",
                "app_store_url": row.get("app_store_url") or row.get("canonical_url") or app_store_url(app_id, country),
                "country": country,
                "provenance_url": str(path),
                "license_note": "Imported from public app metadata seed dump; verify upstream license/provenance before redistribution.",
            }
            count += 1
            if limit and count >= limit:
                break


def iter_appstoredb_sqlite_rows(path: str | Path, seed_source: str, country: str, limit: int | None = None) -> list[dict]:
    path = Path(path)
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        if "apps" not in tables:
            raise ValueError("appstoredb sqlite input must contain an apps table")
        has_stores = "stores" in tables
        if has_stores:
            query = """
                select
                    a.store_id,
                    a.bundle_id,
                    s.canonical_url,
                    s.app_name,
                    s.code
                from apps a
                left join stores s on s.int_app_id = a.int_id
                order by a.int_id, s.code
            """
        else:
            query = """
                select
                    store_id,
                    bundle_id,
                    null as canonical_url,
                    null as app_name,
                    null as code
                from apps
                order by int_id
            """
        count = 0
        for row in conn.execute(query):
            app_id = parse_app_id(row["store_id"])
            if not app_id:
                continue
            row_country = str(row["code"] or country).lower()
            if len(row_country) != 2:
                row_country = country
            key = (row_country, app_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "seed_source": seed_source,
                "app_id": app_id,
                "bundle_id": row["bundle_id"] or "",
                "app_store_url": row["canonical_url"] or app_store_url(app_id, row_country),
                "country": row_country,
                "provenance_url": str(path),
                "license_note": "Imported from appstoredb SQLite dataset; verify upstream license/provenance before redistribution.",
            })
            count += 1
            if limit and count >= limit:
                break
    finally:
        conn.close()
    return rows


def write_seed_csv(path: str | Path, rows: Iterable[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["app_id", "bundle_id", "app_store_url", "country", "provenance_url", "license_note", "seed_source"]
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
            count += 1
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import large iOS app seed dumps.")
    parser.add_argument("--input", required=True, help="Input .tsv, .csv, .tsv.xz, .csv.xz, or SQLite seed dump.")
    parser.add_argument("--format", choices=["appgoblin", "appstoredb-sqlite"], default="appgoblin")
    parser.add_argument("--source", default="public-seed-dump")
    parser.add_argument("--country", default="us")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--db", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.format == "appgoblin":
        rows = list(iter_appgoblin_rows(args.input, args.source, args.country, args.limit))
    elif args.format == "appstoredb-sqlite":
        rows = list(iter_appstoredb_sqlite_rows(args.input, args.source, args.country, args.limit))
    else:
        raise AssertionError(args.format)
    result = {"rows": len(rows)}
    if args.output_csv:
        result["output_csv_rows"] = write_seed_csv(args.output_csv, rows)
        result["output_csv"] = args.output_csv
    if args.db:
        result["db_import"] = queue_store.import_seeds(args.db, rows)
        result["db"] = args.db
    if not args.output_csv and not args.db:
        for row in rows:
            print(row)
    else:
        import json

        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
