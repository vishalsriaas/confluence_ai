from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from confluence_ai.services.executor import _prepare_voice_start_context


class TestExecutorVoiceStartContext(unittest.TestCase):
    def test_voice_task_runs_start_context_enrichment(self):
        task = SimpleNamespace(
            name="task-unit-outbound",
            channel="Voice",
            assigned_agent="agent-unit",
            target_agent=None,
            context_json=frappe.as_json({"phone": "+919873090386"}),
            save=Mock(),
        )
        enriched = {"phone": "+919873090386", "whatsapp_conversation_summary": "Customer asked for details on WhatsApp."}

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=SimpleNamespace(name="agent-unit")),
        )

        with patch("confluence_ai.services.executor.frappe", fake_frappe), \
            patch("confluence_ai.services.sales_context.enrich_start_context_tools", Mock(return_value=enriched)) as enrich:
            result = _prepare_voice_start_context(task, {"phone": "+919873090386"})

        self.assertEqual(result, enriched)
        enrich.assert_called_once()
        self.assertIn("whatsapp_conversation_summary", task.context_json)
        task.save.assert_called_once_with(ignore_permissions=True)

    def test_non_voice_task_does_not_run_start_context_enrichment(self):
        task = SimpleNamespace(
            name="task-unit-whatsapp",
            channel="WhatsApp",
            assigned_agent="agent-unit",
            target_agent=None,
            context_json=frappe.as_json({"phone": "+919873090386"}),
            save=Mock(),
        )

        with patch("confluence_ai.services.sales_context.enrich_start_context_tools") as enrich:
            result = _prepare_voice_start_context(task, {"phone": "+919873090386"})

        self.assertEqual(result, {"phone": "+919873090386"})
        enrich.assert_not_called()
        task.save.assert_not_called()
