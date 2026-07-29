from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from confluence_ai.api.shipkia_voice import (
    _create_voice_test_session,
    _redact,
    get_voice_test_run,
    submit_voice_test_feedback,
)
from confluence_ai.services.livekit import _upsert_voice_test_run


class TestShipKiaVoiceLab(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def _create_run(self):
        token_builder = MagicMock()
        token_builder.with_identity.return_value = token_builder
        token_builder.with_ttl.return_value = token_builder
        token_builder.with_grants.return_value = token_builder
        token_builder.to_jwt.return_value = "memory-only-test-token"
        dispatch = {
            "status": "dispatched",
            "dispatch_id": "AD_test",
            "room_name": "shipkia-voice-test-room",
            "livekit_agent_name": "shipkia-voice-sales",
        }
        with (
            patch(
                "confluence_ai.api.shipkia_voice.livekit.start_voice_task",
                return_value=dispatch,
            ),
            patch(
                "confluence_ai.api.shipkia_voice.api.AccessToken",
                return_value=token_builder,
            ),
            patch("confluence_ai.api.shipkia_voice.frappe.db.commit"),
        ):
            return _create_voice_test_session(
                customer_phone="+919812345678",
                customer_name="Voice Test",
                test_case_id=None,
                prompt_version="shipkia-voice-v2",
                sandbox=True,
                confirm_integration_writes=False,
            )

    def test_session_api_persists_run_but_never_token(self):
        response = self._create_run()
        self.assertEqual(response["participant_token"], "memory-only-test-token")
        self.assertTrue(response["sandbox"])

        run = frappe.get_doc("AI Voice Test Run", response["run_id"])
        task = frappe.get_doc("AI Task", response["task"])
        context = json.loads(task.context_json)
        persisted = json.dumps({"run": run.as_dict(), "task": task.as_dict()}, default=str)
        self.assertNotIn("memory-only-test-token", persisted)
        self.assertEqual(run.prompt_version, "shipkia-voice-v2")
        self.assertEqual(context["voice_lab_sandbox"], 1)
        self.assertEqual(context["voice_test_run"], run.name)

    def test_callback_updates_transcript_metrics_and_failure(self):
        response = self._create_run()
        task = frappe.get_doc("AI Task", response["task"])
        payload = {
            "event": "call_ended",
            "status": "failed",
            "failure_code": "model_timeout",
            "reason": "watchdog exhausted",
            "transcript": "CUSTOMER: Please share rates\nAGENT: I can help with that.",
            "metrics": {
                "duration_seconds": 23,
                "response_latencies_ms": [800, 1200, 4800],
                "tool_latencies_ms": [900, 14000],
                "reconnect_count": 2,
                "recovery_attempted": 1,
                "recovery_succeeded": 0,
                "failure_code": "model_timeout",
            },
        }

        _upsert_voice_test_run(payload, task, "call_ended")
        run = frappe.get_doc("AI Voice Test Run", response["run_id"])
        self.assertEqual(run.status, "Failed")
        self.assertEqual(run.failure_code, "model_timeout")
        self.assertEqual(run.response_p95_ms, 4800)
        self.assertEqual(run.tool_p95_ms, 14000)
        self.assertEqual(run.reconnect_count, 2)
        self.assertEqual(run.recovery_outcome, "Failed")
        self.assertIn("CUSTOMER:", run.transcript)

    def test_feedback_api_saves_structured_review_and_redacts_secret(self):
        response = self._create_run()
        with patch("confluence_ai.api.shipkia_voice.frappe.db.commit"):
            saved = submit_voice_test_feedback(
                run_id=response["run_id"],
                verdict="Needs Work",
                scores={"sales_flow": 3},
                issue_tags=["repeated-question", "repeated-question"],
                notes="Customer said OTP is 123456; agent should ignore it.",
            )
        run = frappe.get_doc("AI Voice Test Run", response["run_id"])
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(run.verdict, "Needs Work")
        self.assertEqual(json.loads(run.scores_json), {"sales_flow": 3})
        self.assertEqual(run.issue_tags, "repeated-question")
        self.assertNotIn("123456", run.feedback_notes)

    def test_get_run_response_contains_no_token_field(self):
        response = self._create_run()
        result = get_voice_test_run(response["run_id"])
        serialized = json.dumps(result, default=str).lower()
        self.assertNotIn("participant_token", serialized)
        self.assertNotIn("api_secret", serialized)

    def test_feedback_redaction_covers_payment_and_access_secrets(self):
        redacted = _redact("CVV: 123 and access token = abc123")
        self.assertNotIn("123", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)
