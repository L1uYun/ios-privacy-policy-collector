import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "discover_commoncrawl_slug_prefixes.py"


def load_module():
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("discover_commoncrawl_slug_prefixes", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DiscoverCommonCrawlSlugPrefixesTests(unittest.TestCase):
    def setUp(self):
        self.slug = load_module()

    def test_parse_csv_trims_and_lowercases(self):
        self.assertEqual(self.slug.parse_csv("US, jp, ,DE"), ["us", "jp", "de"])

    def test_safe_prefix_name_replaces_punctuation(self):
        self.assertEqual(self.slug.safe_prefix_name("a-b"), "a_b")
        self.assertEqual(self.slug.safe_prefix_name(""), "root")

    def test_default_prefixes_include_letters_and_digits(self):
        prefixes = self.slug.parse_csv(self.slug.DEFAULT_PREFIXES)

        self.assertIn("0", prefixes)
        self.assertIn("a", prefixes)
        self.assertIn("z", prefixes)

    def test_generate_prefixes_from_alphabet_and_lengths(self):
        prefixes = self.slug.generate_prefixes("ab", "1,2")

        self.assertEqual(prefixes, ["a", "b", "aa", "ab", "ba", "bb"])

    def test_load_completed_progress_tasks_only_ok_shards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "progress.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"country": "us", "prefix": "ab", "status": "ok"}),
                        json.dumps({"country": "jp", "prefix": "cd", "status": "error"}),
                        "not-json",
                    ]
                ),
                encoding="utf-8",
            )

            completed = self.slug.load_completed_progress_shards(path)

        self.assertEqual(completed, {("us", "ab")})

    def test_progress_completion_keeps_later_success_after_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "progress.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"country": "us", "prefix": "st", "status": "error"}),
                        json.dumps({"country": "us", "prefix": "st", "status": "ok"}),
                    ]
                ),
                encoding="utf-8",
            )

            completed = self.slug.load_completed_progress_shards(path)

        self.assertEqual(completed, {("us", "st")})


if __name__ == "__main__":
    unittest.main()
