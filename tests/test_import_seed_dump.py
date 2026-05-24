import importlib.util
import pathlib
import sys
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
