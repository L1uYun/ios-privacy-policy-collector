import importlib.util
import pathlib
import sys
import unittest


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "rediscover_apple_platform_policies.py"
)


def load_module():
    scripts_dir = str(SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("rediscover_apple_platform_policies", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RediscoverApplePlatformPoliciesTests(unittest.TestCase):
    def setUp(self):
        self.rediscover = load_module()

    def test_web_search_fallback_finds_policy_for_no_seller_url_row(self):
        def fake_web_search(query, timeout, user_agent, proxy=None, limit=6, endpoint=""):
            return [
                {"url": "https://apps.apple.com/us/app/example/id123", "source": "web-search", "evidence": "App Store"},
                {"url": "https://developer.example.com/privacy", "source": "web-search", "evidence": "Privacy Policy"},
            ]

        original_web_search = self.rediscover.collector.web_search_url_candidates
        try:
            self.rediscover.collector.web_search_url_candidates = fake_web_search
            url, method, evidence, external_base = self.rediscover.try_web_search_fallback(
                "1234567890",
                "Example App",
                "us",
                10,
                "agent",
                None,
                8,
                5,
            )
        finally:
            self.rediscover.collector.web_search_url_candidates = original_web_search

        self.assertEqual(url, "https://developer.example.com/privacy")
        self.assertEqual(method, "web-search:direct-policy-candidate")
        self.assertEqual(evidence, "Privacy Policy")
        self.assertEqual(external_base, "https://developer.example.com/privacy")

    def test_web_search_fallback_probes_developer_homepage(self):
        def fake_web_search(query, timeout, user_agent, proxy=None, limit=6, endpoint=""):
            return [{"url": "https://developer.example.com/app", "source": "web-search", "evidence": "Developer"}]

        def fake_discover_from_base_url(base_url, timeout, user_agent, proxy, max_paths, js_fallback=False):
            self.assertEqual(base_url, "https://developer.example.com/app")
            return "https://developer.example.com/privacy-policy", "seller-home:anchor", "privacy link"

        original_web_search = self.rediscover.collector.web_search_url_candidates
        original_discover = self.rediscover.discover_from_base_url
        try:
            self.rediscover.collector.web_search_url_candidates = fake_web_search
            self.rediscover.discover_from_base_url = fake_discover_from_base_url
            url, method, evidence, external_base = self.rediscover.try_web_search_fallback(
                "1234567890",
                "Example App",
                "us",
                10,
                "agent",
                None,
                8,
                5,
            )
        finally:
            self.rediscover.collector.web_search_url_candidates = original_web_search
            self.rediscover.discover_from_base_url = original_discover

        self.assertEqual(url, "https://developer.example.com/privacy-policy")
        self.assertEqual(method, "web-search:seller-home:anchor")
        self.assertEqual(evidence, "privacy link")
        self.assertEqual(external_base, "https://developer.example.com/app")

    def test_rediscover_row_uses_classified_live_urls_before_lookup(self):
        row = {
            "app_id": "1234567890",
            "country": "us",
            "app_name": "Example App",
            "old_root_url": "https://apps.apple.com/us/app/example/id1234567890",
            "live_app_store_url": "https://apps.apple.com/us/app/example/id1234567890?uo=4",
            "live_seller_url": "",
        }

        def fail_lookup(*args, **kwargs):
            raise AssertionError("lookup should not run when live_app_store_url is already present")

        def fake_app_store(app_store_url, timeout, user_agent, proxy, js_fallback=True):
            self.assertEqual(app_store_url, row["live_app_store_url"])
            return "https://developer.example.com/privacy", "app-store-external:seller-home:anchor", "developer site", "https://developer.example.com"

        original_lookup = self.rediscover.lookup_app
        original_app_store = self.rediscover.try_extract_from_app_store
        try:
            self.rediscover.lookup_app = fail_lookup
            self.rediscover.try_extract_from_app_store = fake_app_store
            result = self.rediscover.rediscover_row(
                row,
                10,
                "agent",
                None,
                8,
                ["gb"],
            )
        finally:
            self.rediscover.lookup_app = original_lookup
            self.rediscover.try_extract_from_app_store = original_app_store

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.recovered_policy_url, "https://developer.example.com/privacy")
        self.assertEqual(result.recovery_method, "app-store-external:seller-home:anchor")


if __name__ == "__main__":
    unittest.main()
