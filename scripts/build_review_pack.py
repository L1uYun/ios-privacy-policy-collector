#!/usr/bin/env python3
"""Build reviewer-facing README and sample index CSV for policy-cluster exports."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path


def rel_for_package(path: Path, package_root: Path) -> str:
    return path.relative_to(package_root).as_posix()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_rows(package_root: Path) -> list[dict[str, str | int]]:
    policy_root = package_root / "data" / "policy-clusters"
    rows: list[dict[str, str | int]] = []
    for app_dir in sorted(p for p in policy_root.glob("*/*") if p.is_dir()):
        country = app_dir.parent.name
        slug = app_dir.name
        match = re.match(r"^(\d+)-(.*)$", slug)
        app_id = match.group(1) if match else ""
        app_name = (match.group(2) if match else slug).replace("-", " ")

        pp_md = app_dir / "privacy-policy.md"
        pp_txt = app_dir / "privacy-policy.txt"
        links = app_dir / "privacy-policy.links.jsonl"
        cluster = app_dir / "policy-cluster" / "cluster.json"
        if not pp_md.exists():
            continue

        root_chars = pp_txt.stat().st_size if pp_txt.exists() else pp_md.stat().st_size
        link_count = 0
        if links.exists():
            with links.open("r", encoding="utf-8", errors="replace") as handle:
                link_count = sum(1 for line in handle if line.strip())

        root_url = ""
        root_title = ""
        cluster_docs = 0
        cluster_chars = 0
        cluster_errors = 0
        fetch_methods: set[str] = set()
        quality_values: set[str] = set()
        node_rel_paths: list[str] = []

        if cluster.exists():
            data = load_json(cluster)
            root_url = str(data.get("canonical_root_url") or "")
            cluster_errors = len(data.get("errors") or [])
            for node in data.get("nodes") or []:
                cluster_docs += 1
                cluster_chars += int(node.get("text_chars") or 0)
                if node.get("fetch_method"):
                    fetch_methods.add(str(node["fetch_method"]))
                if node.get("text_quality"):
                    quality_values.add(str(node["text_quality"]))
                if not root_title and node.get("role") == "root":
                    root_title = str(node.get("title") or "")
                markdown_path = str(node.get("markdown_path") or "")
                marker = "/data/policy-clusters/"
                if marker in markdown_path:
                    node_rel_paths.append("data/policy-clusters/" + markdown_path.split(marker, 1)[1])

        if root_chars >= 5000 and cluster_docs >= 2:
            sample_priority = "rich_cluster"
        elif root_chars >= 2000:
            sample_priority = "good_root"
        elif root_chars >= 500:
            sample_priority = "short_ok"
        else:
            sample_priority = "review_short"

        rows.append(
            {
                "country": country,
                "app_id": app_id,
                "app_name": app_name,
                "root_url": root_url,
                "root_title": root_title,
                "root_text_chars": root_chars,
                "cluster_docs": cluster_docs,
                "cluster_text_chars": cluster_chars,
                "root_link_count": link_count,
                "cluster_error_count": cluster_errors,
                "fetch_methods": "+".join(sorted(fetch_methods)),
                "text_quality_values": "+".join(sorted(quality_values)),
                "sample_priority": sample_priority,
                "privacy_policy_md": rel_for_package(pp_md, package_root),
                "privacy_policy_links": rel_for_package(links, package_root) if links.exists() else "",
                "cluster_json": rel_for_package(cluster, package_root) if cluster.exists() else "",
                "first_cluster_node_md": node_rel_paths[0] if node_rel_paths else "",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    fieldnames = [
        "country",
        "app_id",
        "app_name",
        "root_url",
        "root_title",
        "root_text_chars",
        "cluster_docs",
        "cluster_text_chars",
        "root_link_count",
        "cluster_error_count",
        "fetch_methods",
        "text_quality_values",
        "sample_priority",
        "privacy_policy_md",
        "privacy_policy_links",
        "cluster_json",
        "first_cluster_node_md",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize(package_root: Path, rows: list[dict[str, str | int]]) -> dict:
    snapshot = package_root / "data" / "stage-snapshots" / "stage1-10k-auto-stop-reached-10000-20260525-135514"
    manifest = {}
    manifest_path = snapshot / "snapshot-manifest.json"
    if manifest_path.exists():
        manifest = load_json(manifest_path)
    chars = [int(row["root_text_chars"]) for row in rows]
    cluster_docs = [int(row["cluster_docs"]) for row in rows]
    methods: dict[str, int] = {}
    countries: dict[str, int] = {}
    priorities: dict[str, int] = {}
    for row in rows:
        methods[str(row["fetch_methods"] or "unknown")] = methods.get(str(row["fetch_methods"] or "unknown"), 0) + 1
        countries[str(row["country"])] = countries.get(str(row["country"]), 0) + 1
        priorities[str(row["sample_priority"])] = priorities.get(str(row["sample_priority"]), 0) + 1
    return {
        "indexed_policy_folders": len(rows),
        "snapshot_policy_documents": (manifest.get("stats") or {}).get("policy_documents"),
        "final_known_policy_documents": 10038,
        "root_text_chars_min": min(chars) if chars else 0,
        "root_text_chars_avg": round(statistics.mean(chars), 2) if chars else 0,
        "root_text_chars_max": max(chars) if chars else 0,
        "cluster_docs_avg": round(statistics.mean(cluster_docs), 2) if cluster_docs else 0,
        "fetch_method_groups": dict(sorted(methods.items())),
        "sample_priority_counts": dict(sorted(priorities.items())),
        "country_counts": dict(sorted(countries.items())),
    }


def write_readme(path: Path, summary: dict) -> None:
    readme = f"""# iOS Privacy Policy Collector 10k Review Pack

