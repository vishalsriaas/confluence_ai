from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from confluence_ai.services import vobiz
from confluence_ai.services.vobiz import normalize_vobiz_ai_transcript_labels


class TestVobizOutboundStart(unittest.TestCase):
    def _docs(self, endpoint_paths_json='{"answer_url":"https://example.com/vobiz/answer?task={task}&attempt={attempt}"}'):
        task = SimpleNamespace(
            name="task-unit",
            company="globifit",
            assigned_agent="agent-unit",
            target_agent=None,
            channel="Voice",
            external_record_type=None,
        )
        agent = SimpleNamespace(name="agent-unit", allowed_channel_account="channel-unit")
        attempt = SimpleNamespace(name="attempt-unit", task="task-unit", company="globifit")

        class Account(SimpleNamespace):
            def get(self, fieldname):
                return getattr(self, fieldname, None)

            def get_password(self, fieldname, raise_exception=False):
                return getattr(self, fieldname, "")

        account = Account(
            name="channel-unit",
            company="globifit",
            vobiz_auth_id="MA_TEST",
            vobiz_auth_token="secret",
            default_from="+919262175574",
            trunk_id="TRUNK_TEST",
            endpoint_paths_json=endpoint_paths_json,
        )
        return task, agent, account, attempt

    def test_start_voice_task_calls_vobiz_call_api(self):
        task, agent, account, attempt = self._docs()

        class FakeResponse:
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {"api_id": "api-unit", "request_uuid": "vobiz-call-unit"}

        def get_doc(doctype, name):
            return {
                "AI Task": task,
                "AI Agent": agent,
                "AI Channel Account": account,
                "AI Task Attempt": attempt,
            }[doctype]

        fake_frappe = SimpleNamespace(
            get_doc=Mock(side_effect=get_doc),
            db=SimpleNamespace(exists=Mock(return_value=True), get_value=Mock(return_value="globifit")),
        )
        post = Mock(return_value=FakeResponse())

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz.requests.post", post), \
            patch("confluence_ai.services.vobiz.upsert_call_log", Mock(return_value="call-unit")) as upsert, \
            patch("confluence_ai.services.vobiz.record_provider_event"):
            result = vobiz.start_voice_task("task-unit", {"phone": "+919873090386", "attempt": "attempt-unit"})

        self.assertEqual(result["provider"], "Vobiz")
        self.assertEqual(result["sip_call_id"], "vobiz-call-unit")
        self.assertEqual(result["call_log"], "call-unit")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.vobiz.ai/api/v1/Account/MA_TEST/Call/")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Auth-ID"], "MA_TEST")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Auth-Token"], "secret")
        self.assertEqual(post.call_args.kwargs["json"]["from"], "+919262175574")
        self.assertEqual(post.call_args.kwargs["json"]["to"], "+919873090386")
        self.assertEqual(post.call_args.kwargs["json"]["answer_url"], "https://example.com/vobiz/answer?task=task-unit&attempt=attempt-unit")
        upsert.assert_called_once()

    def test_start_voice_task_requires_vobiz_answer_url(self):
        task, agent, account, attempt = self._docs(endpoint_paths_json="{}")

        def get_doc(doctype, name):
            return {
                "AI Task": task,
                "AI Agent": agent,
                "AI Channel Account": account,
                "AI Task Attempt": attempt,
            }[doctype]

        fake_frappe = SimpleNamespace(
            get_doc=Mock(side_effect=get_doc),
            db=SimpleNamespace(exists=Mock(return_value=True)),
        )

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "answer_url"):
                vobiz.start_voice_task("task-unit", {"phone": "+919873090386", "attempt": "attempt-unit"})

        post.assert_not_called()


