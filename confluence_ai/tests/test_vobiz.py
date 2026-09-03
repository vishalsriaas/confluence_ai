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
