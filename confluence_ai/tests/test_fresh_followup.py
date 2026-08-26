from __future__ import annotations

import unittest

import frappe
from frappe.utils import now_datetime

from confluence_ai.services import fresh_followup
from confluence_ai.services.utils import parse_json_object


class TestFreshFollowUp(unittest.TestCase):
    def tearDown(self):
        frappe.db.delete("AI Do Not Follow Up", {"normalized_phone": "+919999999999"})
        frappe.db.delete("AI Fresh Follow Up Workflow", {"idempotency_key": ["like", "unit-fresh-followup-%"]})
        frappe.db.delete("AI Task", {"external_record_type": "AI Fresh Follow Up Workflow"})
        frappe.db.delete("AI Task Batch", {"source_system": "AI Fresh Follow Up"})
        frappe.db.delete("AI Task Template", {"template_key": "unit_fresh_followup_voice"})
        frappe.db.commit()

    def test_connected_call_without_structured_outcome_schedules_next_agent(self):
        agent_1 = self._ensure_agent("Unit Fresh Follow Up Agent 1")
        agent_2 = self._ensure_agent("Unit Fresh Follow Up Agent 2")
        task = self._create_agent_1_task(agent_1)
        workflow = self._create_workflow(agent_1, agent_2, task.name)

        result = fresh_followup.handle_voice_result(task=task.name)
        workflow.reload()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(workflow.status, "Scheduled")
        self.assertEqual(workflow.next_agent_no, 2)
        self.assertIn("outcome_missing_after_connected_call", workflow.result_json)
        self.assertIn("Agent 2 scheduled", workflow.timer_status)
        agent_1_context = parse_json_object(frappe.db.get_value("AI Task", task.name, "context_json"))
        self.assertEqual(agent_1_context["fresh_followup_workflow"], workflow.name)

    def test_do_not_follow_up_blocks_scheduled_workflow(self):
        agent_1 = self._ensure_agent("Unit Fresh Follow Up Agent 1")
        workflow = frappe.new_doc("AI Fresh Follow Up Workflow")
        workflow.update(
            {
                "company": "sriaas",
                "enabled": 1,
                "status": "Scheduled",
                "idempotency_key": f"unit-fresh-followup-dnfu-{frappe.generate_hash(length=10)}",
                "customer_phone": "+919999999999",
                "current_agent_no": 1,
                "next_agent_no": 1,
                "minimum_connected_seconds": 10,
                "context_json": frappe.as_json({"phone": "+919999999999"}),
                "task_history_json": "[]",
            }
        )
        workflow.append(
            "agents",
            {
                "enabled": 1,
                "agent_no": 1,
                "agent": agent_1,
                "status": "Scheduled",
                "max_attempts": 3,
                "retry_after_value": 30,
                "retry_after_unit": "Minutes",
                "followup_timing_mode": "Manual",
                "followup_after_value": 0,
                "followup_after_unit": "Minutes",
            },
        )
        workflow.insert(ignore_permissions=True)

        do_not_follow_up = frappe.new_doc("AI Do Not Follow Up")
        do_not_follow_up.update(
            {
                "company": "sriaas",
                "enabled": 1,
                "phone": "9999999999",
                "reason": "Do Not Follow Up",
            }
        )
        do_not_follow_up.insert(ignore_permissions=True)

        result = fresh_followup.queue_agent_call(workflow.name, 1)
        workflow.reload()

        self.assertEqual(result["status"], "no_follow_up")
        self.assertEqual(workflow.status, "No Follow Up")
        self.assertEqual(workflow.agents[0].status, "Skipped")
        self.assertIn("No follow-up calls allowed", workflow.final_reason)

    def _ensure_agent(self, label: str) -> str:
        existing = frappe.db.get_value("AI Agent", {"agent_name": label}, "name")
        if existing:
            return existing
        agent = frappe.new_doc("AI Agent")
        agent.agent_name = label
        agent.company = "sriaas"
        agent.enabled = 1
        agent.system_prompt = "Unit fresh follow-up test agent."
        agent.agent_type = "Single-Stage"
        agent.insert(ignore_permissions=True)
        return agent.name

    def _create_agent_1_task(self, agent: str):
        template = self._ensure_template()
        batch = frappe.new_doc("AI Task Batch")
        batch.update(
            {
                "company": "sriaas",
                "status": "Queued",
                "source_system": "AI Fresh Follow Up",
                "batch_label": "unit fresh follow-up",
                "idempotency_key": f"unit-fresh-followup-batch-{frappe.generate_hash(length=10)}",
                "task_template": template,
                "target_agent": agent,
            }
        )
        batch.insert(ignore_permissions=True)

        task = frappe.new_doc("AI Task")
        task.update(
            {
                "company": "sriaas",
                "status": "Completed",
                "task_batch": batch.name,
                "task_template": template,
                "target_agent": agent,
                "assigned_agent": agent,
                "channel": "Voice",
                "external_record_type": "AI Fresh Follow Up Workflow",
                "external_record_id": "pending-workflow",
                "duration": 205,
                "transcript": "[CUSTOMER]: Namaste\n[AGENT]: Abhi busy hoon, baad mein call karna.",
                "result_json": frappe.as_json({"duration_ms": 205000}),
            }
        )
        task.insert(ignore_permissions=True)
        return task

    def _ensure_template(self) -> str:
        existing = frappe.db.get_value("AI Task Template", {"template_key": "unit_fresh_followup_voice"}, "name")
        if existing:
            return existing
        template = frappe.new_doc("AI Task Template")
        template.template_key = "unit_fresh_followup_voice"
        template.template_name = "Unit Fresh Follow Up Voice"
        template.objective_prompt = "Unit fresh follow-up voice task."
        template.default_channel = "Voice"
        template.insert(ignore_permissions=True)
        return template.name

    def _create_workflow(self, agent_1: str, agent_2: str, task: str):
        workflow = frappe.new_doc("AI Fresh Follow Up Workflow")
        workflow.update(
            {
                "company": "sriaas",
                "enabled": 1,
                "status": "Queued",
                "idempotency_key": f"unit-fresh-followup-{frappe.generate_hash(length=10)}",
                "customer_phone": "+919999999999",
                "current_agent_no": 1,
                "next_agent_no": 1,
                "minimum_connected_seconds": 10,
                "context_json": frappe.as_json({"phone": "+919999999999"}),
                "task_history_json": "[]",
            }
        )
        workflow.append(
            "agents",
            {
                "enabled": 1,
                "agent_no": 1,
                "agent": agent_1,
                "status": "Queued",
                "task": task,
                "attempt_count": 1,
                "scheduled_at": now_datetime(),
                "max_attempts": 3,
                "retry_after_value": 30,
                "retry_after_unit": "Minutes",
                "followup_timing_mode": "Manual",
                "followup_after_value": 0,
                "followup_after_unit": "Minutes",
            },
        )
        workflow.append(
            "agents",
            {
                "enabled": 1,
                "agent_no": 2,
                "agent": agent_2,
                "status": "Pending",
                "max_attempts": 3,
                "retry_after_value": 30,
                "retry_after_unit": "Minutes",
                "followup_timing_mode": "Manual",
                "followup_after_value": 1,
                "followup_after_unit": "Days",
            },
        )
        workflow.insert(ignore_permissions=True)
        frappe.db.set_value("AI Task", task, "external_record_id", workflow.name)
        fresh_followup._attach_outcome_contract_to_task(task, workflow.name)
        frappe.db.commit()
        return workflow
