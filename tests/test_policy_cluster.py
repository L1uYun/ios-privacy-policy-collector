import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "policy_cluster.py"


def load_module():
    spec = importlib.util.spec_from_file_location("policy_cluster", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PolicyClusterTests(unittest.TestCase):
    def setUp(self):
        self.cluster = load_module()

    def test_filters_policy_related_links_and_skips_unrelated_or_apple_links(self):
        links = [
            {"text": "Terms of Service", "url": "https://example.com/legal/terms"},
            {"text": "Cookie Policy", "url": "/cookies"},
            {"text": "Support", "url": "https://example.com/support"},
            {"text": "App Privacy", "url": "https://www.apple.com/legal/privacy/data/en/app-store/"},
        ]

        selected = self.cluster.select_policy_links(
            links,
            base_url="https://example.com/privacy",
            root_url="https://example.com/privacy",
        )

        self.assertEqual(
            [link["url"] for link in selected],
            ["https://example.com/legal/terms", "https://example.com/cookies"],
        )

    def test_collect_policy_cluster_writes_nodes_edges_and_absolute_markdown_links(self):
        pages = {
            "https://example.com/privacy": """
                <html><body>
                <h1>Privacy Policy</h1>
                <p>We process personal information for service operations.</p>
                <a href="/legal/terms">Terms of Service</a>
                <a href="/cookies">Cookie Policy</a>
                <a href="/support">Support</a>
                </body></html>
            """,
            "https://example.com/legal/terms": """
                <html><body>
                <h1>Terms</h1>
                <p>These terms govern the service and user account rules.</p>
                <a href="/privacy">Privacy Policy</a>
                </body></html>
            """,
            "https://example.com/cookies": """
                <html><body>
                <h1>Cookie Policy</h1>
                <p>This cookie policy explains analytics cookies.</p>
                </body></html>
            """,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.cluster.collect_policy_cluster(
                root_url="https://example.com/privacy",
                output_dir=pathlib.Path(tmpdir),
                fetch_text=lambda url: pages[url],
                max_depth=1,
                max_docs=30,
                min_chars=20,
            )
            manifest_path = pathlib.Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["root_url"], "https://example.com/privacy")
            self.assertEqual(len(manifest["nodes"]), 3)
            self.assertEqual(len(manifest["edges"]), 2)
            self.assertEqual({node["role"] for node in manifest["nodes"]}, {"root", "linked"})
            self.assertTrue(all(pathlib.Path(node["markdown_path"]).exists() for node in manifest["nodes"]))

            root_node = next(node for node in manifest["nodes"] if node["url"] == "https://example.com/privacy")
            root_markdown = pathlib.Path(root_node["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("[Terms of Service](https://example.com/legal/terms)", root_markdown)
            self.assertIn("[Cookie Policy](https://example.com/cookies)", root_markdown)
            self.assertNotIn("support", json.dumps(manifest, ensure_ascii=False).lower())

    def test_collect_policy_cluster_can_probe_common_related_paths(self):
        pages = {
            "https://example.com/privacy": """
                <html><body>
                <h1>Privacy Policy</h1>
                <p>We process personal information for service operations.</p>
                </body></html>
            """,
            "https://example.com/terms": """
                <html><body>
                <h1>Terms of Service</h1>
                <p>These terms govern user accounts and service access.</p>
                </body></html>
            """,
            "https://example.com/cookie-policy": """
                <html><body>
                <h1>Cookie Policy</h1>
                <p>This cookie policy explains analytics cookies.</p>
                </body></html>
            """,
            "https://example.com/terms-of-service": "<html><body>no</body></html>",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.cluster.collect_policy_cluster(
                root_url="https://example.com/privacy",
                output_dir=pathlib.Path(tmpdir),
                fetch_text=lambda url: pages.get(url, "<html><body>no</body></html>"),
                max_depth=1,
                max_docs=30,
                min_chars=20,
                probe_common_paths=True,
            )
            manifest = json.loads(pathlib.Path(result["manifest_path"]).read_text(encoding="utf-8"))

            self.assertIn("https://example.com/terms", {node["url"] for node in manifest["nodes"]})
            self.assertIn("https://example.com/cookie-policy", {node["url"] for node in manifest["nodes"]})
            self.assertNotIn("https://example.com/terms-of-service", {node["url"] for node in manifest["nodes"]})
            self.assertEqual(
                {edge["text"] for edge in manifest["edges"]},
                {"common-path:/terms", "common-path:/cookie-policy"},
            )
            self.assertTrue(
                any(error["error_class"] == "QualityError" for error in manifest["errors"])
            )

    def test_collect_policy_cluster_uses_js_fallback_for_short_nodes(self):
        static_pages = {
            "https://example.com/privacy": "<html><body><div id='app'>Loading...</div></body></html>",
        }
        rendered_pages = {
            "https://example.com/privacy": """
                <html><body>
                <h1>Privacy Policy</h1>
                <p>We process personal information for account services.</p>
                <a href="/terms">Terms of Service</a>
                </body></html>
            """,
            "https://example.com/terms": """
                <html><body>
                <h1>Terms</h1>
                <p>These terms govern user accounts and service access.</p>
                </body></html>
            """,
        }
        js_calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.cluster.collect_policy_cluster(
                root_url="https://example.com/privacy",
                output_dir=pathlib.Path(tmpdir),
                fetch_text=lambda url: static_pages.get(url, "<html><body>Loading...</body></html>"),
                js_fetch_text=lambda url: js_calls.append(url) or rendered_pages[url],
                max_depth=1,
                max_docs=5,
                min_chars=20,
                js_fallback=True,
            )
            manifest = json.loads(pathlib.Path(result["manifest_path"]).read_text(encoding="utf-8"))

            self.assertEqual(js_calls, ["https://example.com/privacy", "https://example.com/terms"])
            self.assertEqual(len(manifest["nodes"]), 2)
            self.assertTrue(all(node["fetch_method"] == "js" for node in manifest["nodes"]))


if __name__ == "__main__":
    unittest.main()
