import importlib.util
import json
import pathlib
import sys
import unittest


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

    def test_generates_common_privacy_url_candidates(self):
        candidates = self.collector.common_privacy_url_candidates(
            "https://developer.example.com/apps/product?ref=store"
        )

        self.assertEqual(candidates[0], "https://developer.example.com/privacy")
        self.assertIn("https://developer.example.com/privacy-policy", candidates)
        self.assertIn("https://developer.example.com/legal/privacy", candidates)

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

    def test_quality_flags_redirect_shell_as_too_short(self):
        quality, reason = self.collector.policy_text_quality("Privacy Center", 100)

        self.assertEqual(quality, "too_short")
        self.assertIn("below", reason)

    def test_quality_accepts_non_english_privacy_terms(self):
        text = "本隐私政策说明我们如何处理您的个人信息。" * 80

        quality, reason = self.collector.policy_text_quality(text, 100)

        self.assertEqual(quality, "ok")
        self.assertIsNone(reason)

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
