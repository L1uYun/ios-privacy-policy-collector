#!/usr/bin/env python3
"""Rediscover developer policy URLs for rows resolved to Apple platform pages."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ios_privacy_policy_collector as collector


APPLE_SUFFIXES = ("apple.com", "apple.com.cn", "itunes.apple.com", "apps.apple.com")


@dataclass
class RediscoveryResult:
    app_id: str
    country: str
    app_name: str
    old_root_url: str
    app_store_url: str = ""
    seller_url: str = ""
    recovered_policy_url: str = ""
    recovery_method: str = ""
    status: str = "unrecovered"
    error: str = ""
    evidence: str = ""


def url_host(url: str) -> str:
    return (urllib.parse.urlparse(url or "").hostname or "").lower()


def is_apple_platform_url(url: str) -> bool:
    host = url_host(url)
    return bool(host) and any(host == suffix or host.endswith("." + suffix) for suffix in APPLE_SUFFIXES)


def is_acceptable_policy_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    return not is_apple_platform_url(url)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def append_csv_row(path: Path, row: dict[str, str], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()


def timed_call(fn, timeout_seconds: float, default):
    result: dict[str, object] = {"value": default, "error": None}

    def runner():
        try:
            result["value"] = fn()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return default, TimeoutError(f"timed out after {timeout_seconds}s")
    if result["error"] is not None:
        return default, result["error"]
    return result["value"], None


def lookup_app(app_id: str, country: str, timeout: float, user_agent: str, proxy: str | None) -> dict:
    query = urllib.parse.urlencode({"id": app_id, "country": country, "entity": "software", "limit": 1})
    url = f"https://itunes.apple.com/lookup?{query}"
    payload = collector.request_text(url, timeout=timeout, user_agent=user_agent, proxy=proxy)
    data = json.loads(payload)
    results = data.get("results") or []
    return results[0] if results else {}


def lookup_app_any_country(
    app_id: str,
    preferred_country: str,
    alternate_countries: list[str],
    timeout: float,
    user_agent: str,
    proxy: str | None,
    hard_timeout_seconds: float | None = None,
) -> tuple[dict, str]:
    attempts: list[str] = []
    if preferred_country:
        attempts.append(preferred_country)
    for alternate_country in alternate_countries:
        if alternate_country and alternate_country not in attempts:
            attempts.append(alternate_country)
    for country in attempts:
        lookup_fn = lambda: lookup_app(app_id, country, timeout, user_agent, proxy)
        lookup, lookup_err = timed_call(lookup_fn, hard_timeout_seconds, {}) if hard_timeout_seconds else (lookup_fn(), None)
        if lookup:
            return lookup, country
        if lookup_err is not None:
            continue
    return {}, ""


def try_extract_from_app_store(
    app_store_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
    js_fallback: bool = True,
) -> tuple[str, str, str, str]:
    if not app_store_url:
        return "", "", "", ""
    html = collector.request_text(app_store_url, timeout=timeout, user_agent=user_agent, proxy=proxy)
    result = collector.extract_privacy_policy_url(html, app_store_url)
    if result and is_acceptable_policy_url(result.url):
        return result.url, f"app-store:{result.method}", result.evidence, ""
    for link in collector.extract_app_store_external_links(html, app_store_url):
        base_url = link["url"]
        recovered_url, method, evidence = discover_from_base_url(
            base_url,
            timeout,
            user_agent,
            proxy,
            max_paths=18,
            js_fallback=js_fallback,
        )
        if recovered_url:
            return recovered_url, f"app-store-external:{method}", link.get("evidence") or evidence, base_url
    return "", "", "", ""


def try_extract_from_seller_home(
    seller_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
    js_fallback: bool = False,
) -> tuple[str, str, str]:
    if not seller_url or is_apple_platform_url(seller_url):
        return "", "", ""
    for candidate in collector.seller_home_policy_candidates(
        seller_url,
        int(timeout),
        user_agent,
        proxy=proxy,
        js_fallback=js_fallback,
        js_timeout=int(timeout),
    ):
        if is_acceptable_policy_url(candidate["url"]):
            return candidate["url"], candidate["source"], "seller home link discovery"
    return "", "", ""


def try_common_paths(
    seller_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
    max_paths: int,
) -> tuple[str, str, str]:
    if not seller_url or is_apple_platform_url(seller_url):
        return "", "", ""
    for candidate in collector.common_privacy_url_candidates(seller_url)[:max_paths]:
        if not is_acceptable_policy_url(candidate):
            continue
        try:
            text = collector.request_text(candidate, timeout=timeout, user_agent=user_agent, proxy=proxy)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, UnicodeError):
            continue
        plain = collector.html_to_text(text)
        quality, _reason = collector.policy_text_quality(plain, 200)
        if quality in {"ok", "too_short"} and len(plain.strip()) >= 200:
            return candidate, "seller-common-path", f"text_chars={len(plain.strip())}"
    return "", "", ""


def discover_from_base_url(
    base_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
    max_paths: int,
    js_fallback: bool = False,
) -> tuple[str, str, str]:
    if not base_url or is_apple_platform_url(base_url):
        return "", "", ""
    recovered_url, method, evidence = try_shortlink(base_url, timeout, user_agent, proxy)
    if recovered_url:
        base_url = recovered_url
    recovered_url, method, evidence = try_extract_from_seller_home(base_url, timeout, user_agent, proxy, js_fallback)
    if recovered_url:
        return recovered_url, method, evidence
    recovered_url, method, evidence = try_common_paths(base_url, timeout, user_agent, proxy, max_paths)
    if recovered_url:
        return recovered_url, method, evidence
    recovered_url, method, evidence = try_sitemaps(base_url, timeout, user_agent, proxy)
    if recovered_url:
        return recovered_url, method, evidence
    return "", "", ""


def web_search_queries(app_id: str, app_name: str, country: str) -> list[str]:
    clean_name = " ".join((app_name or "").replace("\u00a0", " ").split())
    queries: list[str] = []
    if clean_name:
        queries.extend(
            [
                f'"{clean_name}" "{app_id}" privacy policy',
                f'"{clean_name}" app privacy policy',
                f'"{clean_name}" developer website',
            ]
        )
    queries.append(f'"id{app_id}" privacy policy')
    if country:
        queries.append(f'"{app_id}" "{country}" privacy policy')
    return queries


def try_web_search_fallback(
    app_id: str,
    app_name: str,
    country: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
    max_paths: int,
    max_results: int,
    js_fallback: bool = False,
    search_endpoint: str = "https://html.duckduckgo.com/html/",
) -> tuple[str, str, str, str]:
    seen: set[str] = set()
    for query in web_search_queries(app_id, app_name, country):
        try:
            candidates = collector.web_search_url_candidates(
                query,
                int(timeout),
                user_agent,
                proxy=proxy,
                limit=max_results,
                endpoint=search_endpoint,
            )
        except Exception:
            continue
        for candidate in candidates:
            candidate_url = candidate.get("url", "")
            if not candidate_url or candidate_url in seen or not is_acceptable_policy_url(candidate_url):
                continue
            seen.add(candidate_url)
            if collector.privacy_candidate_score_url(candidate_url) > 0:
                return candidate_url, "web-search:direct-policy-candidate", candidate.get("evidence", "") or query, candidate_url
            recovered_url, method, evidence = discover_from_base_url(
                candidate_url,
                timeout,
                user_agent,
                proxy,
                max_paths,
                js_fallback,
            )
            if recovered_url:
                return recovered_url, f"web-search:{method}", evidence or candidate.get("evidence", "") or query, candidate_url
    return "", "", "", ""


def try_shortlink(
    seller_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
) -> tuple[str, str, str]:
    if not seller_url or is_apple_platform_url(seller_url):
        return "", "", ""
    try:
        candidate = collector.expand_shortlink_candidate(seller_url, int(timeout), user_agent, proxy=proxy)
    except Exception:
        return "", "", ""
    if candidate and is_acceptable_policy_url(candidate["url"]):
        return candidate["url"], candidate["source"], "sellerUrl shortlink expanded"
    return "", "", ""


def try_sitemaps(
    seller_url: str,
    timeout: float,
    user_agent: str,
    proxy: str | None,
) -> tuple[str, str, str]:
    if not seller_url or is_apple_platform_url(seller_url):
        return "", "", ""
    for discovery in (collector.robots_sitemap_policy_candidates, collector.sitemap_policy_candidates):
        for candidate in discovery(seller_url, int(timeout), user_agent, proxy=proxy):
            if is_acceptable_policy_url(candidate["url"]):
                return candidate["url"], candidate["source"], "privacy-like URL found in sitemap"
    return "", "", ""


def rediscover_row(
    row: dict[str, str],
    timeout: float,
    user_agent: str,
    proxy: str | None,
    max_common_paths: int,
    alternate_countries: list[str],
    js_fallback: bool = False,
    hard_timeout_seconds: float | None = None,
    web_search_fallback: bool = False,
    web_search_results: int = 6,
    search_endpoint: str = "https://html.duckduckgo.com/html/",
    skip_alternate_countries: bool = False,
) -> RediscoveryResult:
    app_store_url = row.get("app_store_url", "") or row.get("live_app_store_url", "")
    seller_url = row.get("seller_url", "") or row.get("live_seller_url", "")
    result = RediscoveryResult(
        app_id=row.get("app_id", ""),
        country=row.get("country", ""),
        app_name=row.get("app_name", ""),
        old_root_url=row.get("root_url", "") or row.get("old_root_url", ""),
        app_store_url=app_store_url,
        seller_url=seller_url,
    )
    try:
        lookup: dict = {}
        lookup_country = result.country
        if not result.app_store_url:
            lookup, lookup_country = lookup_app_any_country(
                result.app_id,
                result.country,
                alternate_countries,
                timeout,
                user_agent,
                proxy,
                hard_timeout_seconds,
            )
        if not lookup and not result.app_store_url:
            result.status = "lookup_missing"
            result.error = "storefront lookup returned no result"
            return result
        result.app_store_url = result.app_store_url or lookup.get("trackViewUrl") or ""
        result.seller_url = result.seller_url or lookup.get("sellerUrl") or ""

        attempts = [
            ("app-store", lambda: try_extract_from_app_store(result.app_store_url, timeout, user_agent, proxy, js_fallback)[:3]),
            (
                "seller-base",
                lambda: discover_from_base_url(
                    result.seller_url,
                    timeout,
                    user_agent,
                    proxy,
                    max_common_paths,
                    js_fallback,
                ),
            ),
        ]
        for _name, attempt in attempts:
            recovered_url, method, evidence = attempt()
            if recovered_url:
                result.recovered_policy_url = recovered_url
                result.recovery_method = method
                result.evidence = evidence
                result.status = "recovered"
                return result

        if web_search_fallback:
            recovered_url, method, evidence, external_base = try_web_search_fallback(
                result.app_id,
                result.app_name or lookup.get("trackName", ""),
                result.country,
                timeout,
                user_agent,
                proxy,
                max_common_paths,
                web_search_results,
                js_fallback,
                search_endpoint,
            )
            if recovered_url:
                result.recovered_policy_url = recovered_url
                result.recovery_method = method
                result.evidence = evidence
                result.status = "recovered"
                if not result.seller_url:
                    result.seller_url = external_base
                return result

        if not skip_alternate_countries:
            for alternate_country in alternate_countries:
                if alternate_country == result.country:
                    continue
                alternate_lookup_fn = lambda: lookup_app(result.app_id, alternate_country, timeout, user_agent, proxy)
                alternate_lookup, _alt_err = timed_call(alternate_lookup_fn, hard_timeout_seconds, {}) if hard_timeout_seconds else (alternate_lookup_fn(), None)
                alternate_app_store_url = alternate_lookup.get("trackViewUrl") or ""
                recovered_url, method, evidence, external_base = try_extract_from_app_store(
                    alternate_app_store_url,
                    timeout,
                    user_agent,
                    proxy,
                    js_fallback,
                )
                if recovered_url:
                    result.recovered_policy_url = recovered_url
                    result.recovery_method = f"alternate-country:{alternate_country}:{method}"
                    result.evidence = evidence
                    result.status = "recovered"
                    result.error = ""
                    if not result.seller_url:
                        result.seller_url = alternate_lookup.get("sellerUrl") or ""
                    return result
                if not result.seller_url:
                    result.seller_url = alternate_lookup.get("sellerUrl") or external_base or ""
                    recovered_url, method, evidence = try_extract_from_seller_home(
                        result.seller_url,
                        timeout,
                        user_agent,
                        proxy,
                        js_fallback,
                    )
                    if recovered_url:
                        result.recovered_policy_url = recovered_url
                        result.recovery_method = f"alternate-country:{alternate_country}:{method}"
                        result.evidence = evidence
                        result.status = "recovered"
                        result.error = ""
                        return result

        recovered_url, method, evidence = try_common_paths(
            result.seller_url,
            timeout,
            user_agent,
            proxy,
            max_common_paths,
        )
        if recovered_url:
            result.recovered_policy_url = recovered_url
            result.recovery_method = method
            result.evidence = evidence
            result.status = "recovered"
            result.error = ""
            return result

        result.status = "unrecovered"
        result.error = "no non-Apple developer policy URL found"
        return result
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-index", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-name", default="apple-platform-policy-rediscovery", help="Base name for output CSV and summary JSON files.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--max-common-paths", type=int, default=18)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent rediscovery workers.")
    parser.add_argument("--all-rows", action="store_true", help="Process all sample rows instead of only rows whose root_url is an Apple platform URL.")
    parser.add_argument("--failure-class-filter", default="", help="Comma-separated failure_class values to keep from an already classified CSV.")
    parser.add_argument("--web-search-fallback", action="store_true", help="Use web search as a last-resort candidate source for rows with no seller URL or App Store external link.")
    parser.add_argument("--web-search-results", type=int, default=6, help="Maximum web search result URLs to inspect per query.")
    parser.add_argument("--search-endpoint", default="https://html.duckduckgo.com/html/", help="HTML search endpoint accepting a q query parameter.")
    parser.add_argument("--js-fallback", action="store_true", help="Use browser rendering when seller homepage static discovery finds no policy link.")
    parser.add_argument("--hard-timeout", type=float, default=0, help="Optional per-row wall-clock timeout in seconds for lookup and discovery.")
    parser.add_argument("--resume", action="store_true", help="Skip rows already present in the rediscovery CSV.")
    parser.add_argument(
        "--retry-status",
        default="",
        help="Comma-separated statuses to retry when --resume is enabled, for example: error,unrecovered.",
    )
    parser.add_argument(
        "--alternate-countries",
        default="us,gb,ca,au,de,fr,jp,kr,cn",
        help="Comma-separated App Store countries to try after the row country fails.",
    )
    parser.add_argument("--skip-alternate-countries", action="store_true", help="Skip cross-country lookup fallback; useful for already classified no-sellerUrl repair batches.")
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    args = parser.parse_args()
    alternate_countries = [country.strip().lower() for country in args.alternate_countries.split(",") if country.strip()]
    retry_statuses = {status.strip() for status in args.retry_status.split(",") if status.strip()}

    rows = read_rows(args.sample_index)
    failure_classes = {item.strip() for item in args.failure_class_filter.split(",") if item.strip()}
    input_rows = rows
    if failure_classes:
        input_rows = [row for row in input_rows if row.get("failure_class", "") in failure_classes]
    apple_rows = input_rows if args.all_rows else [row for row in input_rows if is_apple_platform_url(row.get("root_url", ""))]
    if args.limit:
        apple_rows = apple_rows[: args.limit]

    apple_row_path = args.output_dir / f"{args.output_name}-rows.csv"
    write_csv(apple_row_path, apple_rows, list(rows[0].keys()) if rows else [])

    result_fields = [
        "app_id",
        "country",
        "app_name",
        "old_root_url",
        "app_store_url",
        "seller_url",
        "recovered_policy_url",
        "recovery_method",
        "status",
        "error",
        "evidence",
    ]
    result_path = args.output_dir / f"{args.output_name}.csv"
    result_rows: list[dict[str, str]] = []
    completed: set[tuple[str, str]] = set()
    if args.resume and result_path.exists():
        with result_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for previous in csv.DictReader(handle):
                key = (previous.get("country", ""), previous.get("app_id", ""))
                if key in completed:
                    continue
                if previous.get("status", "") in retry_statuses:
                    continue
                completed.add(key)
                result_rows.append({field: previous.get(field, "") for field in result_fields})
        write_csv(result_path, result_rows, result_fields)
    elif result_path.exists():
        result_path.unlink()
    pending_rows = []
    for row in apple_rows:
        key = (row.get("country", ""), row.get("app_id", ""))
        if key in completed:
            continue
        pending_rows.append(row)

    def run_one(row: dict[str, str]) -> dict[str, str]:
        if args.hard_timeout and args.hard_timeout > 0:
            def do_row():
                return rediscover_row(
                    row,
                    args.timeout,
                    args.user_agent,
                    args.proxy,
                    args.max_common_paths,
                    alternate_countries,
                    args.js_fallback,
                    None,
                    args.web_search_fallback,
                    args.web_search_results,
                    args.search_endpoint,
                    args.skip_alternate_countries,
                ).__dict__

            rediscovered_row, err = timed_call(do_row, args.hard_timeout, None)
            if err is not None:
                return {
                    "app_id": row.get("app_id", ""),
                    "country": row.get("country", ""),
                    "app_name": row.get("app_name", ""),
                    "old_root_url": row.get("root_url", ""),
                    "app_store_url": "",
                    "seller_url": "",
                    "recovered_policy_url": "",
                    "recovery_method": "",
                    "status": "error",
                    "error": f"{type(err).__name__}: {err}",
                    "evidence": "",
                }
            return rediscovered_row
        rediscovered = rediscover_row(
            row,
            args.timeout,
            args.user_agent,
            args.proxy,
            args.max_common_paths,
            alternate_countries,
            args.js_fallback,
            None,
            args.web_search_fallback,
            args.web_search_results,
            args.search_endpoint,
            args.skip_alternate_countries,
        )
        return rediscovered.__dict__

    def write_result(rediscovered_row: dict[str, str]) -> None:
        if not rediscovered_row:
            return
        key = (rediscovered_row.get("country", ""), rediscovered_row.get("app_id", ""))
        result_rows.append(rediscovered_row)
        append_csv_row(result_path, rediscovered_row, result_fields)
        completed.add(key)
        if len(result_rows) % 25 == 0:
            recovered = sum(1 for item in result_rows if item["status"] == "recovered")
            print(f"processed={len(result_rows)} recovered={recovered}", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    if args.workers <= 1:
        for row in pending_rows:
            write_result(run_one(row))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_one, row) for row in pending_rows]
            for future in concurrent.futures.as_completed(futures):
                write_result(future.result())

    status_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    for row in result_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        if row["recovery_method"]:
            method_counts[row["recovery_method"]] = method_counts.get(row["recovery_method"], 0) + 1
    summary = {
        "input_rows": len(rows),
        "target_rows": len(apple_rows),
        "apple_platform_rows": sum(1 for row in apple_rows if is_apple_platform_url(row.get("root_url", ""))),
        "failure_class_filter": sorted(failure_classes),
        "web_search_fallback": bool(args.web_search_fallback),
        "skip_alternate_countries": bool(args.skip_alternate_countries),
        "processed_rows": len(result_rows),
        "recovered_rows": status_counts.get("recovered", 0),
        "unique_recovered_apps": len({row["app_id"] for row in result_rows if row["status"] == "recovered"}),
        "status_counts": status_counts,
        "method_counts": method_counts,
        "apple_rows_csv": str(apple_row_path),
        "rediscovery_csv": str(result_path),
    }
    summary_path = args.output_dir / f"{args.output_name}-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
