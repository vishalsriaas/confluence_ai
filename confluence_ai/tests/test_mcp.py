import unittest
from unittest.mock import patch
from types import SimpleNamespace

import frappe

from confluence_ai.api.mcp import summarize_related_messages_for_prompt
from confluence_ai.services.mcp import assert_tool_allowed
from confluence_ai.services.sales_context import _summarize_start_context_records
from confluence_ai.services.utils import _extract_provider_chat_summary


class TestMCPPermissions(unittest.TestCase):
    @patch("confluence_ai.services.mcp.frappe")
    def test_assert_tool_allowed_blocks_missing_permission(self, fake_frappe):
        fake_frappe.db = SimpleNamespace(exists=lambda *args, **kwargs: False)
        fake_frappe.PermissionError = frappe.PermissionError
        with self.assertRaises(frappe.PermissionError):
            assert_tool_allowed("create_patient_note", agent="agent-1")

    def test_related_messages_build_compact_chat_summary(self):
        summary = summarize_related_messages_for_prompt(
            {"channel_account": "GLOBIFIT_MI", "linked_reference_name": "GSL-PAT-49"},
            [
                {"direction": "Inbound", "sender_type": "Customer", "body": "Hi"},
                {
                    "direction": "Outbound",
                    "sender_type": "AI",
                    "body": "Aapko kis cheez ka concern hai - erection, timing, energy, ya fertility?",
                },
                {"direction": "Inbound", "sender_type": "Customer", "body": "erection"},
                {"direction": "Inbound", "sender_type": "Customer", "body": "Ramy 32 age"},
            ],
        )

        self.assertIn("WhatsApp account: GLOBIFIT_MI", summary)
        self.assertIn("Customer said on WhatsApp", summary)
        self.assertIn("erection", summary)
        self.assertIn("Ramy 32 age", summary)
        self.assertLessEqual(len(summary), 1800)

    def test_provider_event_summary_extracts_from_mcp_body(self):
        summary = _extract_provider_chat_summary(
            {
                "body": [
                    {
                        "chat_summary": "Customer discussed erection concern and shared age 32.",
                        "related_messages": [{"body": "raw row should not be needed"}],
                    }
                ]
            }
        )

        self.assertEqual(summary, "Customer discussed erection concern and shared age 32.")

    def test_start_context_prefers_chat_summary_over_raw_messages(self):
        summary = _summarize_start_context_records(
            [
                {
                    "chat_summary": "Customer discussed erection concern and shared age 32.",
                    "related_messages": [
                        {"direction": "Inbound", "sender_type": "Customer", "body": "raw chat row"}
                    ],
                }
            ]
        )

        self.assertIn("Chat Summary: Customer discussed erection concern", summary)
        self.assertNotIn("raw chat row", summary)
