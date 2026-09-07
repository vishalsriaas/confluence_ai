from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe

from confluence_ai.services import fresh_followup as flow


class TestFreshFollowUpDeduplication(unittest.TestCase):
    """Isolated DB fixtures; no calls, MCPs or external jobs are dispatched."""

    def setUp(self):
        self.company = "unit-dedupe-" + frappe.generate_hash(length=10).lower()
        self.clock = patch.object(flow, "now_datetime", return_value=datetime(2099, 1, 1, 12)).start()
        self.enqueue = patch.object(flow, "enqueue_task_execution").start()
        self.counts = patch.object(flow, "refresh_batch_counts").start()
        self.addCleanup(patch.stopall)
        frappe.get_doc({"doctype": "AI Company", "company_key": self.company, "company_name": self.company}).insert(ignore_permissions=True)
        self.agent = frappe.get_doc({
            "doctype": "AI Agent", "company": self.company, "enabled": 1,
            "agent_name": self.company, "system_prompt": "Isolated unit test", "agent_type": "Single-Stage",
        }).insert(ignore_permissions=True).name
        self.template = frappe.get_doc({
            "doctype": "AI Task Template", "template_key": self.company,
            "template_name": self.company, "objective_prompt": "Isolated test", "default_channel": "Voice",
        }).insert(ignore_permissions=True).name
        self.settings = frappe.get_doc({
            "doctype": flow.SETTINGS, "company": self.company, "enabled": 1,
            "settings_name": self.company, "voice_task_template": self.template,
            "phone_field_names": "phone", "customer_name_field_names": "customer_name",
            "idempotency_key_field_names": "idempotency_key", "voice_call_timeout_minutes": 5,
            "minimum_connected_seconds": 10,
            "agents": [{
                "enabled": 1, "agent": self.agent, "max_attempts": 3,
                "retry_after_value": 30, "retry_after_unit": "Minutes",
                "followup_timing_mode": "Manual", "followup_after_value": 1, "followup_after_unit": "Days",
            } for _ in range(3)],
        }).insert(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        frappe.db.rollback()
        names = frappe.get_all(flow.WORKFLOW, filters={"company": self.company}, pluck="name")
        if names:
            frappe.db.delete(flow.WORKFLOW_AGENT, {"parent": ["in", names]})
        frappe.db.delete(flow.WORKFLOW, {"company": self.company})
        for doctype in ("AI Task Attempt", "AI Task", "AI Task Batch", "AI Agent"):
            frappe.db.delete(doctype, {"company": self.company})
        frappe.db.delete("AI Fresh Follow Up Agent", {"parent": self.settings.name})
        frappe.db.delete(flow.SETTINGS, {"name": self.settings.name})
        frappe.db.delete("AI Task Template", {"name": self.template})
        frappe.db.delete("AI Company", {"name": self.company})
        frappe.db.commit()

    def start(self, key="one", phone="9999999999"):
        result = flow.start_from_event({"company": self.company, "phone": phone, "idempotency_key": key})
        return result, frappe.get_doc(flow.WORKFLOW, result["workflow"])

    def test_different_events_and_phone_formats_share_one_workflow(self):
        first, doc = self.start()
        for index, phone in enumerate(("+919999999999", "919999999999", "00919999999999", "09999999999", "+91 99999 99999")):
            result, _ = self.start(str(index), phone)
            self.assertEqual(result["workflow"], first["workflow"])
            self.assertEqual(result["status"], "duplicate")
        self.assertEqual(frappe.db.count(flow.WORKFLOW, {"company": self.company}), 1)
        doc.reload()
        self.assertEqual(doc.agents[0].attempt_count, 1)
        self.enqueue.assert_called_once()

    def test_duplicate_webhook_after_completion_does_not_restart(self):
        result, doc = self.start()
        flow.handle_voice_result(task=result["task"], transcript="Customer: done", result={"duration_sec": 40, "follow_up_required": False})
        again, _ = self.start()
        self.assertEqual(again["status"], "duplicate")
        self.enqueue.assert_called_once()

    def test_different_company_and_international_phone_are_not_merged(self):
        _, doc = self.start()
        self.assertIsNone(flow._active_customer_workflow("different-company", doc.customer_phone))
        self.assertIsNone(flow._active_customer_workflow(self.company, "+449999999999"))

    def test_retry_interval_and_max_attempts_are_enforced(self):
        _, doc = self.start()
        for attempt in range(1, 4):
            doc.reload()
            self.assertEqual(doc.agents[0].attempt_count, attempt)
            result = flow.mark_call_missed(doc.name, task=doc.agents[0].task)
            doc.reload()
            if attempt < 3:
                due = self.clock.return_value + timedelta(minutes=30)
                self.assertEqual(doc.next_call_time, due)
                self.assertEqual(flow.queue_agent_call(doc.name)["reason"], "not_due")
                self.clock.return_value = due
                self.assertEqual(flow.queue_agent_call(doc.name)["status"], "queued")
            else:
                self.assertEqual(result["status"], "missed_after_attempts")
                self.assertEqual(flow.queue_agent_call(doc.name)["reason"], "final_state")
        self.assertEqual(self.enqueue.call_count, 3)

    def test_duplicate_missed_callback_does_not_postpone_retry(self):
        result, doc = self.start()
        flow.mark_call_missed(doc.name, task=result["task"])
        doc.reload()
        due = doc.next_call_time
        self.clock.return_value += timedelta(minutes=5)
        again = flow.mark_call_missed(doc.name, task=result["task"])
        self.assertEqual(again["status"], "ignored")
        doc.reload()
        self.assertEqual(doc.next_call_time, due)

    def test_duplicate_completion_does_not_reschedule_next_agent(self):
        result, doc = self.start()
        args = {"task": result["task"], "transcript": "Customer: call tomorrow", "result": {"duration_sec": 40, "follow_up_required": True}}
        flow.handle_voice_result(**args)
        doc.reload()
        due = doc.next_call_time
        self.clock.return_value += timedelta(hours=2)
        self.assertEqual(flow.handle_voice_result(**args)["status"], "ignored")
        doc.reload()
        self.assertEqual(doc.next_call_time, due)
        self.assertEqual(doc.next_agent_no, 2)

    def test_late_cancelled_callback_cannot_reactivate(self):
        result, doc = self.start()
        doc.status = "Cancelled"
        doc.enabled = 0
        doc.save(ignore_permissions=True)
        for operation in (
            lambda: flow.handle_voice_result(task=result["task"], transcript="Customer: hello", result={"follow_up_required": True}),
            lambda: flow.mark_call_missed(doc.name, task=result["task"]),
            lambda: flow.wait_for_voice_transcript(doc.name, task=result["task"]),
        ):
            self.assertEqual(operation()["status"], "ignored")
        doc.reload()
        self.assertEqual(doc.status, "Cancelled")
        task = frappe.get_doc("AI Task", result["task"])
        self.assertFalse(flow.task_dispatch_allowed(task))

    def test_old_attempt_callback_preserves_history_without_resetting_new_attempt(self):
        first, doc = self.start()
        flow.mark_call_missed(doc.name, task=first["task"])
        doc.reload()
        self.clock.return_value = doc.next_call_time
        second = flow.queue_agent_call(doc.name)
        response = flow.handle_voice_result(workflow=doc.name, task=first["task"], transcript="Customer: older call", result={"follow_up_required": True})
        self.assertEqual(response["status"], "ignored")
        doc.reload()
        self.assertEqual(doc.agents[0].task, second["task"])
        self.assertEqual(doc.agents[0].attempt_count, 2)
        self.assertIn("older call", doc.agents[0].transcript)

    def test_short_call_retries_same_agent(self):
        result, doc = self.start()
        response = flow.handle_voice_result(task=result["task"], result={"duration_sec": 5, "follow_up_required": True})
        self.assertEqual(response["status"], "retry_scheduled")
        doc.reload()
        self.assertEqual(doc.next_agent_no, 1)

    def test_agent_time_is_used_and_other_agent_cannot_be_forced(self):
        result, doc = self.start()
        doc.agents[1].followup_timing_mode = "Agent"
        doc.save(ignore_permissions=True)
        flow.handle_voice_result(task=result["task"], result={"duration_sec": 40, "follow_up_required": True, "next_follow_up_at": "2099-01-03 17:00:00"})
        doc.reload()
        self.assertEqual(doc.next_call_time, datetime(2099, 1, 3, 17))
        self.clock.return_value = doc.next_call_time
        self.assertEqual(flow.queue_agent_call(doc.name, 1)["reason"], "wrong_agent")
        self.assertEqual(flow.queue_agent_call(doc.name, 2)["agent_no"], 2)

    def test_stale_timeout_snapshot_does_not_miss_newer_call(self):
        _, doc = self.start()
        self.assertEqual(flow.mark_call_missed(doc.name, timeout_only=True)["reason"], "timeout_not_due")

    def test_cancelled_queued_job_cannot_dial(self):
        from confluence_ai.services import executor

        result, doc = self.start()
        self.assertTrue(flow.task_dispatch_allowed(frappe.get_doc("AI Task", result["task"])))
        doc.enabled = 0
        doc.status = "Cancelled"
        doc.save(ignore_permissions=True)
        with patch.object(executor, "_run_channel") as dial:
            response = executor.execute_task(result["task"])
            self.assertEqual(response["skipped"], "inactive_fresh_followup")
            dial.assert_not_called()
        self.assertEqual(frappe.db.get_value("AI Task", result["task"], "status"), "Cancelled")

    def test_delayed_hangup_cannot_clear_new_attempt_timeout(self):
        result, doc = self.start()
        flow.mark_call_missed(doc.name, task=result["task"])
        doc.reload()
        self.clock.return_value = doc.next_call_time
        flow.queue_agent_call(doc.name)
        doc.reload()
        deadline = doc.active_call_timeout_at
        self.assertEqual(flow.wait_for_voice_transcript(doc.name, task=result["task"])["status"], "ignored")
        doc.reload()
        self.assertEqual(doc.active_call_timeout_at, deadline)

    def test_normal_inbound_reuses_workflow_and_previous_transcript(self):
        first, doc = self.start()
        flow.handle_voice_result(task=first["task"], transcript="Customer: family se poochunga", result={"duration_sec": 40, "follow_up_required": True})
        inbound = frappe.get_doc({
            "doctype": "AI Task", "company": self.company, "status": "Running", "channel": "Voice",
            "task_batch": frappe.db.get_value("AI Task", first["task"], "task_batch"),
            "target_agent": self.agent, "assigned_agent": self.agent, "task_template": self.template,
            "external_record_type": "Vobiz Inbound Call", "external_record_id": "new-call",
            "context_json": frappe.as_json({"phone": "00919999999999"}),
        }).insert(ignore_permissions=True)
        response = flow.maybe_start_from_task(inbound)
        self.assertEqual(response["workflow"], doc.name)
        inbound.reload()
        context = frappe.parse_json(inbound.context_json)
        self.assertIn("family se poochunga", context["previous_transcript_summary"])
        doc.reload()
        self.assertEqual(doc.next_agent_no, 2)
        self.assertEqual(doc.agents[0].attempt_count, 1)
        self.assertEqual(frappe.db.count(flow.WORKFLOW, {"company": self.company}), 1)

    def test_legacy_duplicates_are_cancelled_and_attempts_are_preserved(self):
        first, doc = self.start()
        flow.mark_call_missed(doc.name, task=first["task"])
        doc.reload()
        duplicate = frappe.copy_doc(doc)
        duplicate.idempotency_key = "legacy-second"
        duplicate.agents[0].task = None
        duplicate.agents[0].transcript = "Customer: previous duplicate conversation"
        duplicate.insert(ignore_permissions=True)
        self.clock.return_value = doc.next_call_time
        response = flow.queue_agent_call(duplicate.name)
        self.assertEqual(response["status"], "queued")
        doc.reload()
        duplicate.reload()
        self.assertEqual(doc.status, "Cancelled")
        self.assertEqual(doc.enabled, 0)
        self.assertEqual(duplicate.agents[0].attempt_count, 3)
        self.assertIn("previous duplicate conversation", duplicate.agents[0].transcript)
        self.assertEqual(flow.queue_agent_call(doc.name)["reason"], "disabled")

    def _parallel(self, operations):
        site, sites_path = frappe.local.site, frappe.local.sites_path
        barrier = threading.Barrier(len(operations))
        def run(operation):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            try:
                barrier.wait(timeout=10)
                result = operation()
                frappe.db.commit()
                return result
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(run, operation) for operation in operations]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_creation_produces_one_workflow_and_one_dispatch(self):
        results = self._parallel([
            lambda key=key: flow.start_from_event({"company": self.company, "phone": "9999999999", "idempotency_key": key})
            for key in ("parallel-a", "parallel-b")
        ])
        self.assertEqual(len({r["workflow"] for r in results}), 1)
        self.assertEqual(sorted(r["status"] for r in results), ["duplicate", "queued"])
        self.assertEqual(self.enqueue.call_count, 1)

    def test_concurrent_dispatch_produces_only_one_retry(self):
        result, doc = self.start()
        flow.mark_call_missed(doc.name, task=result["task"])
        doc.reload()
        self.clock.return_value = doc.next_call_time
        frappe.db.commit()
        results = self._parallel([lambda: flow.queue_agent_call(doc.name)] * 2)
        self.assertEqual(sum(r["status"] == "queued" for r in results), 1)
        self.assertEqual(self.enqueue.call_count, 2)

    def test_concurrent_results_advance_stage_once(self):
        result, doc = self.start()
        results = self._parallel([lambda: flow.handle_voice_result(task=result["task"], result={"duration_sec": 40, "follow_up_required": True})] * 2)
        self.assertEqual(sorted(r["status"] for r in results), ["completed", "ignored"])
        doc = frappe.get_doc(flow.WORKFLOW, doc.name, for_update=True)
        self.assertEqual(doc.next_agent_no, 2)
