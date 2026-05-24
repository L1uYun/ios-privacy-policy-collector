import importlib.util
import pathlib
import sys
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
        }
        links = [{"text": "Contact", "url": "https://example.com/contact"}]

        result = self.worker.queue_result_from_collector_row(collector_row, links)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["policy_url"], "https://example.com/privacy")
        self.assertEqual(result["policy_markdown_path"], "out/us/123/privacy-policy.md")
        self.assertEqual(result["policy_links"], links)

    def test_retryable_error_classifies_transient_failures(self):
        self.assertTrue(self.worker.retryable_error("HTTP Error 403: Forbidden"))
        self.assertTrue(self.worker.retryable_error("TimeoutError: timed out"))
        self.assertFalse(self.worker.retryable_error("privacy policy URL not found"))
        self.assertFalse(self.worker.retryable_error("policy text not complete enough"))

    def test_load_links_jsonl_ignores_blank_lines(self):
        content = '\n{"text":"A","url":"https://example.com/a"}\n\n'

        links = self.worker.links_from_jsonl_text(content)

        self.assertEqual(links, [{"text": "A", "url": "https://example.com/a"}])


if __name__ == "__main__":
    unittest.main()
