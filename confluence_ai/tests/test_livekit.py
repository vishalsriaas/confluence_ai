from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.exceptions import TimestampMismatchError

from confluence_ai.services.livekit import _outbound_sip_trunk_id, _upsert_livekit_call_log, _voice_metadata_context


class TestLiveKit(unittest.TestCase):
    def test_outbound_sip_trunk_prefers_explicit_outbound_id(self):
        account = frappe._dict({"trunk_id": "ST_INBOUND"})
        endpoints = {"outbound_sip_trunk_id": "ST_OUTBOUND", "sip_trunk_id": "ST_GENERIC"}

        self.assertEqual(_outbound_sip_trunk_id(account, endpoints), "ST_OUTBOUND")

    def test_outbound_sip_trunk_falls_back_to_legacy_fields(self):
        account = frappe._dict({"trunk_id": "ST_ACCOUNT"})

        self.assertEqual(_outbound_sip_trunk_id(account, {"sip_trunk_id": "ST_GENERIC"}), "ST_GENERIC")
        self.assertEqual(_outbound_sip_trunk_id(account, {}), "ST_ACCOUNT")

    def test_call_log_upsert_retries_after_timestamp_mismatch(self):
        class FakeMeta:
            def has_field(self, fieldname):
                return False

        class FakeCallLog:
            def __init__(self, fail_once=False):
                self.meta = FakeMeta()
                self.fail_once = fail_once

            def __getattr__(self, fieldname):
                return None

            def save(self, ignore_permissions=False):
                if self.fail_once:
                    self.fail_once = False
                    raise TimestampMismatchError("stale")

        task = SimpleNamespace(
            name="task-unit-livekit",
            context_json=frappe.as_json({"phone": "+919999999999"}),
            call_uuid="call-unit-livekit",
            external_record_id=None,
            assigned_agent="agent-unit",
            target_agent=None,
            company="globifit",
            trunk_id=None,
        )
        first_doc = FakeCallLog(fail_once=True)
        second_doc = FakeCallLog()
        fake_db = SimpleNamespace(exists=Mock(return_value=True), get_value=Mock(return_value=None))
        get_doc = Mock(side_effect=[first_doc, second_doc])

        with patch("confluence_ai.services.livekit.frappe.db", fake_db), \
            patch("confluence_ai.services.livekit._livekit_call_log_name", Mock(return_value="call-unit")), \
            patch("confluence_ai.services.livekit.frappe.get_doc", get_doc), \
            patch("confluence_ai.services.livekit.create_error") as create_error, \
            patch("confluence_ai.services.livekit.frappe.clear_messages", Mock()):
            _upsert_livekit_call_log(
                {
                    "event": "call_ended",
                    "status": "completed",
                    "duration_ms": 31000,
                    "ended_at": "2026-08-31 10:00:00",
                },
                task,
            )

        self.assertEqual(get_doc.call_count, 2)
        self.assertEqual(first_doc.status, "Completed")
        self.assertEqual(second_doc.status, "Completed")
        self.assertEqual(second_doc.duration_sec, 31)
        create_error.assert_not_called()

    def test_voice_metadata_promotes_start_context_whatsapp_summary(self):
        context = {
            "event": "inbound-sales-call",
            "customer_phone": "9582005503",
            "selected_sales_route": {"route": "sales-route"},
            "start_context_tools": {
                "GLOBIFIT_whatsapp_conversation_summary": {
                    "status": "success",
                    "found": True,
                    "summary": "1. Chat Summary: fallback should not be preferred.",
                    "records": [
                        {
                            "channel_account": "GLOBIFIT_MI",
                            "ai_summary": "Customer discussed erection concern, shared age 32, and asked to confirm order.",
                            "chat_summary": "Old/noisy summary should not be preferred.",
                        }
                    ],
                }
            },
        }

        metadata = _voice_metadata_context(context)

        self.assertTrue(metadata["whatsapp_conversation_found"])
        self.assertEqual(
            metadata["whatsapp_conversation_summary"],
            "Customer discussed erection concern, shared age 32, and asked to confirm order.",
        )
        self.assertNotIn("start_context_tools", metadata)
