#!/usr/bin/env python3
"""Build reviewer-facing CSVs from Apple-platform rediscovery results."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


APPLE_HOST_RE = re.compile(r"(^|\.)(apple\.[a-z.]+|apps\.apple\.com)$", re.I)


def _host(url: str) -> str:
    try:
        return urlparse(url.strip()).netloc.lower()
    except Exception:
        return ""


def _audit_class(row: dict[str, str]) -> str:
    status = row.get("status", "")
    if status == "recovered":
        return "developer_policy_recovered"
    if status == "apple_platform_rejected":
        return "apple_platform_or_first_party_rejected"
    if status == "lookup_missing":
        return "itunes_lookup_missing_or_app_unavailable"
    error = row.get("error", "").lower()
    if "timeout" in error or "timed out" in error:
        return "network_timeout_retryable"
    if status == "unrecovered":
        return "no_non_apple_developer_policy_found"
    if status == "error":
        return "error_needs_retry_or_manual_review"
    return status or "unknown"


def _confidence(row: dict[str, str]) -> str:
    status = row.get("status", "")
    method = row.get("recovery_method", "")
    if status != "recovered":
        return "none"
    if method.startswith("manual-audit:"):
        return "manual_verified"
    if method.startswith("domain-rule:"):
        return "rule_verified"
    if (
        "app-store" in method
        or "seller-" in method
        or "sitemap" in method
        or "robots" in method
    ):
        return "high"
    return "medium"


def _source_type(row: dict[str, str]) -> str:
    method = row.get("recovery_method", "")
    if method.startswith("manual-audit:"):
        return "manual_audit"
    if method.startswith("domain-rule:"):
        return "domain_rule"
    if method.startswith("alternate-country:"):
        return "alternate_country"
    if method.startswith("app-store-external:"):
        return "app_store_external_or_support"
    if method.startswith("app-store:"):
        return "app_store_privacy_link"
    if method.startswith("seller-"):
        return "seller_site_discovery"
    if method in {"sitemap", "robots-sitemap"}:
        return "sitemap_or_robots"
    return method


def build_clean_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    clean_rows: list[dict[str, str]] = []
    for row_id, row in enumerate(rows, 1):
        policy_url = row.get("recovered_policy_url", "").strip()
        policy_domain = _host(policy_url)
        clean_rows.append(
            {
                "row_id": str(row_id),
                "app_id": row.get("app_id", ""),
                "country": row.get("country", ""),
                "app_name": row.get("app_name", ""),
                "status": row.get("status", ""),
                "audit_class": _audit_class(row),
                "confidence": _confidence(row),
                "source_type": _source_type(row),
                "policy_url": policy_url,
                "policy_domain": policy_domain,
                "app_store_url": row.get("app_store_url", "").strip(),
                "seller_url": row.get("seller_url", "").strip(),
                "old_root_url": row.get("old_root_url", "").strip(),
                "recovery_method": row.get("recovery_method", ""),
                "evidence": row.get("evidence", ""),
                "error": row.get("error", ""),
                "is_apple_policy_url": "1"
                if APPLE_HOST_RE.search(policy_domain)
                else "0",
            }
        )
    return clean_rows


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(input_csv: Path, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with input_csv.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = list(csv.DictReader(handle))
    if not source_rows:
        raise ValueError(f"no rows found in {input_csv}")

    clean_rows = build_clean_rows(source_rows)
    fieldnames = list(clean_rows[0])

    clean_csv = output_dir / "apple-platform-policy-rediscovery-clean.csv"
    recovered_csv = output_dir / "apple-platform-policy-rediscovery-recovered-clean.csv"
    nonrecovered_csv = output_dir / "apple-platform-policy-rediscovery-nonrecovered-clean.csv"
    summary_json = output_dir / "apple-platform-policy-rediscovery-clean-summary.json"
    readme = output_dir / "README-clean.md"

    recovered_rows = [row for row in clean_rows if row["status"] == "recovered"]
    nonrecovered_rows = [row for row in clean_rows if row["status"] != "recovered"]

    _write_csv(clean_csv, clean_rows, fieldnames)
    _write_csv(recovered_csv, recovered_rows, fieldnames)
    _write_csv(nonrecovered_csv, nonrecovered_rows, fieldnames)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_csv": str(input_csv),
        "clean_csv": str(clean_csv),
        "recovered_csv": str(recovered_csv),
        "nonrecovered_csv": str(nonrecovered_csv),
        "rows": len(clean_rows),
        "status_counts": dict(Counter(row["status"] for row in clean_rows).most_common()),
        "audit_class_counts": dict(
            Counter(row["audit_class"] for row in clean_rows).most_common()
        ),
        "confidence_counts": dict(
            Counter(row["confidence"] for row in clean_rows).most_common()
        ),
        "source_type_counts": dict(
            Counter(row["source_type"] for row in clean_rows).most_common()
        ),
        "recovered_rows": len(recovered_rows),
        "nonrecovered_rows": len(nonrecovered_rows),
        "unique_recovered_apps": len(
            {row["app_id"] for row in recovered_rows if row["app_id"]}
        ),
        "unique_policy_urls": len(
            {row["policy_url"] for row in recovered_rows if row["policy_url"]}
        ),
        "apple_policy_url_rows": sum(
            1 for row in clean_rows if row["is_apple_policy_url"] == "1"
        ),
    }
    summary_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    readme.write_text(_render_readme(summary, input_csv), encoding="utf-8")
    return summary


def _render_readme(summary: dict[str, object], input_csv: Path) -> str:
    status_counts = json.dumps(summary["status_counts"], ensure_ascii=False, indent=2)
    audit_counts = json.dumps(
        summary["audit_class_counts"], ensure_ascii=False, indent=2
    )
    return f"""# Clean Apple-Platform Rediscovery Results

Generated: {summary["created_at"]}

This directory contains a cleaned view of the Apple-platform rediscovery repair
subset. The source file is:

```text
{input_csv}
```

## Files

- `apple-platform-policy-rediscovery-clean.csv`: all {summary["rows"]} rows with
  normalized audit columns.
- `apple-platform-policy-rediscovery-recovered-clean.csv`: recovered developer
  policy rows only.
- `apple-platform-policy-rediscovery-nonrecovered-clean.csv`: non-recovered,
  Apple-rejected, error, and lookup-missing rows only.
- `apple-platform-policy-rediscovery-clean-summary.json`: machine-readable
  counts.

## Current Counts

```text
rows: {summary["rows"]}
recovered developer policies: {summary["recovered_rows"]}
non-recovered / rejected / error rows: {summary["nonrecovered_rows"]}
unique recovered apps: {summary["unique_recovered_apps"]}
unique recovered policy URLs: {summary["unique_policy_urls"]}
```

Status counts:

```json
{status_counts}
```

Audit class counts:

```json
{audit_counts}
```

## Column Notes

- `policy_url` is only considered usable when `status=recovered` and
  `is_apple_policy_url=0`.
- `apple_platform_rejected` is deliberately not counted as developer-policy
  recovery.
- `audit_class` gives reviewers a cleaner grouping than raw crawler status.
- `confidence=manual_verified` includes manually checked recoveries.
- `source_type` describes the URL-discovery route.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = write_outputs(args.input_csv, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
