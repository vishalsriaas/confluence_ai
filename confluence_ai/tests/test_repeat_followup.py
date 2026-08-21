from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from confluence_ai.services import repeat_followup
from confluence_ai.services.livekit import _voice_metadata_context
from confluence_ai.services.utils import parse_json_object


class TestRepeatFollowUp(unittest.TestCase):
    def setUp(self):
        repeat_followup.ensure_defaults()
        self.settings = frappe.get_single("AI Repeat Follow Up Settings")
        self._settings_snapshot = {
            "enabled": self.settings.enabled,
            "agent_1": self.settings.agent_1,
            "agent_2": self.settings.agent_2,
            "voice_task_template": self.settings.voice_task_template,
            "max_agent_1_attempts": self.settings.max_agent_1_attempts,
            "retry_delay_minutes": self.settings.retry_delay_minutes,
            "voice_call_timeout_minutes": self.settings.voice_call_timeout_minutes,
            "agent_2_delay_days": self.settings.agent_2_delay_days,
            "shipkia_tracking_enabled": self.settings.shipkia_tracking_enabled,
            "shipkia_prefetch_before_call": self.settings.get("shipkia_prefetch_before_call"),
            "shipkia_tracking_api_url": self.settings.shipkia_tracking_api_url,
            "diet_chart_whatsapp_enabled": self.settings.get("diet_chart_whatsapp_enabled"),
            "diet_chart_prefetch_before_call": self.settings.get("diet_chart_prefetch_before_call"),
            "diet_chart_auto_send_before_call": self.settings.get("diet_chart_auto_send_before_call"),
            "diet_chart_dept_field_names": self.settings.get("diet_chart_dept_field_names"),
            "diet_chart_public_file_base_url": self.settings.get("diet_chart_public_file_base_url"),
            "diet_chart_whatsapp_channel_account": self.settings.get("diet_chart_whatsapp_channel_account"),
            "diet_chart_whatsapp_remote_mcp_server": self.settings.get("diet_chart_whatsapp_remote_mcp_server"),
            "diet_chart_whatsapp_method": self.settings.get("diet_chart_whatsapp_method"),
            "phone_field_names": self.settings.phone_field_names,
            "awb_field_names": self.settings.awb_field_names,
            "idempotency_key_field_names": self.settings.idempotency_key_field_names,
        }
        if not self.settings.agent_1:
            self.settings.agent_1 = self._ensure_agent("Radha Repeat Agent Sriaas 1")
        if not self.settings.voice_task_template:
            self.settings.voice_task_template = self._ensure_template()
        self.settings.enabled = 1
        self.settings.max_agent_1_attempts = 3
        self.settings.retry_delay_minutes = 60
        self.settings.voice_call_timeout_minutes = 5
        self.settings.agent_2_delay_days = 7
        self.settings.shipkia_tracking_enabled = 1
        self.settings.shipkia_prefetch_before_call = 0
        self.settings.shipkia_tracking_api_url = "https://shipkia.test/api/track.php"
        self.settings.diet_chart_whatsapp_enabled = 1
        self.settings.diet_chart_prefetch_before_call = 0
        self.settings.diet_chart_auto_send_before_call = 0
        self.settings.diet_chart_dept_field_names = "sr_pe_deptt,patient_encounter.sr_pe_deptt"
        self.settings.diet_chart_public_file_base_url = "https://public.test"
        self.settings.diet_chart_whatsapp_channel_account = "Interakt Test Channel"
        self.settings.diet_chart_whatsapp_remote_mcp_server = ""
        self.settings.diet_chart_whatsapp_method = "wa_chat_hub.api.runtime.send_reply"
        self.settings.phone_field_names = "phone,mobile,patient_encounter.sr_pe_mobile"
        self.settings.awb_field_names = "awb_number,patient_encounter.pe_shipkia_awb_number"
        self.settings.idempotency_key_field_names = "idempotency_key,patient_encounter.name"
        self.settings.save(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        frappe.db.delete("AI Repeat Follow Up Workflow", {"workflow_type": ["!=", "Scenario Config"]})
        frappe.db.delete("AI Repeat Follow Up Workflow", {"scenario_key": "medicine_delivery_config"})
        frappe.db.delete("AI Call Log", {"event_type": "Unit Test Repeat Follow Up"})
        frappe.db.delete("AI Task", {"external_record_type": "AI Repeat Follow Up Workflow"})
        frappe.db.delete("AI Task Batch", {"source_system": "AI Repeat Follow Up"})
        if getattr(self, "_settings_snapshot", None):
            settings = frappe.get_single("AI Repeat Follow Up Settings")
            for fieldname, value in self._settings_snapshot.items():
                settings.set(fieldname, value)
            settings.save(ignore_permissions=True)
        frappe.db.delete("AI Knowledge Document", {"title": ["like", "Test RFU Diet Chart%"]})
        frappe.db.delete("AI Channel Account", {"account_name": ["like", "Test RFU Channel%"]})
        frappe.db.commit()

    def _ensure_agent(self, label: str) -> str:
        existing = frappe.db.get_value("AI Agent", {"agent_name": label}, "name")
        if existing:
            return existing
        agent = frappe.new_doc("AI Agent")
        agent.agent_name = label
        agent.company = "sriaas"
        agent.enabled = 1
        agent.system_prompt = "Repeat follow-up test agent."
        agent.agent_type = "Single-Stage"
        agent.insert(ignore_permissions=True)
        return agent.name

    def _ensure_template(self) -> str:
        existing = frappe.db.get_value("AI Task Template", {"template_key": "repeat_followup_voice_test"}, "name")
        if existing:
            return existing
        tmpl = frappe.new_doc("AI Task Template")
        tmpl.template_key = "repeat_followup_voice_test"
        tmpl.template_name = "Repeat Follow Up Voice Test"
        tmpl.objective_prompt = "Test repeat follow-up."
        tmpl.default_channel = "Voice"
        tmpl.insert(ignore_permissions=True)
        return tmpl.name

    def _ensure_channel(self) -> str:
        existing = frappe.db.get_value("AI Channel Account", {"account_name": "Test RFU Channel"}, "name")
        if existing:
            return existing
        channel = frappe.new_doc("AI Channel Account")
        channel.account_name = "Test RFU Channel"
        channel.company = "sriaas"
        channel.channel_type = "LiveKit"
        channel.enabled = 1
        channel.insert(ignore_permissions=True)
        return channel.name

    def _payload(self, idempotency_key: str = "rfu-test-1") -> dict:
        return {
            "event": "medicine_followup",
            "idempotency_key": idempotency_key,
            "patient_encounter": {
                "name": "PE-0001",
                "patient_name": "Ramesh",
                "sr_pe_mobile": "9876543210",
                "pe_shipkia_awb_number": "AWB123",
                "pe_shipkia_order_id": "ORD123",
                "sr_pe_deptt": "Liver",
                "sr_notes": "Full encounter details should stay available.",
                "nested": {"long": "x" * 3000},
            },
        }

    def _payload_with_medicines(self, idempotency_key: str = "rfu-test-meds", count: int = 12) -> dict:
        payload = self._payload(idempotency_key)
        payload["patient_encounter"]["drug_prescription"] = [
            {
                "idx": index,
                "drug_name": f"MED-{index}",
                "sr_medication_name_print": f"Medicine {index}",
                "dosage": "1-0-1",
                "dosage_form": "Tablet",
                "period": "30 Day",
                "sr_drug_instruction": "After Food",
            }
            for index in range(1, count + 1)
        ]
        return payload

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_start_stores_full_encounter_but_task_context_is_compact(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload())

        self.assertEqual(result["status"], "queued")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        encounter = parse_json_object(workflow.encounter_json)
        self.assertEqual(encounter["nested"]["long"], "x" * 3000)
        self.assertEqual(workflow.retry_delay_minutes, 60)
        self.assertEqual(workflow.voice_call_timeout_minutes, 5)
        self.assertEqual(workflow.agent_2_delay_days, 7)
        self.assertEqual(workflow.shipkia_tracking_api_url, "https://shipkia.test/api/track.php")
        self.assertEqual(workflow.outbound_phone_number, "+919876543210")
        self.assertIn("get_repeat_encounter_full_data", workflow.mcp_tools_enabled)
        self.assertIn("get_repeat_medicine_list", workflow.mcp_tools_enabled)
        self.assertIn("verify_repeat_medicine_in_prescription", workflow.mcp_tools_enabled)
        self.assertIn("get_shipkia_tracking_status", workflow.mcp_tools_enabled)
        self.assertIn("log_repeat_followup_outcome", workflow.mcp_tools_enabled)
        self.assertNotIn("send_repeat_diet_chart_whatsapp", workflow.mcp_tools_enabled)
        self.assertEqual(parse_json_object(workflow.mcp_tools_used_json), {"events": []})

        task = frappe.get_doc("AI Task", result["task"])
        context = parse_json_object(task.context_json)
        self.assertEqual(context["repeat_followup_compacted"], 1)
        self.assertEqual(context["full_encounter_available_via_tool"], 1)
        self.assertEqual(context["simple_followup_mode"], 1)
        self.assertEqual(context["state_machine_required"], 0)
        self.assertEqual(context["active_stage_id"], "SIMPLE_FOLLOWUP")
        self.assertEqual(context["stage_prompt_loading_required"], 0)
        self.assertEqual(context["current_step_key"], "opening")
        self.assertNotIn("nested", context)
        self.assertEqual(context["awb_number"], "AWB123")
        self.assertEqual(context["patient_department"], "Liver")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_payload_can_override_per_workflow_config(self, _enqueue):
        payload = self._payload("scenario-key-1")
        payload["workflow_config"] = {
            "scenario_key": "medicine_delivery_day_7",
            "retry_delay_minutes": 15,
            "voice_call_timeout_minutes": 3,
            "agent_2_delay_days": 10,
            "shipkia_tracking_api_url": "https://shipkia.scenario/api/track.php",
            "awb_field_names": "custom_awb,patient_encounter.pe_shipkia_awb_number",
        }

        result = repeat_followup.start_from_event(payload)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        task = frappe.get_doc("AI Task", result["task"])
        context = parse_json_object(task.context_json)

        self.assertEqual(workflow.scenario_key, "medicine_delivery_day_7")
        self.assertEqual(workflow.retry_delay_minutes, 15)
        self.assertEqual(workflow.voice_call_timeout_minutes, 5)
        self.assertEqual(workflow.agent_2_delay_days, 10)
        self.assertEqual(workflow.shipkia_tracking_api_url, "https://shipkia.scenario/api/track.php")
        self.assertEqual(workflow.awb_field_names, "custom_awb,patient_encounter.pe_shipkia_awb_number")
        self.assertEqual(context["scenario_key"], "medicine_delivery_day_7")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_scenario_config_workflow_is_used_before_payload_overrides(self, _enqueue):
        config = frappe.new_doc("AI Repeat Follow Up Workflow")
        config.update(
            {
                "workflow_type": "Scenario Config",
                "enabled": 1,
                "status": "Draft",
                "company": "sriaas",
                "scenario_key": "medicine_delivery_config",
                "agent_1": self.settings.agent_1,
                "voice_task_template": self.settings.voice_task_template,
                "voice_channel_account": self._ensure_channel(),
                "retry_delay_minutes": 22,
                "voice_call_timeout_minutes": 4,
                "agent_2_delay_days": 9,
                "max_retry_count": 2,
                "diet_chart_whatsapp_template_name": "approved_diet_pdf_template",
                "diet_chart_whatsapp_send_strategy": "Template Document",
            }
        )
        config.insert(ignore_permissions=True)
        frappe.db.commit()

        payload = self._payload("scenario-config-key")
        payload["workflow_config"] = {
            "scenario_key": "medicine_delivery_config",
            "retry_delay_minutes": 33,
        }

        result = repeat_followup.start_from_event(payload)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(workflow.workflow_type, "Call Instance")
        self.assertEqual(workflow.scenario_key, "medicine_delivery_config")
        self.assertEqual(workflow.voice_channel_account, config.voice_channel_account)
        self.assertEqual(workflow.voice_channel_source, "Scenario Config")
        self.assertEqual(workflow.retry_delay_minutes, 33)
        self.assertEqual(workflow.voice_call_timeout_minutes, 4)
        self.assertEqual(workflow.agent_2_delay_days, 9)
        self.assertEqual(workflow.max_retry_count, 2)
        self.assertEqual(workflow.diet_chart_whatsapp_template_name, "approved_diet_pdf_template")
        self.assertEqual(workflow.diet_chart_whatsapp_send_strategy, "Template Document")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_payload_cannot_reduce_scenario_voice_timeout(self, _enqueue):
        config = frappe.new_doc("AI Repeat Follow Up Workflow")
        config.update(
            {
                "workflow_type": "Scenario Config",
                "enabled": 1,
                "status": "Draft",
                "company": "sriaas",
                "scenario_key": "medicine_delivery_config",
                "agent_1": self.settings.agent_1,
                "voice_task_template": self.settings.voice_task_template,
                "voice_call_timeout_minutes": 15,
            }
        )
        config.insert(ignore_permissions=True)
        frappe.db.commit()

        payload = self._payload("scenario-timeout-floor")
        payload["workflow_config"] = {
            "scenario_key": "medicine_delivery_config",
            "voice_call_timeout_minutes": 5,
        }

        result = repeat_followup.start_from_event(payload)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(workflow.voice_call_timeout_minutes, 15)

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_raw_n8n_webhook_body_encounter_shape_is_accepted(self, _enqueue):
        payload = {
            "headers": {"host": "n8n-ai.buopso.net"},
            "body": {
                "schema_version": 1,
                "source": {"name": "HLC-ENC-2026-1036299"},
                "encounter": self._payload("n8n-shape")["patient_encounter"],
                "workflow_config": {"scenario_key": "from_n8n_body", "voice_channel_account": "channel-146"},
            },
            "executionMode": "test",
        }

        result = repeat_followup.start_from_event(payload)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(result["status"], "queued")
        self.assertEqual(workflow.patient_encounter, "PE-0001")
        self.assertEqual(workflow.outbound_phone_number, "+919876543210")
        self.assertEqual(workflow.awb_number, "AWB123")
        self.assertEqual(workflow.scenario_key, "from_n8n_body")
        self.assertEqual(workflow.voice_channel_account, "channel-146")
        self.assertEqual(workflow.voice_channel_source, "workflow_config")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_n8n_array_wrapper_and_all_medicines_are_preserved(self, _enqueue):
        payload = self._payload_with_medicines("n8n-array-many-meds", count=12)
        wrapped_payload = [{"headers": {"host": "n8n-ai.buopso.net"}, "body": {"encounter": payload["patient_encounter"]}}]

        result = repeat_followup.start_from_event(wrapped_payload)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        task = frappe.get_doc("AI Task", result["task"])
        medicine_summary = parse_json_object(workflow.medicine_summary_json)
        context = parse_json_object(task.context_json)

        self.assertEqual(medicine_summary["medicine_count"], 12)
        self.assertEqual(len(medicine_summary["drug_prescription"]), 12)
        self.assertIn("Medicine 12", medicine_summary["required_medicine_script"])
        self.assertEqual(context["medicine_summary"]["medicine_count"], 12)
        self.assertIn("Medicine 12", context["required_medicine_script"])
        livekit_context = _voice_metadata_context(context)
        self.assertEqual(livekit_context["active_stage_id"], "SIMPLE_FOLLOWUP")
        self.assertEqual(livekit_context["simple_followup_mode"], 1)
        self.assertIn("stage_sequence", livekit_context)
        self.assertIn("required_order_script", livekit_context)
        self.assertIn("medicine_summary", livekit_context)
        self.assertIn("required_medicine_script", livekit_context)
        self.assertIn("required_diet_script", livekit_context)
        self.assertIn("simple_followup_script", livekit_context)
        self.assertIn("Medicine 12", livekit_context["required_medicine_script"])
        self.assertNotIn("strict_followup_script", livekit_context)
        self.assertNotIn("current_speech_unit", livekit_context)
        self.assertNotIn("state_machine_tools", livekit_context)

    def test_agent_1_defaults_are_multistage_with_split_prompts(self):
        agent_name = repeat_followup._ensure_agent_1()
        agent = frappe.get_doc("AI Agent", agent_name)

        self.assertEqual(agent.agent_type, "Multi-Stage State Machine")
        self.assertIn("RADHA_REPEAT_AGENT1_MULTISTAGE_V4", agent.system_prompt)
        stage_ids = [row.stage_id for row in agent.get("stage_prompts")]
        self.assertEqual(stage_ids, ["ORDER_STATUS", "MEDICINE_EXPLANATION", "DIET_EXPLANATION", "OUTCOME_CLOSE"])
        self.assertIn("Do not explain medicines yet", agent.stage_prompts[0].system_prompt)
        self.assertIn("{{ required_medicine_script }}", agent.stage_prompts[1].system_prompt)
        self.assertIn("{{ required_diet_script }}", agent.stage_prompts[2].system_prompt)

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_medicine_list_and_verify_tools_use_current_prescription(self, _enqueue):
        payload = self._payload_with_medicines("medicine-verify", count=2)
        payload["patient_encounter"]["drug_prescription"][0]["sr_medication_name_print"] = "Neuro M Oil"
        payload["patient_encounter"]["drug_prescription"][0]["drug_name"] = "NEUROMOIL"
        payload["patient_encounter"]["drug_prescription"][0]["medication"] = "Neuro M Oil"
        payload["patient_encounter"]["drug_prescription"][0]["dosage"] = "1 unit"
        payload["patient_encounter"]["drug_prescription"][0]["dosage_form"] = "Oil"
        payload["patient_encounter"]["drug_prescription"][0]["sr_drug_instruction"] = "APPLY ON AFFECTED AREA"

        result = repeat_followup.start_from_event(payload)
        medicines = repeat_followup.get_repeat_medicine_list({}, task_id=result["task"])
        verified = repeat_followup.verify_repeat_medicine_in_prescription({"medicine_name": "Neuro M Oil"}, task_id=result["task"])

        self.assertEqual(medicines["status"], "success")
        self.assertEqual(medicines["medicine_count"], 2)
        self.assertIn("Neuro M Oil", medicines["medicine_names"])
        self.assertEqual(verified["status"], "found")
        self.assertEqual(verified["medicine"]["display_name"], "Neuro M Oil")
        self.assertIn("APPLY ON AFFECTED AREA", verified["customer_safe_answer"])

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_agent_1_state_machine_generates_one_required_step_per_medicine(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload_with_medicines("state-machine-meds", count=6))
        state = repeat_followup.get_repeat_workflow_state({}, task_id=result["task"])
        steps = state["call"]
        current = state["current_step"]

        self.assertEqual(state["status"], "success")
        self.assertEqual(state["journey"]["active_stage_key"], "AGENT_1")
        self.assertEqual(current["step_key"], "opening")
        self.assertEqual(steps["total_steps"], 16)

        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        machine = parse_json_object(workflow.context_json)["repeat_state_machine"]
        medicine_steps = [s for s in machine["steps"] if s["step_key"].startswith("medicine_item_")]
        self.assertEqual(len(medicine_steps), 6)
        self.assertEqual([s["variables"]["medicine_index"] for s in medicine_steps], [1, 2, 3, 4, 5, 6])
        self.assertFalse(any(s.get("can_skip") for s in medicine_steps))
        self.assertEqual(len(workflow.journey_stages), 4)
        self.assertEqual(workflow.journey_stages[0].stage_key, "AGENT_1")
        self.assertEqual(len(workflow.step_runs), 16)
        self.assertEqual(
            [row.step_key for row in workflow.step_runs if row.step_key.startswith("medicine_item_")],
            [f"medicine_item_{index}" for index in range(1, 7)],
        )

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_state_machine_blocks_jump_and_resumes_interrupted_medicine(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload_with_medicines("state-machine-no-jump", count=2))

        self.assertEqual(repeat_followup.get_current_speech_unit({}, task_id=result["task"])["step_key"], "opening")
        repeat_followup.mark_repeat_step_complete({"step_key": "opening"}, task_id=result["task"])
        repeat_followup.mark_repeat_step_complete({"step_key": "delivery_check", "structured_details": {"order_received": True}}, task_id=result["task"])
        self.assertEqual(repeat_followup.get_current_speech_unit({}, task_id=result["task"])["step_key"], "medicine_intro")
        repeat_followup.mark_repeat_step_complete({"step_key": "medicine_intro"}, task_id=result["task"])

        current = repeat_followup.get_current_speech_unit({}, task_id=result["task"])
        self.assertEqual(current["step_key"], "medicine_item_1")
        blocked = repeat_followup.mark_repeat_step_complete({"step_key": "medicine_item_2"}, task_id=result["task"])
        self.assertEqual(blocked["status"], "blocked_out_of_order")
        self.assertEqual(blocked["active_step"], "medicine_item_1")

        interrupted = repeat_followup.mark_repeat_step_interrupted({"patient_text": "diet batao"}, task_id=result["task"])
        self.assertEqual(interrupted["status"], "resume_required")
        resumed = repeat_followup.resume_repeat_pending_step({}, task_id=result["task"])
        self.assertEqual(resumed["current_step"]["step_key"], "medicine_item_1")

        incomplete = repeat_followup.mark_repeat_step_complete({"step_key": "medicine_item_1"}, task_id=result["task"])
        self.assertEqual(incomplete["status"], "blocked_incomplete_step")

        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        machine_context = parse_json_object(workflow.context_json)
        machine = machine_context["repeat_state_machine"]
        for step in machine["steps"]:
            if step["step_key"] == "medicine_item_1":
                step["started_at"] = str(add_to_date(now_datetime(), seconds=-30))
                break
        workflow.context_json = json.dumps(machine_context)
        workflow.save(ignore_permissions=True)
        frappe.db.commit()

        repeat_followup.mark_repeat_step_complete(
            {
                "step_key": "medicine_item_1",
                "structured_details": {
                    "medicine_name": "Medicine 1",
                    "spoken_text": "Pehli medicine Medicine 1 hai. Iski prescribed dose 1-0-1 hai, subah aur shaam khaane ke baad leni hai, aur isko 30 Day tak follow karna hai.",
                    "medicine_name_spoken": True,
                    "dose_spoken": True,
                    "timing_or_instruction_spoken": True,
                    "period_spoken": True,
                },
            },
            task_id=result["task"],
        )
        self.assertEqual(repeat_followup.get_current_speech_unit({}, task_id=result["task"])["step_key"], "medicine_item_2")

    @patch("confluence_ai.services.repeat_followup.requests.post")
    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_shipkia_prefetch_adds_tracking_summary_to_voice_context(self, _enqueue, post):
        self.settings.shipkia_prefetch_before_call = 1
        self.settings.save(ignore_permissions=True)
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = "{}"
        response.json.return_value = {
            "success": True,
            "result": {
                "tracking_id": "AWB123",
                "status": "In Transit",
                "estimated_delivery": "2026-08-02",
                "order_details": {"awb_number": "AWB123", "delivery_partner": "Delhivery", "delivery_location": "Raisen"},
                "shipment_timeline": [{"status": "Pickup Scheduled", "detail": "Manifest uploaded", "location": "Gurgaon", "date_time": "2026-07-28"}],
            },
        }
        post.return_value = response

        result = repeat_followup.start_from_event(self._payload("prefetch-key"))
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        task = frappe.get_doc("AI Task", result["task"])
        context = parse_json_object(task.context_json)

        self.assertEqual(parse_json_object(workflow.shipkia_result_json)["shipment_status"], "In Transit")
        self.assertEqual(context["tracking_summary"]["delivery_partner"], "Delhivery")
        self.assertEqual(context["tracking_summary"]["latest_detail"], "Manifest uploaded")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_duplicate_idempotency_is_reused(self, _enqueue):
        first = repeat_followup.start_from_event(self._payload("same-key"))
        second = repeat_followup.start_from_event(self._payload("same-key"))

        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["workflow"], first["workflow"])

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_full_encounter_tool_is_task_scoped(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload())
        data = repeat_followup.get_repeat_encounter_full_data({}, task_id=result["task"])

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["encounter"]["name"], "PE-0001")
        self.assertEqual(data["encounter"]["nested"]["long"], "x" * 3000)
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        usage = parse_json_object(workflow.mcp_tools_used_json)
        self.assertEqual(usage["events"][-1]["tool"], "get_repeat_encounter_full_data")

    @patch("confluence_ai.services.repeat_followup.requests.post")
    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_shipkia_uses_settings_url_and_compacts_result(self, _enqueue, post):
        result = repeat_followup.start_from_event(self._payload())
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = "{}"
        response.json.return_value = {
            "success": True,
            "result": {
                "tracking_id": "AWB123",
                "status": "In Transit",
                "estimated_delivery": "2026-08-01",
                "order_details": {
                    "awb_number": "AWB123",
                    "delivery_partner": "Shipkia",
                    "delivery_location": "Delhi",
                },
                "shipment_timeline": [
                    {"status": "Picked Up", "detail": "Shipment picked", "location": "Delhi", "date_time": "2026-07-29 10:00:00"},
                    {"status": "In Transit", "detail": "Reached hub", "location": "Jaipur", "date_time": "2026-07-29 18:00:00"},
                ],
            },
        }
        post.return_value = response

        self.settings.shipkia_tracking_api_url = "https://shipkia.changed-after-start/api/track.php"
        self.settings.save(ignore_permissions=True)

        tracking = repeat_followup.get_shipkia_tracking_status({}, task_id=result["task"])

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://shipkia.test/api/track.php")
        self.assertEqual(tracking["status"], "success")
        self.assertEqual(tracking["latest_location"], "Jaipur")
        self.assertEqual(tracking["estimated_delivery"], "2026-08-01")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        usage = parse_json_object(workflow.mcp_tools_used_json)
        self.assertEqual(usage["events"][-1]["tool"], "get_shipkia_tracking_status")

    @patch("confluence_ai.services.repeat_followup._send_whatsapp_document")
    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_diet_chart_whatsapp_matches_department_from_encounter(self, _enqueue, send_doc):
        doc = frappe.new_doc("AI Knowledge Document")
        doc.update(
            {
                "enabled": 1,
                "company": "sriaas",
                "title": f"Test RFU Diet Chart Liver {frappe.generate_hash(length=6)}",
                "status": "Published",
                "source_type": "Manual",
                "content": "Liver diet guidance for Radha to explain.",
                "repeat_followup_document_type": "Diet Chart",
                "department_match_values": "Liver,Liver Department",
                "customer_attachment_url": "https://example.com/liver-diet.pdf",
                "whatsapp_caption": "Yeh aapka liver diet chart hai.",
            }
        )
        doc.insert(ignore_permissions=True)
        send_doc.return_value = {"delivery_status": "Sent", "message": "mocked"}

        result = repeat_followup.start_from_event(self._payload("diet-chart-key"))
        sent = repeat_followup.send_repeat_diet_chart_whatsapp({}, task_id=result["task"])

        self.assertEqual(sent["status"], "success")
        self.assertEqual(sent["department"], "Liver")
        self.assertEqual(sent["knowledge_document"], doc.name)
        send_doc.assert_called_once()
        call_kwargs = send_doc.call_args.kwargs
        self.assertEqual(call_kwargs["channel_account"], "Interakt Test Channel")
        self.assertEqual(call_kwargs["media_url"], "https://example.com/liver-diet.pdf")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        self.assertEqual(workflow.diet_chart_dept, "Liver")
        self.assertEqual(workflow.diet_chart_knowledge_document, doc.name)
        usage = parse_json_object(workflow.mcp_tools_used_json)
        self.assertEqual(usage["events"][-1]["tool"], "send_repeat_diet_chart_whatsapp")

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_diet_chart_prefetch_adds_summary_without_livekit_tool(self, _enqueue):
        self.settings.diet_chart_prefetch_before_call = 1
        self.settings.diet_chart_auto_send_before_call = 0
        self.settings.save(ignore_permissions=True)
        doc = frappe.new_doc("AI Knowledge Document")
        doc.update(
            {
                "enabled": 1,
                "company": "sriaas",
                "title": f"Test RFU Diet Chart Prefetch {frappe.generate_hash(length=6)}",
                "status": "Published",
                "source_type": "Manual",
                "content": "Liver diet guidance for Radha to explain.",
                "repeat_followup_document_type": "Diet Chart",
                "department_match_values": "Liver",
                "customer_pdf_file": "/files/liver-diet.pdf",
            }
        )
        doc.insert(ignore_permissions=True)

        result = repeat_followup.start_from_event(self._payload("diet-prefetch-key"))
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        task = frappe.get_doc("AI Task", result["task"])
        task_context = parse_json_object(task.context_json)
        summary = parse_json_object(workflow.diet_chart_summary_json)

        self.assertEqual(workflow.diet_chart_knowledge_document, doc.name)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["department"], "Liver")
        self.assertEqual(summary["pdf_url"], "https://public.test/files/liver-diet.pdf")
        self.assertEqual(task_context["diet_chart_summary"]["knowledge_document"], doc.name)
        self.assertFalse(workflow.diet_chart_whatsapp_result_json)

    @patch("confluence_ai.services.repeat_followup._send_whatsapp_document")
    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_diet_chart_auto_send_before_call_is_ignored_for_agent_1_explain_only(self, _enqueue, send_doc):
        self.settings.diet_chart_prefetch_before_call = 1
        self.settings.diet_chart_auto_send_before_call = 1
        self.settings.save(ignore_permissions=True)
        doc = frappe.new_doc("AI Knowledge Document")
        doc.update(
            {
                "enabled": 1,
                "company": "sriaas",
                "title": f"Test RFU Diet Chart Auto Send {frappe.generate_hash(length=6)}",
                "status": "Published",
                "source_type": "Manual",
                "content": "Liver diet guidance for Radha to explain.",
                "repeat_followup_document_type": "Diet Chart",
                "department_match_values": "Liver",
                "customer_attachment_url": "https://example.com/liver-auto.pdf",
            }
        )
        doc.insert(ignore_permissions=True)
        send_doc.return_value = {"delivery_status": "Sent", "message": "mocked"}

        result = repeat_followup.start_from_event(self._payload("diet-auto-send-key"))
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        send_doc.assert_not_called()
        summary = parse_json_object(workflow.diet_chart_summary_json)
        self.assertEqual(summary["status"], "ready")
        self.assertFalse(workflow.diet_chart_whatsapp_result_json)

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_diet_chart_content_is_forced_into_agent_1_context(self, _enqueue):
        self.settings.diet_chart_prefetch_before_call = 0
        self.settings.save(ignore_permissions=True)
        doc = frappe.new_doc("AI Knowledge Document")
        doc.update(
            {
                "enabled": 1,
                "company": "sriaas",
                "title": f"Test RFU Diet Chart Context {frappe.generate_hash(length=6)}",
                "status": "Published",
                "source_type": "Manual",
                "content": "Allowed: brown rice, oats, moong. Avoid: fast food, alcohol, sweets.",
                "repeat_followup_document_type": "Diet Chart",
                "department_match_values": "Liver",
                "customer_attachment_url": "https://example.com/liver-context.pdf",
            }
        )
        doc.insert(ignore_permissions=True)

        result = repeat_followup.start_from_event(self._payload("diet-context-key"))
        task = frappe.get_doc("AI Task", result["task"])
        context = parse_json_object(task.context_json)

        self.assertIn("brown rice", context["required_diet_script"].lower())
        self.assertIn("fast food", context["required_diet_script"].lower())
        self.assertEqual(context["diet_chart_summary"]["knowledge_document"], doc.name)
        machine = context["repeat_state_machine"]
        diet_steps = [step for step in machine["steps"] if step["step_key"] == "diet_explanation"]
        self.assertEqual(len(diet_steps), 1)
        self.assertIn("brown rice", diet_steps[0]["speech_unit"].lower())
        self.assertIn("fast food", diet_steps[0]["speech_unit"].lower())

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_outcome_logging_schedules_agent_2_delay_when_configured(self, _enqueue):
        agent_2 = self._ensure_agent("Radha Repeat Agent Sriaas 2")
        self.settings.agent_2 = agent_2
        self.settings.agent_2_delay_days = 7
        self.settings.save(ignore_permissions=True)
        result = repeat_followup.start_from_event(self._payload())
        repeat_followup.mark_repeat_step_complete({"step_key": "opening"}, task_id=result["task"])
        repeat_followup.mark_repeat_step_complete({"step_key": "delivery_check", "structured_details": {"order_received": True}}, task_id=result["task"])
        repeat_followup.mark_repeat_step_complete({"step_key": "medicine_data_missing"}, task_id=result["task"])
        repeat_followup.mark_repeat_step_complete({"step_key": "diet_explanation"}, task_id=result["task"])
        repeat_followup.mark_repeat_step_complete({"step_key": "whatsapp_diet_chart"}, task_id=result["task"])

        logged = repeat_followup.log_repeat_followup_outcome(
            {
                "primary_outcome": "medicine_received",
                "customer_summary": "Customer received medicine.",
                "next_action": "Agent 2 after 7 days",
                "structured_details": {"medicine_received": True},
            },
            task_id=result["task"],
        )

        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        self.assertEqual(logged["status"], "success")
        self.assertEqual(workflow.status, "Agent 2 Scheduled")
        self.assertIsNotNone(workflow.agent_2_scheduled_at)

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_outcome_logging_can_complete_without_state_tool_steps(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload_with_medicines("premature-outcome-block", count=2))

        logged = repeat_followup.log_repeat_followup_outcome(
            {
                "primary_outcome": "medicine_received",
                "customer_summary": "Voice worker logged outcome without state-step tool calls.",
                "structured_details": {"medicine_received": True},
            },
            task_id=result["task"],
        )

        self.assertEqual(logged["status"], "success")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        self.assertIn(workflow.status, {"Agent 2 Scheduled", "Agent 2 Pending Config"})

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_voice_result_does_not_schedule_agent_2_when_agent_1_steps_incomplete(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload_with_medicines("incomplete-call-result", count=2))

        handled = repeat_followup.handle_voice_result(task=result["task"], outcome="completed", notes="Customer disconnected early.")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(handled["status"], "unclear_logged")
        self.assertIn(workflow.status, {"Agent 2 Scheduled", "Agent 2 Pending Config"})

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_late_call_log_transcript_syncs_after_agent_2_is_scheduled(self, _enqueue):
        agent_2 = self._ensure_agent("Radha Repeat Agent Sriaas 2")
        self.settings.agent_2 = agent_2
        self.settings.save(ignore_permissions=True)
        result = repeat_followup.start_from_event(self._payload("late-transcript-sync"))

        first = repeat_followup.handle_voice_result(task=result["task"], outcome="completed")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])
        self.assertEqual(first["status"], "unclear_logged")
        self.assertEqual(workflow.status, "Agent 2 Scheduled")
        self.assertFalse(workflow.call_log)
        self.assertFalse(workflow.customer_summary)

        call_log = frappe.new_doc("AI Call Log")
        call_log.update(
            {
                "company": "sriaas",
                "status": "Completed",
                "provider": "LiveKit",
                "event_type": "Unit Test Repeat Follow Up",
                "task": result["task"],
                "transcript_summary": "Customer confirmed medicine was received and asked about dosage.",
                "transcript": "[AGENT]: Medicine package receive ho gaya?\n[CUSTOMER]: Haan ji, receive ho gaya.",
            }
        )
        call_log.insert(ignore_permissions=True)

        synced = repeat_followup.handle_voice_result(task=result["task"], outcome="completed")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(synced["status"], "synced_transcript")
        self.assertEqual(workflow.call_log, call_log.name)
        self.assertIn("medicine was received", workflow.customer_summary)

        workflow.agent_2_scheduled_at = now_datetime()
        workflow.save(ignore_permissions=True)
        queued = repeat_followup.queue_agent_2_call(workflow.name)
        self.assertEqual(queued["status"], "queued")
        agent_2_task = frappe.get_doc("AI Task", queued["task"])
        agent_2_context = parse_json_object(agent_2_task.context_json)
        self.assertEqual(agent_2_context["previous_call_log"], call_log.name)
        self.assertIn("receive ho gaya", agent_2_context["previous_call_transcript"])

    @patch("confluence_ai.services.repeat_followup.enqueue_task_execution")
    def test_missed_call_uses_configured_retry_delay_and_max_attempts(self, _enqueue):
        result = repeat_followup.start_from_event(self._payload())
        first = repeat_followup.mark_call_missed(result["workflow"], "No answer")
        workflow = frappe.get_doc("AI Repeat Follow Up Workflow", result["workflow"])

        self.assertEqual(first["status"], "retry_queued")
        self.assertEqual(workflow.status, "Retry Queued")
        self.assertIsNotNone(workflow.next_call_time)

        workflow.retry_count = 3
        workflow.save(ignore_permissions=True)
        final = repeat_followup.mark_call_missed(workflow.name, "No answer again")

        self.assertEqual(final["status"], "missed_after_retries")
        self.assertEqual(frappe.db.get_value("AI Repeat Follow Up Workflow", workflow.name, "status"), "Missed After Retries")


if __name__ == "__main__":
    unittest.main()
