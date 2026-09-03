from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from confluence_ai.services import vobiz
from confluence_ai.services.vobiz import normalize_vobiz_ai_transcript_labels


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

    def test_recording_backfill_matches_existing_call_log_by_phone_and_time(self):
        payload = {
            "company": "globifit",
            "Direction": "Inbound",
            "from_number": "00919035019329",
            "to_number": "00919262175574",
            "started_at": "2026-09-03 15:40:16.925828+05:30",
        }
        fake_frappe = SimpleNamespace(
            utils=SimpleNamespace(add_to_date=Mock(side_effect=["start", "end"])),
            db=SimpleNamespace(sql=Mock(return_value=[SimpleNamespace(name="call-existing")])),
        )

        with patch("confluence_ai.services.vobiz.frappe", fake_frappe):
            result = vobiz._find_existing_call_log_by_phone_window(payload)

        self.assertEqual(result, "call-existing")
        params = fake_frappe.db.sql.call_args.args[1]
        self.assertEqual(params["suffix_like"], "%9035019329")
        self.assertEqual(params["company"], "globifit")

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
