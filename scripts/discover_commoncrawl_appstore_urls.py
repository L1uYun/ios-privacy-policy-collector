#!/usr/bin/env python3
"""Discover App Store seed URLs from Common Crawl CDX indexes."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ios_privacy_policy_collector as collector
import queue_store


COMMONCRAWL_INDEXES_URL = "https://index.commoncrawl.org/collinfo.json"
CDX_FIELDS = "url,timestamp,status,mime"
DEFAULT_PATTERN = "apps.apple.com/us/app/"
APPSTORE_HOST_RE = re.compile(r"(^|\.)apps\.apple\.com$", re.IGNORECASE)


def request_json(url: str, timeout: int, user_agent: str, proxy: str | None) -> object:
    text = request_text(url, timeout=timeout, user_agent=user_agent, proxy=proxy, backend="requests")
    return json.loads(text)


def request_text(url: str, timeout: int, user_agent: str, proxy: str | None, backend: str = "urllib") -> str:
    if backend == "requests":
        import requests

        proxies = {"http": proxy, "https": proxy} if proxy else None
        response = requests.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
            },
            proxies=proxies,
            timeout=timeout,
        )
        if response.status_code == 404:
            raise urllib.error.HTTPError(url, 404, "Not Found", response.headers, None)
        response.raise_for_status()
        return response.text
    return collector.request_text(url, timeout=timeout, user_agent=user_agent, proxy=proxy)


def latest_index(timeout: int, user_agent: str, proxy: str | None) -> str:
    payload = request_json(COMMONCRAWL_INDEXES_URL, timeout, user_agent, proxy)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Common Crawl index list is empty or malformed")
    ids = [str(row.get("id")) for row in payload if isinstance(row, dict) and row.get("id")]
    if not ids:
        raise RuntimeError("Common Crawl index list did not contain ids")
    return ids[0]


def cdx_query_url(
    index: str,
    url_pattern: str,
    collapse_urlkey: bool,
    match_type: str | None = "prefix",
    server_limit: int | None = None,
    page: int | None = None,
) -> str:
    params = {
        "url": url_pattern,
        "output": "json",
        "fl": CDX_FIELDS,
        "filter": ["status:200", "mime:text/html"],
    }
    if match_type:
        params["matchType"] = match_type
    if server_limit is not None:
        params["limit"] = str(server_limit)
    query = urllib.parse.urlencode(params, doseq=True)
    if collapse_urlkey:
        query += "&collapse=urlkey"
    if page is not None:
        query += f"&page={page}"
    return f"https://index.commoncrawl.org/{urllib.parse.quote(index, safe='')}-index?{query}"


def iter_cdx_json_lines(url: str, timeout: int, user_agent: str, proxy: str | None, backend: str = "urllib") -> Iterator[dict]:
    if backend == "requests":
        import requests

        proxies = {"http": proxy, "https": proxy} if proxy else None
        response = requests.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
            },
            proxies=proxies,
            stream=True,
            timeout=timeout,
        )
        if response.status_code == 404:
            return
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            yield json.loads(line)
        return
    opener = collector.build_opener(proxy)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                yield json.loads(line)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise


def canonical_appstore_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if not APPSTORE_HOST_RE.search(parsed.netloc):
        return None
    app_id = collector.parse_app_id(url)
    if not app_id:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 3 or path_parts[1] != "app":
        return None
    country = "us"
    slug = "app"
    if len(path_parts[0]) == 2:
        country = path_parts[0].lower()
        slug = path_parts[2]
    return f"https://apps.apple.com/{country}/app/{slug}/id{app_id}"


def country_from_appstore_url(url: str, fallback: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 1 and len(parts[0]) == 2:
        return parts[0].lower()
    return fallback.lower()


def seed_row_from_cdx_record(record: dict, index: str, fallback_country: str, seed_source: str) -> dict | None:
    url = str(record.get("url") or "").strip()
    app_id = collector.parse_app_id(url)
    canonical_url = canonical_appstore_url(url)
    if not app_id or not canonical_url:
        return None
    timestamp = str(record.get("timestamp") or "")
    return {
        "seed_source": seed_source,
        "app_id": app_id,
        "bundle_id": "",
        "app_store_url": canonical_url,
        "country": country_from_appstore_url(canonical_url, fallback_country),
        "observed_at": timestamp,
        "provenance_url": f"commoncrawl:{index}:{timestamp}:{url}",
        "license_note": "Discovered from Common Crawl CDX URL index; validate with iTunes lookup before policy fetching.",
    }


def iter_seed_rows_from_cdx(
    records: Iterable[dict],
    index: str,
    fallback_country: str,
    seed_source: str,
    limit: int | None = None,
) -> Iterator[dict]:
    seen: set[tuple[str, str]] = set()
    count = 0
    for record in records:
        row = seed_row_from_cdx_record(record, index, fallback_country, seed_source)
        if row is None:
            continue
        key = (row["country"], row["app_id"])
        if key in seen:
            continue
        seen.add(key)
        yield row
        count += 1
        if limit is not None and count >= limit:
            break


def discover_rows(args: argparse.Namespace) -> tuple[str, list[dict]]:
    index = args.index
    backend = getattr(args, "backend", "urllib")
    if index == "latest":
        index = latest_index(args.timeout, args.user_agent, args.proxy)
    query_url = cdx_query_url(
        index,
        args.url_pattern,
        args.collapse_urlkey,
        match_type=args.match_type,
        server_limit=args.server_limit or args.limit,
    )
    records = iter_cdx_json_lines(query_url, args.timeout, args.user_agent, args.proxy, backend=backend)
    rows = list(iter_seed_rows_from_cdx(records, index, args.country, args.source, args.limit))
    return query_url, rows


def write_seed_csv(path: str | Path, rows: Iterable[dict]) -> int:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["app_id", "bundle_id", "app_store_url", "country", "observed_at", "provenance_url", "license_note", "seed_source"]
    count = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
            count += 1
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover App Store URL seeds from Common Crawl CDX.")
    parser.add_argument("--index", default="latest", help="Common Crawl index id, for example CC-MAIN-2026-18, or latest.")
    parser.add_argument("--url-pattern", default=DEFAULT_PATTERN)
    parser.add_argument("--match-type", choices=["exact", "prefix", "host", "domain"], default="prefix")
    parser.add_argument("--country", default="us", help="Fallback country when URL has no storefront prefix.")
    parser.add_argument("--source", default="commoncrawl-appstore-url")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--server-limit", type=int, default=None, help="CDX API limit. Defaults to --limit.")
    parser.add_argument("--collapse-urlkey", action="store_true", default=True)
    parser.add_argument("--no-collapse-urlkey", action="store_false", dest="collapse_urlkey")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--backend", choices=["urllib", "requests"], default="requests")
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--db", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    query_url, rows = discover_rows(args)
    result = {
        "query_url": query_url,
        "rows": len(rows),
        "source": args.source,
    }
    if args.output_csv:
        result["output_csv_rows"] = write_seed_csv(args.output_csv, rows)
        result["output_csv"] = args.output_csv
    if args.db:
        result["db_import"] = queue_store.import_seeds(args.db, rows)
        result["db"] = args.db
    if not args.output_csv and not args.db:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
