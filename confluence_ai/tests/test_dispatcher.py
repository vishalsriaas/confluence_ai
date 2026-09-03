from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from confluence_ai.services import dispatcher


class TestDispatcherTenantGuard(unittest.TestCase):
    def test_detects_task_template_company_mismatch(self):
        batch = SimpleNamespace(
            company="bharat",
            task_template="tmpl-08",
            target_agent=None,
            target_group=None,
        )
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(
                exists=Mock(return_value=True),
                get_value=Mock(return_value="sriaas"),
            ),
            get_meta=Mock(return_value=SimpleNamespace(has_field=Mock(return_value=True))),
        )

        with patch("confluence_ai.services.dispatcher.frappe", fake_frappe):
            result = dispatcher._batch_tenant_config_error(batch)

        self.assertEqual(result, "Task Template must belong to company bharat.")

    def test_dispatch_batch_marks_permanent_tenant_mismatch_failed(self):
        batch = SimpleNamespace(
            name="batch-tenant-mismatch",
            status="Running",
            company="eternity",
            task_template="tmpl-08",
            target_agent=None,
            target_group=None,
            save=Mock(),
        )
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(
                exists=Mock(return_value=True),
                get_value=Mock(return_value="sriaas"),
                sql=Mock(),
                set_value=Mock(),
            ),
            get_doc=Mock(return_value=batch),
            get_meta=Mock(return_value=SimpleNamespace(has_field=Mock(return_value=True))),
            utils=SimpleNamespace(now=Mock(return_value="2026-09-03 15:30:00")),
        )

        with patch("confluence_ai.services.dispatcher.frappe", fake_frappe), \
            patch("confluence_ai.services.dispatcher.refresh_batch_counts", Mock()):
            result = dispatcher.dispatch_batch(batch.name)

        self.assertEqual(
            result,
            {
                "failed": "Task Template must belong to company eternity.",
                "permanent": True,
            },
        )
        batch.save.assert_not_called()
        fake_frappe.db.sql.assert_called_once()
        fake_frappe.db.set_value.assert_called_once()


if __name__ == "__main__":
    unittest.main()
