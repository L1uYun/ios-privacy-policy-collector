#!/usr/bin/env python3
"""Rebuild accepted policy Markdown files when extracted text is complete but Markdown is truncated."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def load_collector():
    spec = importlib.util.spec_from_file_location("ios_privacy_policy_collector", SCRIPT_DIR / "ios_privacy_policy_collector.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = load_collector()


def text_len(path_value: str | None) -> int:
    if not path_value:
        return 0
    path = Path(path_value)
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").strip())


def repair(db_path: str | Path, min_chars: int, dry_run: bool) -> dict:
    repaired: list[dict] = []
    skipped: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select document_id, app_id, country, policy_url, policy_markdown_path, policy_text_path
            from policy_document
            order by document_id
            """
        ).fetchall()
        for row in rows:
            markdown_chars = text_len(row["policy_markdown_path"])
            if markdown_chars >= min_chars:
                continue
            text_path = Path(row["policy_text_path"]) if row["policy_text_path"] else None
            if not text_path or not text_path.exists():
                skipped.append(
                    {
                        "document_id": row["document_id"],
                        "app_id": row["app_id"],
                        "country": row["country"],
                        "reason": "missing_text_file",
                    }
                )
                continue
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) < min_chars:
                skipped.append(
                    {
                        "document_id": row["document_id"],
                        "app_id": row["app_id"],
                        "country": row["country"],
                        "reason": "text_too_short",
                        "markdown_chars": markdown_chars,
                        "text_chars": len(text),
                    }
                )
                continue
            markdown = collector.fallback_text_markdown(text, row["policy_url"])
            markdown_path = Path(row["policy_markdown_path"])
            result = {
                "document_id": row["document_id"],
                "app_id": row["app_id"],
                "country": row["country"],
                "policy_url": row["policy_url"],
                "markdown_path": str(markdown_path),
                "old_markdown_chars": markdown_chars,
                "new_markdown_chars": len(markdown.strip()),
                "dry_run": dry_run,
            }
            if not dry_run:
                markdown_path.write_text(markdown, encoding="utf-8")
                existing_link = conn.execute(
                    "select 1 from policy_link where document_id = ? and url = ? limit 1",
                    (row["document_id"], row["policy_url"]),
                ).fetchone()
                if not existing_link:
                    conn.execute(
                        "insert into policy_link(document_id, text, url) values (?, ?, ?)",
                        (row["document_id"], row["policy_url"], row["policy_url"]),
                    )
            repaired.append(result)
        if not dry_run:
            conn.commit()
    return {
        "db": str(db_path),
        "min_chars": min_chars,
        "dry_run": dry_run,
        "repaired_count": len(repaired),
        "skipped_count": len(skipped),
        "repaired": repaired,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair short Markdown files using already archived text.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--min-chars", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    result = repair(args.db, args.min_chars, args.dry_run)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
