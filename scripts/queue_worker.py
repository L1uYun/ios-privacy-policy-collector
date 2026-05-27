#!/usr/bin/env python3
"""Run one or more SQLite queue tasks using the iOS policy collector."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import uuid
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


queue_store = load_script_module("queue_store", SCRIPT_DIR / "queue_store.py")
collector = load_script_module(
    "ios_privacy_policy_collector",
    SCRIPT_DIR / "ios_privacy_policy_collector.py",
)
policy_cluster = load_script_module("policy_cluster", SCRIPT_DIR / "policy_cluster.py")


def links_from_jsonl_text(text: str) -> list[dict]:
    links = []
    for line in text.splitlines():
        if line.strip():
            links.append(json.loads(line))
    return links


def load_links(path: str | None) -> list[dict]:
    if not path:
        return []
    link_path = Path(path)
    if not link_path.exists():
        return []
    return links_from_jsonl_text(link_path.read_text(encoding="utf-8"))


def retryable_error(message: str | None) -> bool:
    if not message:
        return False
    lower = message.lower()
    retryable_markers = [
        "timeout",
        "temporarily",
        "connection",
        "http error 403",
        "http error 406",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "ssl",
    ]
    permanent_markers = [
        "privacy policy url not found",
        "policy text not complete enough",
        "not found on app store page",
    ]
    if any(marker in lower for marker in permanent_markers):
        return False
    return any(marker in lower for marker in retryable_markers)


def queue_result_from_collector_row(row: dict, links: list[dict]) -> dict:
    return {
        "status": "ok",
        "policy_url": row["policy_url"],
        "canonical_policy_url": row.get("policy_url"),
        "policy_url_method": row.get("policy_url_method"),
        "policy_url_evidence": row.get("policy_url_evidence"),
        "policy_text_sha256": row.get("policy_text_sha256"),
        "policy_text_chars": row.get("policy_text_chars"),
        "policy_markdown_path": row.get("policy_markdown_path"),
        "policy_html_path": row.get("policy_html_path"),
        "policy_text_path": row.get("policy_text_path"),
        "policy_fetch_method": row.get("policy_fetch_method"),
        "policy_cluster_manifest_path": row.get("policy_cluster_manifest_path"),
        "policy_cluster_nodes_count": row.get("policy_cluster_nodes_count"),
        "policy_cluster_edges_count": row.get("policy_cluster_edges_count"),
        "policy_cluster_errors_count": row.get("policy_cluster_errors_count"),
        "policy_links": links,
        "policy_url_attempts": row.get("policy_url_attempts") or [],
    }


def collect_task(task: dict, args: argparse.Namespace) -> dict:
    record = collector.AppRecord(
        app_id=str(task["app_id"]),
        name=None,
        bundle_id=task.get("bundle_id"),
        seller_name=None,
        app_store_url=task.get("app_store_url"),
        seller_url=None,
        source=task.get("seed_source") or "queue",
        raw={},
    )
    if args.enrich_lookup:
        record = collector.enrich_record_from_lookup(record, task["country"], args)
    row = collector.collect_app(record, args, country=task["country"])
    if row.get("error") or row.get("policy_text_quality") != "ok":
        exc = RuntimeError(row.get("error") or row.get("policy_text_quality") or "collector failed")
        setattr(exc, "policy_url_attempts", row.get("policy_url_attempts") or [])
        raise exc
    if args.collect_cluster:
        cluster_dir = Path(row["policy_markdown_path"]).parent / "policy-cluster"

        def fetch(url: str) -> str:
            if url == row["policy_url"] and row.get("policy_html_path"):
                html_path = Path(row["policy_html_path"])
                if html_path.exists():
                    return html_path.read_text(encoding="utf-8")
            return collector.request_text(url, args.timeout, args.user_agent, proxy=args.proxy)

        def js_fetch(url: str) -> str:
            return collector.render_text_with_playwright(
                url,
                args.js_timeout,
                args.user_agent,
                proxy=args.proxy,
                wait_ms=args.js_wait_ms,
            )

        cluster_result = policy_cluster.collect_policy_cluster(
            root_url=row["policy_url"],
            output_dir=cluster_dir,
            fetch_text=fetch,
            max_depth=args.cluster_max_depth,
            max_docs=args.cluster_max_docs,
            min_chars=args.cluster_min_chars,
            probe_common_paths=args.cluster_probe_common_paths,
            js_fetch_text=js_fetch if getattr(args, "js_fallback", False) else None,
            js_fallback=getattr(args, "js_fallback", False),
        )
        row["policy_cluster_manifest_path"] = cluster_result["manifest_path"]
        row["policy_cluster_nodes_count"] = cluster_result["nodes_count"]
        row["policy_cluster_edges_count"] = cluster_result["edges_count"]
        row["policy_cluster_errors_count"] = cluster_result["errors_count"]
    return queue_result_from_collector_row(row, load_links(row.get("policy_links_path")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run queued policy collection tasks.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--worker-id", default=f"worker-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--fetch-id", type=int, default=None, help="Claim one specific pending fetch ID for targeted retry.")
    parser.add_argument("--active-only", action="store_true", help="Claim only seeds validated as active by iTunes lookup.")
    parser.add_argument("--countries", default=None, help="Comma-separated storefront countries to claim.")
    parser.add_argument("--sources", default=None, help="Comma-separated seed sources to claim. A trailing * means prefix match.")
    parser.add_argument("--claim-order", choices=["oldest", "newest"], default="oldest")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--output-dir", default=str(queue_store.default_data_root() / "out"))
    parser.add_argument("--jsonl", default=str(queue_store.default_data_root() / "worker-results.jsonl"))
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--fallback-timeout", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument("--browser-user-agent", default=collector.DEFAULT_BROWSER_USER_AGENT)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-fetch-policy", action="store_true")
    parser.add_argument("--min-policy-chars", type=int, default=1000)
    parser.add_argument("--js-fallback", action="store_true", help="Use Playwright rendering when static pages are too short or blocked.")
    parser.add_argument("--js-timeout", type=int, default=60)
    parser.add_argument("--js-wait-ms", type=int, default=2000)
    parser.add_argument("--try-common-paths", action="store_true")
    parser.add_argument("--domain-rules", default=str(collector.default_domain_rules_path()))
    parser.add_argument("--enrich-lookup", action="store_true")
    parser.add_argument("--collect-cluster", action="store_true", help="Archive linked legal/privacy documents as a policy cluster.")
    parser.add_argument("--cluster-max-depth", type=int, default=1)
    parser.add_argument("--cluster-max-docs", type=int, default=12)
    parser.add_argument("--cluster-min-chars", type=int, default=200)
    parser.add_argument("--cluster-probe-common-paths", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    processed = 0
    countries = [item.strip().lower() for item in args.countries.split(",") if item.strip()] if args.countries else None
    sources = [item.strip() for item in args.sources.split(",") if item.strip()] if args.sources else None
    while processed < args.limit:
        if args.fetch_id is not None:
            if processed > 0:
                break
            task = queue_store.claim_fetch_by_id(args.db, args.fetch_id, args.worker_id)
        else:
            task = queue_store.claim_next_fetch(
                args.db,
                args.worker_id,
                active_only=args.active_only,
                countries=countries,
                sources=sources,
                claim_order=args.claim_order,
            )
        if task is None:
            break
        try:
            result = collect_task(task, args)
        except Exception as exc:
            queue_store.fail_fetch(
                args.db,
                task["fetch_id"],
                type(exc).__name__,
                str(exc),
                retryable=retryable_error(str(exc)),
                max_attempts=args.max_attempts,
                policy_url_attempts=getattr(exc, "policy_url_attempts", []),
            )
            print(json.dumps({"fetch_id": task["fetch_id"], "status": "failed", "error": str(exc)}, sort_keys=True))
        else:
            queue_store.complete_fetch(args.db, task["fetch_id"], result)
            print(json.dumps({"fetch_id": task["fetch_id"], "status": "ok"}, sort_keys=True))
        processed += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
