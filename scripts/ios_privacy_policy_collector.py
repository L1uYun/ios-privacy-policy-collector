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
import codecs
import dataclasses
import hashlib
import html
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
APPLE_RSS_TOP_FREE_URL = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-free/{limit}/apps.json"
APPLE_RSS_TOP_PAID_URL = "https://rss.applemarketingtools.com/api/v2/{country}/apps/top-paid/{limit}/apps.json"
DEFAULT_USER_AGENT = "privacy-policy-ios-collector/0.1 (+research; contact: local)"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PRIVACY_JSON_FIELD_RE = re.compile(
    r'"(?P<key>privacyPolicyUrl|privacyPolicyURL|privacyUrl|privacyURL)"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
APP_STORE_EXTERNAL_JSON_FIELD_RE = re.compile(
    r'"(?P<key>developerWebsite|developerWebsiteUrl|appSupport|appSupportUrl|websiteUrl|sellerWebsiteUrl)"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
APP_ID_RE = re.compile(r"(?:^|[/\?&])id(?P<id>\d{5,})(?:[/?&#]|$)")
NUMERIC_ID_RE = re.compile(r"^\d{5,}$")
ID_PREFIX_RE = re.compile(r"^id(?P<id>\d{5,})$")
TEXT_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
PRIVACY_CONTEXT_RE = re.compile(r"privacy(?:\s+policy)?", re.IGNORECASE)
SEARCH_RESULT_NOISE_HOSTS = {
    "bing.com",
    "duckduckgo.com",
    "google.com",
    "apps.apple.com",
    "itunes.apple.com",
    "apple.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "play.google.com",
    "support.apple.com",
    "twitter.com",
    "x.com",
    "youtube.com",
}
SHORTLINK_HOSTS = {
    "bit.ly",
    "buff.ly",
    "cutt.ly",
    "goo.gl",
    "is.gd",
    "lnkd.in",
    "on.fb.me",
    "ow.ly",
    "rebrand.ly",
    "t.co",
    "tiny.cc",
    "tinyurl.com",
}


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


class UnsupportedDocumentError(RuntimeError):
    """Raised when a policy URL returns content we cannot archive as text."""


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


def request_json_with_proxy(url: str, timeout: int, user_agent: str, proxy: str | None, retries: int = 2) -> dict:
    text = request_text(url, timeout=timeout, user_agent=user_agent, proxy=proxy, retries=retries)
    return json.loads(text)


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def is_pdf_response(content: bytes, content_type: str | None, url: str) -> bool:
    lower_type = content_type.lower() if isinstance(content_type, str) else ""
    lower_path = urllib.parse.urlparse(url).path.lower()
    return "application/pdf" in lower_type or content.lstrip().startswith(b"%PDF-") or lower_path.endswith(".pdf")


def looks_like_binary(content: bytes, content_type: str | None) -> bool:
    lower_type = content_type.lower() if isinstance(content_type, str) else ""
    if lower_type.startswith(("text/", "application/json", "application/xml", "application/xhtml+xml")):
        return False
    if b"\x00" in content[:4096]:
        return True
    sample = content[:4096]
    if not sample:
        return False
    control = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control / len(sample) > 0.15


def extract_pdf_text(content: bytes, url: str) -> str:
    errors: list[str] = []
    text = ""
    try:
        from pypdf import PdfReader  # type: ignore
        try:
            reader = PdfReader(io.BytesIO(content))
            page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
            text = "\n\n".join(page for page in page_texts if page).strip()
        except Exception as exc:
            errors.append(f"pypdf: {exc}")
    except ImportError:
        errors.append("pypdf: not installed")

    if not text:
        try:
            import fitz  # type: ignore

            with fitz.open(stream=content, filetype="pdf") as document:
                page_texts = [page.get_text("text").strip() for page in document]
            text = "\n\n".join(page for page in page_texts if page).strip()
        except Exception as exc:
            errors.append(f"pymupdf: {exc}")

    if not text:
        raise UnsupportedDocumentError("PDF policy did not contain extractable text; " + "; ".join(errors))
    title = Path(urllib.parse.urlparse(url).path).name or "privacy-policy.pdf"
    return f"# {title}\n\n{text}\n"


def request_text(
    url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    retries: int = 2,
    retry_sleep: float = 1.0,
) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    request = urllib.request.Request(url, headers=headers)
    opener = build_opener(proxy)
    retryable_exceptions = (
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        TimeoutError,
        urllib.error.URLError,
    )
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                content = response.read()
                content_type = response.headers.get("Content-Type")
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if is_pdf_response(content, content_type, final_url):
                    return extract_pdf_text(content, final_url)
                if looks_like_binary(content, content_type):
                    raise UnsupportedDocumentError(f"unsupported binary policy response: content-type={content_type or 'unknown'}")
                try:
                    charset = response.headers.get_content_charset() or "utf-8"
                except LookupError:
                    charset = "utf-8"
                try:
                    codecs.lookup(charset)
                except LookupError:
                    charset = "utf-8"
                return content.decode(charset, "replace")
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            if retry_sleep:
                time.sleep(retry_sleep * (attempt + 1))
    raise RuntimeError(f"request failed without response: {last_exc}")


def request_final_url(
    url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    retries: int = 1,
    retry_sleep: float = 0.5,
) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = build_opener(proxy)
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.geturl() if hasattr(response, "geturl") else url
        except (http.client.RemoteDisconnected, TimeoutError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            if retry_sleep:
                time.sleep(retry_sleep * (attempt + 1))
    raise RuntimeError(f"final URL request failed without response: {last_exc}")


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
            context = browser.new_context(
                user_agent=user_agent or DEFAULT_BROWSER_USER_AGENT,
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                },
            )
            page = context.new_page()
            blocked_resource_types = {"image", "media", "font"}
            blocked_url_fragments = [
                "doubleclick",
                "googletagmanager",
                "google-analytics",
                "facebook.net",
                "analytics",
                "adsystem",
                "/ads/",
            ]

            def route_request(route):
                request = route.request
                lower_url = request.url.lower()
                if request.resource_type in blocked_resource_types or any(fragment in lower_url for fragment in blocked_url_fragments):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_request)
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


def is_apple_platform_policy_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path.lower()
    if not host:
        return False
    if host == "apps.apple.com" or host.endswith(".apps.apple.com"):
        return True
    if re.fullmatch(r"(?:www\.)?apple\.[a-z.]+", host) and (
        "/legal/" in path
        or "/privacy/" in path
        or path.rstrip("/").endswith("/privacy")
        or path.rstrip("/").endswith("/cookies")
    ):
        return True
    return False


def is_apple_privacy_label_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path.lower()
    if (host == "apps.apple.com" or host.endswith(".apps.apple.com")) and (
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
    if is_apple_platform_policy_url(url):
        score -= 100
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        score -= 100
    return score


def extract_privacy_policy_url(html_text: str, base_url: str) -> PrivacyUrlResult | None:
    for match in PRIVACY_JSON_FIELD_RE.finditer(html_text):
        url = normalize_url(decode_json_string(match.group("url")), base_url)
        if url and not is_apple_platform_policy_url(url):
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
        if is_apple_platform_policy_url(url):
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


def app_store_external_link_score(link: dict[str, str], base_url: str) -> int:
    url = normalize_url(link["href"], base_url)
    if is_apple_platform_policy_url(url):
        return -100
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return -100
    host = (parsed.hostname or "").lower()
    if host.endswith("apple.com") or host.endswith("apple.com.cn"):
        return -100
    haystack = " ".join(
        [
            url.lower(),
            link.get("text", "").lower(),
            link.get("aria-label", "").lower(),
            link.get("class", "").lower(),
        ]
    )
    score = 0
    if "developer website" in haystack or "developer" in haystack:
        score += 50
    if "app support" in haystack or "support" in haystack:
        score += 40
    if "website" in haystack:
        score += 20
    if "privacy" in haystack:
        score += 30
    if privacy_candidate_score_url(url) > 0:
        score += 20
    if parsed.netloc:
        score += 5
    return score


def extract_app_store_external_links(html_text: str, base_url: str, limit: int = 5) -> list[dict]:
    def add_candidate(candidates: list[dict], seen: set[str], url: str, source: str, evidence: str, score: int) -> None:
        if not url or url in seen:
            return
        if is_apple_platform_policy_url(url):
            return
        seen.add(url)
        candidates.append(
            {
                "url": url,
                "source": source,
                "browser_first": False,
                "evidence": evidence,
                "score": str(score),
            }
        )

    candidates: list[dict] = []
    seen: set[str] = set()

    for match in APP_STORE_EXTERNAL_JSON_FIELD_RE.finditer(html_text):
        url = normalize_url(decode_json_string(match.group("url")), base_url)
        score = 100 if match.group("key").lower().startswith("developer") else 80
        add_candidate(candidates, seen, url, "app-store-external-json", match.group("key"), score)
        if len(candidates) >= limit:
            return candidates

    parser = LinkExtractor()
    parser.feed(html_text)
    scored = []
    for link in parser.links:
        score = app_store_external_link_score(link, base_url)
        if score <= 0:
            continue
        scored.append((score, normalize_url(link["href"], base_url), link))
    scored.sort(key=lambda item: item[0], reverse=True)
    for score, url, link in scored:
        add_candidate(candidates, seen, url, "app-store-external-link", (link.get("text") or link.get("aria-label") or link.get("href")).strip(), score)
        if len(candidates) >= limit:
            break
    return candidates


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


def fallback_text_markdown(text: str, source_url: str) -> str:
    return f"# Privacy Policy\n\nSource: [{source_url}]({source_url})\n\n{text.strip()}\n"


def ensure_markdown_complete(markdown: str, text: str, source_url: str, min_chars: int) -> tuple[str, str, list[dict[str, str]]]:
    if len(markdown.strip()) >= min_chars or len(text.strip()) < min_chars:
        return markdown, "", []
    return fallback_text_markdown(text, source_url), "text-fallback", [{"text": source_url, "url": source_url}]


def ensure_source_link(links: list[dict[str, str]], source_url: str) -> list[dict[str, str]]:
    if any(link.get("url") == source_url for link in links):
        return links
    return [{"text": source_url, "url": source_url}, *links]


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
        "/policies/privacy-policy",
        "/policy/privacy",
        "/privacy/notice",
        "/privacy-notice",
        "/privacy-center",
        "/privacy/security",
        "/legal",
        "/legal/privacy-notice",
        "/legal/privacy-center",
        "/privacy.html",
        "/privacy-policy.html",
        "/en/privacy",
        "/en/privacy-policy",
        "/terms",
        "/terms-of-service",
        "/terms-of-use",
        "/legal/terms",
        "/legal/terms-of-service",
        "/legal/terms-of-use",
        "/policies/terms",
        "/policies/terms-of-service",
    ]
    return [origin + path for path in paths]


def origin_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def is_shortlink_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host in SHORTLINK_HOSTS


def is_search_result_noise_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if not host:
        return True
    if is_apple_platform_policy_url(url):
        return True
    return any(host == noise or host.endswith("." + noise) for noise in SEARCH_RESULT_NOISE_HOSTS)


def web_search_url_candidates(
    query: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    limit: int = 6,
    endpoint: str = "https://html.duckduckgo.com/html/",
) -> list[dict]:
    if not query.strip():
        return []
    payload = urllib.parse.urlencode({"q": query})
    search_url = endpoint + ("&" if "?" in endpoint else "?") + payload
    html_text = request_text(search_url, timeout, user_agent, proxy=proxy, retries=0)
    parser = LinkExtractor()
    parser.feed(html_text)
    candidates: list[dict] = []
    seen: set[str] = set()
    scored: list[tuple[int, str, dict[str, str]]] = []
    for link in parser.links:
        href = normalize_url(link.get("href", ""), search_url)
        parsed = urllib.parse.urlparse(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            query_params = urllib.parse.parse_qs(parsed.query)
            if query_params.get("uddg"):
                href = urllib.parse.unquote(query_params["uddg"][0])
        if href in seen or is_search_result_noise_url(href):
            continue
        seen.add(href)
        haystack = f"{link.get('text', '')} {href}".lower()
        score = privacy_candidate_score_url(href)
        if "privacy" in haystack:
            score += 60
        if "policy" in haystack:
            score += 20
        if "official" in haystack or "developer" in haystack:
            score += 10
        if urllib.parse.urlparse(href).scheme not in {"http", "https"}:
            continue
        scored.append((score, href, link))
    scored.sort(key=lambda item: item[0], reverse=True)
    for score, href, link in scored[:limit]:
        candidates.append(
            {
                "url": href,
                "source": "web-search",
                "browser_first": False,
                "evidence": (link.get("text") or href).strip(),
                "score": str(score),
            }
        )
    return candidates


def expand_shortlink_candidate(
    url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
) -> dict | None:
    if not is_shortlink_url(url):
        return None
    final_url = request_final_url(url, timeout=timeout, user_agent=user_agent, proxy=proxy)
    if final_url and final_url != url and urllib.parse.urlparse(final_url).scheme in {"http", "https"}:
        return candidate_dict(final_url, "shortlink-expand", False)
    return None


def sitemap_urls_from_xml(xml_text: str, base_url: str, max_urls: int = 100) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    urls: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "loc" or not element.text:
            continue
        url = normalize_url(element.text.strip(), base_url)
        if url:
            urls.append(url)
        if len(urls) >= max_urls:
            break
    return urls


def privacy_candidate_score_url(url: str) -> int:
    lower = url.lower()
    score = 0
    for token, value in [
        ("privacy-policy", 80),
        ("privacy_policy", 80),
        ("privacy-notice", 75),
        ("privacy/notice", 75),
        ("privacy", 50),
        ("datenschutz", 50),
        ("privacidad", 50),
        ("legal", 15),
        ("policy", 10),
    ]:
        if token in lower:
            score += value
    for token in ["/tag/", "/category/", "/author/", "/feed", ".jpg", ".png", ".css", ".js"]:
        if token in lower:
            score -= 80
    return score


def sitemap_policy_candidates(
    base_url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    max_sitemaps: int = 3,
    max_urls: int = 80,
) -> list[dict]:
    origin = origin_from_url(base_url)
    if not origin:
        return []
    sitemap_urls = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    candidates: list[dict] = []
    seen_sitemaps: set[str] = set()
    seen_candidates: set[str] = set()
    for sitemap_url in sitemap_urls:
        if sitemap_url in seen_sitemaps or len(seen_sitemaps) >= max_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            xml_text = request_text(sitemap_url, timeout, user_agent, proxy=proxy, retries=0)
        except Exception:
            continue
        locs = sitemap_urls_from_xml(xml_text, sitemap_url, max_urls=max_urls)
        nested = [url for url in locs if "sitemap" in url.lower()][: max_sitemaps - len(seen_sitemaps)]
        for nested_url in nested:
            if nested_url in seen_sitemaps or len(seen_sitemaps) >= max_sitemaps:
                continue
            seen_sitemaps.add(nested_url)
            try:
                nested_xml = request_text(nested_url, timeout, user_agent, proxy=proxy, retries=0)
            except Exception:
                continue
            locs.extend(sitemap_urls_from_xml(nested_xml, nested_url, max_urls=max_urls))
        scored = sorted(((privacy_candidate_score_url(url), url) for url in locs), reverse=True)
        for score, url in scored:
            if score <= 0 or url in seen_candidates or is_apple_platform_policy_url(url):
                continue
            seen_candidates.add(url)
            candidates.append(candidate_dict(url, "sitemap", False))
    return candidates


def robots_sitemap_policy_candidates(
    base_url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    max_sitemaps: int = 4,
) -> list[dict]:
    origin = origin_from_url(base_url)
    if not origin:
        return []
    try:
        robots_text = request_text(origin + "/robots.txt", timeout, user_agent, proxy=proxy, retries=0)
    except Exception:
        return []
    sitemap_urls = []
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_urls.append(line.split(":", 1)[1].strip())
    candidates: list[dict] = []
    seen: set[str] = set()
    for sitemap_url in sitemap_urls[:max_sitemaps]:
        try:
            xml_text = request_text(sitemap_url, timeout, user_agent, proxy=proxy, retries=0)
        except Exception:
            continue
        scored = sorted(
            ((privacy_candidate_score_url(url), url) for url in sitemap_urls_from_xml(xml_text, sitemap_url)),
            reverse=True,
        )
        for score, url in scored:
            if score <= 0 or url in seen or is_apple_platform_policy_url(url):
                continue
            seen.add(url)
            candidates.append(candidate_dict(url, "robots-sitemap", False))
    return candidates


def seller_home_policy_candidates(
    seller_url: str,
    timeout: int,
    user_agent: str,
    proxy: str | None = None,
    render_text=None,
    js_fallback: bool = False,
    js_timeout: int | None = None,
    js_wait_ms: int = 2000,
) -> list[dict]:
    if not seller_url:
        return []
    candidates: list[dict] = []
    try:
        html_text = request_text(seller_url, timeout, user_agent, proxy=proxy)
    except Exception:
        html_text = ""
    if html_text:
        result = extract_privacy_policy_url(html_text, seller_url)
        if result and not is_apple_platform_policy_url(result.url):
            candidates.append(candidate_dict(result.url, f"seller-home:{result.method}", False))
    if js_fallback and not candidates:
        renderer = render_text or render_text_with_playwright
        try:
            rendered_html = renderer(
                seller_url,
                js_timeout or timeout,
                DEFAULT_BROWSER_USER_AGENT,
                proxy=proxy,
                wait_ms=js_wait_ms,
            )
        except Exception:
            rendered_html = ""
        if rendered_html:
            result = extract_privacy_policy_url(rendered_html, seller_url)
            if result and not is_apple_platform_policy_url(result.url):
                candidates.append(candidate_dict(result.url, f"seller-home-js:{result.method}", False))
    return candidates


def default_domain_rules_path() -> Path:
    return SCRIPT_DIR.parent / "config" / "domain_rules.json"


def load_domain_rules(path: str | Path | None) -> list[dict]:
    if not path:
        path = default_domain_rules_path()
    rules_path = Path(path)
    if not rules_path.exists():
        return []
    payload = json.loads(rules_path.read_text(encoding="utf-8"))
    return list(payload.get("rules") or [])


def host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def rule_matches_record(rule: dict, record: AppRecord) -> bool:
    match = rule.get("match") or {}
    seller = (record.seller_name or "").lower()
    name = (record.name or "").lower()
    bundle = (record.bundle_id or "").lower()
    seller_host = (urllib.parse.urlparse(record.seller_url or "").hostname or "").lower()
    for value in match.get("seller_contains") or []:
        needle = str(value).lower()
        if needle and (needle in seller or needle in name):
            return True
    for prefix in match.get("bundle_prefixes") or []:
        if bundle.startswith(str(prefix).lower()):
            return True
    if seller_host and host_matches(seller_host, match.get("seller_hosts") or []):
        return True
    return False


def domain_rule_policy_candidates(record: AppRecord, rules_path: str | Path | None = None) -> list[dict]:
    candidates: list[dict] = []
    for rule in load_domain_rules(rules_path):
        if not rule_matches_record(rule, record):
            continue
        for url in rule.get("policy_urls") or []:
            candidates.append(
                {
                    "url": str(url),
                    "source": f"domain-rule:{rule.get('id') or 'unknown'}",
                    "browser_first": bool(rule.get("browser_first")),
                }
            )
    return candidates


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
    source: str = "primary",
    browser_first: bool = False,
) -> dict:
    attempt = {
        "policy_url": policy_url,
        "canonical_policy_url": policy_url,
        "attempt_index": None,
        "source": source,
        "status": "started",
        "fetch_method": "js" if browser_first else "static",
        "text_chars": 0,
        "quality": None,
        "quality_reason": None,
        "error_class": None,
        "error_message": None,
    }
    if is_apple_platform_policy_url(policy_url):
        raise ValueError(f"refusing Apple platform policy URL: {policy_url}")
    if browser_first:
        render_text = getattr(args, "render_text", render_text_with_playwright)
        policy_html = render_text(
            policy_url,
            getattr(args, "js_timeout", args.timeout),
            getattr(args, "browser_user_agent", DEFAULT_BROWSER_USER_AGENT),
            proxy=args.proxy,
            wait_ms=getattr(args, "js_wait_ms", 2000),
        )
        fetch_method = "js"
    else:
        policy_html = request_text(policy_url, args.timeout, args.user_agent, proxy=args.proxy)
        fetch_method = "static"
    policy_text = html_to_text(policy_html)
    policy_markdown, markdown_method, policy_links = best_markdown_and_links(policy_html, policy_url)
    quality, quality_reason = policy_text_quality(policy_text, args.min_policy_chars)
    fallback_markdown, fallback_method, fallback_links = ensure_markdown_complete(
        policy_markdown,
        policy_text,
        policy_url,
        args.min_policy_chars,
    )
    if fallback_method:
        policy_markdown = fallback_markdown
        markdown_method = fallback_method
        policy_links = fallback_links + policy_links
    policy_links = ensure_source_link(policy_links, policy_url)
    if getattr(args, "js_fallback", False) and quality in {"too_short", "possibly_blocked"}:
        render_text = getattr(args, "render_text", render_text_with_playwright)
        rendered_html = render_text(
            policy_url,
            getattr(args, "js_timeout", args.timeout),
            getattr(args, "browser_user_agent", DEFAULT_BROWSER_USER_AGENT),
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
    fallback_markdown, fallback_method, fallback_links = ensure_markdown_complete(
        policy_markdown,
        policy_text,
        policy_url,
        args.min_policy_chars,
    )
    if fallback_method:
        policy_markdown = fallback_markdown
        markdown_method = fallback_method
        policy_links = fallback_links + policy_links
    policy_links = ensure_source_link(policy_links, policy_url)
    attempt.update(
        {
            "status": "accepted" if quality == "ok" else "rejected",
            "fetch_method": fetch_method,
            "text_chars": len(policy_text),
            "quality": quality,
            "quality_reason": quality_reason,
        }
    )

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
        "policy_url_attempt": attempt,
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


def candidate_dict(url: str, source: str, browser_first: bool = False) -> dict:
    return {"url": url, "source": source, "browser_first": browser_first}


def append_policy_candidate(candidates: list[dict], seen: set[str], candidate: dict | None) -> None:
    if not candidate:
        return
    url = candidate.get("url")
    if not url or url in seen or is_apple_platform_policy_url(url):
        return
    candidates.append(candidate)
    seen.add(url)


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
        "policy_url_attempts": [],
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
        candidate_urls: list[dict] = []
        seen_candidates: set[str] = set()
        for candidate in domain_rule_policy_candidates(record, getattr(args, "domain_rules", None)):
            append_policy_candidate(candidate_urls, seen_candidates, candidate)
        if privacy_result and privacy_result.url not in seen_candidates:
            append_policy_candidate(candidate_urls, seen_candidates, candidate_dict(privacy_result.url, f"app-store:{privacy_result.method}", False))
        if args.try_common_paths and record.seller_url:
            try:
                append_policy_candidate(
                    candidate_urls,
                    seen_candidates,
                    expand_shortlink_candidate(record.seller_url, args.fallback_timeout, args.user_agent, args.proxy),
                )
            except Exception as exc:
                candidate_errors.append(f"{record.seller_url}: shortlink-expand: {type(exc).__name__}: {exc}")
            for seller_candidate in seller_home_policy_candidates(
                record.seller_url,
                args.fallback_timeout,
                args.user_agent,
                proxy=args.proxy,
                render_text=getattr(args, "render_text", render_text_with_playwright),
                js_fallback=bool(getattr(args, "js_fallback", False)),
                js_timeout=getattr(args, "js_timeout", args.timeout),
                js_wait_ms=getattr(args, "js_wait_ms", 2000),
            ):
                append_policy_candidate(candidate_urls, seen_candidates, seller_candidate)
        if args.try_common_paths:
            for external_link in extract_app_store_external_links(app_html, record.app_store_url):
                external_url = external_link["url"]
                try:
                    append_policy_candidate(
                        candidate_urls,
                        seen_candidates,
                        expand_shortlink_candidate(external_url, args.fallback_timeout, args.user_agent, args.proxy),
                    )
                except Exception as exc:
                    candidate_errors.append(f"{external_url}: app-store-external shortlink-expand: {type(exc).__name__}: {exc}")
                for external_candidate in seller_home_policy_candidates(
                    external_url,
                    args.fallback_timeout,
                    args.user_agent,
                    proxy=args.proxy,
                    render_text=getattr(args, "render_text", render_text_with_playwright),
                    js_fallback=bool(getattr(args, "js_fallback", False)),
                    js_timeout=getattr(args, "js_timeout", args.timeout),
                    js_wait_ms=getattr(args, "js_wait_ms", 2000),
                ):
                    external_candidate = dict(external_candidate)
                    external_candidate["source"] = f"app-store-external:{external_candidate['source']}"
                    append_policy_candidate(candidate_urls, seen_candidates, external_candidate)
                for fallback_url in fallback_policy_urls(
                    dataclasses.replace(record, seller_url=external_url),
                    privacy_result.url if privacy_result else None,
                ):
                    append_policy_candidate(candidate_urls, seen_candidates, candidate_dict(fallback_url, "app-store-external:common-path", False))
                for candidate in robots_sitemap_policy_candidates(external_url, args.fallback_timeout, args.user_agent, proxy=args.proxy):
                    candidate = dict(candidate)
                    candidate["source"] = f"app-store-external:{candidate['source']}"
                    append_policy_candidate(candidate_urls, seen_candidates, candidate)
                for candidate in sitemap_policy_candidates(external_url, args.fallback_timeout, args.user_agent, proxy=args.proxy):
                    candidate = dict(candidate)
                    candidate["source"] = f"app-store-external:{candidate['source']}"
                    append_policy_candidate(candidate_urls, seen_candidates, candidate)
        if args.try_common_paths:
            for fallback_url in fallback_policy_urls(record, privacy_result.url if privacy_result else None):
                append_policy_candidate(candidate_urls, seen_candidates, candidate_dict(fallback_url, "common-path", False))
            if record.seller_url:
                for candidate in robots_sitemap_policy_candidates(record.seller_url, args.fallback_timeout, args.user_agent, proxy=args.proxy):
                    append_policy_candidate(candidate_urls, seen_candidates, candidate)
                for candidate in sitemap_policy_candidates(record.seller_url, args.fallback_timeout, args.user_agent, proxy=args.proxy):
                    append_policy_candidate(candidate_urls, seen_candidates, candidate)
        if not candidate_urls:
            row["error"] = "privacy policy URL not found on App Store page"
            return row

        for index, candidate in enumerate(candidate_urls):
            candidate_url = candidate["url"]
            suffix = "" if index == 0 else f"-fallback-{index}"
            try:
                previous_timeout = args.timeout
                if index > 0:
                    args.timeout = args.fallback_timeout
                candidate_row = fetch_policy_candidate(
                    candidate_url,
                    record,
                    args,
                    app_dir,
                    suffix,
                    source=candidate.get("source") or ("primary" if index == 0 else "fallback"),
                    browser_first=bool(candidate.get("browser_first")),
                )
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, UnicodeError, RuntimeError) as exc:
                candidate_errors.append(f"{candidate_url}: {type(exc).__name__}: {exc}")
                row["policy_url_attempts"].append(
                    {
                        "policy_url": candidate_url,
                        "canonical_policy_url": candidate_url,
                        "attempt_index": index,
                        "source": candidate.get("source") or ("primary" if index == 0 else "fallback"),
                        "status": "error",
                        "fetch_method": "js" if candidate.get("browser_first") or getattr(args, "js_fallback", False) else "static",
                        "text_chars": 0,
                        "quality": None,
                        "quality_reason": None,
                        "error_class": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                continue
            finally:
                if index > 0:
                    args.timeout = previous_timeout
            attempt = candidate_row.pop("policy_url_attempt", None)
            if attempt:
                attempt["attempt_index"] = index
                row["policy_url_attempts"].append(attempt)
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
    parser.add_argument("--browser-user-agent", default=os.environ.get("IOS_POLICY_BROWSER_USER_AGENT", DEFAULT_BROWSER_USER_AGENT))
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"), help="Optional HTTP(S) proxy URL, for example http://127.0.0.1:7890.")
    parser.add_argument("--no-fetch-policy", action="store_true", help="Only discover policy URLs; do not fetch policy pages.")
    parser.add_argument("--min-policy-chars", type=int, default=1000, help="Minimum extracted text length to count as a full policy candidate.")
    parser.add_argument("--js-fallback", action="store_true", help="Use Playwright rendering when static policy HTML is too short or blocked.")
    parser.add_argument("--js-timeout", type=int, default=60, help="Playwright rendering timeout in seconds.")
    parser.add_argument("--js-wait-ms", type=int, default=2000, help="Extra wait after DOMContentLoaded before saving rendered HTML.")
    parser.add_argument("--try-common-paths", action="store_true", help="If the explicit policy URL is missing or too short, try common privacy paths on seller/app origins.")
    parser.add_argument("--domain-rules", default=str(default_domain_rules_path()), help="JSON file with auditable domain-specific policy URL rules.")
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
