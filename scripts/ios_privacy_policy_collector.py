#!/usr/bin/env python3
"""
Collect iOS App Store privacy policy URLs and policy text.

Inputs can be App Store numeric IDs, App Store URLs, bundle IDs, or search terms.
The script uses Apple's public iTunes Search API to resolve app metadata, then
fetches the App Store HTML page and extracts the developer privacy policy URL.

It intentionally treats the App Store privacy label as supporting metadata, not
as the policy text itself.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator


ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
APPLE_RSS_TOP_FREE_URL = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-free/{limit}/apps.json"
APPLE_RSS_TOP_PAID_URL = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-paid/{limit}/apps.json"
DEFAULT_USER_AGENT = "privacy-policy-ios-collector/0.1 (+research; contact: local)"

PRIVACY_JSON_FIELD_RE = re.compile(
    r'"(?P<key>privacyPolicyUrl|privacyPolicyURL|privacyUrl|privacyURL)"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
APP_ID_RE = re.compile(r"(?:^|[/\?&])id(?P<id>\d{5,})(?:[/?&#]|$)")
NUMERIC_ID_RE = re.compile(r"^\d{5,}$")
ID_PREFIX_RE = re.compile(r"^id(?P<id>\d{5,})$")
TEXT_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
PRIVACY_CONTEXT_RE = re.compile(r"privacy(?:\s+policy)?", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class AppRecord:
    app_id: str
    name: str | None
    bundle_id: str | None
    seller_name: str | None
    app_store_url: str | None
    seller_url: str | None
    source: str
    raw: dict


@dataclasses.dataclass(frozen=True)
class PrivacyUrlResult:
    url: str
    method: str
    evidence: str | None = None


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href", "")
        if href:
            self._current = {
                "href": href,
                "text": "",
                "aria-label": values.get("aria-label", ""),
                "class": values.get("class", ""),
            }

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current is not None:
            self.links.append(self._current)
            self._current = None


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        rough = " ".join(self.parts)
        rough = re.sub(r"[ \t\r\f\v]+", " ", rough)
        rough = re.sub(r" *\n+ *", "\n", rough)
        rough = re.sub(r"\n{3,}", "\n\n", rough)
        return html.unescape(rough).strip()


class MarkdownLinkExtractor(HTMLParser):
    HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    BLOCK_TAGS = TextExtractor.BLOCK_TAGS
    SKIP_TAGS = TextExtractor.SKIP_TAGS

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0
        self._link_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in self.HEADING_LEVELS:
            self.parts.append("\n\n" + "#" * self.HEADING_LEVELS[tag] + " ")
        elif tag == "a" and values.get("href"):
            self._link_stack.append({"href": normalize_url(values["href"], self.base_url), "text": ""})
            self.parts.append("[")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a" and self._link_stack:
            link = self._link_stack.pop()
            text = link["text"].strip()
            self.parts.append(f"]({link['href']})")
            if text:
                self.links.append({"text": text, "url": link["href"]})
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            return
        self.parts.append(text)
        if self._link_stack:
            self._link_stack[-1]["text"] += text

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return html.unescape(text).strip()


def parse_app_id(value: str) -> str | None:
    value = value.strip()
    if NUMERIC_ID_RE.match(value):
        return value
    prefixed = ID_PREFIX_RE.match(value)
    if prefixed:
        return prefixed.group("id")
    match = APP_ID_RE.search(value)
    if match:
        return match.group("id")
    return None


def request_json_with_proxy(url: str, timeout: int, user_agent: str, proxy: str | None) -> dict:
    text = request_text(url, timeout=timeout, user_agent=user_agent, proxy=proxy)
    return json.loads(text)


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def request_text(url: str, timeout: int, user_agent: str, proxy: str | None = None) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    opener = build_opener(proxy)
    with opener.open(request, timeout=timeout) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, "replace")


def render_text_with_playwright(
    url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    wait_ms: int = 2000,
) -> str:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: python -m pip install playwright && python -m playwright install chromium") from exc

    launch_options: dict = {"headless": True}
    if proxy:
        launch_options["proxy"] = {"server": proxy}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


def itunes_lookup_url(app_id: str | None = None, bundle_id: str | None = None, country: str = "us") -> str:
    params: dict[str, str] = {"country": country, "entity": "software"}
    if app_id:
        params["id"] = app_id
    if bundle_id:
        params["bundleId"] = bundle_id
    return f"{ITUNES_LOOKUP_URL}?{urllib.parse.urlencode(params)}"


def itunes_search_url(term: str, country: str = "us", limit: int = 10) -> str:
    params = {
        "term": term,
        "country": country,
        "entity": "software",
        "media": "software",
        "limit": str(limit),
    }
    return f"{ITUNES_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def apple_rss_url(chart: str, country: str, limit: int) -> str:
    if chart == "top-free":
        return APPLE_RSS_TOP_FREE_URL.format(country=country, limit=limit)
    if chart == "top-paid":
        return APPLE_RSS_TOP_PAID_URL.format(country=country, limit=limit)
    raise ValueError(f"unsupported chart: {chart}")


def records_from_itunes_payload(payload: dict, source: str) -> Iterator[AppRecord]:
    for result in payload.get("results", []):
        app_id = result.get("trackId")
        if app_id is None:
            continue
        yield AppRecord(
            app_id=str(app_id),
            name=result.get("trackName"),
            bundle_id=result.get("bundleId"),
            seller_name=result.get("sellerName"),
            app_store_url=result.get("trackViewUrl"),
            seller_url=result.get("sellerUrl"),
            source=source,
            raw=result,
        )


def records_from_apple_rss_payload(payload: dict, source: str) -> Iterator[AppRecord]:
    for result in payload.get("feed", {}).get("results", []):
        app_id = result.get("id")
        if not app_id:
            continue
        yield AppRecord(
            app_id=str(app_id),
            name=result.get("name"),
            bundle_id=None,
            seller_name=result.get("artistName"),
            app_store_url=result.get("url"),
            seller_url=None,
            source=source,
            raw=result,
        )


def enrich_record_from_lookup(record: AppRecord, country: str, args: argparse.Namespace) -> AppRecord:
    if record.bundle_id and record.seller_url:
        return record
    try:
        payload = request_json_with_proxy(
            itunes_lookup_url(app_id=record.app_id, country=country),
            timeout=args.timeout,
            user_agent=args.user_agent,
            proxy=args.proxy,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return record
    lookup_records = list(records_from_itunes_payload(payload, f"{record.source}+lookup"))
    if not lookup_records:
        return record
    lookup = lookup_records[0]
    return AppRecord(
        app_id=record.app_id,
        name=lookup.name or record.name,
        bundle_id=lookup.bundle_id or record.bundle_id,
        seller_name=lookup.seller_name or record.seller_name,
        app_store_url=lookup.app_store_url or record.app_store_url,
        seller_url=lookup.seller_url or record.seller_url,
        source=lookup.source,
        raw={**record.raw, "lookup": lookup.raw},
    )


def decode_json_string(value: str) -> str:
    return json.loads(f'"{value}"')


def normalize_url(url: str, base_url: str) -> str:
    url = html.unescape(url.strip())
    if not url:
        return url
    return urllib.parse.urljoin(base_url, url)


def is_apple_privacy_label_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host.endswith("apps.apple.com") and (
        "/story/" in path
        or "/privacy/" in path
        or "app-privacy" in path
        or "privacy-label" in path
    ):
        return True
    if host in {"www.apple.com", "apple.com"} and "/legal/privacy/data/" in path and path.rstrip("/").endswith("/app-store"):
        return True
    if host in {"www.apple.com", "apple.com"} and (
        "/legal/privacy/" in path
        or "/privacy/" in path
        or path.rstrip("/").endswith("/privacy")
        or path.rstrip("/").endswith("/cookies")
    ):
        return True
    return False


def privacy_link_score(link: dict[str, str], base_url: str) -> int:
    url = normalize_url(link["href"], base_url)
    haystack = " ".join(
        [
            url.lower(),
            link.get("text", "").lower(),
            link.get("aria-label", "").lower(),
            link.get("class", "").lower(),
        ]
    )
    score = 0
    if "privacy policy" in haystack:
        score += 50
    if "privacy" in haystack:
        score += 20
    if "policy" in haystack:
        score += 10
    if "legal" in haystack:
        score += 5
    if is_apple_privacy_label_url(url):
        score -= 100
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        score -= 100
    return score


def extract_privacy_policy_url(html_text: str, base_url: str) -> PrivacyUrlResult | None:
    for match in PRIVACY_JSON_FIELD_RE.finditer(html_text):
        url = normalize_url(decode_json_string(match.group("url")), base_url)
        if url and not is_apple_privacy_label_url(url):
            return PrivacyUrlResult(url=url, method="json-field", evidence=match.group("key"))

    parser = LinkExtractor()
    parser.feed(html_text)
    candidates = [
        (privacy_link_score(link, base_url), link)
        for link in parser.links
        if privacy_link_score(link, base_url) > 0
    ]
    if not candidates:
        return extract_text_privacy_policy_url(html_text, base_url)
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][1]
    return PrivacyUrlResult(
        url=normalize_url(best["href"], base_url),
        method="anchor",
        evidence=(best.get("text") or best.get("aria-label") or best.get("href")).strip(),
    )


def extract_text_privacy_policy_url(html_text: str, base_url: str) -> PrivacyUrlResult | None:
    readable = html.unescape(html_to_text(html_text))
    matches = list(TEXT_URL_RE.finditer(readable))
    best: tuple[int, str, str] | None = None
    for match in matches:
        url = normalize_url(match.group(0).rstrip(".,;:"), base_url)
        if is_apple_privacy_label_url(url):
            continue
        start = max(0, match.start() - 80)
        end = min(len(readable), match.end() + 80)
        context = readable[start:end]
        score = 0
        if PRIVACY_CONTEXT_RE.search(context):
            score += 50
        if "privacy" in url.lower():
            score += 20
        if "policy" in url.lower():
            score += 10
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, url, context.strip())
    if best is None:
        return None
    return PrivacyUrlResult(url=best[1], method="text-url", evidence=best[2])


def extract_app_privacy_label_text(html_text: str) -> str:
    text = html_to_text(html_text)
    match = re.search(r"\bApp Privacy\b", text, re.IGNORECASE)
    if not match:
        return ""
    tail = text[match.start() :]
    stop = re.search(
        r"\n\s*(Information|Supports|What's New|Ratings and Reviews|More By|You Might Also Like)\b",
        tail,
        re.IGNORECASE,
    )
    if stop:
        tail = tail[: stop.start()]
    return tail.strip()


def html_to_text(html_text: str) -> str:
    parser = TextExtractor()
    parser.feed(html_text)
    return parser.text()


def html_to_markdown_and_links(html_text: str, base_url: str) -> tuple[str, list[dict[str, str]]]:
    parser = MarkdownLinkExtractor(base_url)
    parser.feed(html_text)
    return parser.markdown(), parser.links


def links_from_markdown(markdown: str, base_url: str) -> list[dict[str, str]]:
    links = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)\s]+)\)", markdown):
        links.append({"text": match.group(1).strip(), "url": normalize_url(match.group(2), base_url)})
    return links


def absolutize_markdown_links(markdown: str, base_url: str) -> str:
    def replace(match: re.Match[str]) -> str:
        text = match.group(1)
        url = normalize_url(match.group(2), base_url)
        return f"[{text}]({url})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", replace, markdown)


def best_markdown_and_links(html_text: str, base_url: str) -> tuple[str, str, list[dict[str, str]]]:
    internal_markdown, internal_links = html_to_markdown_and_links(html_text, base_url)

    try:
        import trafilatura  # type: ignore

        extracted = trafilatura.extract(
            html_text,
            url=base_url,
            output_format="markdown",
            include_links=True,
            include_comments=False,
            include_tables=True,
        )
        if extracted and len(extracted.strip()) >= 200:
            extracted = absolutize_markdown_links(extracted.strip(), base_url)
            links = links_from_markdown(extracted, base_url) or internal_links
            if links or not internal_links:
                return extracted, "trafilatura", links
    except Exception:
        pass

    try:
        from markdownify import markdownify as md  # type: ignore

        converted = md(html_text, heading_style="ATX", bullets="-")
        converted = re.sub(r"\n{3,}", "\n\n", converted).strip()
        converted = absolutize_markdown_links(converted, base_url)
        links = links_from_markdown(converted, base_url) or internal_links
        if converted and (links or not internal_links):
            return converted, "markdownify", links
    except Exception:
        pass

    return internal_markdown, "internal", internal_links


def common_privacy_url_candidates(base_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    paths = [
        "/privacy",
        "/privacy-policy",
        "/privacy_policy",
        "/legal/privacy",
        "/legal/privacy-policy",
        "/policies/privacy",
        "/policy/privacy",
        "/privacy.html",
        "/privacy-policy.html",
        "/en/privacy",
        "/en/privacy-policy",
    ]
    return [origin + path for path in paths]


def slug(value: str | None, fallback: str) -> str:
    value = value or fallback
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return value[:80] or fallback


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def app_output_dir(output_dir: str | Path, country: str, record: AppRecord) -> Path:
    app_slug = f"{record.app_id}-{slug(record.name, 'app')}"
    return Path(output_dir) / country / app_slug


def successful_row(row: dict, no_fetch_policy: bool) -> bool:
    if row.get("error"):
        return False
    if not row.get("app_id") or not row.get("country"):
        return False
    if no_fetch_policy:
        return bool(row.get("policy_url"))
    return int(row.get("policy_text_chars") or 0) > 0 and row.get("policy_text_quality") == "ok"


def completed_keys_from_rows(rows: Iterable[dict], no_fetch_policy: bool) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if successful_row(row, no_fetch_policy=no_fetch_policy):
            keys.add((str(row["country"]), str(row["app_id"])))
    return keys


def load_completed_keys(jsonl_path: str | Path, no_fetch_policy: bool) -> set[tuple[str, str]]:
    path = Path(jsonl_path)
    if not path.exists():
        return set()
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return completed_keys_from_rows(rows, no_fetch_policy=no_fetch_policy)


def iter_input_lines(args: argparse.Namespace) -> Iterator[tuple[str, str]]:
    for app_id in args.app_id or []:
        yield "app-id", app_id
    for bundle_id in args.bundle_id or []:
        yield "bundle-id", bundle_id
    for term in args.search or []:
        yield "search", term
    for chart in args.chart or []:
        yield "chart", chart
    if args.input:
        with Path(args.input).open("r", encoding="utf-8") as handle:
            for line in handle:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                yield "auto", value


def resolve_apps(
    input_type: str,
    value: str,
    country: str,
    search_limit: int,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
) -> list[AppRecord]:
    if input_type == "bundle-id":
        payload = request_json_with_proxy(
            itunes_lookup_url(bundle_id=value, country=country),
            timeout=timeout,
            user_agent=user_agent,
            proxy=proxy,
        )
        return list(records_from_itunes_payload(payload, "lookup:bundle-id"))

    if input_type == "search":
        payload = request_json_with_proxy(
            itunes_search_url(value, country=country, limit=search_limit),
            timeout=timeout,
            user_agent=user_agent,
            proxy=proxy,
        )
        return list(records_from_itunes_payload(payload, "search"))

    if input_type == "chart":
        payload = request_json_with_proxy(
            apple_rss_url(value, country=country, limit=search_limit),
            timeout=timeout,
            user_agent=user_agent,
            proxy=proxy,
        )
        return list(records_from_apple_rss_payload(payload, f"chart:{value}"))

    app_id = parse_app_id(value)
    if app_id:
        payload = request_json_with_proxy(
            itunes_lookup_url(app_id=app_id, country=country),
            timeout=timeout,
            user_agent=user_agent,
            proxy=proxy,
        )
        return list(records_from_itunes_payload(payload, "lookup:app-id"))

    if "." in value and " " not in value and "/" not in value:
        payload = request_json_with_proxy(
            itunes_lookup_url(bundle_id=value, country=country),
            timeout=timeout,
            user_agent=user_agent,
            proxy=proxy,
        )
        return list(records_from_itunes_payload(payload, "lookup:bundle-id"))

    payload = request_json_with_proxy(
        itunes_search_url(value, country=country, limit=search_limit),
        timeout=timeout,
        user_agent=user_agent,
        proxy=proxy,
    )
    return list(records_from_itunes_payload(payload, "search:auto"))


def policy_text_quality(text: str, min_chars: int) -> tuple[str, str | None]:
    normalized = text.strip()
    lower = normalized.lower()
    if len(normalized) < min_chars:
        return "too_short", f"extracted policy text has {len(normalized)} chars, below --min-policy-chars={min_chars}"
    blocker_markers = [
        "enable javascript",
        "please enable javascript",
        "access denied",
        "request blocked",
        "captcha",
        "verify you are human",
        "temporarily unavailable",
    ]
    for marker in blocker_markers:
        if marker in lower:
            return "possibly_blocked", f"policy text contains blocker marker: {marker}"
    privacy_terms = [
        "privacy",
        "personal data",
        "personal information",
        "隐私",
        "个人信息",
        "個人情報",
        "プライバシー",
        "개인정보",
        "개인 정보",
        "privacidad",
        "données personnelles",
        "datenschutz",
    ]
    if not any(term in lower for term in privacy_terms):
        return "weak_policy_signal", "policy text lacks common privacy-policy terms"
    return "ok", None


def fetch_policy_candidate(
    policy_url: str,
    record: AppRecord,
    args: argparse.Namespace,
    app_dir: Path,
    suffix: str,
) -> dict:
    policy_html = request_text(policy_url, args.timeout, args.user_agent, proxy=args.proxy)
    fetch_method = "static"
    policy_text = html_to_text(policy_html)
    policy_markdown, markdown_method, policy_links = best_markdown_and_links(policy_html, policy_url)
    quality, quality_reason = policy_text_quality(policy_text, args.min_policy_chars)
    if getattr(args, "js_fallback", False) and quality in {"too_short", "possibly_blocked"}:
        render_text = getattr(args, "render_text", render_text_with_playwright)
        rendered_html = render_text(
            policy_url,
            getattr(args, "js_timeout", args.timeout),
            args.user_agent,
            proxy=args.proxy,
            wait_ms=getattr(args, "js_wait_ms", 2000),
        )
        rendered_text = html_to_text(rendered_html)
        rendered_quality, rendered_quality_reason = policy_text_quality(rendered_text, args.min_policy_chars)
        if rendered_quality == "ok" or len(rendered_text) > len(policy_text):
            policy_html = rendered_html
            policy_text = rendered_text
            policy_markdown, markdown_method, policy_links = best_markdown_and_links(policy_html, policy_url)
            quality = rendered_quality
            quality_reason = rendered_quality_reason
            fetch_method = "js"

    policy_html_path = app_dir / f"privacy-policy{suffix}.html"
    policy_text_path = app_dir / f"privacy-policy{suffix}.txt"
    policy_markdown_path = app_dir / f"privacy-policy{suffix}.md"
    policy_links_path = app_dir / f"privacy-policy{suffix}.links.jsonl"

    write_text(policy_html_path, policy_html)
    write_text(policy_text_path, policy_text)
    write_text(policy_markdown_path, policy_markdown)
    policy_links_path.parent.mkdir(parents=True, exist_ok=True)
    with policy_links_path.open("w", encoding="utf-8") as handle:
        for link in policy_links:
            handle.write(json.dumps(link, ensure_ascii=False, sort_keys=True) + "\n")

    return {
        "policy_url": policy_url,
        "policy_html_path": str(policy_html_path),
        "policy_text_path": str(policy_text_path),
        "policy_markdown_path": str(policy_markdown_path),
        "policy_links_path": str(policy_links_path),
        "policy_fetch_method": fetch_method,
        "policy_markdown_method": markdown_method,
        "policy_text_sha256": sha256_text(policy_text),
        "policy_text_chars": len(policy_text),
        "policy_text_quality": quality,
        "policy_text_quality_reason": quality_reason,
        "policy_links_count": len(policy_links),
    }


def fallback_policy_urls(record: AppRecord, primary_url: str | None) -> Iterator[str]:
    seen: set[str] = set()
    for base in [record.seller_url]:
        if not base:
            continue
        for candidate in common_privacy_url_candidates(base):
            if candidate == primary_url or candidate in seen:
                continue
            seen.add(candidate)
            yield candidate


def collect_app(record: AppRecord, args: argparse.Namespace, country: str) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    app_dir = app_output_dir(args.output_dir, country, record)

    row = {
        "source_type": "ios_app",
        "crawl_time": started_at,
        "country": country,
        "app_id": record.app_id,
        "name": record.name,
        "bundle_id": record.bundle_id,
        "seller_name": record.seller_name,
        "app_store_url": record.app_store_url,
        "seller_url": record.seller_url,
        "itunes_source": record.source,
        "policy_url": None,
        "policy_url_method": None,
        "policy_url_evidence": None,
        "app_store_html_path": None,
        "policy_html_path": None,
        "policy_text_path": None,
        "policy_markdown_path": None,
        "policy_markdown_method": None,
        "policy_links_path": None,
        "policy_links_count": 0,
        "app_privacy_label_path": None,
        "policy_text_sha256": None,
        "policy_text_chars": 0,
        "policy_text_quality": None,
        "policy_text_quality_reason": None,
        "candidate_errors": [],
        "error": None,
    }

    if not record.app_store_url:
        row["error"] = "itunes result did not include trackViewUrl"
        return row

    try:
        app_html = request_text(record.app_store_url, args.timeout, args.user_agent, proxy=args.proxy)
        app_html_path = app_dir / "app-store.html"
        write_text(app_html_path, app_html)
        row["app_store_html_path"] = str(app_html_path)

        privacy_label = extract_app_privacy_label_text(app_html)
        if privacy_label:
            privacy_label_path = app_dir / "app-privacy-label.txt"
            write_text(privacy_label_path, privacy_label)
            row["app_privacy_label_path"] = str(privacy_label_path)

        privacy_result = extract_privacy_policy_url(app_html, record.app_store_url)
        if privacy_result:
            row["policy_url"] = privacy_result.url
            row["policy_url_method"] = privacy_result.method
            row["policy_url_evidence"] = privacy_result.evidence
        elif not args.try_common_paths:
            row["error"] = "privacy policy URL not found on App Store page"
            return row

        if args.no_fetch_policy:
            return row

        candidate_errors: list[str] = []
        candidate_urls = [privacy_result.url] if privacy_result else []
        if args.try_common_paths:
            candidate_urls.extend(fallback_policy_urls(record, privacy_result.url if privacy_result else None))
        if not candidate_urls:
            row["error"] = "privacy policy URL not found on App Store page"
            return row

        for index, candidate_url in enumerate(candidate_urls):
            suffix = "" if index == 0 else f"-fallback-{index}"
            try:
                previous_timeout = args.timeout
                if index > 0:
                    args.timeout = args.fallback_timeout
                candidate_row = fetch_policy_candidate(candidate_url, record, args, app_dir, suffix)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, UnicodeError) as exc:
                candidate_errors.append(f"{candidate_url}: {type(exc).__name__}: {exc}")
                continue
            finally:
                if index > 0:
                    args.timeout = previous_timeout
            row.update(candidate_row)
            if candidate_row["policy_text_quality"] == "ok":
                break
            candidate_errors.append(
                f"{candidate_url}: {candidate_row['policy_text_quality']}: {candidate_row['policy_text_quality_reason']}"
            )

        if row["policy_text_quality"] != "ok":
            row["error"] = "policy text not complete enough"
            row["candidate_errors"] = candidate_errors
        return row
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, UnicodeError) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect iOS App Store privacy policy URLs and policy text.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--app-id", action="append", help="App Store numeric ID, id-prefixed ID, or App Store URL.")
    parser.add_argument("--bundle-id", action="append", help="iOS bundle ID, for example com.example.app.")
    parser.add_argument("--search", action="append", help="App Store search term.")
    parser.add_argument(
        "--chart",
        action="append",
        choices=["top-free", "top-paid"],
        help="Seed from Apple's public RSS app charts.",
    )
    parser.add_argument("--input", help="UTF-8 text file with one app id, bundle id, App Store URL, or search term per line.")
    parser.add_argument("--country", action="append", default=None, help="Two-letter App Store country code. Repeat for multi-country runs.")
    parser.add_argument("--search-limit", type=int, default=5, help="Maximum app results per search term.")
    parser.add_argument("--output-dir", default="privacy-policy-access-literature/out/ios", help="Directory for HTML/text artifacts.")
    parser.add_argument("--jsonl", default="privacy-policy-access-literature/out/ios/results.jsonl", help="JSONL result path.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--fallback-timeout", type=int, default=12, help="HTTP timeout for common-path fallback candidates.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between app collections.")
    parser.add_argument("--user-agent", default=os.environ.get("IOS_POLICY_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"), help="Optional HTTP(S) proxy URL, for example http://127.0.0.1:7890.")
    parser.add_argument("--no-fetch-policy", action="store_true", help="Only discover policy URLs; do not fetch policy pages.")
    parser.add_argument("--min-policy-chars", type=int, default=1000, help="Minimum extracted text length to count as a full policy candidate.")
    parser.add_argument("--js-fallback", action="store_true", help="Use Playwright rendering when static policy HTML is too short or blocked.")
    parser.add_argument("--js-timeout", type=int, default=60, help="Playwright rendering timeout in seconds.")
    parser.add_argument("--js-wait-ms", type=int, default=2000, help="Extra wait after DOMContentLoaded before saving rendered HTML.")
    parser.add_argument("--try-common-paths", action="store_true", help="If the explicit policy URL is missing or too short, try common privacy paths on seller/app origins.")
    parser.add_argument("--enrich-lookup", action="store_true", help="For chart seeds, call iTunes lookup per app to add sellerUrl and bundleId before fetching policies.")
    parser.add_argument("--resume", action="store_true", help="Skip country/app pairs already completed in the JSONL output.")
    parser.add_argument("--max-apps", type=int, default=0, help="Stop after collecting this many new country/app pairs. 0 means no cap.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    inputs = list(iter_input_lines(args))
    if not inputs:
        parser.error("provide --app-id, --bundle-id, --search, or --input")

    countries = args.country or ["us"]
    seen_app_ids: set[str] = set()
    completed = load_completed_keys(args.jsonl, args.no_fetch_policy) if args.resume else set()
    wrote = 0
    exit_code = 0
    for country in countries:
        for input_type, value in inputs:
            try:
                records = resolve_apps(
                    input_type=input_type,
                    value=value,
                    country=country,
                    search_limit=args.search_limit,
                    timeout=args.timeout,
                    user_agent=args.user_agent,
                    proxy=args.proxy,
                )
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                append_jsonl(
                    Path(args.jsonl),
                    {
                        "source_type": "ios_app",
                        "input_type": input_type,
                        "input": value,
                        "country": country,
                        "crawl_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "error": f"resolve failed: {type(exc).__name__}: {exc}",
                    },
                )
                exit_code = 1
                continue

            if not records:
                append_jsonl(
                    Path(args.jsonl),
                    {
                        "source_type": "ios_app",
                        "input_type": input_type,
                        "input": value,
                        "country": country,
                        "crawl_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "error": "no app records found",
                    },
                )
                exit_code = 1
                continue

            for record in records:
                if args.enrich_lookup:
                    record = enrich_record_from_lookup(record, country, args)
                seen_key = (country, record.app_id)
                if seen_key in seen_app_ids or seen_key in completed:
                    continue
                if args.max_apps and wrote >= args.max_apps:
                    return 0
                seen_app_ids.add(seen_key)
                row = collect_app(record, args, country=country)
                row["input_type"] = input_type
                row["input"] = value
                append_jsonl(Path(args.jsonl), row)
                wrote += 1
                print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
                if successful_row(row, no_fetch_policy=args.no_fetch_policy):
                    completed.add(seen_key)
                if row.get("error"):
                    exit_code = 1
                time.sleep(max(args.sleep, 0))

    return exit_code if wrote == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
