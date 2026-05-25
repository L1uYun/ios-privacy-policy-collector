import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "discover_commoncrawl_appstore_urls.py"


def load_module():
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("discover_commoncrawl_appstore_urls", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DiscoverCommonCrawlAppStoreUrlsTests(unittest.TestCase):
    def setUp(self):
        self.discovery = load_module()

    def test_canonicalizes_appstore_urls_and_country(self):
        url = "https://apps.apple.com/gb/app/uber-request-a-ride/id368677368?mt=8"

        canonical = self.discovery.canonical_appstore_url(url)
        country = self.discovery.country_from_appstore_url(canonical, "us")

        self.assertEqual(canonical, "https://apps.apple.com/gb/app/uber-request-a-ride/id368677368")
        self.assertEqual(country, "gb")

    def test_rejects_non_product_urls(self):
        self.assertIsNone(self.discovery.canonical_appstore_url("https://apps.apple.com/us/story/id1538632801"))
        self.assertIsNone(self.discovery.canonical_appstore_url("https://example.com/us/app/example/id123456789"))

    def test_iter_seed_rows_deduplicates_country_and_app_id(self):
        records = [
            {
                "url": "https://apps.apple.com/us/app/chatgpt/id6448311069",
                "timestamp": "20260501000000",
                "status": "200",
                "mime": "text/html",
            },
            {
                "url": "https://apps.apple.com/us/app/chatgpt/id6448311069?platform=iphone",
                "timestamp": "20260502000000",
                "status": "200",
                "mime": "text/html",
            },
            {
                "url": "https://apps.apple.com/de/app/chatgpt/id6448311069",
                "timestamp": "20260503000000",
                "status": "200",
                "mime": "text/html",
            },
        ]

        rows = list(
            self.discovery.iter_seed_rows_from_cdx(
                records,
                index="CC-MAIN-2026-18",
                fallback_country="us",
                seed_source="fixture-cc",
            )
        )

        self.assertEqual([(row["country"], row["app_id"]) for row in rows], [("us", "6448311069"), ("de", "6448311069")])
        self.assertEqual(rows[0]["seed_source"], "fixture-cc")
        self.assertIn("commoncrawl:CC-MAIN-2026-18", rows[0]["provenance_url"])

    def test_cdx_query_url_contains_filters_match_type_limit_and_collapse(self):
        url = self.discovery.cdx_query_url("CC-MAIN-2026-18", "apps.apple.com/us/app/", True, "prefix", 5)

        self.assertIn("CC-MAIN-2026-18-index", url)
        self.assertIn("filter=status%3A200", url)
        self.assertIn("filter=mime%3Atext%2Fhtml", url)
        self.assertIn("matchType=prefix", url)
        self.assertIn("limit=5", url)
        self.assertIn("collapse=urlkey", url)

    def test_build_parser_defaults_to_requests_backend(self):
        args = self.discovery.build_parser().parse_args([])

        self.assertEqual(args.backend, "requests")


if __name__ == "__main__":
    unittest.main()
