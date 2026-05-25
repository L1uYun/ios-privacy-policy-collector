import importlib.util
import pathlib
import sys
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


if __name__ == "__main__":
    unittest.main()
