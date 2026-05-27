#!/usr/bin/env python3
"""Audit archived privacy policy Markdown files referenced by the queue DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def classify_markdown(path_value: str | None, min_chars: int) -> tuple[str, dict]:
    detail: dict = {"path": path_value}
    if not path_value:
        return "missing_path", detail
    path = Path(path_value)
    if not path.exists():
        return "missing_file", detail
    size = path.stat().st_size
    detail["bytes"] = size
    if size == 0:
        return "empty_file", detail
    prefix = path.read_bytes()[:16]
    if prefix.lstrip().startswith(b"%PDF-"):
        return "pdf_as_markdown", detail
    text = path.read_text(encoding="utf-8", errors="replace")
    chars = len(text.strip())
    detail["chars"] = chars
    if chars < min_chars:
        return "too_short", detail
    return "ok", detail


def audit(db_path: str | Path, min_chars: int, sample_limit: int) -> dict:
    counters: dict[str, int] = {}
    samples: dict[str, list[dict]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                d.document_id,
                d.app_id,
                d.country,
                d.policy_url,
                d.policy_markdown_path,
                count(l.link_id) as policy_links_count
            from policy_document d
            left join policy_link l on l.document_id = d.document_id
            group by d.document_id
            order by d.document_id
            """
        )
        total = 0
        for row in rows:
            total += 1
            status, detail = classify_markdown(row["policy_markdown_path"], min_chars)
            counters[status] = counters.get(status, 0) + 1
            links_count = int(row["policy_links_count"] or 0)
            if links_count <= 0:
                counters["zero_links"] = counters.get("zero_links", 0) + 1
            if status != "ok" or links_count <= 0:
                bucket = status if status != "ok" else "zero_links"
                bucket_samples = samples.setdefault(bucket, [])
                if len(bucket_samples) < sample_limit:
                    bucket_samples.append(
                        {
                            "document_id": row["document_id"],
                            "app_id": row["app_id"],
                            "country": row["country"],
                            "policy_url": row["policy_url"],
                            "links_count": links_count,
                            **detail,
                        }
                    )
    counters.setdefault("ok", 0)
    return {
        "db": str(db_path),
        "total_policy_documents": total,
        "min_chars": min_chars,
        "counters": dict(sorted(counters.items())),
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit archived policy Markdown quality.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--min-chars", type=int, default=500)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    result = audit(args.db, args.min_chars, args.sample_limit)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
