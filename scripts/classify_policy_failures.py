#!/usr/bin/env python3
"""Classify failed policy fetches from queue audit tables."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


queue_store = load_script_module("queue_store", SCRIPT_DIR / "queue_store.py")


HTTP_RE = re.compile(r"http error (?P<status>\d{3})", re.IGNORECASE)


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.lower()


def classify(error_class: str | None, error_message: str | None, quality: str | None, quality_reason: str | None) -> str:
    text = " ".join(item or "" for item in [error_class, error_message, quality, quality_reason]).lower()
    if "privacy policy url not found" in text or "not found on app store page" in text:
        return "policy_url_not_found"
    if "policy text not complete enough" in text or "too short" in text or "short" in text:
        return "policy_text_too_short"
    match = HTTP_RE.search(text)
    if match:
        status = match.group("status")
        if status in {"403", "406", "429"}:
            return f"http_blocked_{status}"
        if status == "404":
            return "http_not_found_404"
        if status.startswith("5"):
            return f"http_server_{status}"
        return f"http_{status}"
    if "timeout" in text:
        return "timeout"
    if "ssl" in text or "certificate" in text:
        return "ssl"
    if "connection" in text or "remote end closed" in text or "reset" in text:
        return "connection"
    if "captcha" in text or "cloudflare" in text:
        return "bot_protection"
    if "lookup" in text:
        return "lookup_or_metadata"
    return "other"


def build_report(
    db_path: str | Path,
    limit_samples: int,
    countries: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict:
    queue_store.init_db(db_path)
    clauses = ["f.status = 'permanent_error'"]
    params: list[object] = []
    if countries:
        placeholders = ",".join("?" for _country in countries)
        clauses.append(f"f.country in ({placeholders})")
        params.extend(countries)
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
    where_sql = " and ".join(clauses)
    with queue_store.connect(db_path) as conn:
        failed_fetches = [
            dict(row)
            for row in conn.execute(
                f"""
                select f.fetch_id, f.app_id, f.country, s.seed_source,
                       f.last_error_class, f.last_error_message, f.attempts
                from policy_fetch f
                join app_seed s on s.seed_id = f.seed_id
                where {where_sql}
                order by f.fetch_id desc
                """,
                params,
            )
        ]
        attempt_rows = [
            dict(row)
            for row in conn.execute(
                """
                select a.fetch_id, a.app_id, a.country, a.policy_url,
                       a.source, a.status, a.fetch_method, a.text_chars,
                       a.quality, a.quality_reason,
                       a.error_class, a.error_message
                from policy_url_attempt a
                join policy_fetch f on f.fetch_id = a.fetch_id
                where f.status = 'permanent_error'
                order by a.fetch_id desc, a.attempt_index
                """
            )
        ]

    attempts_by_fetch: dict[int, list[dict]] = {}
    for row in attempt_rows:
        attempts_by_fetch.setdefault(int(row["fetch_id"]), []).append(row)

    category_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    fetch_method_counts: Counter[str] = Counter()
    samples: list[dict] = []

    for fetch in failed_fetches:
        attempts = attempts_by_fetch.get(int(fetch["fetch_id"]), [])
        if attempts:
            last = attempts[-1]
            category = classify(
                last.get("error_class"),
                last.get("error_message"),
                last.get("quality"),
                last.get("quality_reason"),
            )
            domain = domain_of(last.get("policy_url"))
            method = last.get("fetch_method") or "unknown"
        else:
            category = classify(fetch.get("last_error_class"), fetch.get("last_error_message"), None, None)
            domain = ""
            method = "unknown"
        category_counts[category] += 1
        if domain:
            domain_counts[domain] += 1
        source_counts[fetch.get("seed_source") or "unknown"] += 1
        fetch_method_counts[method] += 1
        if len(samples) < limit_samples:
            samples.append(
                {
                    "fetch_id": fetch["fetch_id"],
                    "app_id": fetch["app_id"],
                    "country": fetch["country"],
                    "seed_source": fetch["seed_source"],
                    "category": category,
                    "domain": domain,
                    "last_error_class": fetch.get("last_error_class"),
                    "last_error_message": fetch.get("last_error_message"),
                    "attempts": attempts[-3:],
                }
            )

    return {
        "failed_fetches": len(failed_fetches),
        "category_counts": dict(category_counts.most_common()),
        "domain_counts": dict(domain_counts.most_common(30)),
        "seed_source_counts": dict(source_counts.most_common()),
        "last_attempt_fetch_method_counts": dict(fetch_method_counts.most_common()),
        "samples": samples,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify permanent policy fetch failures.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--countries", default=None, help="Comma-separated countries to include.")
    parser.add_argument("--sources", default=None, help="Comma-separated seed sources to include. Trailing * means prefix match.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    countries = [item.strip().lower() for item in args.countries.split(",") if item.strip()] if args.countries else None
    sources = [item.strip() for item in args.sources.split(",") if item.strip()] if args.sources else None
    report = build_report(args.db, args.sample_limit, countries=countries, sources=sources)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
