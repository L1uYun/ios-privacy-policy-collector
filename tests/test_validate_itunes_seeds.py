import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "validate_itunes_seeds.py"


def load_module():
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("validate_itunes_seeds", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ValidateItunesSeedsTests(unittest.TestCase):
    def setUp(self):
        self.validator = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "queue.sqlite"
        self.validator.queue_store.import_seeds(
            self.db_path,
            [
                {"seed_source": "fixture", "app_id": "6448311069", "country": "us"},
                {"seed_source": "fixture", "app_id": "999999999", "country": "us"},
                {"seed_source": "fixture", "app_id": "368677368", "country": "gb"},
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_records_active_inactive_and_lookup_error(self):
        def fake_lookup(url, timeout, user_agent, proxy):
            if "id=6448311069" in url:
                return {
                    "resultCount": 1,
                    "results": [
                        {
                            "trackId": 6448311069,
                            "bundleId": "com.openai.chat",
                            "trackName": "ChatGPT",
                            "sellerName": "OpenAI",
                            "trackViewUrl": "https://apps.apple.com/us/app/chatgpt/id6448311069",
                        }
                    ],
                }
            if "id=999999999" in url:
                return {"resultCount": 0, "results": []}
            raise urllib.error.URLError("offline")

        args = self.validator.build_parser().parse_args(
            [
                "--db",
                str(self.db_path),
                "--sleep",
                "0",
                "--progress-every",
                "0",
            ]
        )
        with mock.patch.object(self.validator.collector, "request_json_with_proxy", side_effect=fake_lookup):
            summary = self.validator.run(args)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["inactive"], 1)
        self.assertEqual(summary["lookup_error"], 1)

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "select app_id, status from seed_validation order by seed_id"
            ).fetchall()
            metadata = conn.execute("select app_id, name from app_metadata").fetchall()

        self.assertEqual(rows, [("6448311069", "active"), ("999999999", "inactive"), ("368677368", "lookup_error")])
        self.assertEqual(metadata, [("6448311069", "ChatGPT")])

    def test_include_validated_controls_revalidation(self):
        args = self.validator.build_parser().parse_args(
            [
                "--db",
                str(self.db_path),
                "--limit",
                "1",
                "--sleep",
                "0",
                "--progress-every",
                "0",
            ]
        )
        with mock.patch.object(
            self.validator.collector,
            "request_json_with_proxy",
            return_value={"resultCount": 0, "results": []},
        ):
            first = self.validator.run(args)
            second = self.validator.run(args)

        self.assertEqual(first["total"], 1)
        self.assertEqual(second["total"], 1)
        self.assertEqual(first["inactive"], 1)
        self.assertEqual(second["inactive"], 1)

    def test_batch_lookup_maps_missing_results_to_inactive(self):
        urls = []

        def fake_lookup(url, timeout, user_agent, proxy):
            urls.append(url)
            return {
                "resultCount": 1,
                "results": [
                    {
                        "trackId": 6448311069,
                        "bundleId": "com.openai.chat",
                        "trackName": "ChatGPT",
                        "sellerName": "OpenAI",
                        "trackViewUrl": "https://apps.apple.com/us/app/chatgpt/id6448311069",
                    }
                ],
            }

        args = self.validator.build_parser().parse_args(
            [
                "--db",
                str(self.db_path),
                "--country",
                "us",
                "--limit",
                "2",
                "--batch-size",
                "100",
                "--sleep",
                "0",
                "--progress-every",
                "0",
            ]
        )
        with mock.patch.object(self.validator.collector, "request_json_with_proxy", side_effect=fake_lookup):
            summary = self.validator.run(args)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["inactive"], 1)
        self.assertEqual(len(urls), 1)
        self.assertIn("id=6448311069%2C999999999", urls[0])


if __name__ == "__main__":
    unittest.main()
