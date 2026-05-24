import importlib.util
import argparse
import pathlib
import sys
import tempfile
import unittest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "queue_worker.py"


def load_module():
    spec = importlib.util.spec_from_file_location("queue_worker", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class QueueWorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_module()

    def test_queue_result_keeps_markdown_and_links(self):
        collector_row = {
            "error": None,
            "policy_url": "https://example.com/privacy",
            "policy_url_method": "anchor",
            "policy_url_evidence": "developer privacy policy",
            "policy_text_sha256": "abc123",
            "policy_text_chars": 1500,
            "policy_text_quality": "ok",
            "policy_markdown_path": "out/us/123/privacy-policy.md",
            "policy_html_path": "out/us/123/privacy-policy.html",
            "policy_text_path": "out/us/123/privacy-policy.txt",
            "policy_cluster_manifest_path": "out/us/123/policy-cluster/cluster.json",
            "policy_cluster_nodes_count": 3,
            "policy_cluster_edges_count": 2,
            "policy_url_attempts": [{"policy_url": "https://example.com/privacy", "status": "accepted"}],
        }
        links = [{"text": "Contact", "url": "https://example.com/contact"}]

        result = self.worker.queue_result_from_collector_row(collector_row, links)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["policy_url"], "https://example.com/privacy")
        self.assertEqual(result["policy_markdown_path"], "out/us/123/privacy-policy.md")
        self.assertEqual(result["policy_links"], links)
        self.assertEqual(result["policy_cluster_manifest_path"], "out/us/123/policy-cluster/cluster.json")
        self.assertEqual(result["policy_cluster_nodes_count"], 3)
        self.assertEqual(result["policy_url_attempts"][0]["status"], "accepted")

    def test_retryable_error_classifies_transient_failures(self):
        self.assertTrue(self.worker.retryable_error("HTTP Error 403: Forbidden"))
        self.assertTrue(self.worker.retryable_error("TimeoutError: timed out"))
        self.assertFalse(self.worker.retryable_error("privacy policy URL not found"))
        self.assertFalse(self.worker.retryable_error("policy text not complete enough"))

    def test_load_links_jsonl_ignores_blank_lines(self):
        content = '\n{"text":"A","url":"https://example.com/a"}\n\n'

        links = self.worker.links_from_jsonl_text(content)

        self.assertEqual(links, [{"text": "A", "url": "https://example.com/a"}])

    def test_collect_task_can_archive_policy_cluster_from_saved_root_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = pathlib.Path(tmpdir) / "out"
            root_dir = output_dir / "us" / "123-Test"
            root_dir.mkdir(parents=True)
            root_html = root_dir / "privacy-policy.html"
            root_html.write_text(
                """
                <html><body>
                <h1>Privacy Policy</h1>
                <p>We process personal information for account services.</p>
                <a href="/terms">Terms of Service</a>
                </body></html>
                """,
                encoding="utf-8",
            )
            (root_dir / "privacy-policy.links.jsonl").write_text(
                '{"text":"Terms of Service","url":"https://example.com/terms"}\n',
                encoding="utf-8",
            )

            def fake_collect_app(record, args, country):
                return {
                    "error": None,
                    "policy_url": "https://example.com/privacy",
                    "policy_url_method": "fixture",
                    "policy_url_evidence": "fixture",
                    "policy_text_sha256": "abc123",
                    "policy_text_chars": 1200,
                    "policy_text_quality": "ok",
                    "policy_markdown_path": str(root_dir / "privacy-policy.md"),
                    "policy_html_path": str(root_html),
                    "policy_text_path": str(root_dir / "privacy-policy.txt"),
                    "policy_links_path": str(root_dir / "privacy-policy.links.jsonl"),
                }

            def fake_request_text(url, timeout, user_agent, proxy=None):
                self.assertEqual(url, "https://example.com/terms")
                return """
                    <html><body>
                    <h1>Terms</h1>
                    <p>These terms govern user accounts and service access.</p>
                    </body></html>
                """

            original_collect_app = self.worker.collector.collect_app
            original_request_text = self.worker.collector.request_text
            try:
                self.worker.collector.collect_app = fake_collect_app
                self.worker.collector.request_text = fake_request_text
                result = self.worker.collect_task(
                    {"app_id": "123", "country": "us", "seed_source": "fixture"},
                    argparse.Namespace(
                        enrich_lookup=False,
                        collect_cluster=True,
                        cluster_max_depth=1,
                        cluster_max_docs=5,
                        cluster_min_chars=20,
                        cluster_probe_common_paths=False,
                        timeout=10,
                        user_agent="test-agent",
                        proxy=None,
                        output_dir=str(output_dir),
                        no_fetch_policy=False,
                        min_policy_chars=20,
                        try_common_paths=False,
                        fallback_timeout=1,
                    ),
                )
            finally:
                self.worker.collector.collect_app = original_collect_app
                self.worker.collector.request_text = original_request_text

            manifest_path = pathlib.Path(result["policy_cluster_manifest_path"])
            self.assertTrue(manifest_path.exists())
            self.assertEqual(result["policy_cluster_nodes_count"], 2)
            self.assertEqual(result["policy_cluster_edges_count"], 1)


if __name__ == "__main__":
    unittest.main()
