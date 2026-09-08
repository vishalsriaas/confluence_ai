"""Production two-leg identities and real commit boundaries; no external calls."""
import unittest
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from unittest.mock import patch

import frappe

from confluence_ai.api import webhook
from confluence_ai.services import call_disposition as disposition, inbound_sales, vobiz
from confluence_ai.tests import test_inbound_startup as startup


class TestBridgeDispositionFlow(unittest.TestCase):
    setUp = startup.TestInboundStartup.setUp
    provider = startup.TestInboundStartup.provider
    resolver = startup.TestInboundStartup.resolver

    def tearDown(self):
        frappe.db.rollback()
        for dt in ("AI Call Identity", "AI Webhook Event", "AI Provider Event", "AI Error Log"):
            frappe.db.delete(dt, {"company": self.company})
        startup.TestInboundStartup.tearDown(self)

    def event(self, event):
        suffix = getattr(self, "case_suffix", "")
        payload = {**self.provider("customer-leg" + suffix), "company": self.company, "Event": event}
        if event == "Hangup":
            payload.update(CallUUID=self.company + "hangup-leg" + suffix,
                BridgeUUID=self.company + "sip-leg" + suffix, Status="completed", Duration=59)
        if event == "recording.completed":
            payload.update(recording_url="https://media.invalid/customer-leg.wav")
        if event == "transcription.completed":
            # Real transcript callbacks omit Direction and carry lower-case bridge_uuid.
            payload.pop("Direction")
            payload.update(bridge_uuid=self.company + "sip-leg" + suffix, transcript="[CUSTOMER]: Call tomorrow.", transcript_labels_normalized=True)
        return payload

    def receive(self, event):
        return webhook._process_telephony_receipt("vobiz", self.event(event), vobiz.handle_callback)

    def isolated_callbacks(self):
        from contextlib import ExitStack
        stack = ExitStack()
        for name in ("backfill_vobiz_recording_from_media", "_handle_order_confirmation_callback",
                     "_handle_repeat_followup_callback", "_handle_fresh_followup_callback"):
            stack.enter_context(patch.object(vobiz, name, return_value=None))
        return stack

    def test_customer_leg_and_sip_leg_never_create_two_tasks(self):
        with self.isolated_callbacks(), patch("frappe.enqueue"):
            self.assertEqual(self.receive("CallInitiated")["status"], "pending_matching")
            self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 0)
            result = inbound_sales.resolve_latest_inbound_metadata(self.resolver("sip-leg"))
            for event in ("recording.completed", "Hangup", "transcription.completed"):
                self.receive(event)
            for dt in ("AI Task", "AI Task Attempt", "AI Call Log"):
                self.assertEqual(frappe.db.count(dt, {"company": self.company}), 1, dt)
            doc = frappe.get_last_doc("AI Call Log", filters={"company": self.company})
            self.assertEqual(doc.task, result["task"])
            self.assertEqual(doc.customer_phone, "+919873090386")
            self.assertEqual(doc.direction, "Inbound")
            for kind in ("initiate", "hangup", "recording", "transcript"):
                self.assertEqual(doc.get(kind + "_event_status"), "Applied", kind)
            self.assertTrue(doc.recording_url)
            self.assertTrue(doc.transcript)
            self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)

    def test_bridge_receipts_before_room_replay_when_room_is_known(self):
        with self.isolated_callbacks(), patch("frappe.enqueue"):
            for event in ("CallInitiated", "transcription.completed", "recording.completed", "Hangup"):
                self.assertEqual(self.receive(event)["status"], "pending_matching")
            result = inbound_sales.resolve_latest_inbound_metadata(self.resolver("sip-leg"))
            doc = frappe.get_last_doc("AI Call Log", filters={"task": result["task"]})
            webhook.replay_pending_receipts(doc.name)
            doc.reload()
            self.assertTrue(doc.transcript)
            self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
            self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)

    def test_all_arrival_orders_with_distinct_customer_bridge_and_hangup_ids(self):
        events = ("CallInitiated", "Hangup", "recording.completed", "transcription.completed")
        with self.isolated_callbacks(), patch("frappe.enqueue"):
            for index, order in enumerate(permutations(events)):
                self.case_suffix = f"-{index}"
                resolved = inbound_sales.resolve_latest_inbound_metadata(self.resolver("sip-leg" + self.case_suffix))
                for event in order:
                    self.receive(event)
                    self.receive(event)
                docs = frappe.get_all("AI Call Log", filters={"task": resolved["task"]}, pluck="name")
                self.assertEqual(len(docs), 1)
                doc = frappe.get_doc("AI Call Log", docs[0])
                self.assertTrue(doc.transcript)
                self.assertTrue(doc.recording_url)
                self.assertEqual(doc.customer_phone, "+919873090386")
                for kind in ("initiate", "hangup", "recording", "transcript"):
                    self.assertEqual(doc.get(kind + "_event_status"), "Applied", (order, kind))
            self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 24)
            self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "status": "Pending Matching"}), 0)

    def new_log(self):
        result = inbound_sales.resolve_latest_inbound_metadata(self.resolver("sip-leg"))
        return frappe.get_last_doc("AI Call Log", filters={"task": result["task"]})

    def test_inbound_resolver_queues_pending_replay_after_commit(self):
        from confluence_ai.api import inbound
        with patch.object(inbound, "require_access"), \
             patch.object(inbound, "get_request_json", return_value=self.resolver("sip-leg")), \
             patch("frappe.enqueue") as queue:
            result = inbound.resolve_call()
            self.assertEqual(result["status"], "resolved")
            self.assertEqual(queue.call_args.args[0], "confluence_ai.api.webhook.replay_pending_receipts")
            self.assertTrue(queue.call_args.kwargs["enqueue_after_commit"])

    def decision(self):
        return {"ai_disposition": "Follow up", "ai_disposition_reason": "Requested tomorrow",
                "ai_disposition_summary": "Call tomorrow", "ai_disposition_confidence": 0.9}

    def test_transcript_commit_precedes_real_disposition_processing(self):
        doc = self.new_log()
        received = []
        site, sites_path = frappe.local.site, frappe.local.sites_path

        def execute_job(method, call_log, **kwargs):
            self.assertTrue(kwargs.get("enqueue_after_commit"))

            def consume():
                # Separate DB connection, like an RQ worker, not the webhook transaction.
                def run():
                    frappe.init(site=site, sites_path=sites_path)
                    frappe.connect()
                    frappe.flags.in_test = True
                    try:
                        visible = frappe.db.get_value("AI Call Log", call_log, "transcript")
                        received.append(visible)
                        return disposition.process_call_log(call_log)
                    finally:
                        frappe.destroy()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(run).result(timeout=30)
                self.assertEqual(result["status"], "success")
            frappe.db.after_commit.add(consume)

        with self.isolated_callbacks(), \
             patch("frappe.enqueue", side_effect=execute_job), \
             patch.object(disposition, "get_disposition_config", return_value=frappe._dict(enabled=True)), \
             patch.object(disposition, "classify_transcript", return_value=self.decision()) as classify, \
             patch.object(disposition, "update_crm_lead_status", return_value={"erp_status_update_status": "Succeeded"}) as erp:
            self.receive("transcription.completed")
            doc.reload()
            self.assertEqual(received, ["[CUSTOMER]: Call tomorrow."])
            self.assertEqual(doc.ai_disposition, "Follow up")
            self.assertEqual(doc.erp_status_update_status, "Succeeded")
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(erp.call_count, 1)

    def test_replayed_transcript_does_not_repeat_mcp(self):
        doc = self.new_log()
        with self.isolated_callbacks(), patch("frappe.enqueue"), \
             patch.object(disposition, "get_disposition_config", return_value=frappe._dict(enabled=True)), \
             patch.object(disposition, "classify_transcript", return_value=self.decision()) as classify, \
             patch.object(disposition, "update_crm_lead_status", return_value={"erp_status_update_status": "Succeeded"}) as erp:
            self.receive("transcription.completed")
            self.assertEqual(disposition.process_call_log(doc.name)["status"], "success")
            self.assertEqual(self.receive("transcription.completed")["status"], "duplicate")
            self.assertEqual(disposition.process_call_log(doc.name)["reason"], "already_updated")
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(erp.call_count, 1)

    def test_db_write_deadlock_retries_without_reclassifying(self):
        doc = self.new_log()
        original = frappe.db.get_value
        writes = 0

        def get_value(*args, **kwargs):
            nonlocal writes
            if args[:3] == ("AI Call Log", doc.name, "name") and kwargs.get("for_update"):
                writes += 1
                if writes == 1:
                    raise frappe.QueryDeadlockError("injected write deadlock")
            return original(*args, **kwargs)

        with patch.object(frappe.db, "get_value", side_effect=get_value):
            self.assertTrue(disposition._save_disposition(doc, self.decision()))
        doc.reload()
        self.assertEqual(doc.ai_disposition, "Follow up")
        self.assertEqual(writes, 2)

    def test_concurrent_disposition_jobs_classify_and_update_once(self):
        doc = self.new_log()
        frappe.db.set_value("AI Call Log", doc.name, "transcript", "Call tomorrow")
        frappe.db.commit()
        site, sites_path = frappe.local.site, frappe.local.sites_path
        barrier = threading.Barrier(2)

        def run():
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.flags.in_test = True
            try:
                barrier.wait(timeout=10)
                return disposition.process_call_log(doc.name)
            finally:
                frappe.destroy()

        def classify(*args):
            time.sleep(0.1)
            return self.decision()

        with patch.object(disposition, "get_disposition_config", return_value=frappe._dict(enabled=True)), \
             patch.object(disposition, "classify_transcript", side_effect=classify) as model, \
             patch.object(disposition, "update_crm_lead_status", return_value={"erp_status_update_status": "Succeeded"}) as erp:
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(run) for _ in range(2)]
                results = [job.result(timeout=30) for job in jobs]
            self.assertEqual(sorted(r["status"] for r in results), ["skipped", "success"])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(erp.call_count, 1)

    def test_late_transcript_cannot_be_overwritten_by_no_transcript_decision(self):
        doc = self.new_log()
        frappe.db.set_value("AI Call Log", doc.name, "transcript", "Now available")
        frappe.db.commit()
        self.assertFalse(disposition._save_disposition(doc, self.decision()))
        self.assertFalse(frappe.db.get_value("AI Call Log", doc.name, "ai_disposition"))

    def test_manual_edit_during_classification_is_synced_not_reclassified(self):
        doc = self.new_log()
        frappe.db.set_value("AI Call Log", doc.name, "ai_disposition", "Not Interested")
        frappe.db.commit()
        self.assertFalse(disposition._save_disposition(doc, self.decision()))
        with patch.object(disposition, "enqueue_saved_disposition_sync") as sync, \
             patch.object(disposition, "enqueue_call_disposition") as classify:
            disposition._enqueue_changed_call(doc)
            sync.assert_called_once_with(doc.name)
            classify.assert_not_called()

    def test_manual_disposition_sync_is_queued_after_commit(self):
        doc = self.new_log()
        with patch("frappe.enqueue") as queue:
            doc.ai_disposition = "Not Interested"
            doc.save(ignore_permissions=True)
            self.assertEqual(queue.call_args.args[0], "confluence_ai.services.call_disposition.sync_saved_disposition_to_erp")
            self.assertTrue(queue.call_args.kwargs["enqueue_after_commit"])
        frappe.db.rollback()

    def test_existing_two_task_duplicate_is_repaired_only_with_bridge_evidence(self):
        from confluence_ai.services.call_identity import repair_bridged_call_logs
        target = self.new_log()
        legacy = inbound_sales.handle_vobiz_inbound_call(self.provider("customer-leg"))
        source = frappe.get_last_doc("AI Call Log", filters={"task": legacy["task"]})
        for doc in (source, target):
            frappe.db.set_value("AI Call Log", doc.name, "status", "Completed")
            frappe.db.set_value("AI Task", doc.task, "status", "Completed")
        with self.assertRaises(frappe.ValidationError):
            repair_bridged_call_logs(source.name, target.name)
        frappe.db.set_value("AI Call Log", source.name, {
            "status_payload_json": json.dumps(self.event("Hangup")), "transcript": "Call tomorrow",
            "recording_url": "https://media.invalid/customer-leg.wav", "transcript_event_status": "Applied",
            "hangup_event_status": "Applied", "recording_event_status": "Applied"})
        frappe.db.commit()
        preview = repair_bridged_call_logs(source.name, target.name)
        self.assertEqual(preview["status"], "verified")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 2)
        with patch("frappe.enqueue"):
            repaired = repair_bridged_call_logs(source.name, target.name, dry_run=False)
            frappe.db.commit()
        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        target.reload()
        self.assertEqual(target.transcript, "Call tomorrow")
        self.assertEqual(target.customer_phone, "+919873090386")
        self.assertTrue(frappe.db.exists("AI Task", source.task))
        self.assertEqual(frappe.db.count("AI Webhook Event", {"company": self.company, "source": "call_bridge_repair"}), 1)
        from confluence_ai.services.call_registry import find_call
        self.assertEqual(find_call(self.event("Hangup"), self.company), target.name)
