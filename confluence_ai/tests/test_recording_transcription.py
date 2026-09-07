from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from confluence_ai.services import recording_transcription
from confluence_ai.services.recording_transcription import RecordingTranscriptionConfig, RecordingTranscriptionSkipped


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

    def test_successful_fallback_saves_transcript_and_queues_disposition(self):
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
        saved_values = {}

        def set_value(doctype, name, values, update_modified=True):
            saved_values.update(values)
            for fieldname, value in values.items():
                setattr(doc, fieldname, value)

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True), commit=Mock(), set_value=Mock(side_effect=set_value)),
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
        self.assertIn("transcript_payload_json", saved_values)
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

    def test_openai_compatible_config_reuses_existing_summary_key(self):
        class FakeMeta:
            def has_field(self, fieldname):
                return True

        class FakeSettings:
            meta = FakeMeta()

            def get(self, fieldname):
                return {
                    "enable_recording_transcription_fallback": 1,
                    "recording_transcription_provider": "OpenAI Compatible",
                    "recording_transcription_model": "",
                    "recording_transcription_base_url": "",
                    "recording_transcription_path": "",
                    "recording_transcription_timeout_seconds": 60,
                    "recording_transcription_lookback_minutes": 360,
                    "recording_transcription_limit": 50,
                    "recording_transcription_wait_minutes": 2,
                    "recording_transcription_max_audio_mb": 25,
                    "whatsapp_summary_base_url": "https://api.openai.com/v1",
                }.get(fieldname)

            def get_password(self, fieldname, raise_exception=False):
                return {"whatsapp_summary_api_key": "summary-secret"}.get(fieldname, "")

        fake_frappe = SimpleNamespace(
            get_single=Mock(return_value=FakeSettings()),
            conf={},
        )

        with patch("confluence_ai.services.recording_transcription.frappe", fake_frappe):
            config = recording_transcription.get_recording_transcription_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.provider, "OpenAI Compatible")
        self.assertEqual(config.model, "whisper-1")
        self.assertEqual(config.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.api_key, "summary-secret")

    def test_fallback_transcript_syncs_related_docs_and_queues_disposition(self):
        class FakeDoc:
            name = "call-unit"
            company = "globifit"
            task = "task-unit"
            attempt = "attempt-unit"
            agent = "agent-unit"
            call_uuid = "call-unit"
            sip_call_id = "sip-unit"
            trunk_id = "trunk-unit"
            direction = "Outbound"
            from_number = "+919262175574"
            to_number = "+919873090386"
            customer_phone = "+919873090386"
            recording_url = "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-unit.wav"
            external_recording_url = recording_url

            def get(self, fieldname):
                return getattr(self, fieldname, None)

        writes = []

        def set_value(doctype, name, values, update_modified=True):
            writes.append((doctype, name, values))

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(
                exists=Mock(return_value=True),
                set_value=Mock(side_effect=set_value),
                commit=Mock(),
            ),
            get_doc=Mock(return_value=FakeDoc()),
            get_meta=Mock(return_value=SimpleNamespace(has_field=Mock(return_value=True))),
        )

        with patch("confluence_ai.services.recording_transcription.frappe", fake_frappe), \
            patch("confluence_ai.services.recording_transcription._notify_task_workflow_transcript", Mock(return_value={"fresh_followup": {"status": "completed"}})), \
            patch("confluence_ai.services.recording_transcription._enqueue_disposition_after_transcript") as enqueue:
            result = recording_transcription.emit_synthetic_transcript_callback(
                "call-unit",
                "[AGENT]: hello",
                "hello",
            )

        self.assertEqual(result["status"], "success")
        enqueue.assert_called_once_with("call-unit")
        self.assertEqual(result["task"], "task-unit")
        self.assertEqual(result["attempt"], "attempt-unit")
        self.assertEqual(result["workflow"]["fresh_followup"]["status"], "completed")
        self.assertIn(("AI Task", "task-unit"), [(row[0], row[1]) for row in writes])
        self.assertIn(("AI Task Attempt", "attempt-unit"), [(row[0], row[1]) for row in writes])

    def test_fallback_transcript_notifies_workflow_handlers_directly(self):
        class FakeDoc:
            name = "call-unit"
            company = "globifit"
            task = "task-unit"
            attempt = "attempt-unit"
            agent = "agent-unit"
            call_uuid = "call-unit"
            sip_call_id = "sip-unit"
            trunk_id = "trunk-unit"
            direction = "Outbound"
            from_number = "+919262175574"
            to_number = "+919873090386"
            customer_phone = "+919873090386"
            status = "Completed"
            recording_url = "https://media.vobiz.ai/v1/Account/MA_TEST/Recording/call-unit.wav"
            external_recording_url = recording_url

            def get(self, fieldname):
                return getattr(self, fieldname, None)

        task = SimpleNamespace(name="task-unit", channel="Voice", external_record_type="AI Fresh Follow Up Workflow", external_record_id="ffu-unit")
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=Mock(return_value=True)),
            get_doc=Mock(return_value=task),
        )
        fresh_handler = Mock(return_value={"status": "completed", "workflow": "ffu-unit"})

        with patch("confluence_ai.services.recording_transcription.frappe", fake_frappe), \
            patch("confluence_ai.services.vobiz._handle_order_confirmation_callback", Mock(return_value=None)), \
            patch("confluence_ai.services.vobiz._handle_repeat_followup_callback", Mock(return_value=None)), \
            patch("confluence_ai.services.vobiz._handle_fresh_followup_callback", fresh_handler):
            result = recording_transcription._notify_task_workflow_transcript(FakeDoc(), "[AGENT]: hello", "hello")

        self.assertEqual(result["fresh_followup"]["status"], "completed")
        args = fresh_handler.call_args.args
        self.assertEqual(args[0], task)
        self.assertEqual(args[2], "transcription.completed")
        self.assertEqual(args[1]["source"], "recording_transcription_fallback")

    def test_empty_wav_is_clean_skip_not_provider_error(self):
        empty_wav = (
            b"RIFF$\x00\x00\x00WAVE"
            b"fmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00"
            b"data\x00\x00\x00\x00"
        )

        with self.assertRaises(RecordingTranscriptionSkipped) as raised:
            recording_transcription._checked_audio_bytes(empty_wav, max_audio_mb=25)

        self.assertEqual(raised.exception.reason, "recording_audio_empty")

    def test_oversized_recording_is_clean_skip(self):
        with self.assertRaises(RecordingTranscriptionSkipped) as raised:
            recording_transcription._checked_audio_bytes(b"x" * 1025, max_audio_mb=0)

        self.assertEqual(raised.exception.reason, "recording_audio_too_large")


if __name__ == "__main__":
    unittest.main()
