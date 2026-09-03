from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from confluence_ai.services import recording_transcription
from confluence_ai.services.recording_transcription import RecordingTranscriptionConfig


def _config(**overrides):
    values = {
        "enabled": True,
        "provider": "Gemini",
        "model": "gemini-2.5-flash",
        "api_key": "secret",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "path": "",
        "timeout": 60,
        "lookback_minutes": 360,
        "limit": 50,
        "wait_minutes": 2,
        "max_audio_mb": 25,
    }
    values.update(overrides)
    return RecordingTranscriptionConfig(**values)


class TestRecordingTranscription(unittest.TestCase):
    def test_gemini_transcription_sends_inline_audio(self):
        class FakeResponse:
            ok = True

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "नमस्ते transcript"}]}}]}

        post = Mock(return_value=FakeResponse())

        with patch("confluence_ai.services.recording_transcription.requests.post", post):
            transcript = recording_transcription._transcribe_gemini(
                b"audio-bytes",
                mime_type="audio/wav",
                config=_config(),
            )

        self.assertEqual(transcript, "नमस्ते transcript")
        request_json = post.call_args.kwargs["json"]
        parts = request_json["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "audio/wav")
        self.assertTrue(parts[1]["inline_data"]["data"])

    def test_successful_fallback_saves_transcript_and_replays_callback(self):
        class FakeDoc:
            name = "call-unit"
            transcript = ""
            transcript_summary = ""
            recording_url = "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-unit.wav"
            external_recording_url = recording_url
            task = "task-unit"
            company = "globifit"
            agent = "agent-unit"
            call_uuid = "call-unit"
            sip_call_id = "sip-unit"
            trunk_id = "trunk-unit"
            flags = SimpleNamespace()

            def get(self, fieldname):
                return getattr(self, fieldname, None)

            def save(self, ignore_permissions=False):
                self.saved = True

        doc = FakeDoc()
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True), commit=Mock()),
            get_doc=Mock(return_value=doc),
            get_meta=Mock(return_value=SimpleNamespace(has_field=Mock(return_value=True))),
        )

        with patch("confluence_ai.services.recording_transcription.frappe", fake_frappe), \
            patch("confluence_ai.services.recording_transcription.fetch_call_recording_audio", Mock(return_value=(b"audio", "audio/wav"))), \
            patch("confluence_ai.services.recording_transcription.transcribe_recording_audio", Mock(return_value="[AGENT]: Namaste\n[CUSTOMER]: Hello")), \
            patch("confluence_ai.services.recording_transcription.emit_synthetic_transcript_callback", Mock(return_value={"status": "success"})) as replay, \
            patch("confluence_ai.services.recording_transcription.record_provider_event", Mock()):
            result = recording_transcription.process_call_log_recording_transcript("call-unit", config=_config())

        self.assertEqual(result["status"], "success")
        self.assertIn("[AGENT]: Namaste", doc.transcript)
        self.assertEqual(doc.transcript_summary, doc.transcript[:1000])
        replay.assert_called_once()

    def test_process_skips_when_transcript_already_present(self):
        class FakeDoc:
            name = "call-unit"
            transcript = "already there"
            transcript_summary = ""

            def get(self, fieldname):
                return getattr(self, fieldname, None)

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=FakeDoc()),
            get_meta=Mock(return_value=SimpleNamespace(has_field=Mock(return_value=True))),
        )

        with patch("confluence_ai.services.recording_transcription.frappe", fake_frappe):
            result = recording_transcription.process_call_log_recording_transcript("call-unit", config=_config())

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "transcript_already_present")


if __name__ == "__main__":
    unittest.main()
