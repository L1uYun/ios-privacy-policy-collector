from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MilestoneStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "queue.sqlite"
        self.queue = load_script("queue_store")
        self.milestone_status = load_script("milestone_status")

    def tearDown(self):
        self.temp_dir.cleanup()

    def complete_one(self, app_id: str, policy_url: str, chars: int, links: list[dict], nodes: int):
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": app_id, "country": "us"}],
        )
        task = self.queue.claim_next_fetch(self.db_path, worker_id=f"w-{app_id}")
        self.queue.complete_fetch(
            self.db_path,
            fetch_id=task["fetch_id"],
            result={
                "status": "ok",
                "policy_url": policy_url,
                "canonical_policy_url": policy_url,
                "policy_text_sha256": f"sha-{app_id}",
                "policy_text_chars": chars,
                "policy_markdown_path": f"out/us/{app_id}/privacy-policy.md",
                "policy_html_path": f"out/us/{app_id}/privacy-policy.html",
                "policy_text_path": f"out/us/{app_id}/privacy-policy.txt",
                "policy_fetch_method": "static",
                "policy_cluster_manifest_path": f"out/us/{app_id}/policy-cluster/cluster.json",
                "policy_cluster_nodes_count": nodes,
                "policy_cluster_edges_count": max(nodes - 1, 0),
                "policy_cluster_errors_count": 0,
                "policy_links": links,
                "policy_url_attempts": [
                    {
                        "policy_url": policy_url,
                        "canonical_policy_url": policy_url,
                        "attempt_index": 0,
                        "status": "accepted",
                        "fetch_method": "static",
                        "text_chars": chars,
                        "quality": "ok",
                        "quality_reason": None,
                        "error_class": None,
                        "error_message": None,
                    }
                ],
            },
        )

    def test_collect_status_includes_quality_report_fields(self):
        self.queue.init_db(self.db_path)
        self.complete_one(
            "100",
            "https://example.com/privacy",
            1200,
            [{"text": "source", "url": "https://example.com/privacy"}],
            3,
        )
        self.complete_one(
            "200",
            "https://example.com/privacy.pdf",
            21000,
            [{"text": "source", "url": "https://example.com/privacy.pdf"}],
            1,
        )

        report = self.milestone_status.collect_status(self.db_path, [10_000])

        self.assertEqual(report["document_quality"]["total_documents"], 2)
        self.assertEqual(report["document_quality"]["pdf_documents"], 1)
        self.assertEqual(report["document_quality"]["documents_with_cluster_nodes"], 1)
        self.assertEqual(report["document_quality"]["total_cluster_nodes"], 4)
        self.assertEqual(report["link_quality"]["documents_with_links"], 2)
        self.assertEqual(report["link_quality"]["documents_with_source_link"], 2)
        self.assertEqual(report["accepted_attempt_quality"], {"ok": 2})
        self.assertEqual(
            {row["bucket"]: row["count"] for row in report["document_quality"]["text_char_buckets"]},
            {"1000-4999": 1, "20000+": 1},
        )


if __name__ == "__main__":
    unittest.main()