class TestVobizTranscript(unittest.TestCase):
    def test_normalizes_reversed_ai_call_labels(self):
        transcript = (
            "[AGENT]: hello\n"
            "[CUSTOMER]: Namaste, main Vaani bol rahi hoon.\n"
            "[AGENT]: price kya hai?"
        )

        normalized = normalize_vobiz_ai_transcript_labels(transcript)

        self.assertEqual(
            normalized,
            "[CUSTOMER]: hello\n"
            "[AGENT]: Namaste, main Vaani bol rahi hoon.\n"
            "[CUSTOMER]: price kya hai?",
        )

    def test_leaves_unlabelled_text_unchanged(self):
        self.assertEqual(normalize_vobiz_ai_transcript_labels("plain transcript"), "plain transcript")

    def test_recording_transcription_fallback_labels_are_not_swapped(self):
        transcript = "[AGENT]: Namaste\n[CUSTOMER]: Hello"
        payload = {
            "source": "recording_transcription_fallback",
            "transcript": transcript,
        }

        self.assertEqual(vobiz._transcript_from_payload(payload), transcript)

    def test_pulled_vobiz_transcript_payload_is_not_swapped(self):
        class FakeDoc:
            name = "call-unit"

            def get(self, fieldname):
                return {
                    "company": "globifit",
                    "task": "task-unit",
                    "attempt": "attempt-unit",
                    "sip_call_id": "provider-sip",
                }.get(fieldname)

        transcript = "[AGENT]: Namaste\n[CUSTOMER]: Hello"
        payload = vobiz._vobiz_transcription_api_payload(
            {"call_uuid": "provider-sip", "transcription_id": "provider-sip", "transcription_text": transcript},
            "provider-sip",
            FakeDoc(),
            "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/provider-sip.wav",
            "MA_TEST",
        )

        self.assertTrue(payload["transcript_labels_normalized"])
        self.assertEqual(vobiz._transcript_from_payload(payload), transcript)

    def test_builds_expected_recording_url_from_transcript_payload(self):
        payload = {
            "event": "transcription.completed",
            "account_id": "MA_TEST",
            "call_uuid": "call-uuid-123",
        }

        self.assertEqual(
            vobiz._expected_vobiz_recording_url(payload),
            "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-uuid-123.wav",
        )

    def test_hangup_recording_url_prefers_sip_call_id(self):
        payload = {
            "Event": "Hangup",
            "AccountId": "MA_TEST",
            "CallUUID": "bridge-uuid",
            "SIPCallID": "original-call-uuid",
        }

        self.assertEqual(
            vobiz._expected_vobiz_recording_url(payload),
            "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/original-call-uuid.wav",
        )

    def test_backfills_recording_url_when_recording_webhook_is_missing(self):
        class FakeCallLog:
            recording_url = None
            external_recording_url = None
            recording_payload_json = None

            def save(self, ignore_permissions=False):
                self.saved = True

        call_log = FakeCallLog()
        task = SimpleNamespace(recording_url=None)
        attempt = SimpleNamespace(recording_url=None)
        payload = {
            "event": "transcription.completed",
            "account_id": "MA_TEST",
            "call_uuid": "call-uuid-123",
        }

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=call_log),
            get_all=Mock(return_value=[]),
        )

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz._vobiz_media_auth_candidates", Mock(return_value=[{"X-Auth-ID": "MA_TEST", "X-Auth-Token": "secret"}])), \
            patch("confluence_ai.services.vobiz._vobiz_media_url_exists", Mock(return_value=True)):
            recovered_url = vobiz.backfill_vobiz_recording_from_media(
                payload,
                task=task,
                attempt=attempt,
                call_log="call-unit",
            )

        self.assertEqual(
            recovered_url,
            "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-uuid-123.wav",
        )
        self.assertEqual(call_log.recording_url, recovered_url)
        self.assertEqual(call_log.external_recording_url, recovered_url)
        self.assertEqual(task.recording_url, recovered_url)
        self.assertEqual(attempt.recording_url, recovered_url)
        self.assertIn("recording.backfilled", call_log.recording_payload_json)

    def test_customer_phone_uses_inbound_caller_when_direction_is_inbound(self):
        payload = {
            "Direction": "Inbound",
            "From": "00919035019329",
            "To": "00919262175574",
        }

        self.assertEqual(vobiz._customer_phone_from_payload(payload), "00919035019329")

    def test_recording_api_payload_is_safe_for_backfilled_call_log(self):
        class FakeChannel:
            name = "channel-24547"

            def get(self, fieldname):
                return {
                    "company": "globifit",
                    "trunk_id": "ST_TEST",
                    "endpoint_paths_json": "{}",
                    "default_from": "+919262175574",
                }.get(fieldname)

        payload = vobiz._vobiz_recording_api_payload(
            {
                "add_time": "2026-09-03 15:40:16.925828+05:30",
                "call_uuid": "call-123",
                "recording_id": "call-123",
                "recording_duration_ms": "155360.00000",
                "recording_url": "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-123.wav",
                "from_number": "00919035019329",
                "to_number": "00919262175574",
            },
            FakeChannel(),
            "MA_TEST",
        )

        self.assertEqual(payload["event"], "recording.completed")
        self.assertEqual(payload["company"], "globifit")
        self.assertEqual(payload["CallUUID"], "call-123")
        self.assertEqual(payload["Duration"], 155)
        self.assertEqual(payload["Direction"], "Inbound")

    def test_phone_suffix_normalizes_common_number_formats(self):
        self.assertEqual(vobiz._phone_suffix("00919035019329"), "9035019329")
        self.assertEqual(vobiz._phone_suffix("+91 98730 90386"), "9873090386")
        self.assertIsNone(vobiz._phone_suffix(None))

    def test_media_call_ids_skip_livekit_internal_ids(self):
        doc = frappe._dict(
            sip_call_id="SCL_internal",
            call_uuid="agent-army-task-unit",
            recording_url="https://media.vobiz.ai/v1/Account/MA_TEST/Recording/provider-recording.wav",
            status_payload_json='{"SIPCallID":"provider-status"}',
        )
        attempt = frappe._dict(external_id="provider-attempt", call_uuid="SCL_attempt", response_json='{"vobiz_call_uuid":"provider-response"}')

        self.assertEqual(
            vobiz._vobiz_media_call_ids(doc, attempt=attempt),
            ["provider-recording", "provider-status", "provider-attempt", "provider-response"],
        )

    def test_recording_backfill_does_not_match_by_phone_and_time(self):
        payload = {"company": "globifit", "From": "00919035019329", "started_at": "2026-09-03 15:40:16"}
        with patch("confluence_ai.services.call_registry.company_for", return_value="globifit"):
            self.assertIsNone(vobiz._find_existing_call_log(payload))

    def test_fetch_vobiz_transcription_uses_exact_id(self):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "objects": [
                        {"call_uuid": "other-call", "transcription_text": "Wrong"},
                        {"call_uuid": "wanted-call", "transcription_text": "Correct"},
                    ]
                }

        with patch("confluence_ai.services.vobiz.requests.get", Mock(return_value=FakeResponse())):
            row = vobiz._fetch_vobiz_transcription_by_id("MA_TEST", "secret", "wanted-call")

        self.assertEqual(row["transcription_text"], "Correct")

    def test_recover_vobiz_media_uses_direct_callback_path(self):
        class FakeDoc:
            name = "call-unit"
            recording_url = None
            external_recording_url = None
            transcript = None
            task = None
            attempt = None

            def get(self, fieldname):
                return getattr(self, fieldname, None)

            def reload(self):
                return None

        class FakeChannel:
            name = "channel-unit"

            def get(self, fieldname):
                return {
                    "vobiz_auth_id": "MA_TEST",
                    "company": "globifit",
                    "trunk_id": "ST_TEST",
                    "endpoint_paths_json": "{}",
                }.get(fieldname)

        doc = FakeDoc()
        doc.company = "globifit"
        doc.sip_call_id = "provider-call"
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=doc),
        )
        callbacks = []

        def emit(payload):
            callbacks.append(payload)
            return {"status": "success", "call_log": "call-unit"}

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz._vobiz_media_recovery_auth_candidates", Mock(return_value=[{"X-Auth-ID": "MA_TEST", "X-Auth-Token": "secret", "_channel": FakeChannel()}])), \
            patch("confluence_ai.services.vobiz._fetch_vobiz_recording_by_id", Mock(return_value={
                "call_uuid": "provider-call",
                "recording_id": "provider-call",
                "recording_duration_ms": "1000",
                "recording_url": "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/provider-call.wav",
            })), \
            patch("confluence_ai.services.vobiz._fetch_vobiz_transcription_by_id", Mock(return_value={
                "call_uuid": "provider-call",
                "transcription_id": "provider-call",
                "transcription_text": "[AGENT]: Hi",
            })), \
            patch("confluence_ai.services.vobiz._emit_vobiz_media_callback", Mock(side_effect=emit)):
            result = vobiz.recover_vobiz_media_for_call_log("call-unit")

        self.assertEqual(result["status"], "success")
        self.assertEqual([payload["event"] for payload in callbacks], ["recording.completed", "transcription.completed"])
        self.assertEqual(callbacks[0]["source"], "vobiz_direct_media_recovery")
        self.assertEqual(callbacks[1]["source"], "recording_transcription_fallback")

    def test_recording_backfill_clears_missing_transcript_fallback_disposition(self):
        class FakeCallLog:
            ai_disposition = "Not Answered"
            ai_disposition_reason = "No transcript was received within 10 minutes after the call ended."
            ai_disposition_confidence = 0.95
            ai_disposition_summary = "Call ended, but no usable transcript was received for disposition review."
            transcript = None
            transcript_summary = None
            recording_url = "https://media.vobiz.ai/recording.wav"
            external_recording_url = "https://media.vobiz.ai/recording.wav"
            erp_status_update_status = "Succeeded"
            erp_status_update_response = "{}"
            flags = SimpleNamespace()

            def get(self, fieldname):
                return getattr(self, fieldname, None)

            def save(self, ignore_permissions=False):
                self.saved = True

        doc = FakeCallLog()
        fake_meta = SimpleNamespace(has_field=Mock(return_value=True))
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=doc),
            get_meta=Mock(return_value=fake_meta),
        )

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe):
            vobiz._mark_call_log_waiting_for_transcript("call-unit")

        self.assertEqual(doc.ai_disposition, "")
        self.assertEqual(doc.ai_disposition_reason, "")
        self.assertEqual(doc.ai_disposition_confidence, 0)
        self.assertEqual(doc.erp_status_update_status, "Skipped")
        self.assertIn("waiting_for_transcript", doc.erp_status_update_response)

    def test_recording_list_fetches_multiple_pages_until_limit(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        page_one = {
            "objects": [{"call_uuid": f"call-{idx}"} for idx in range(250)],
            "meta": {"next": "/next"},
        }
        page_two = {
            "objects": [{"call_uuid": f"call-{idx}"} for idx in range(250, 310)],
            "meta": {"next": None},
        }
        get = Mock(side_effect=[FakeResponse(page_one), FakeResponse(page_two)])

        with patch("confluence_ai.services.vobiz.requests.get", get):
            rows = vobiz._fetch_vobiz_recording_list("MA_TEST", "secret", limit=300)

        self.assertEqual(len(rows), 300)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["offset"], 0)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["offset"], 250)

    def test_existing_call_log_prefers_attempt_then_task(self):
        task = SimpleNamespace(name="task-unit")
        attempt = SimpleNamespace(name="attempt-unit")

        def exists(doctype, filters, *args, **kwargs):
            if filters == {"attempt": "attempt-unit"}:
                return "call-by-attempt"
            if filters == {"task": "task-unit"}:
                return "call-by-task"
            return None

        fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=Mock(side_effect=exists)))

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe):
            self.assertEqual(vobiz._find_existing_call_log_for_task(task=task, attempt=attempt), "call-by-attempt")
            self.assertEqual(vobiz._find_existing_call_log_for_task(task=task), "call-by-task")

    def test_vobiz_sip_id_replaces_livekit_room_sip_id(self):
        self.assertTrue(vobiz._should_replace_sip_call_id("agent-army-task-1", "Y0tbpQnHeQV6mxse"))
        self.assertTrue(vobiz._should_replace_sip_call_id("", "Y0tbpQnHeQV6mxse"))
        self.assertFalse(vobiz._should_replace_sip_call_id("already-real-sip", "Y0tbpQnHeQV6mxse"))

    def test_vobiz_upsert_updates_existing_livekit_task_call_log(self):
        class FakeCallLog:
            name = "call-existing"
            customer_phone = None
            sip_call_id = "agent-army-task-unit"
            call_uuid = "room-call-id"
            company = None
            agent = None
            status = None
            started_at = None
            ended_at = None
            reason = None
            trunk_id = None
            domain = None

            def get(self, fieldname):
                return getattr(self, fieldname, None)

            def save(self, ignore_permissions=False):
                self.saved = True

        existing_doc = FakeCallLog()
        task = SimpleNamespace(
            name="task-unit",
            assigned_agent="agent-unit",
            target_agent=None,
            company="globifit",
            context_json='{"customer_name": "Jagmohan", "customer_phone": "+919873090386"}',
        )
        attempt = SimpleNamespace(name="attempt-unit", company="globifit")

        def exists(doctype, filters=None):
            if doctype == "DocType" and filters == "AI Call Log":
                return True
            if filters == {"attempt": "attempt-unit"}:
                return None
            if filters == {"task": "task-unit"}:
                return "call-existing"
            return None

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(side_effect=exists), get_value=Mock(side_effect=lambda dt, filters, *a, **k: exists(dt, filters))),
            get_doc=Mock(return_value=existing_doc),
            new_doc=Mock(side_effect=AssertionError("upsert should reuse the existing task call log")),
        )

        payload = {
            "event": "recording.completed",
            "Direction": "Outbound",
            "From": "+919262175574",
            "To": "+919873090386",
            "CallUUID": "bridge-call-id",
            "SIPCallID": "Y0tbpQnHeQV6mxse",
            "CallStatus": "completed",
            "recording_url": "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/Y0tbpQnHeQV6mxse.wav",
        }

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz._candidate_channel_accounts", Mock(return_value=[])), \
            patch("confluence_ai.services.call_registry.resolve_call", return_value=existing_doc), \
            patch("confluence_ai.services.call_registry.register_call"), \
            patch("confluence_ai.services.call_registry.apply_event_state"):
            result = vobiz.upsert_call_log(payload, task=task, attempt=attempt)

        self.assertEqual(result, "call-existing")
        self.assertEqual(existing_doc.customer_name, "Jagmohan")
        self.assertEqual(existing_doc.customer_phone, "+919873090386")
        self.assertEqual(existing_doc.task, "task-unit")
        self.assertEqual(existing_doc.attempt, "attempt-unit")
        self.assertEqual(existing_doc.sip_call_id, "Y0tbpQnHeQV6mxse")
        self.assertEqual(existing_doc.recording_url, payload["recording_url"])
        self.assertTrue(existing_doc.saved)

    def test_unmatched_callback_is_logged_without_core_error(self):
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(get_value=Mock(return_value=None), commit=Mock()),
        )
        provider_event = Mock()

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe), \
            patch("confluence_ai.services.inbound_sales.handle_vobiz_inbound_call", Mock(return_value={"status": "ignored"})), \
            patch("confluence_ai.services.vobiz.find_task_and_attempt", Mock(return_value=(None, None))), \
            patch("confluence_ai.services.vobiz.upsert_call_log", Mock(return_value=None)), \
            patch("confluence_ai.services.vobiz.record_provider_event", provider_event):
            result = vobiz.handle_callback({"event": "recording.completed", "company": "globifit"})

        self.assertEqual(result["status"], "pending_matching")
        self.assertIsNone(result["call_log"])
        provider_event.assert_called_once()
