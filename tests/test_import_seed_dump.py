import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "import_seed_dump.py"


def load_module():
    spec = importlib.util.spec_from_file_location("import_seed_dump", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImportSeedDumpTests(unittest.TestCase):
    def setUp(self):
        self.importer = load_module()

    def test_appgoblin_import_filters_ios_numeric_store_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "sample.tsv"
            path.write_text(
                "store\tstore_id\tcanonical_url\tbundle_id\n"
                "apple_app_store\t6448311069\thttps://apps.apple.com/us/app/chatgpt/id6448311069\tcom.openai.chat\n"
                "android\tcom.example.android\thttps://play.google.com/store/apps/details?id=com.example.android\t\n"
                "apple_app_store\tid368677368\thttps://apps.apple.com/us/app/uber/id368677368\tcom.ubercab.UberClient\n",
                encoding="utf-8",
            )

            rows = list(self.importer.iter_appgoblin_rows(path, "fixture", "us"))

        self.assertEqual([row["app_id"] for row in rows], ["6448311069", "368677368"])
        self.assertEqual(rows[0]["seed_source"], "fixture")
        self.assertEqual(rows[0]["country"], "us")

    def test_appstoredb_sqlite_import_reads_apps_and_storefront_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "appstore.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    create table apps (
                        store_id integer not null,
                        bundle_id text not null,
                        int_id integer primary key
                    );
                    create table stores (
                        canonical_url text not null,
                        code text not null,
                        app_name text not null,
                        int_app_id integer not null
                    );
                    insert into apps(store_id, bundle_id, int_id)
                    values (6448311069, 'com.openai.chat', 1),
                           (368677368, 'com.ubercab.UberClient', 2);
                    insert into stores(canonical_url, code, app_name, int_app_id)
                    values ('https://apps.apple.com/us/app/chatgpt/id6448311069', 'us', 'ChatGPT', 1),
                           ('https://apps.apple.com/gb/app/chatgpt/id6448311069', 'gb', 'ChatGPT', 1),
                           ('https://apps.apple.com/us/app/uber/id368677368', 'us', 'Uber', 2);
                    """
                )

            rows = list(self.importer.iter_appstoredb_sqlite_rows(path, "appstoredb-fixture", "us"))

        self.assertEqual(
            sorted((row["country"], row["app_id"]) for row in rows),
            [("gb", "6448311069"), ("us", "368677368"), ("us", "6448311069")],
        )
        chatgpt_gb = next(row for row in rows if row["country"] == "gb")
        self.assertEqual(chatgpt_gb["bundle_id"], "com.openai.chat")
        self.assertEqual(chatgpt_gb["app_store_url"], "https://apps.apple.com/gb/app/chatgpt/id6448311069")


if __name__ == "__main__":
    unittest.main()