This folder is an offline review package for the first 10k-stage iOS privacy-policy corpus collected on T430.

## What Is Included

- `data/policy-clusters/`: per-app policy folders. Each folder contains the root privacy-policy Markdown, extracted text, link JSONL, raw HTML when saved, and a `policy-cluster/` subfolder for same-domain linked legal/privacy documents.
- `data/stage-snapshots/stage1-10k-auto-stop-reached-10000-20260525-135514/`: frozen stage evidence, including snapshot manifest, failure classification, milestone status, disk usage, and batch summaries.
- `sample-index.csv`: one row per indexed app policy folder, with relative paths into `data/policy-clusters/`.
- `sample-index-top200.csv`: a deterministic reviewer subset sorted toward richer clusters and longer documents.

## Collection Status

- Gate date: 2026-05-25
- T430 remote repo: `/data/xiaolab-research/ios-privacy-policy-collector`
- Snapshot name: `stage1-10k-auto-stop-reached-10000-20260525-135514`
- Snapshot policy documents: {summary.get("snapshot_policy_documents") or "not available in this local copy yet"}
- Final post-stop policy documents observed: {summary["final_known_policy_documents"]}
- Indexed policy folders in `sample-index.csv`: {summary["indexed_policy_folders"]}
- Root text length: min {summary["root_text_chars_min"]}, average {summary["root_text_chars_avg"]}, max {summary["root_text_chars_max"]} bytes/chars depending on source file encoding

## How To Review

1. Start with `sample-index-top200.csv` for a manageable quality sample.
2. For any row, open `privacy_policy_md` relative to this folder. That is the root Markdown extracted for the app's privacy-policy entry URL.
3. Open `cluster_json` to see same-domain linked documents followed by the crawler, fetch methods, text lengths, link counts, and errors.
4. Open `first_cluster_node_md` or other Markdown files under `policy-cluster/nodes/` to inspect linked privacy/legal documents.
5. Use `root_url`, `fetch_methods`, `root_text_chars`, `cluster_docs`, and `cluster_error_count` to stratify manual checks.

## Suggested Manual Quality Labels

For lab review, add a separate annotation column instead of editing crawler output:

- `full_policy`: app-specific or service-specific privacy policy, enough text for downstream analysis.
- `generic_policy`: valid legal/privacy page but shared across many apps or platform-wide.
- `terms_or_wrong_policy`: terms, marketing, support, or unrelated legal page selected instead of privacy policy.
- `too_short_or_broken`: page extracted but content is too short, blocked, or structurally incomplete.
- `needs_browser_or_domain_rule`: likely recoverable with stronger JS/browser/domain-rule handling.

## Notes

The Markdown files preserve discovered links where extraction kept them. The `.links.jsonl` files provide the more auditable link inventory for each root page or cluster node.
"""
    path.write_text(readme, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    package_root = args.package_root.resolve()
    output_dir = (args.output_dir or package_root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = build_rows(package_root)
    write_csv(output_dir / "sample-index.csv", rows)
    top_rows = sorted(
        rows,
        key=lambda row: (int(row["cluster_docs"]), int(row["cluster_text_chars"]), int(row["root_text_chars"])),
        reverse=True,
    )[:200]
    write_csv(output_dir / "sample-index-top200.csv", top_rows)
    summary = summarize(package_root, rows)
    (output_dir / "sample-index-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_readme(output_dir / "README-for-review.md", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
