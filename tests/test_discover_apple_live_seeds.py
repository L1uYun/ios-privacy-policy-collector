import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "discover_apple_live_seeds.py"


def load_module():
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("discover_apple_live_seeds", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DiscoverAppleLiveSeedsTests(unittest.TestCase):
    def setUp(self):
        self.live = load_module()

    def test_parse_csv(self):
        self.assertEqual(self.live.parse_csv("US, gb, ,JP"), ["us", "gb", "jp"])

    def test_app_record_to_seed(self):
        record = self.live.collector.AppRecord(
            app_id="6448311069",
            name="ChatGPT",
            bundle_id="com.openai.chat",
            seller_name="OpenAI",
            app_store_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            seller_url=None,
            source="fixture",
            raw={},
        )

        row = self.live.app_record_to_seed(record, "us", "apple-rss-top-free", "https://example.test/rss")

        self.assertEqual(row["app_id"], "6448311069")
        self.assertEqual(row["country"], "us")
        self.assertEqual(row["seed_source"], "apple-rss-top-free")
        self.assertEqual(row["bundle_id"], "com.openai.chat")

    def test_dedupe_keeps_source_country_app_unique(self):
        rows = [
            {"seed_source": "s", "country": "us", "app_id": "1"},
            {"seed_source": "s", "country": "us", "app_id": "1"},
            {"seed_source": "s", "country": "gb", "app_id": "1"},
        ]

        self.assertEqual(len(self.live.dedupe_rows(rows)), 2)


if __name__ == "__main__":
    unittest.main()
