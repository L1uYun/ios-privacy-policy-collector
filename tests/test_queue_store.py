import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "queue_store.py"


def load_module():
    spec = importlib.util.spec_from_file_location("queue_store", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.queue = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "queue.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_db_creates_expected_tables(self):
        self.queue.init_db(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }

        self.assertTrue(
            {
                "app_seed",
                "app_metadata",
                "policy_url_candidate",
                "policy_fetch",
                "policy_document",
                "policy_link",
                "run_event",
            }.issubset(tables)
        )

    def test_import_seeds_deduplicates_by_source_country_and_app(self):
        self.queue.init_db(self.db_path)
        rows = [
            {
                "seed_source": "fixture",
                "app_id": "123",
                "bundle_id": "com.example.app",
                "app_store_url": "https://apps.apple.com/us/app/example/id123",
                "country": "us",
                "provenance_url": "file://fixture",
                "license_note": "test",
            },
            {
                "seed_source": "fixture",
                "app_id": "123",
                "bundle_id": "com.example.app",
                "app_store_url": "https://apps.apple.com/us/app/example/id123",
                "country": "us",
                "provenance_url": "file://fixture",
                "license_note": "test",
            },
            {
                "seed_source": "fixture",
                "app_id": "123",
                "country": "gb",
            },
        ]

        result = self.queue.import_seeds(self.db_path, rows)
        stats = self.queue.stats(self.db_path)

        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(stats["seed_rows"], 2)
        self.assertEqual(stats["pending_fetches"], 2)

    def test_claim_next_fetch_marks_rows_running(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [
                {"seed_source": "fixture", "app_id": "123", "country": "us"},
                {"seed_source": "fixture", "app_id": "456", "country": "us"},
            ],
        )

        first = self.queue.claim_next_fetch(self.db_path, worker_id="w1")
        second = self.queue.claim_next_fetch(self.db_path, worker_id="w1")
        none_left = self.queue.claim_next_fetch(self.db_path, worker_id="w2")
        stats = self.queue.stats(self.db_path)

        self.assertEqual(first["app_id"], "123")
        self.assertEqual(second["app_id"], "456")
        self.assertIsNone(none_left)
        self.assertEqual(stats["running_fetches"], 2)

    def test_claim_next_fetch_can_prefer_active_validated_seed(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [
                {"seed_source": "fixture", "app_id": "111", "country": "us"},
                {"seed_source": "fixture", "app_id": "222", "country": "us"},
            ],
        )
        with closing(self.queue.connect(self.db_path)) as conn:
            seed_id = conn.execute("select seed_id from app_seed where app_id = '222'").fetchone()["seed_id"]
            conn.execute(
                """
                insert into seed_validation(seed_id, seed_source, app_id, country, status, result_count, validated_at)
                values (?, 'fixture', '222', 'us', 'active', 1, 'now')
                """,
                (seed_id,),
            )
            conn.commit()

        task = self.queue.claim_next_fetch(self.db_path, worker_id="w1", active_only=True)

        self.assertEqual(task["app_id"], "222")

    def test_claim_next_fetch_can_filter_countries(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [
                {"seed_source": "fixture", "app_id": "111", "country": "us"},
                {"seed_source": "fixture", "app_id": "222", "country": "gb"},
            ],
        )

        task = self.queue.claim_next_fetch(self.db_path, worker_id="w1", countries=["gb"])

        self.assertEqual(task["country"], "gb")

    def test_claim_next_fetch_can_filter_sources_and_order_newest(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [
                {"seed_source": "old-source", "app_id": "111", "country": "us"},
                {"seed_source": "apple-search:ai", "app_id": "222", "country": "us"},
                {"seed_source": "apple-search:ai", "app_id": "333", "country": "us"},
            ],
        )

        task = self.queue.claim_next_fetch(
            self.db_path,
            worker_id="w1",
            sources=["apple-search:ai"],
            claim_order="newest",
        )

        self.assertEqual(task["app_id"], "333")
        self.assertEqual(task["seed_source"], "apple-search:ai")

    def test_complete_fetch_records_policy_document_and_links(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": "123", "country": "us"}],
        )
        task = self.queue.claim_next_fetch(self.db_path, worker_id="w1")

        self.queue.complete_fetch(
            self.db_path,
            fetch_id=task["fetch_id"],
            result={
                "status": "ok",
                "policy_url": "https://example.com/privacy",
                "canonical_policy_url": "https://example.com/privacy",
                "policy_text_sha256": "abc123",
                "policy_text_chars": 1200,
                "policy_markdown_path": "out/us/123/privacy-policy.md",
                "policy_html_path": "out/us/123/privacy-policy.html",
                "policy_text_path": "out/us/123/privacy-policy.txt",
                "policy_fetch_method": "js",
                "policy_cluster_manifest_path": "out/us/123/policy-cluster/cluster.json",
                "policy_cluster_nodes_count": 3,
                "policy_cluster_edges_count": 2,
                "policy_links": [
                    {"text": "Contact", "url": "https://example.com/contact"}
                ],
                "policy_url_attempts": [
                    {
                        "policy_url": "https://example.com/loading",
                        "canonical_policy_url": "https://example.com/loading",
                        "attempt_index": 0,
                        "status": "rejected",
                        "fetch_method": "js",
                        "text_chars": 12,
                        "quality": "too_short",
                        "quality_reason": "below threshold",
                        "error_class": None,
                        "error_message": None,
                    },
                    {
                        "policy_url": "https://example.com/privacy",
                        "canonical_policy_url": "https://example.com/privacy",
                        "attempt_index": 1,
                        "status": "accepted",
                        "fetch_method": "static",
                        "text_chars": 1200,
                        "quality": "ok",
                        "quality_reason": None,
                        "error_class": None,
                        "error_message": None,
                    },
                ],
            },
        )
        stats = self.queue.stats(self.db_path)

        self.assertEqual(stats["ok_fetches"], 1)
        self.assertEqual(stats["policy_documents"], 1)
        self.assertEqual(stats["policy_links"], 1)
        self.assertEqual(stats["policy_url_attempts"], 2)
        with closing(self.queue.connect(self.db_path)) as conn:
            row = conn.execute(
                "select policy_fetch_method, policy_cluster_manifest_path, policy_cluster_nodes_count from policy_document"
            ).fetchone()
            attempts = conn.execute(
                "select status, fetch_method, text_chars, quality from policy_url_attempt order by attempt_index"
            ).fetchall()
        self.assertEqual(row["policy_fetch_method"], "js")
        self.assertEqual(row["policy_cluster_manifest_path"], "out/us/123/policy-cluster/cluster.json")
        self.assertEqual(row["policy_cluster_nodes_count"], 3)
        self.assertEqual([attempt["status"] for attempt in attempts], ["rejected", "accepted"])
        self.assertEqual(attempts[0]["fetch_method"], "js")

    def test_fail_fetch_records_policy_url_attempts(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": "123", "country": "us"}],
        )
        task = self.queue.claim_next_fetch(self.db_path, worker_id="w1")

        self.queue.fail_fetch(
            self.db_path,
            fetch_id=task["fetch_id"],
            error_class="RuntimeError",
            error_message="policy text not complete enough",
            retryable=False,
            max_attempts=1,
            policy_url_attempts=[
                {
                    "policy_url": "https://example.com/privacy",
                    "attempt_index": 0,
                    "status": "error",
                    "fetch_method": "static",
                    "text_chars": 0,
                    "quality": None,
                    "quality_reason": None,
                    "error_class": "HTTPError",
                    "error_message": "404",
                }
            ],
        )

        with closing(self.queue.connect(self.db_path)) as conn:
            attempt = conn.execute(
                "select status, error_class, error_message from policy_url_attempt"
            ).fetchone()
        self.assertEqual(attempt["status"], "error")
        self.assertEqual(attempt["error_class"], "HTTPError")

    def test_fail_fetch_can_retry_then_become_permanent(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": "123", "country": "us"}],
        )
        task = self.queue.claim_next_fetch(self.db_path, worker_id="w1")

        self.queue.fail_fetch(
            self.db_path,
            fetch_id=task["fetch_id"],
            error_class="HTTPError",
            error_message="403",
            retryable=True,
            max_attempts=2,
        )
        retried = self.queue.claim_next_fetch(self.db_path, worker_id="w2")
        self.queue.fail_fetch(
            self.db_path,
            fetch_id=retried["fetch_id"],
            error_class="HTTPError",
            error_message="403",
            retryable=True,
            max_attempts=2,
        )
        stats = self.queue.stats(self.db_path)

        self.assertEqual(stats["permanent_error_fetches"], 1)
        self.assertEqual(stats["pending_fetches"], 0)

    def test_requeue_stale_running_fetches_by_worker_prefix(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": "123", "country": "us"}],
        )
        self.queue.claim_next_fetch(self.db_path, worker_id="batch-old-001")

        result = self.queue.requeue_running_fetches(self.db_path, worker_prefix="batch-old")
        task = self.queue.claim_next_fetch(self.db_path, worker_id="new")

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(task["app_id"], "123")

    def test_requeue_fetch_resets_one_permanent_error(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [{"seed_source": "fixture", "app_id": "123", "country": "us"}],
        )
        task = self.queue.claim_next_fetch(self.db_path, worker_id="old")
        self.queue.fail_fetch(
            self.db_path,
            fetch_id=task["fetch_id"],
            error_class="RuntimeError",
            error_message="policy text not complete enough",
            retryable=False,
            max_attempts=1,
        )

        result = self.queue.requeue_fetch(self.db_path, task["fetch_id"])
        retried = self.queue.claim_next_fetch(self.db_path, worker_id="new")

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(retried["fetch_id"], task["fetch_id"])
        self.assertEqual(retried["app_id"], "123")

    def test_claim_fetch_by_id_claims_only_that_pending_fetch(self):
        self.queue.init_db(self.db_path)
        self.queue.import_seeds(
            self.db_path,
            [
                {"seed_source": "fixture", "app_id": "123", "country": "us"},
                {"seed_source": "fixture", "app_id": "456", "country": "us"},
            ],
        )
        first = self.queue.claim_next_fetch(self.db_path, worker_id="old")
        self.queue.fail_fetch(
            self.db_path,
            fetch_id=first["fetch_id"],
            error_class="RuntimeError",
            error_message="policy text not complete enough",
            retryable=False,
            max_attempts=1,
        )
        self.queue.requeue_fetch(self.db_path, first["fetch_id"])

        claimed = self.queue.claim_fetch_by_id(self.db_path, first["fetch_id"], "targeted")

        self.assertEqual(claimed["fetch_id"], first["fetch_id"])
        self.assertEqual(claimed["app_id"], "123")


if __name__ == "__main__":
    unittest.main()
