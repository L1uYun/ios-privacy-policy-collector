#!/usr/bin/env python3
"""Repair accepted policy documents whose Markdown file still contains PDF bytes."""

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


def is_pdf_markdown(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    if not path.exists() or path.stat().st_size == 0:
        return False
    return path.read_bytes()[:16].lstrip().startswith(b"%PDF-")


def fetch_pdf_bytes(url: str, timeout: int, user_agent: str, proxy: str | None) -> bytes:
    request = collector.urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/pdf,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    opener = collector.build_opener(proxy)
    with opener.open(request, timeout=timeout) as response:
        content = response.read()
        content_type = response.headers.get("Content-Type")
        final_url = response.geturl() if hasattr(response, "geturl") else url
        if not collector.is_pdf_response(content, content_type, final_url):
            raise collector.UnsupportedDocumentError(f"repair URL did not return PDF: content-type={content_type or 'unknown'}")
        return content


def repair_document(conn: sqlite3.Connection, row: sqlite3.Row, dry_run: bool, timeout: int, user_agent: str, proxy: str | None) -> dict:
    markdown_path = Path(row["policy_markdown_path"])
    try:
        pdf_bytes = fetch_pdf_bytes(row["policy_url"], timeout, user_agent, proxy)
        repaired_from = "url"
    except Exception:
        pdf_bytes = markdown_path.read_bytes()
        repaired_from = "local-corrupt-md"
    markdown = collector.extract_pdf_text(pdf_bytes, row["policy_url"])
    text = collector.html_to_text(markdown)
    pdf_path = markdown_path.with_suffix(".pdf")
    text_path = Path(row["policy_text_path"]) if row["policy_text_path"] else markdown_path.with_suffix(".txt")

    result = {
        "document_id": row["document_id"],
        "app_id": row["app_id"],
        "country": row["country"],
        "policy_url": row["policy_url"],
        "markdown_path": str(markdown_path),
        "pdf_path": str(pdf_path),
        "repaired_from": repaired_from,
        "text_chars": len(text),
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    if not pdf_path.exists():
        pdf_path.write_bytes(pdf_bytes)
    markdown_path.write_text(markdown, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    conn.execute(
        """
        update policy_document
        set policy_text_sha256 = ?,
            policy_text_chars = ?,
            policy_text_path = ?
        where document_id = ?
        """,
        (collector.sha256_text(text), len(text), str(text_path), row["document_id"]),
    )
    return result


def repair(db_path: str | Path, dry_run: bool, timeout: int, user_agent: str, proxy: str | None) -> dict:
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
            if not is_pdf_markdown(row["policy_markdown_path"]):
                continue
            try:
                repaired.append(repair_document(conn, row, dry_run, timeout, user_agent, proxy))
            except Exception as exc:
                skipped.append(
                    {
                        "document_id": row["document_id"],
                        "app_id": row["app_id"],
                        "country": row["country"],
                        "policy_url": row["policy_url"],
                        "error_class": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
        if not dry_run:
            conn.commit()
    return {
        "db": str(db_path),
        "dry_run": dry_run,
        "repaired_count": len(repaired),
        "skipped_count": len(skipped),
        "repaired": repaired,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair policy Markdown files that contain raw PDF bytes.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--user-agent", default=collector.DEFAULT_BROWSER_USER_AGENT)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    result = repair(args.db, args.dry_run, args.timeout, args.user_agent, args.proxy)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["skipped_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
