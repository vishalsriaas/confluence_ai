import unittest

from confluence_ai.api.ingest import _task_idempotency_key


class TestIngestHelpers(unittest.TestCase):
    def test_task_idempotency_key_prefers_batch_key(self):
        self.assertEqual(_task_idempotency_key("BATCH-1", "external-1", "PAT-1"), "external-1:PAT-1")

    def test_task_idempotency_key_falls_back_to_batch_name(self):
        self.assertEqual(_task_idempotency_key("BATCH-1", None, "PAT-1"), "BATCH-1:PAT-1")
