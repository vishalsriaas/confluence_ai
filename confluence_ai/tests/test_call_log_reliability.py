import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from confluence_ai.services import call_disposition, livekit, recording_transcription, vobiz


class TestCallLogReliability(unittest.TestCase):
    def test_transcript_selection_rejects_other_calls(self):
        row = {"call_uuid": "other-call", "transcription_id": "other-call", "transcription_text": "Other customer"}
        self.assertIsNone(recording_transcription._select_vobiz_transcription([row], "wanted-call"))

    def test_transcript_selection_requires_identity(self):
        self.assertIsNone(recording_transcription._select_vobiz_transcription([{"transcript": "Unidentified"}], "wanted-call"))

    def test_transcript_selection_uses_exact_match(self):
        correct = {"call_uuid": "wanted-call", "transcription_text": "Correct customer"}
        wrong = {"call_uuid": "other-call", "transcription_text": "Wrong customer"}
        self.assertEqual(recording_transcription._select_vobiz_transcription([wrong, correct], "wanted-call"), correct)

    def test_unknown_call_id_does_not_match_another_call_by_phone(self):
        with patch.object(vobiz.frappe.db, "exists", return_value=None), patch.object(vobiz, "_find_existing_call_log_by_phone_window") as nearby:
            self.assertIsNone(vobiz._find_existing_call_log({"call_uuid": "new-call", "To": "9999999999", "From": "8888888888"}))
            nearby.assert_not_called()

    def test_recording_download_uses_vobiz_media_credentials(self):
        response = Mock(status_code=200, content=b"RIFF-audio")
        task = SimpleNamespace(name="task-unit", call_uuid="call-unit")
        credentials = {"X-Auth-ID": "MA_UNIT", "X-Auth-Token": "unit-token"}
        with patch.object(vobiz, "_vobiz_media_auth_candidates", return_value=[credentials]), patch.object(vobiz.requests, "get", return_value=response) as request, patch("frappe.utils.file_manager.save_file", return_value=SimpleNamespace(file_url="/private/files/unit.wav")):
            result = vobiz.download_vobiz_recording("https://media.vobiz.ai/v1/Account/MA_UNIT/Recording/unit.wav", task)
        self.assertEqual(result, "/private/files/unit.wav")
        self.assertEqual(request.call_args.kwargs["headers"], credentials)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_late_recording_does_not_attach_to_new_task_by_phone(self):
        payload = {"TrunkID": "trunk-unit", "CallUUID": "old-call", "event": "recording"}
        with patch.object(vobiz, "_candidate_livekit_trunk_ids", return_value=["trunk-unit"]), patch.object(vobiz, "_customer_phone_from_payload", return_value="+919999999999"), patch.object(vobiz.frappe, "get_all", return_value=[]) as query, patch.object(vobiz, "_find_repeat_followup_task_by_phone_and_trunk") as fallback:
            self.assertEqual(vobiz.find_task_and_attempt(payload), (None, None))
        fallback.assert_not_called()
        for call in query.call_args_list:
            self.assertNotIn("status", call.kwargs.get("filters", {}))

    def test_initial_callback_can_still_resolve_pending_task(self):
        payload = {"TrunkID": "trunk-unit", "CallUUID": "new-call", "event": "CallInitiated"}
        with patch.object(vobiz, "_candidate_livekit_trunk_ids", return_value=["trunk-unit"]), patch.object(vobiz, "_customer_phone_from_payload", return_value="+919999999999"), patch.object(vobiz.frappe, "get_all", return_value=[]), patch.object(vobiz, "_find_repeat_followup_task_by_phone_and_trunk", return_value=("task-unit", "attempt-unit")) as fallback:
            self.assertEqual(vobiz.find_task_and_attempt(payload), ("task-unit", "attempt-unit"))
        fallback.assert_called_once_with(["trunk-unit"], "9999999999")

    def test_recording_download_does_not_send_secrets_to_other_host(self):
        with patch.object(vobiz.requests, "get") as request:
            self.assertIsNone(vobiz.download_vobiz_recording("https://example.com/audio.wav", SimpleNamespace()))
            request.assert_not_called()

    def test_provider_schema_accepts_skipped_events(self):
        schema = Path(__file__).parents[1] / "confluence_ai/doctype/ai_provider_event/ai_provider_event.json"
        fields = json.loads(schema.read_text())["fields"]
        status = next(field for field in fields if field["fieldname"] == "status")
        self.assertIn("Skipped", status["options"].splitlines())

    def test_webhook_naming_does_not_acquire_series_lock(self):
        from confluence_ai.confluence_ai.doctype.ai_webhook_event.ai_webhook_event import AIWebhookEvent

        with patch("frappe.model.naming.getseries", side_effect=AssertionError("No naming series for callback logs")):
            doc = AIWebhookEvent({"doctype": "AI Webhook Event"})
            doc.autoname()
        self.assertTrue(doc.name.startswith("webhook-"))
        self.assertEqual(len(doc.name), 28)

    def test_waiting_disposition_cannot_overwrite_arrived_transcript(self):
        doc = frappe._dict(name="call-unit", ai_disposition="Fresh")
        current = Mock()
        current.get.side_effect = {"transcript": "Customer reply", "ai_disposition": "Fresh"}.get
        with patch.object(call_disposition.frappe, "get_doc", return_value=current) as get_doc:
            call_disposition._save_update_state(doc, "Skipped", {"reason": "waiting_for_transcript"})
        get_doc.assert_called_once_with("AI Call Log", "call-unit", for_update=True)
        current.save.assert_not_called()

    def test_stale_erp_response_cannot_overwrite_manual_disposition(self):
        doc = frappe._dict(name="call-unit", ai_disposition="Fresh")
        current = Mock()
        current.get.side_effect = {"ai_disposition": "Not Interested"}.get
        with patch.object(call_disposition.frappe, "get_doc", return_value=current):
            call_disposition._save_update_state(doc, "Succeeded", {})
        current.save.assert_not_called()

    def test_crm_lead_id_is_not_used_as_call_id(self):
        task = SimpleNamespace(name="task-unit", context_json="{}", call_uuid=None, external_record_id="CRM-LEAD-1")
        doc = Mock(name="call-unit")
        fake = SimpleNamespace(db=SimpleNamespace(exists=Mock(return_value=True)), new_doc=Mock(return_value=doc))
        with patch.object(livekit, "frappe", fake), patch.object(livekit, "_livekit_call_log_name", return_value=None), patch.object(livekit, "_apply_livekit_call_log_payload") as apply:
            livekit._upsert_livekit_call_log({"room_name": "agent-army-task-unit"}, task)
        self.assertEqual(apply.call_args.kwargs["call_uuid"], "agent-army-task-unit")
