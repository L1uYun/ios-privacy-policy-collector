import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "export_seed_dump.py"


def load_module():
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("export_seed_dump", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExportSeedDumpTests(unittest.TestCase):
    def setUp(self):
        self.exporter = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "queue.sqlite"
        self.exporter.queue_store.import_seeds(
            self.db_path,
            [
                {"seed_source": "commoncrawl-appstore-url", "app_id": "6448311069", "country": "us"},
                {"seed_source": "commoncrawl-appstore-url", "app_id": "6448311069", "country": "gb"},
                {"seed_source": "old-dump", "app_id": "368677368", "country": "us"},
            ],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for seed_id, status, name in [
                (1, "active", "ChatGPT"),
                (2, "inactive", "ChatGPT"),
                (3, "active", "Uber"),
            ]:
                seed = conn.execute("select * from app_seed where seed_id = ?", (seed_id,)).fetchone()
                conn.execute(
                    """
                    insert into seed_validation(
                        seed_id, seed_source, app_id, country, status, result_count,
                        track_id, bundle_id, name, seller_name, app_store_url,
                        lookup_url, validated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seed["seed_id"],
                        seed["seed_source"],
                        seed["app_id"],
                        seed["country"],
                        status,
                        1 if status == "active" else 0,
                        seed["app_id"],
                        "com.example",
                        name,
                        "Seller",
                        seed["app_store_url"],
                        "https://itunes.apple.com/lookup?id=" + seed["app_id"],
                        "2026-05-24T00:00:00Z",
                    ),
                )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exports_active_rows_for_selected_sources(self):
        rows = [
            self.exporter.dump_row(row)
            for row in self.exporter.load_rows(
                self.db_path,
                ["commoncrawl-appstore-url"],
                "active",
            )
        ]
        summary = self.exporter.summarize(rows)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["app_id"], "6448311069")
        self.assertEqual(rows[0]["validation_status"], "active")
        self.assertEqual(summary["unique_app_ids"], 1)
        self.assertEqual(summary["sources"], {"commoncrawl-appstore-url": 1})


if __name__ == "__main__":
    unittest.main()
