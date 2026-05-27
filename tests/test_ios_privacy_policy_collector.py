import importlib.util
import argparse
import http.client
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "ios_privacy_policy_collector.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("ios_privacy_policy_collector", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class IosPrivacyPolicyCollectorTests(unittest.TestCase):
    def setUp(self):
        self.collector = load_module()

    def test_parse_app_id_from_common_inputs(self):
        cases = {
            "284882215": "284882215",
            "id284882215": "284882215",
            "https://apps.apple.com/us/app/facebook/id284882215": "284882215",
            "https://apps.apple.com/us/app/example/id1234567890?mt=8": "1234567890",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.collector.parse_app_id(raw), expected)

    def test_extracts_privacy_policy_url_from_json_state(self):
        html = r'''
        <html><head><script>
        window.__data = {"privacyPolicyUrl":"https:\/\/example.com\/legal\/privacy-policy"};
        </script></head></html>
        '''

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/us/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://example.com/legal/privacy-policy")
        self.assertEqual(result.method, "json-field")

    def test_prefers_developer_policy_anchor_over_apple_privacy_label(self):
        html = """
        <main>
          <a href="https://apps.apple.com/us/story/id1538632801">App Privacy</a>
          <a href="https://support.example.com/legal/privacy">Developer Privacy Policy</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/us/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://support.example.com/legal/privacy")
        self.assertEqual(result.method, "anchor")

    def test_ignores_apple_store_privacy_data_page(self):
        html = """
        <main>
          <a href="https://www.apple.com/legal/privacy/data/zh-cn/app-store/">App Store 与隐私</a>
          <a href="https://developer.example.cn/privacy">开发者隐私政策</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/cn/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://developer.example.cn/privacy")
        self.assertEqual(result.method, "anchor")

    def test_ignores_apple_cookie_warning_link(self):
        html = """
        <main>
          <a href="https://www.apple.com/legal/privacy/szh/cookies/">Cookie 警告</a>
          <a href="https://developer.example.cn/legal/privacy">开发者隐私政策</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/cn/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://developer.example.cn/legal/privacy")
        self.assertEqual(result.method, "anchor")

    def test_ignores_apple_internet_services_platform_policy_page(self):
        html = """
        <main>
          <a href="https://www.apple.com.cn/legal/internet-services">隐私政策</a>
          <a href="https://developer.example.cn/privacy-policy">开发者隐私政策</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/cn/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://developer.example.cn/privacy-policy")
        self.assertEqual(result.method, "anchor")

    def test_returns_none_when_only_apple_platform_policy_links_exist(self):
        html = """
        <main>
          <a href="https://www.apple.com.cn/legal/internet-services">隐私政策</a>
          <a href="https://apps.apple.com/cn/app/example/id1234567890">App Store</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/cn/app/example/id1234567890",
        )

        self.assertIsNone(result)

    def test_ignores_country_specific_apple_legal_privacy_page(self):
        html = """
        <main>
          <a href="https://www.apple.fr/fr/legal/privacy/">Apple Inc.</a>
          <a href="https://developer.example.fr/confidentialite">Developer Privacy Policy</a>
        </main>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/fr/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://developer.example.fr/confidentialite")
        self.assertEqual(result.method, "anchor")

    def test_extracts_plain_text_privacy_policy_url_from_description(self):
        html = """
        <section>
          Terms: https://example.com/terms
          Privacy Policy: https://example.com/privacy
        </section>
        """

        result = self.collector.extract_privacy_policy_url(
            html,
            "https://apps.apple.com/us/app/example/id1234567890",
        )

        self.assertEqual(result.url, "https://example.com/privacy")
        self.assertEqual(result.method, "text-url")

    def test_extracts_app_privacy_label_as_supporting_text(self):
        html = """
        <main>
          <h2>What's New</h2>
          <p>Bug fixes.</p>
          <h2>App Privacy</h2>
          <p>The developer indicated that data may be collected.</p>
          <h3>Data Linked to You</h3>
          <p>Location</p>
          <h2>Information</h2>
          <p>Seller Example Inc.</p>
        </main>
        """

        label = self.collector.extract_app_privacy_label_text(html)

        self.assertIn("App Privacy", label)
        self.assertIn("Data Linked to You", label)
        self.assertIn("Location", label)
        self.assertNotIn("Seller Example Inc.", label)

    def test_converts_privacy_html_to_readable_text(self):
        html = """
        <html>
          <head><style>.x{display:none}</style><script>alert(1)</script></head>
          <body>
            <h1>Privacy Policy</h1>
            <p>We collect account data.</p>
            <p>Contact privacy@example.com.</p>
          </body>
        </html>
        """

        text = self.collector.html_to_text(html)

        self.assertIn("Privacy Policy", text)
        self.assertIn("We collect account data.", text)
        self.assertIn("Contact privacy@example.com.", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("display:none", text)

    def test_converts_privacy_html_to_markdown_and_links(self):
        html = """
        <html><body>
          <h1>Privacy Policy</h1>
          <p>Read our <a href="/cookies">Cookie Policy</a>.</p>
          <p>Email <a href="mailto:privacy@example.com">privacy@example.com</a>.</p>
        </body></html>
        """

        markdown, links = self.collector.html_to_markdown_and_links(html, "https://example.com/privacy")

        self.assertIn("# Privacy Policy", markdown)
        self.assertIn("[Cookie Policy](https://example.com/cookies)", markdown)
        self.assertIn("[privacy@example.com](mailto:privacy@example.com)", markdown)
        self.assertEqual(links[0]["url"], "https://example.com/cookies")
        self.assertEqual(links[0]["text"], "Cookie Policy")

    def test_best_markdown_keeps_links_and_metadata(self):
        html = """
        <html><body>
          <h1>Privacy Policy</h1>
          <p>Read our <a href="/cookies">Cookie Policy</a>.</p>
        </body></html>
        """

        markdown, method, links = self.collector.best_markdown_and_links(
            html,
            "https://example.com/privacy",
        )

        self.assertIn("[Cookie Policy](https://example.com/cookies)", markdown)
        self.assertIn(method, {"markdownify", "internal"})
        self.assertEqual(links[0]["url"], "https://example.com/cookies")

    def test_detects_pdf_policy_response_from_magic_bytes(self):
        self.assertTrue(
            self.collector.is_pdf_response(
                b"%PDF-1.7\n...",
                "application/octet-stream",
                "https://example.com/privacy",
            )
        )

    def test_detects_binary_policy_response(self):
        self.assertTrue(self.collector.looks_like_binary(b"\x00\x01\x02\x03" * 40, "application/octet-stream"))
        self.assertFalse(self.collector.looks_like_binary(b"<html><body>Privacy Policy</body></html>", "text/html"))

    def test_extracts_pdf_text_as_markdown(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf is not installed")

        buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_metadata({"/Title": "Privacy Policy"})
        writer.write(buffer)

        with mock.patch("pypdf._page.PageObject.extract_text", return_value="Privacy Policy\nPersonal information"):
            markdown = self.collector.extract_pdf_text(buffer.getvalue(), "https://example.com/privacy.pdf")

        self.assertTrue(markdown.startswith("# privacy.pdf"))
        self.assertIn("Personal information", markdown)

    def test_ensure_markdown_complete_falls_back_to_text_and_source_link(self):
        markdown, method, links = self.collector.ensure_markdown_complete(
            "Privacy",
            "Privacy Policy. We process personal information for account services.",
            "https://example.com/privacy",
            50,
        )

        self.assertEqual(method, "text-fallback")
        self.assertIn("[https://example.com/privacy](https://example.com/privacy)", markdown)
        self.assertIn("personal information", markdown)
        self.assertEqual(links[0]["url"], "https://example.com/privacy")

    def test_ensure_source_link_adds_policy_url_once(self):
        links = [{"text": "Cookie Policy", "url": "https://example.com/cookies"}]

        updated = self.collector.ensure_source_link(links, "https://example.com/privacy")
        unchanged = self.collector.ensure_source_link(updated, "https://example.com/privacy")

        self.assertEqual(updated[0]["url"], "https://example.com/privacy")
        self.assertEqual(sum(1 for link in unchanged if link["url"] == "https://example.com/privacy"), 1)

    def test_generates_common_privacy_url_candidates(self):
        candidates = self.collector.common_privacy_url_candidates(
            "https://developer.example.com/apps/product?ref=store"
        )

        self.assertEqual(candidates[0], "https://developer.example.com/privacy")
        self.assertIn("https://developer.example.com/privacy-policy", candidates)
        self.assertIn("https://developer.example.com/legal/privacy", candidates)
        self.assertIn("https://developer.example.com/policies/privacy-policy", candidates)
        self.assertIn("https://developer.example.com/privacy-center", candidates)
        self.assertIn("https://developer.example.com/legal/terms-of-use", candidates)

    def test_sitemap_policy_candidates_rank_privacy_urls(self):
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/blog</loc></url>
          <url><loc>https://example.com/legal/privacy-policy</loc></url>
          <url><loc>https://example.com/terms</loc></url>
        </urlset>
        """

        def fake_request_text(url, timeout, user_agent, proxy=None, retries=0):
            self.assertEqual(url, "https://example.com/sitemap.xml")
            return sitemap

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            candidates = self.collector.sitemap_policy_candidates(
                "https://example.com/app",
                10,
                "agent",
            )
        finally:
            self.collector.request_text = original_request_text

        self.assertEqual(candidates[0]["url"], "https://example.com/legal/privacy-policy")
        self.assertEqual(candidates[0]["source"], "sitemap")

    def test_robots_sitemap_policy_candidates_use_declared_sitemaps(self):
        robots = "User-agent: *\nSitemap: https://example.com/custom-sitemap.xml\n"
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/privacy</loc></url>
        </urlset>
        """

        def fake_request_text(url, timeout, user_agent, proxy=None, retries=0):
            return { "https://example.com/robots.txt": robots, "https://example.com/custom-sitemap.xml": sitemap }[url]

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            candidates = self.collector.robots_sitemap_policy_candidates(
                "https://example.com/app",
                10,
                "agent",
            )
        finally:
            self.collector.request_text = original_request_text

        self.assertEqual(candidates[0]["url"], "https://example.com/privacy")
        self.assertEqual(candidates[0]["source"], "robots-sitemap")

    def test_shortlink_candidate_expands_final_url(self):
        def fake_request_final_url(url, timeout, user_agent, proxy=None):
            self.assertEqual(url, "https://on.fb.me/example")
            return "https://developer.example.com/privacy"

        original_request_final_url = self.collector.request_final_url
        try:
            self.collector.request_final_url = fake_request_final_url
            candidate = self.collector.expand_shortlink_candidate("https://on.fb.me/example", 10, "agent")
        finally:
            self.collector.request_final_url = original_request_final_url

        self.assertEqual(candidate["url"], "https://developer.example.com/privacy")
        self.assertEqual(candidate["source"], "shortlink-expand")

    def test_extracts_app_store_external_links_for_missing_seller_url_fallback(self):
        html = """
        <main>
          <a href="https://apps.apple.com/us/story/id1538632801">App Privacy</a>
          <a href="https://developer.example.com/app">Developer Website</a>
          <a href="https://support.example.com/app">App Support</a>
        </main>
        """

        candidates = self.collector.extract_app_store_external_links(
            html,
            "https://apps.apple.com/us/app/example/id1234567890",
        )

        self.assertEqual(candidates[0]["url"], "https://developer.example.com/app")
        self.assertEqual(candidates[0]["source"], "app-store-external-link")
        self.assertEqual(candidates[1]["url"], "https://support.example.com/app")

    def test_web_search_url_candidates_filter_noise_and_rank_privacy(self):
        html = """
        <html><body>
          <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fapps.apple.com%2Fus%2Fapp%2Fexample%2Fid123">App Store</a>
          <a class="result__a" href="https://html.duckduckgo.com/privacy">DuckDuckGo Privacy</a>
          <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdeveloper.example.com%2F">Example Developer</a>
          <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdeveloper.example.com%2Fprivacy-policy">Example App Privacy Policy</a>
        </body></html>
        """

        def fake_request_text(url, timeout, user_agent, proxy=None, retries=0):
            self.assertIn("Example+App+privacy", url)
            return html

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            candidates = self.collector.web_search_url_candidates(
                "Example App privacy",
                10,
                "agent",
            )
        finally:
            self.collector.request_text = original_request_text

        self.assertEqual(candidates[0]["url"], "https://developer.example.com/privacy-policy")
        self.assertEqual(candidates[0]["source"], "web-search")
        self.assertNotIn("apps.apple.com", {candidate["url"] for candidate in candidates})
        self.assertNotIn("duckduckgo.com", {self.collector.urllib.parse.urlparse(candidate["url"]).hostname for candidate in candidates})

    def test_seller_home_policy_candidates_can_use_js_rendering(self):
        def fake_request_text(url, timeout, user_agent, proxy=None):
            return "<html><body><div id='app'>Loading</div></body></html>"

        def fake_render(url, timeout, user_agent, proxy=None, wait_ms=0):
            return """
            <html><body>
              <a href="/legal/privacy">Privacy Policy</a>
            </body></html>
            """

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            candidates = self.collector.seller_home_policy_candidates(
                "https://example.com",
                10,
                "agent",
                render_text=fake_render,
                js_fallback=True,
            )
        finally:
            self.collector.request_text = original_request_text

        self.assertEqual(candidates[0]["url"], "https://example.com/legal/privacy")
        self.assertEqual(candidates[0]["source"], "seller-home-js:anchor")

    def test_fallback_policy_urls_skip_app_store_origin(self):
        record = self.collector.AppRecord(
            app_id="1234567890",
            name="Example App",
            bundle_id=None,
            seller_name="Example Inc.",
            app_store_url="https://apps.apple.com/us/app/example/id1234567890",
            seller_url="https://developer.example.com/product",
            source="test",
            raw={},
        )

        candidates = list(self.collector.fallback_policy_urls(record, None))

        self.assertTrue(candidates)
        self.assertTrue(all("developer.example.com" in url for url in candidates))

    def test_domain_rules_add_auditable_policy_candidates(self):
        record = self.collector.AppRecord(
            app_id="6448311069",
            name="ChatGPT",
            bundle_id="com.openai.chat",
            seller_name="OpenAI OpCo, LLC",
            app_store_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            seller_url="https://openai.com/chatgpt",
            source="test",
            raw={},
        )

        candidates = self.collector.domain_rule_policy_candidates(record, self.collector.default_domain_rules_path())

        self.assertEqual(candidates[0]["url"], "https://openai.com/policies/privacy-policy")
        self.assertEqual(candidates[0]["source"], "domain-rule:openai")
        self.assertTrue(candidates[0]["browser_first"])

    def test_quality_flags_redirect_shell_as_too_short(self):
        quality, reason = self.collector.policy_text_quality("Privacy Center", 100)

        self.assertEqual(quality, "too_short")
        self.assertIn("below", reason)

    def test_quality_accepts_non_english_privacy_terms(self):
        text = "本隐私政策说明我们如何处理您的个人信息。" * 80

        quality, reason = self.collector.policy_text_quality(text, 100)

        self.assertEqual(quality, "ok")
        self.assertIsNone(reason)

    def test_fetch_policy_candidate_can_fallback_to_js_rendering_for_short_static_html(self):
        record = self.collector.AppRecord(
            app_id="1234567890",
            name="Example App",
            bundle_id=None,
            seller_name="Example Inc.",
            app_store_url="https://apps.apple.com/us/app/example/id1234567890",
            seller_url="https://example.com",
            source="test",
            raw={},
        )
        static_html = "<html><body><div id='app'>Loading...</div><script src='app.js'></script></body></html>"
        rendered_html = """
            <html><body>
            <h1>Privacy Policy</h1>
            <p>We process personal information to provide account services.</p>
            <a href="/terms">Terms of Service</a>
            </body></html>
        """
        calls = []

        def fake_request_text(url, timeout, user_agent, proxy=None):
            return static_html

        def fake_render(url, timeout, user_agent, proxy=None, wait_ms=0):
            calls.append(url)
            return rendered_html

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            with tempfile.TemporaryDirectory() as tmpdir:
                args = argparse.Namespace(
                    timeout=10,
                    user_agent="test-agent",
                    proxy=None,
                    min_policy_chars=50,
                    js_fallback=True,
                    js_timeout=15,
                    js_wait_ms=100,
                    render_text=fake_render,
                )
                row = self.collector.fetch_policy_candidate(
                    "https://example.com/privacy",
                    record,
                    args,
                    pathlib.Path(tmpdir),
                    "",
                )
                markdown = pathlib.Path(row["policy_markdown_path"]).read_text(encoding="utf-8")
        finally:
            self.collector.request_text = original_request_text

        self.assertEqual(calls, ["https://example.com/privacy"])
        self.assertEqual(row["policy_text_quality"], "ok")
        self.assertEqual(row["policy_fetch_method"], "js")
        self.assertIn("Terms of Service", markdown)

    def test_fetch_policy_candidate_refuses_apple_platform_url(self):
        record = self.collector.AppRecord(
            app_id="1234567890",
            name="Example App",
            bundle_id=None,
            seller_name="Example Inc.",
            app_store_url="https://apps.apple.com/us/app/example/id1234567890",
            seller_url="https://example.com",
            source="test",
            raw={},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                timeout=10,
                user_agent="test-agent",
                proxy=None,
                min_policy_chars=50,
            )
            with self.assertRaises(ValueError):
                self.collector.fetch_policy_candidate(
                    "https://www.apple.com.cn/legal/internet-services",
                    record,
                    args,
                    pathlib.Path(tmpdir),
                    "",
                )

    def test_collect_app_continues_after_candidate_js_timeout(self):
        record = self.collector.AppRecord(
            app_id="1234567890",
            name="Example App",
            bundle_id=None,
            seller_name="Example Inc.",
            app_store_url="https://apps.apple.com/us/app/example/id1234567890",
            seller_url="https://example.com",
            source="test",
            raw={},
        )
        app_html = """
            <html><body>
            <a href="https://example.com/privacy-center">Developer Privacy Policy</a>
            </body></html>
        """
        pages = {
            record.app_store_url: app_html,
            "https://example.com/privacy-center": "<html><body>Loading</body></html>",
            "https://example.com/privacy": """
                <html><body>
                <h1>Privacy Policy</h1>
                <p>We process personal information to provide account services.</p>
                </body></html>
            """,
        }

        def fake_request_text(url, timeout, user_agent, proxy=None):
            return pages[url]

        def fake_render(url, timeout, user_agent, proxy=None, wait_ms=0):
            raise TimeoutError("render timed out")

        original_request_text = self.collector.request_text
        try:
            self.collector.request_text = fake_request_text
            with tempfile.TemporaryDirectory() as tmpdir:
                args = argparse.Namespace(
                    output_dir=tmpdir,
                    timeout=10,
                    fallback_timeout=5,
                    user_agent="test-agent",
                    proxy=None,
                    min_policy_chars=50,
                    js_fallback=True,
                    js_timeout=1,
                    js_wait_ms=100,
                    render_text=fake_render,
                    try_common_paths=True,
                    no_fetch_policy=False,
                )
                row = self.collector.collect_app(record, args, "us")
        finally:
            self.collector.request_text = original_request_text

        self.assertIsNone(row["error"])
        self.assertEqual(row["policy_url"], "https://example.com/privacy")
        self.assertEqual(row["policy_text_quality"], "ok")
        self.assertEqual([attempt["status"] for attempt in row["policy_url_attempts"]], ["error", "accepted"])
        self.assertEqual(row["policy_url_attempts"][0]["error_class"], "TimeoutError")

    def test_builds_records_from_offline_lookup_result(self):
        lookup_payload = {
            "resultCount": 1,
            "results": [
                {
                    "trackId": 1234567890,
                    "trackName": "Example App",
                    "bundleId": "com.example.app",
                    "sellerName": "Example Inc.",
                    "trackViewUrl": "https://apps.apple.com/us/app/example/id1234567890",
                    "sellerUrl": "https://example.com",
                }
            ],
        }

        records = list(self.collector.records_from_itunes_payload(lookup_payload, "lookup"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].app_id, "1234567890")
        self.assertEqual(records[0].name, "Example App")
        self.assertEqual(records[0].bundle_id, "com.example.app")
        self.assertEqual(records[0].source, "lookup")

    def test_builds_records_from_apple_rss_chart(self):
        rss_payload = {
            "feed": {
                "results": [
                    {
                        "id": "1234567890",
                        "name": "Example App",
                        "artistName": "Example Inc.",
                        "url": "https://apps.apple.com/us/app/example/id1234567890",
                    }
                ]
            }
        }

        records = list(self.collector.records_from_apple_rss_payload(rss_payload, "chart:top-free"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].app_id, "1234567890")
        self.assertEqual(records[0].seller_name, "Example Inc.")
        self.assertEqual(records[0].app_store_url, "https://apps.apple.com/us/app/example/id1234567890")

    def test_request_text_retries_incomplete_chunked_response(self):
        class FakeResponse:
            headers = mock.Mock()

            def __init__(self, body=None, exc=None):
                self.body = body
                self.exc = exc

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                if self.exc:
                    raise self.exc
                return self.body

        FakeResponse.headers.get_content_charset.return_value = "utf-8"
        opener = mock.Mock()
        opener.open.side_effect = [
            FakeResponse(exc=http.client.IncompleteRead(b'{"partial":')),
            FakeResponse(body=b'{"ok": true}'),
        ]

        with mock.patch.object(self.collector, "build_opener", return_value=opener), mock.patch.object(self.collector.time, "sleep"):
            text = self.collector.request_text("https://example.test/feed", 10, "agent", retries=1)

        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(opener.open.call_count, 2)

    def test_request_text_falls_back_on_invalid_charset(self):
        class FakeResponse:
            headers = mock.Mock()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return "privacy policy".encode("utf-8")

        FakeResponse.headers.get_content_charset.return_value = "utf-8,gbk"
        opener = mock.Mock()
        opener.open.return_value = FakeResponse()

        with mock.patch.object(self.collector, "build_opener", return_value=opener):
            text = self.collector.request_text("https://example.test/privacy", 10, "agent")

        self.assertEqual(text, "privacy policy")

    def test_request_text_falls_back_when_charset_parser_raises(self):
        class FakeResponse:
            headers = mock.Mock()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"resultCount": 1}'

        FakeResponse.headers.get_content_charset.side_effect = LookupError("unknown encoding: utf-8,gbk")
        opener = mock.Mock()
        opener.open.return_value = FakeResponse()

        with mock.patch.object(self.collector, "build_opener", return_value=opener):
            text = self.collector.request_text("https://itunes.apple.com/lookup?id=1", 10, "agent")

        self.assertEqual(text, '{"resultCount": 1}')

    def test_completed_keys_only_include_successful_rows(self):
        rows = [
            {"country": "us", "app_id": "1", "policy_url": "https://example.com/privacy", "policy_text_chars": 123, "policy_text_quality": "ok", "error": None},
            {"country": "cn", "app_id": "1", "policy_text_chars": 0, "error": "timeout"},
            {"country": "jp", "app_id": "2", "policy_url": "https://example.com/privacy", "error": None},
        ]

        keys = self.collector.completed_keys_from_rows(rows, no_fetch_policy=False)
        url_only_keys = self.collector.completed_keys_from_rows(rows, no_fetch_policy=True)

        self.assertEqual(keys, {("us", "1")})
        self.assertEqual(url_only_keys, {("us", "1"), ("jp", "2")})

    def test_country_output_path_is_partitioned_by_country(self):
        record = self.collector.AppRecord(
            app_id="1234567890",
            name="Example App",
            bundle_id="com.example.app",
            seller_name="Example Inc.",
            app_store_url="https://apps.apple.com/us/app/example/id1234567890",
            seller_url=None,
            source="test",
            raw={},
        )

        path = self.collector.app_output_dir("out-root", "cn", record)

        self.assertEqual(path.as_posix(), "out-root/cn/1234567890-Example-App")


if __name__ == "__main__":
    unittest.main()
