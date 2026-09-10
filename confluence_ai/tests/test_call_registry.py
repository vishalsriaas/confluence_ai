import json
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from confluence_ai.api import webhook
from confluence_ai.services import call_registry as registry, livekit, vobiz
from confluence_ai.services.recording_transcription import (
    recovery_wait_reason, process_call_log_recording_transcript, process_missing_recording_transcripts,
)


class TestCallRegistry(unittest.TestCase):
    def setUp(self):
        self.company = "unit-registry-" + frappe.generate_hash(length=10).lower()
        frappe.get_doc({"doctype": "AI Company", "company_key": self.company, "company_name": self.company}).insert(ignore_permissions=True)
        template = frappe.get_doc({"doctype": "AI Task Template", "company": self.company,
            "template_key": self.company, "template_name": self.company, "objective_prompt": "unit"}).insert(ignore_permissions=True)
        batch = frappe.get_doc({"doctype": "AI Task Batch", "company": self.company,
            "source_system": "unit", "task_template": template.name}).insert(ignore_permissions=True)
        self.task = frappe.get_doc({"doctype": "AI Task", "company": self.company, "context_json": json.dumps({
            "direction": "Outbound", "customer_phone": "+919999999999", "outbound_phone_number": "+919999999998"})})
        self.task.flags.ignore_mandatory = True
        self.task.task_template = template.name
        self.task.task_batch = batch.name
        self.task.insert(ignore_permissions=True)
        self.attempt = self.new_attempt()
        self.patches = [
            patch.object(vobiz, "_candidate_channel_accounts", return_value=[]),
            patch("confluence_ai.services.inbound_sales.handle_vobiz_inbound_call", return_value={"status": "ignored"}),
            patch.object(vobiz, "backfill_vobiz_recording_from_media", return_value=None),
            patch.object(vobiz, "download_vobiz_recording", side_effect=lambda url, *args: url),
        ]
        for module in (vobiz, livekit):
            for name in ("_enqueue_call_disposition_if_ready", "_handle_order_confirmation_callback",
                         "_handle_repeat_followup_callback", "_handle_fresh_followup_callback"):
                self.patches.append(patch.object(module, name, return_value={}))
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        frappe.db.commit()

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        frappe.db.rollback()
        for doctype in ("AI Call Identity", "AI Webhook Event", "AI Call Log", "AI Task Attempt", "AI Task", "AI Task Batch", "AI Task Template", "AI Provider Event", "AI Error Log", "AI Company"):
            frappe.db.delete(doctype, {"name" if doctype == "AI Company" else "company": self.company})
        frappe.db.commit()

    def new_attempt(self):
        return frappe.get_doc({"doctype": "AI Task Attempt", "task": self.task.name, "company": self.company}).insert(ignore_permissions=True)

    def reserve(self, attempt=None):
        attempt = attempt or self.attempt
        return registry.reserve_call(self.task, attempt, json.loads(self.task.context_json))

    def payload(self, event, key="one"):
        return {"event": event, "company": self.company, "CallUUID": self.company + "-uuid-" + key,
            "SIPCallID": self.company + "-sip-" + key, "Direction": "Outbound",
            "From": "00919999999998", "To": "00919999999999",
            **({"CallStatus": "completed", "Duration": "42"} if event == "hangup" else {}),
            **({"recording_url": "https://media.invalid/" + key + ".wav"} if event == "recording" else {}),
            **({"transcript": "[CUSTOMER]: Hello", "transcript_labels_normalized": True} if event == "transcript" else {})}

    def identity(self, attempt=None, key="one"):
        attempt = attempt or self.attempt
        p = {"event": "call_identity", "task": self.task.name, "attempt": attempt.name,
            "sip_call_id": self.payload("hangup", key)["SIPCallID"], "identity_source": "sip.callIDFull", "direction": "Outbound"}
        return webhook._process_telephony_receipt("livekit", p, livekit.handle_callback)

    def test_live_failure_case_pending_then_identity_replays_without_second_log(self):
        self.reserve()
        for event in ("hangup", "recording", "transcript"):
            result = webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
            self.assertEqual(result["status"], "pending_matching")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        result = self.identity()
        doc = frappe.get_doc("AI Call Log", result["call_log"])
        self.assertTrue(doc.transcript)
        self.assertTrue(doc.recording_url)
        self.assertEqual(doc.direction, "Outbound")
        self.assertEqual(doc.from_number, "+919999999998")
        self.assertEqual(doc.to_number, "+919999999999")
        self.assertEqual(doc.call_uuid, self.payload("hangup")["CallUUID"])
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)

    def test_backend_dispatch_identity_matches_callbacks_without_worker_identity_event(self):
        from confluence_ai.services.executor import _register_voice_dispatch_identity
        reserved = self.reserve()
        for event in ("initiated", "hangup", "recording", "transcript"):
            webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
        with patch("frappe.enqueue") as enqueue:
            _register_voice_dispatch_identity(self.task, self.attempt, {
                "sip_call_id": self.payload("hangup")["SIPCallID"], "room_name": reserved["room_name"],
            })
        enqueue.assert_called_once()
        name = enqueue.call_args.kwargs["call_log"]
        self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
        frappe.db.commit()
        webhook.replay_pending_receipts(name)
        result = webhook._process_telephony_receipt("livekit", {
            "task": self.task.name, "room_name": reserved["room_name"],
            "event": "call_ended", "status": "completed",
        }, livekit.handle_callback)
        self.assertEqual(result["call_log"], name)
        doc = frappe.get_doc("AI Call Log", name)
        self.assertTrue(doc.transcript)
        self.assertTrue(doc.recording_url)
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)

    def test_all_24_event_orders_with_duplicate_receipts(self):
        for index, order in enumerate(permutations(("initiated", "hangup", "recording", "transcript"))):
            attempt = self.attempt if index == 0 else self.new_attempt()
            self.reserve(attempt)
            name = self.identity(attempt, str(index))["call_log"]
            for event in order:
                payload = self.payload(event, str(index))
                result = webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)
                self.assertEqual(result["call_log"], name)
                self.assertEqual(webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)["status"], "duplicate")
            doc = frappe.get_doc("AI Call Log", name)
            self.assertEqual(doc.status, "Completed")
            self.assertTrue(doc.transcript)
            self.assertTrue(doc.recording_url)
            for kind in ("initiate", "hangup", "recording", "transcript"):
                self.assertEqual(doc.get(kind + "_event_status"), "Applied")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 24)

    def test_old_attempt_does_not_update_latest_attempt(self):
        self.reserve()
        first = self.identity()["call_log"]
        second = self.new_attempt()
        self.reserve(second)
        other = self.identity(second, "two")["call_log"]
        self.assertNotEqual(first, other)
        result = webhook._process_telephony_receipt("vobiz", self.payload("transcript"), vobiz.handle_callback)
        self.assertEqual(result["call_log"], first)
        self.assertTrue(frappe.db.get_value("AI Call Log", first, "transcript"))
        self.assertFalse(frappe.db.get_value("AI Call Log", other, "transcript"))
        second.reload()
        self.assertEqual(second.status, "Started")

    def test_ambiguous_task_without_attempt_stays_pending(self):
        self.new_attempt()
        self.assertEqual(livekit.handle_callback({"task": self.task.name, "event": "call_ended"})["status"], "pending_matching")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 0)

    def test_unknown_phone_cannot_attach_to_reserved_call(self):
        self.reserve()
        self.assertIsNone(registry.find_call(self.payload("recording"), self.company))
        self.assertIsNone(vobiz.upsert_call_log(self.payload("recording")))

    def test_repeated_pending_receipt_does_not_repeat_handler_or_provider_event(self):
        self.reserve()
        payload = self.payload("recording")
        with patch.object(vobiz, "handle_callback", wraps=vobiz.handle_callback) as handler:
            first = webhook._process_telephony_receipt("vobiz", payload, handler)
            self.assertEqual(first["status"], "pending_matching")
            receipt = frappe.get_last_doc("AI Webhook Event", filters={"company": self.company})
            for _ in range(10):
                result = webhook._process_telephony_receipt("vobiz", payload, handler)
                self.assertEqual(result["status"], "pending_matching")
                self.assertEqual(result["webhook_event"], receipt.name)
            self.assertEqual(handler.call_count, 1)
        self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company}), 1)
        self.assertEqual(frappe.db.count("AI Provider Event", {"company": self.company, "operation": "callback_without_task"}), 1)
        self.assertEqual(str(frappe.db.get_value("AI Webhook Event", receipt.name, "modified")), str(receipt.modified))

    def test_recording_scan_stays_pending_then_identity_replays_it(self):
        self.reserve()
        channel = frappe._dict(name="unit-channel", company=self.company, vobiz_auth_id="unit-account")
        recording = {"call_uuid": self.payload("recording")["CallUUID"], "recording_id": "unit-recording",
            "recording_duration_ms": "10000", "recording_url": "https://media.invalid/one.wav"}
        cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10)
        with patch.object(vobiz, "_get_password", return_value="unit-token"), \
             patch.object(vobiz, "_fetch_vobiz_recording_list", return_value=[recording]):
            for _ in range(5):
                result = vobiz._backfill_recent_vobiz_recordings_for_channel(channel, cutoff=cutoff, limit=10)
                self.assertEqual(result, {"processed": [], "skipped": 0, "pending": 1})
            self.assertEqual(frappe.db.count("AI Provider Event", {"company": self.company, "operation": "recording_backfill"}), 0)
            self.assertEqual(frappe.db.count("AI Provider Event", {"company": self.company, "operation": "callback_without_task"}), 1)
            self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company}), 1)
            name = self.identity()["call_log"]
            webhook._process_telephony_receipt("vobiz", self.payload("hangup"), vobiz.handle_callback)
            self.assertEqual(frappe.db.get_value("AI Call Log", name, "recording_url"), recording["recording_url"])
            self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)
            result = vobiz._backfill_recent_vobiz_recordings_for_channel(channel, cutoff=cutoff, limit=10)
            self.assertEqual(result, {"processed": [], "skipped": 1, "pending": 0})
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)

    def test_pending_receipt_reprocesses_when_exact_identity_is_available(self):
        self.reserve()
        payload = self.payload("transcript")
        webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)
        from confluence_ai.services.call_identity import bind_provider_identity
        name = bind_provider_identity(self.task, {"sip_call_id": payload["SIPCallID"],
            "identity_source": "sip.callIDFull", "attempt": self.attempt.name})
        frappe.db.commit()
        result = webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)
        self.assertEqual(result["call_log"], name)
        self.assertTrue(frappe.db.get_value("AI Call Log", name, "transcript"))

    def test_backfill_success_is_logged_only_after_attachment(self):
        self.reserve()
        name = self.identity()["call_log"]
        payload = self.payload("hangup")
        webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)
        channel = frappe._dict(name="unit-channel", company=self.company, vobiz_auth_id="unit-account")
        recording = {"call_uuid": payload["CallUUID"], "recording_id": "unit-recording",
            "recording_duration_ms": "10000", "recording_url": "https://media.invalid/one.wav"}
        with patch.object(vobiz, "_get_password", return_value="unit-token"), \
             patch.object(vobiz, "_fetch_vobiz_recording_list", return_value=[recording]):
            result = vobiz._backfill_recent_vobiz_recordings_for_channel(channel,
                cutoff=frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10), limit=10)
        self.assertEqual(len(result["processed"]), 1)
        self.assertEqual(result["processed"][0]["call_log"], name)
        self.assertTrue(frappe.db.get_value("AI Call Log", name, "recording_url"))
        self.assertEqual(frappe.db.count("AI Provider Event", {"company": self.company, "operation": "recording_backfill", "status": "Succeeded"}), 1)

    def test_cross_company_attempt_rejected(self):
        self.reserve()
        with self.assertRaises(frappe.ValidationError):
            registry.resolve_call({"company": "other", "attempt": self.attempt.name}, self.task)

    def test_failed_receipt_is_preserved_and_retryable(self):
        payload = {"task": self.task.name, "attempt": self.attempt.name, "event": "unit"}
        def fail(_):
            raise RuntimeError("unit failure")
        with self.assertRaises(RuntimeError):
            webhook._process_telephony_receipt("livekit", payload, fail)
        self.assertEqual(frappe.db.count("AI Webhook Event", {"task": self.task.name, "status": "Failed"}), 1)
        self.assertEqual(webhook._process_telephony_receipt("livekit", payload, lambda _: {"status": "success"})["status"], "success")

    def test_recovery_grace_uses_later_timestamp(self):
        now = frappe.utils.now_datetime()
        cfg = SimpleNamespace(wait_minutes=5, retry_minutes=5, max_attempts=3)
        doc = frappe._dict(recording_url="x", sip_call_id="provider-id",
            call_end_received_at=frappe.utils.add_to_date(now, minutes=-10),
            recording_received_at=frappe.utils.add_to_date(now, minutes=-4))
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "grace_or_retry_wait")
        doc.recording_received_at = frappe.utils.add_to_date(now, minutes=-5)
        self.assertIsNone(recovery_wait_reason(doc, cfg, now))
        doc.transcript_recovery_attempts = 3
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "recovery_checks_exhausted")
        doc.transcript = "late webhook"
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "transcript_already_present")

    def test_duration_units_are_not_guessed_from_magnitude(self):
        self.assertEqual(registry.duration_seconds({"duration_ms": 31}), .031)
        self.assertEqual(registry.duration_seconds({"duration": 6000}), 6000)
        self.assertEqual(registry.duration_seconds({"duration_sec": 0, "duration_ms": 30000}), 0)

    def test_legacy_recovery_waits_without_inventing_receipt_times(self):
        now = frappe.utils.now_datetime()
        cfg = SimpleNamespace(wait_minutes=5, retry_minutes=2, max_attempts=3)
        doc = frappe._dict(recording_url="x", sip_call_id="provider-id", status="Completed",
            modified=frappe.utils.add_to_date(now, minutes=-4))
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "grace_or_retry_wait")
        doc.modified = frappe.utils.add_to_date(now, minutes=-6)
        self.assertIsNone(recovery_wait_reason(doc, cfg, now))
        self.assertIsNone(doc.recording_received_at)
        self.assertIsNone(doc.call_end_received_at)
        doc.status = "In Progress"
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "waiting_for_call_end_and_recording")
        doc.status = "Completed"
        doc.transcript_recovery_attempts = 1
        doc.modified = now
        doc.transcript_recovery_next_at = frappe.utils.add_to_date(now, minutes=2)
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "grace_or_retry_wait")
        self.assertIsNone(recovery_wait_reason(doc, cfg, frappe.utils.add_to_date(now, minutes=2)))
        doc.transcript_recovery_attempts = 3
        self.assertEqual(recovery_wait_reason(doc, cfg, now), "recovery_checks_exhausted")

    def test_recording_observed_outside_recording_event_sets_receipt_once(self):
        doc = frappe.new_doc("AI Call Log")
        doc.recording_url = "https://media.invalid/known.wav"
        registry.apply_event_state(doc, {"event": "call_ended", "_received_at": "2026-09-08 10:00:00"})
        self.assertEqual(doc.recording_received_at, "2026-09-08 10:00:00")
        self.assertEqual(doc.call_end_received_at, "2026-09-08 10:00:00")
        registry.apply_event_state(doc, {"event": "recording.completed", "_received_at": "2026-09-08 10:01:00"})
        self.assertEqual(doc.recording_received_at, "2026-09-08 10:00:00")

    def test_legacy_transcript_without_task_queues_disposition_once(self):
        payload = self.payload("transcript")
        doc = frappe.get_doc({"doctype": "AI Call Log", "company": self.company,
            "call_uuid": payload["CallUUID"], "sip_call_id": payload["SIPCallID"], "status": "Completed"}).insert(ignore_permissions=True)
        result = webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["call_log"], doc.name)
        vobiz._enqueue_call_disposition_if_ready.assert_called_once_with(doc.name, "transcript")
        self.assertEqual(webhook._process_telephony_receipt("vobiz", payload, vobiz.handle_callback)["status"], "duplicate")
        self.assertEqual(vobiz._enqueue_call_disposition_if_ready.call_count, 1)
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 1)

    def test_scheduler_fetches_legacy_recording_via_same_callback(self):
        payload = self.payload("recording")
        doc = frappe.get_doc({"doctype": "AI Call Log", "company": self.company,
            "call_uuid": payload["CallUUID"], "sip_call_id": payload["SIPCallID"],
            "status": "Completed", "recording_url": payload["recording_url"]}).insert(ignore_permissions=True)
        past = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-6)
        frappe.db.set_value("AI Call Log", doc.name, "modified", past, update_modified=False)
        cfg = SimpleNamespace(enabled=True, wait_minutes=5, retry_minutes=5, max_attempts=3,
            lookback_minutes=30, limit=200)
        def process_fixture(name, **kwargs):
            if name != doc.name:
                return {"status": "skipped", "reason": "outside_test_fixture"}
            return process_call_log_recording_transcript(name, **kwargs)
        with patch("confluence_ai.services.recording_transcription.get_recording_transcription_config", return_value=cfg), \
             patch("confluence_ai.services.recording_transcription.process_call_log_recording_transcript", side_effect=process_fixture), \
             patch("confluence_ai.services.recording_transcription.fetch_vobiz_transcript_for_call_log",
                   return_value={"status": "success", "transcript": "[CUSTOMER]: Verified legacy text"}) as fetch, \
             patch("confluence_ai.services.recording_transcription.transcribe_recording_audio", side_effect=AssertionError("No AI audio transcription")):
            result = process_missing_recording_transcripts()
        matched = [row for row in result["processed"] if row.get("call_log") == doc.name]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "success")
        self.assertTrue(fetch.called)
        doc.reload()
        self.assertEqual(doc.transcript, "[CUSTOMER]: Verified legacy text")
        self.assertEqual(doc.transcript_recovery_status, "Recovered")
        self.assertEqual(doc.transcript_recovery_attempts, 1)

    def test_utc_event_time_is_saved_in_site_timezone(self):
        from datetime import datetime
        doc = frappe.new_doc("AI Call Log")
        with patch("frappe.utils.get_system_timezone", return_value="Asia/Kolkata"):
            registry.apply_event_state(doc, {"event": "hangup", "EndTime": "2026-09-08T05:03:41Z"})
        self.assertEqual(doc.ended_at, datetime(2026, 9, 8, 10, 33, 41))

    def test_recovery_fetch_never_generates_audio_transcript(self):
        self.reserve()
        name = self.identity()["call_log"]
        for event in ("hangup", "recording"):
            webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
        past = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-6)
        frappe.db.set_value("AI Call Log", name, {"call_end_received_at": past, "recording_received_at": past})
        cfg = SimpleNamespace(enabled=True, wait_minutes=5, retry_minutes=5, max_attempts=3)
        with patch("confluence_ai.services.recording_transcription.fetch_vobiz_transcript_for_call_log",
                   return_value={"status": "success", "transcript": "[AGENT]: Correct transcript"}) as fetch, \
             patch("confluence_ai.services.recording_transcription.transcribe_recording_audio", side_effect=AssertionError("No AI allowed")):
            result = process_call_log_recording_transcript(name, config=cfg)
            self.assertEqual(result["status"], "success")
            self.assertEqual(frappe.db.get_value("AI Call Log", name, "transcript"), "[AGENT]: Correct transcript")
            process_call_log_recording_transcript(name, config=cfg)
            self.assertEqual(fetch.call_count, 1)

    def test_force_recovery_bypasses_retry_wait(self):
        self.reserve()
        name = self.identity()["call_log"]
        for event in ("hangup", "recording"):
            webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
        now = frappe.utils.now_datetime()
        future = frappe.utils.add_to_date(now, minutes=4)
        frappe.db.set_value("AI Call Log", name, {
            "call_end_received_at": now,
            "recording_received_at": now,
            "transcript_recovery_next_at": future,
        })
        cfg = SimpleNamespace(enabled=True, wait_minutes=5, retry_minutes=5, max_attempts=3)
        with patch("confluence_ai.services.recording_transcription.fetch_vobiz_transcript_for_call_log",
                   return_value={"status": "success", "transcript": "[AGENT]: Forced transcript"}) as fetch:
            self.assertEqual(process_call_log_recording_transcript(name, config=cfg)["reason"], "grace_or_retry_wait")
            result = process_call_log_recording_transcript(name, force=True, config=cfg)
        self.assertEqual(result["status"], "success")
        self.assertEqual(frappe.db.get_value("AI Call Log", name, "transcript"), "[AGENT]: Forced transcript")
        self.assertEqual(fetch.call_count, 1)

    def test_concurrent_callbacks_one_record(self):
        self.reserve()
        name = self.identity()["call_log"]
        frappe.db.commit()
        barrier = threading.Barrier(2)
        site, sites_path = frappe.local.site, frappe.local.sites_path
        def run(event):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            try:
                barrier.wait(timeout=10)
                return webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(run, event) for event in ("recording", "transcript")]
            self.assertEqual([job.result(timeout=30)["call_log"] for job in jobs], [name, name])
        doc = frappe.get_doc("AI Call Log", name)
        self.assertTrue(doc.recording_url)
        self.assertTrue(doc.transcript)
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)

    def test_missing_transcript_three_checks_then_late_webhook(self):
        self.reserve()
        name = self.identity()["call_log"]
        for event in ("hangup", "recording"):
            webhook._process_telephony_receipt("vobiz", self.payload(event), vobiz.handle_callback)
        past = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-6)
        frappe.db.set_value("AI Call Log", name, {"call_end_received_at": past, "recording_received_at": past})
        cfg = SimpleNamespace(enabled=True, wait_minutes=5, retry_minutes=5, max_attempts=3)
        with patch("confluence_ai.services.recording_transcription.fetch_vobiz_transcript_for_call_log",
                   return_value={"status": "skipped", "reason": "vobiz_transcript_not_ready"}) as fetch:
            for _ in range(3):
                process_call_log_recording_transcript(name, config=cfg)
                self.assertEqual(process_call_log_recording_transcript(name, config=cfg)["status"], "skipped")
                frappe.db.set_value("AI Call Log", name, "transcript_recovery_next_at", past)
            self.assertEqual(process_call_log_recording_transcript(name, config=cfg)["reason"], "recovery_checks_exhausted")
            self.assertEqual(fetch.call_count, 3)
        webhook._process_telephony_receipt("vobiz", self.payload("transcript"), vobiz.handle_callback)
        self.assertTrue(frappe.db.get_value("AI Call Log", name, "transcript"))
