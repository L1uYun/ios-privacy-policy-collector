#!/usr/bin/env python3
"""Archive a privacy policy together with linked legal/policy documents."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import re
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Callable


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = load_script_module(
    "ios_privacy_policy_collector",
    SCRIPT_DIR / "ios_privacy_policy_collector.py",
)


FetchText = Callable[[str], str]


class QualityError(RuntimeError):
    pass

POLICY_LINK_RE = re.compile(
    r"\b("
    r"privacy|policy|policies|legal|terms|conditions|cookie|cookies|"
    r"data[-\s]?policy|data[-\s]?protection|children|child|ccpa|gdpr|"
    r"eula|agreement|notice|notices|personal[-\s]?information|"
    r"隐私|个人信息|條款|条款|服务协议|用户协议|儿童|Cookie"
    r")\b",
    re.IGNORECASE,
)
NOISE_LINK_RE = re.compile(
    r"\b("
    r"support|help|faq|blog|news|press|career|jobs|login|signin|signup|"
    r"facebook|twitter|linkedin|instagram|youtube|mailto:"
    r")\b",
    re.IGNORECASE,
)
COMMON_RELATED_PATHS = [
    "/terms",
    "/terms-of-service",
    "/terms-and-conditions",
    "/user-agreement",
    "/legal/terms",
    "/legal/terms-of-service",
    "/agreement",
    "/eula",
    "/cookies",
    "/cookie-policy",
    "/legal/cookies",
    "/privacy/cookies",
    "/children-privacy",
    "/childrens-privacy",
    "/legal/children-privacy",
    "/data-policy",
    "/data-protection",
    "/third-parties",
    "/legal/third-parties",
    "/permissions",
    "/legal/permissions",
]


@dataclasses.dataclass(frozen=True)
class ClusterNode:
    url: str
    canonical_url: str
    role: str
    depth: int
    title: str | None
    html_path: str
    markdown_path: str
    text_path: str
    links_path: str
    text_sha256: str
    text_chars: int
    text_quality: str
    text_quality_reason: str | None
    markdown_method: str
    links_count: int


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    tracking_prefixes = ("utm_",)
    query = [
        (key, value)
        for key, value in query
        if not key.lower().startswith(tracking_prefixes)
        and key.lower() not in {"fbclid", "gclid", "msclkid"}
    ]
    normalized_query = urllib.parse.urlencode(query, doseq=True)
    path = path.rstrip("/") or "/"
    return urllib.parse.urlunparse((scheme, netloc, path, "", normalized_query, ""))


def same_registrable_domain(left: str, right: str) -> bool:
    left_host = urllib.parse.urlparse(left).hostname or ""
    right_host = urllib.parse.urlparse(right).hostname or ""
    if left_host == right_host:
        return True
    left_parts = left_host.lower().split(".")
    right_parts = right_host.lower().split(".")
    if len(left_parts) < 2 or len(right_parts) < 2:
        return False
    return left_parts[-2:] == right_parts[-2:]


def allowed_scope(url: str, root_url: str, extra_allowed_hosts: set[str] | None = None) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if extra_allowed_hosts and host in extra_allowed_hosts:
        return True
    return same_registrable_domain(url, root_url)


def policy_link_score(text: str, url: str) -> int:
    haystack = f"{text} {url}".lower()
    score = 0
    if POLICY_LINK_RE.search(haystack):
        score += 20
    for strong in ["privacy", "cookie", "terms", "legal", "personal information", "个人信息", "隐私"]:
        if strong.lower() in haystack:
            score += 10
    if NOISE_LINK_RE.search(haystack):
        score -= 30
    if collector.is_apple_privacy_label_url(url):
        score -= 100
    return score


def select_policy_links(
    links: list[dict],
    base_url: str,
    root_url: str,
    extra_allowed_hosts: set[str] | None = None,
) -> list[dict[str, str]]:
    selected: list[tuple[int, dict[str, str]]] = []
    seen: set[str] = set()
    for link in links:
        raw_url = str(link.get("url") or link.get("href") or "").strip()
        if not raw_url:
            continue
        url = collector.normalize_url(raw_url, base_url)
        canonical = canonicalize_url(url)
        if canonical in seen:
            continue
        if not allowed_scope(url, root_url, extra_allowed_hosts=extra_allowed_hosts):
            continue
        text = str(link.get("text") or "").strip()
        score = policy_link_score(text, url)
        if score <= 0:
            continue
        seen.add(canonical)
        selected.append((score, {"text": text, "url": url, "canonical_url": canonical}))
    selected.sort(key=lambda item: (-item[0], item[1]["url"]))
    return [item[1] for item in selected]


def safe_filename(url: str, index: int) -> str:
    parsed = urllib.parse.urlparse(url)
    stem = f"{parsed.netloc}{parsed.path}".strip("/") or "policy"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-").lower()
    return f"{index:03d}-{stem[:90] or 'policy'}"


def common_related_url_candidates(root_url: str) -> list[dict[str, str]]:
    parsed = urllib.parse.urlparse(root_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return [
        {
            "text": f"common-path:{path}",
            "url": origin + path,
            "canonical_url": canonicalize_url(origin + path),
        }
        for path in COMMON_RELATED_PATHS
    ]


def page_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
    return None


def write_links(path: Path, links: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for link in links:
            handle.write(json.dumps(link, ensure_ascii=False, sort_keys=True) + "\n")


def archive_node(
    url: str,
    html_text: str,
    output_dir: Path,
    index: int,
    role: str,
    depth: int,
    min_chars: int,
    require_quality: bool = False,
) -> tuple[ClusterNode, list[dict]]:
    text = collector.html_to_text(html_text)
    markdown, method, links = collector.best_markdown_and_links(html_text, url)
    quality, quality_reason = collector.policy_text_quality(text, min_chars)
    if require_quality and quality == "too_short":
        raise QualityError(quality_reason or f"extracted text for {url} is too short")
    stem = safe_filename(url, index)
    node_dir = output_dir / "nodes"
    html_path = node_dir / f"{stem}.html"
    markdown_path = node_dir / f"{stem}.md"
    text_path = node_dir / f"{stem}.txt"
    links_path = node_dir / f"{stem}.links.jsonl"

    collector.write_text(html_path, html_text)
    collector.write_text(markdown_path, markdown)
    collector.write_text(text_path, text)
    write_links(links_path, links)

    node = ClusterNode(
        url=url,
        canonical_url=canonicalize_url(url),
        role=role,
        depth=depth,
        title=page_title(markdown),
        html_path=str(html_path),
        markdown_path=str(markdown_path),
        text_path=str(text_path),
        links_path=str(links_path),
        text_sha256=collector.sha256_text(text),
        text_chars=len(text),
        text_quality=quality,
        text_quality_reason=quality_reason,
        markdown_method=method,
        links_count=len(links),
    )
    return node, links


def collect_policy_cluster(
    root_url: str,
    output_dir: str | Path,
    fetch_text: FetchText,
    max_depth: int = 1,
    max_docs: int = 12,
    min_chars: int = 200,
    extra_allowed_hosts: set[str] | None = None,
    probe_common_paths: bool = False,
) -> dict:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    queue = deque([(root_url, 0, "root", None, None)])
    seen: set[str] = set()
    nodes: list[ClusterNode] = []
    edges: list[dict] = []
    errors: list[dict] = []

    while queue and len(nodes) < max_docs:
        url, depth, role, source_url, link_text = queue.popleft()
        canonical = canonicalize_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            html_text = fetch_text(url)
            node, links = archive_node(
                url,
                html_text,
                output_path,
                len(nodes) + 1,
                role,
                depth,
                min_chars,
                require_quality=bool(link_text and str(link_text).startswith("common-path:")),
            )
        except Exception as exc:
            errors.append({"url": url, "error_class": type(exc).__name__, "error_message": str(exc), "depth": depth})
            continue

        nodes.append(node)
        if source_url:
            edges.append(
                {
                    "from": canonicalize_url(source_url),
                    "to": node.canonical_url,
                    "text": link_text or "",
                    "depth": depth,
                }
            )
        if depth >= max_depth:
            continue
        next_links = select_policy_links(
            links,
            base_url=url,
            root_url=root_url,
            extra_allowed_hosts=extra_allowed_hosts,
        )
        if depth == 0 and probe_common_paths:
            existing = {link["canonical_url"] for link in next_links}
            for candidate in common_related_url_candidates(root_url):
                if candidate["canonical_url"] not in existing and candidate["canonical_url"] != canonical:
                    next_links.append(candidate)
                    existing.add(candidate["canonical_url"])
        for link in next_links:
            if link["canonical_url"] not in seen and len(nodes) + len(queue) < max_docs:
                queue.append((link["url"], depth + 1, "linked", url, link.get("text")))

    manifest = {
        "root_url": root_url,
        "canonical_root_url": canonicalize_url(root_url),
        "created_at": started_at,
        "max_depth": max_depth,
        "max_docs": max_docs,
        "nodes": [dataclasses.asdict(node) for node in nodes],
        "edges": edges,
        "errors": errors,
    }
    manifest_path = output_path / "cluster.json"
    collector.write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "manifest_path": str(manifest_path),
        "nodes_count": len(nodes),
        "edges_count": len(edges),
        "errors_count": len(errors),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive a privacy policy protocol cluster.")
    parser.add_argument("url")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--max-docs", type=int, default=12)
    parser.add_argument("--min-chars", type=int, default=200)
    parser.add_argument("--probe-common-paths", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def fetch(url: str) -> str:
        return collector.request_text(url, args.timeout, args.user_agent, proxy=args.proxy)

    result = collect_policy_cluster(
        root_url=args.url,
        output_dir=args.output_dir,
        fetch_text=fetch,
        max_depth=args.max_depth,
        max_docs=args.max_docs,
        min_chars=args.min_chars,
        probe_common_paths=args.probe_common_paths,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
