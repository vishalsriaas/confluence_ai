from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import frappe
import requests
from frappe.utils import add_to_date, get_datetime, get_url, now_datetime
from frappe.utils.synchronization import filelock

from confluence_ai.services.dispatcher import enqueue_task_execution, refresh_batch_counts
from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event

WORKFLOW = "AI Repeat Follow Up Workflow"
SETTINGS = "AI Repeat Follow Up Settings"

FINAL_STATES = {"Agent 2 Scheduled", "Agent 2 Queued", "Agent 2 Pending Config", "Missed After Retries", "Failed", "Cancelled"}
MISSED_OUTCOMES = {"missed", "no_answer", "no answer", "busy", "failed", "timeout", "cancelled", "canceled"}

DEFAULT_AGENT_1_NAME = "Radha Repeat Agent Sriaas 1"
DEFAULT_AGENT_2_NAME = "Radha Repeat Agent Sriaas 2"
DEFAULT_SHIPKIA_URL = "https://shipkia.com/api/track.php"
DEFAULT_KB_TITLE = "Radha Repeat Agent Sriaas 1 Follow Up Guide"
REPEAT_MCP_TOOL_NAMES = (
    "get_repeat_workflow_state",
    "get_current_required_step",
    "get_current_speech_unit",
    "mark_repeat_step_complete",
    "mark_repeat_step_interrupted",
    "resume_repeat_pending_step",
    "get_repeat_encounter_full_data",
    "get_repeat_medicine_list",
    "verify_repeat_medicine_in_prescription",
    "get_shipkia_tracking_status",
    "send_mapped_whatsapp_template",
    "trigger_repeat_renewal_n8n",
    "log_repeat_followup_outcome",
)
STATE_MACHINE_CONTEXT_KEY = "repeat_state_machine"
RADHA_RUNTIME_VERSION = "radha_repeat_multicall_state_machine_v1"
WORKFLOW_CONFIG_FIELDS = (
    "scenario_key",
    "agent_1",
    "agent_2",
    "livekit_channel_account_fallback",
    "voice_channel_account",
    "voice_task_template",
    "default_knowledge_document",
    "medicine_summary_field_names",
    "max_retry_count",
    "retry_delay_minutes",
    "voice_call_timeout_minutes",
    "agent_2_delay_days",
    "schedule_agent_2_only_after_conversation",
    "shipkia_tracking_enabled",
    "shipkia_prefetch_before_call",
    "shipkia_tracking_api_url",
    "diet_chart_whatsapp_enabled",
    "diet_chart_prefetch_before_call",
    "diet_chart_auto_send_before_call",
    "diet_chart_dept_field_names",
    "diet_chart_public_file_base_url",
    "diet_chart_whatsapp_channel_account",
    "diet_chart_whatsapp_template_map",
    "diet_chart_whatsapp_remote_mcp_server",
    "diet_chart_whatsapp_method",
    "diet_chart_whatsapp_send_strategy",
    "diet_chart_whatsapp_template_name",
    "diet_chart_whatsapp_template_language",
    "diet_chart_whatsapp_template_method",
    "diet_chart_template_header_values_json",
    "diet_chart_template_body_values_json",
    "diet_chart_template_button_values_json",
    "outcome_logging_required",
    "idempotency_key_field_names",
    "phone_field_names",
    "awb_field_names",
)


def ensure_defaults() -> None:
    """Create editable defaults for the simple repeat follow-up flow."""
    if not frappe.db.exists("DocType", SETTINGS):
        return

    template_name = _ensure_task_template()
    agent_name = _ensure_agent_1()
    kb_name = _ensure_knowledge_document(agent_name)
    tool_names = _ensure_tools()
    _attach_agent_tools(agent_name, tool_names)

    settings = frappe.get_single(SETTINGS)
    initializing_diet_chart_defaults = not bool(settings.get("diet_chart_dept_field_names"))
    defaults = {
        "enabled": 1,
        "company": "sriaas",
        "agent_1": agent_name,
        "agent_2": _agent_by_label(DEFAULT_AGENT_2_NAME) or "",
        "voice_task_template": template_name,
        "max_agent_1_attempts": 3,
        "retry_delay_minutes": 60,
        "voice_call_timeout_minutes": 5,
        "agent_2_delay_days": 7,
        "schedule_agent_2_only_after_conversation": 1,
        "shipkia_tracking_enabled": 1,
        "shipkia_prefetch_before_call": 1,
        "shipkia_tracking_api_url": DEFAULT_SHIPKIA_URL,
        "diet_chart_whatsapp_enabled": 1,
        "diet_chart_prefetch_before_call": 1,
        "diet_chart_auto_send_before_call": 0,
        "diet_chart_dept_field_names": "sr_pe_deptt,patient_encounter.sr_pe_deptt,data.patient_encounter.sr_pe_deptt,body.encounter.sr_pe_deptt,encounter.sr_pe_deptt",
        "diet_chart_whatsapp_method": "wa_chat_hub.api.runtime.send_reply",
        "diet_chart_whatsapp_send_strategy": "Free-form then Template",
        "diet_chart_whatsapp_template_language": "en",
        "diet_chart_whatsapp_template_method": "wa_chat_hub.api.runtime.send_template_message",
        "diet_chart_template_header_values_json": '["{media_url}"]',
        "diet_chart_template_body_values_json": '["{patient_name}", "{department}"]',
        "diet_chart_template_button_values_json": "{}",
        "outcome_logging_required": 1,
        "default_knowledge_document": kb_name,
        "medicine_summary_field_names": "drug_prescription,sr_allopathy_drug_prescription,sr_homeopathy_drug_prescription,sr_pe_order_items,sr_medication_template,sr_pe_instruction,sr_pe_disease,sr_diagnosis,sr_complaints",
        "idempotency_key_field_names": "idempotency_key,event_id,data.idempotency_key,data.event_id,patient_encounter.name,data.patient_encounter.name,encounter.name,name",
        "phone_field_names": "phone,mobile,customer_phone,patient_mobile,sr_pe_mobile,data.phone,data.mobile,data.patient_encounter.sr_pe_mobile,patient_encounter.sr_pe_mobile,patient_encounter.mobile",
        "awb_field_names": "awb,awb_number,pe_shipkia_awb_number,data.awb,data.awb_number,data.patient_encounter.pe_shipkia_awb_number,patient_encounter.pe_shipkia_awb_number,patient_encounter.awb_number",
    }
    changed = False
    for fieldname, value in defaults.items():
        if settings.get(fieldname) in (None, ""):
            settings.set(fieldname, value)
            changed = True
    if initializing_diet_chart_defaults and not settings.get("diet_chart_whatsapp_enabled"):
        settings.diet_chart_whatsapp_enabled = 1
        changed = True
    if changed:
        settings.save(ignore_permissions=True)


def start_from_event(payload: dict | list) -> dict:
    payload = _coerce_event_payload(payload)
    settings = _settings()
    if not settings.enabled:
        frappe.throw("AI Repeat Follow Up flow is disabled.")

    start_config = _start_config(settings, payload)
    context = _normalize_payload(payload, start_config)
    if not context.get("phone"):
        frappe.throw("Repeat follow-up payload must include phone or mobile.")
    if not isinstance(context.get("encounter"), dict) or not context["encounter"]:
        frappe.throw("Repeat follow-up payload must include full patient_encounter data.")

    idem_key = _idempotency_key(context, start_config)
    if idem_key:
        existing = frappe.db.exists(WORKFLOW, {"idempotency_key": idem_key})
        if existing:
            return {"status": "duplicate", "workflow": existing, "idempotency_key": idem_key}

    workflow = frappe.new_doc(WORKFLOW)
    workflow.update(
        {
            "status": "Draft",
            "workflow_type": "Call Instance",
            "enabled": 1,
            "company": start_config.company or context.get("company") or "sriaas",
            "idempotency_key": idem_key,
            "patient_encounter": context.get("encounter_id"),
            "patient_name": context.get("patient_name"),
            "patient_mobile": context.get("phone"),
            "outbound_phone_number": context.get("phone"),
            "awb_number": context.get("awb_number"),
            "order_id": context.get("order_id"),
            "voice_channel_account": start_config.voice_channel_account,
            "voice_channel_source": start_config.voice_channel_source,
            "mcp_tools_enabled": ",".join(REPEAT_MCP_TOOL_NAMES),
            "mcp_tools_used_json": as_json({"events": []}),
            "source_payload_json": as_json(payload),
            "encounter_json": as_json(context["encounter"]),
            "medicine_summary_json": as_json(context.get("medicine_summary") or {}),
            "context_json": as_json(_voice_bootstrap_context(workflow=None, context=context)),
        }
    )
    workflow.update(_workflow_config_values(start_config))
    workflow.insert(ignore_permissions=True)
    _prefetch_shipkia_before_call(workflow)
    _prepare_diet_chart_before_call(workflow)
    workflow.reload()
    existing_diet_summary = parse_json_object(workflow.diet_chart_summary_json, "Diet Chart Summary JSON") if workflow.diet_chart_summary_json else {}
    if not existing_diet_summary.get("diet_explanation_script"):
        _resolve_diet_chart_for_workflow(workflow)
        workflow.reload()
    _initialize_agent_1_state_machine(workflow)
    return queue_agent_1_call(workflow.name)


def _coerce_event_payload(payload: dict | list | None) -> dict:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return dict(payload[0])
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def queue_agent_1_call(workflow_name: str) -> dict:
    with _workflow_lock(workflow_name):
        workflow = frappe.get_doc(WORKFLOW, workflow_name)
        settings = _workflow_settings(workflow)
        if workflow.status in FINAL_STATES:
            return {"status": "skipped", "reason": "final_state", "workflow": workflow.name}
        if workflow.voice_task:
            task_status = frappe.db.get_value("AI Task", workflow.voice_task, "status")
            if task_status in {"Queued", "Waiting", "Running"}:
                return {"status": "skipped", "reason": "voice_task_already_active", "workflow": workflow.name, "task": workflow.voice_task}

        agent = workflow.agent_1 or settings.agent_1
        if not agent or not frappe.db.exists("AI Agent", agent):
            frappe.throw("AI Repeat Follow Up Settings must have a valid Agent 1.")

        attempt_number = int(workflow.retry_count or 0) + 1
        max_attempts = int(workflow.max_retry_count or settings.max_agent_1_attempts or 3)
        if attempt_number > max_attempts:
            workflow.status = "Missed After Retries"
            workflow.timer_status = "No attempts remaining"
            workflow.save(ignore_permissions=True)
            frappe.db.commit()
            return {"status": "missed_after_retries", "workflow": workflow.name}

        context = _workflow_context(workflow)
        context.update(_voice_bootstrap_context(workflow=workflow, context=context))
        task_template = settings.voice_task_template or _template_by_key("repeat_followup_voice") or _ensure_task_template()
        trunk_id = _workflow_voice_trunk_id(workflow, settings)

        batch = frappe.new_doc("AI Task Batch")
        batch.update(
            {
                "company": workflow.company,
                "status": "Queued",
                "source_system": "AI Repeat Follow Up",
                "batch_label": workflow.name,
                "idempotency_key": f"{workflow.name}:agent1:{attempt_number}",
                "task_template": task_template,
                "target_agent": agent,
                "priority": "High",
                "source_payload_json": workflow.source_payload_json,
            }
        )
        batch.insert(ignore_permissions=True)

        task = frappe.new_doc("AI Task")
        deadline = add_to_date(now_datetime(), minutes=int(settings.voice_call_timeout_minutes or 5), as_datetime=True)
        task.update(
            {
                "company": workflow.company,
                "status": "Queued",
                "task_batch": batch.name,
                "task_template": task_template,
                "target_agent": agent,
                "assigned_agent": agent,
                "channel": "Voice",
                "priority": "High",
                "deadline": deadline,
                "external_record_id": workflow.name,
                "external_record_type": WORKFLOW,
                "idempotency_key": f"{workflow.name}:agent1:{attempt_number}",
                "trunk_id": trunk_id,
                "context_json": as_json(context),
            }
        )
        task.insert(ignore_permissions=True)

        refresh_batch_counts(batch.name)
        workflow.status = "Call Queued"
        workflow.retry_count = attempt_number
        workflow.voice_task = task.name
        workflow.task_batch = batch.name
        workflow.next_call_time = None
        workflow.active_call_timeout_at = deadline
        workflow.next_scheduled_call_time = None
        workflow.next_scheduled_call_stage = "Agent 1 active"
        workflow.timer_status = f"Agent 1 attempt {attempt_number} queued"
        workflow.context_json = as_json(context)
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        enqueue_task_execution(task.name, "Voice", enqueue_after_commit=False)
        return {"status": "queued", "workflow": workflow.name, "task": task.name, "attempt": attempt_number}


def mark_voice_started(task_name: str, provider_result: dict | None = None) -> dict:
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return {"status": "ignored", "reason": "missing_task"}
    task = frappe.get_doc("AI Task", task_name)
    if task.channel != "Voice" or task.external_record_type != WORKFLOW or not task.external_record_id:
        return {"status": "ignored", "reason": "not_repeat_followup_task"}
    if not frappe.db.exists(WORKFLOW, task.external_record_id):
        return {"status": "ignored", "reason": "missing_workflow"}

    workflow = frappe.get_doc(WORKFLOW, task.external_record_id)
    provider_result = provider_result or {}
    if workflow.status == "Call Queued":
        workflow.status = "Call Running"
    workflow.voice_room_name = provider_result.get("room_name") or workflow.voice_room_name
    workflow.voice_room_sid = provider_result.get("room_sid") or workflow.voice_room_sid
    workflow.sip_call_sid = provider_result.get("sip_call_sid") or workflow.sip_call_sid
    workflow.voice_dispatch_id = provider_result.get("dispatch_id") or workflow.voice_dispatch_id
    workflow.active_call_timeout_at = task.deadline
    workflow.next_scheduled_call_time = task.deadline
    workflow.next_scheduled_call_stage = "Agent 1 timeout/retry check"
    workflow.timer_status = f"Agent 1 call running; timeout check at {task.deadline}"
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "updated", "workflow": workflow.name}


def get_repeat_workflow_state(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Return the deterministic repeat-follow-up journey/call state for the active task."""
    workflow = _workflow_for_tool(arguments or {}, task_id)
    state = _repeat_state_machine(workflow)
    _append_mcp_tool_usage(workflow, "get_repeat_workflow_state", task_id=task_id, status="success")
    return {
        "status": "success",
        "workflow": workflow.name,
        "runtime_version": state.get("runtime_version"),
        "journey": state.get("journey") or {},
        "call": _call_state_summary(state),
        "current_step": _active_step(state),
        "rules": state.get("rules") or {},
    }


def get_current_required_step(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Return only the current unlocked step. The voice agent must not advance without completing it."""
    workflow = _workflow_for_tool(arguments or {}, task_id)
    state = _repeat_state_machine(workflow)
    step = _active_step(state)
    _append_mcp_tool_usage(workflow, "get_current_required_step", task_id=task_id, status="success", detail={"step_key": step.get("step_key") if step else ""})
    return {
        "status": "success" if step else "complete",
        "workflow": workflow.name,
        "current_step": step,
        "no_skip_policy": (state.get("rules") or {}).get("no_skip_policy"),
    }


def get_current_speech_unit(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Return the current step's exact compact speech unit and context filters."""
    workflow = _workflow_for_tool(arguments or {}, task_id)
    state = _repeat_state_machine(workflow)
    step = _active_step(state)
    if not step:
        _append_mcp_tool_usage(workflow, "get_current_speech_unit", task_id=task_id, status="complete")
        return {"status": "complete", "workflow": workflow.name, "message": "All Agent 1 required steps are complete."}
    _append_mcp_tool_usage(workflow, "get_current_speech_unit", task_id=task_id, status="success", detail={"step_key": step.get("step_key")})
    return {
        "status": "success",
        "workflow": workflow.name,
        "step_key": step.get("step_key"),
        "step_label": step.get("step_label"),
        "stage_key": step.get("stage_key"),
        "required": step.get("required"),
        "can_skip": step.get("can_skip"),
        "resume_policy": step.get("resume_policy"),
        "speech_unit": step.get("speech_unit"),
        "variables": step.get("variables") or {},
        "rag_filters": step.get("rag_filters") or {},
        "tool_to_call": step.get("tool_to_call") or "",
        "completion_condition": step.get("completion_condition"),
        "agent_instruction": step.get("agent_instruction"),
        "interrupt_rule": "If the patient interrupts, answer only the immediate question briefly, then return to this same step unless safety override applies.",
    }


def mark_repeat_step_complete(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Complete the current active step and unlock the next step. Out-of-order completion is blocked."""
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    state = _repeat_state_machine(workflow)
    step = _active_step(state)
    if not step:
        return {"status": "complete", "workflow": workflow.name, "message": "No pending Agent 1 step."}

    requested = _clean_text(arguments.get("step_key") or step.get("step_key"))
    if requested != step.get("step_key"):
        _append_mcp_tool_usage(workflow, "mark_repeat_step_complete", task_id=task_id, status="blocked", detail={"requested": requested, "active": step.get("step_key")})
        return {
            "status": "blocked_out_of_order",
            "workflow": workflow.name,
            "requested_step": requested,
            "active_step": step.get("step_key"),
            "message": "Complete the active step first. Required medical/order content cannot be skipped.",
        }

    details = arguments.get("structured_details") or arguments.get("details") or {}
    if not isinstance(details, dict):
        details = {"value": details}
    completion_block = _validate_repeat_step_completion(step, details)
    if completion_block:
        _append_mcp_tool_usage(workflow, "mark_repeat_step_complete", task_id=task_id, status="blocked", detail=completion_block)
        return {
            "status": "blocked_incomplete_step",
            "workflow": workflow.name,
            "active_step": step.get("step_key"),
            "message": completion_block.get("message") or "Current step is incomplete. Continue the same step before moving ahead.",
            "required_fields": completion_block.get("required_fields") or [],
            "active_speech_unit": step.get("speech_unit"),
            "active_variables": step.get("variables") or {},
        }
    now_value = frappe.utils.now()
    step["status"] = "COMPLETED"
    step["completed_at"] = now_value
    step["completion_details"] = details
    _apply_step_completion_side_effects(state, step, details)
    next_step = _advance_state_machine(state)
    _save_repeat_state_machine(workflow, state)
    _append_mcp_tool_usage(workflow, "mark_repeat_step_complete", task_id=task_id, status="success", detail={"completed": requested, "next": next_step.get("step_key") if next_step else ""})
    return {
        "status": "success",
        "workflow": workflow.name,
        "completed_step": requested,
        "next_step": next_step,
        "all_required_steps_complete": 0 if next_step else 1,
    }


def _validate_repeat_step_completion(step: dict, details: dict) -> dict:
    """Block unsafe early completion of medical speech units.

    The realtime model can sometimes call the completion tool before the full
    audio has covered dose/instruction/period. The backend cannot hear the
    audio, so require explicit completion flags for medicine item steps.
    """
    step_key = _clean_text(step.get("step_key"))
    if not step_key.startswith("medicine_item_"):
        return {}

    timing_block = _validate_minimum_step_speech_time(step)
    if timing_block:
        return timing_block

    required_flags = {
        "medicine_name_spoken": "medicine name",
        "dose_spoken": "dose",
        "timing_or_instruction_spoken": "timing/instruction",
        "period_spoken": "period",
    }
    missing = [label for key, label in required_flags.items() if not _truthy(details.get(key))]
    if missing:
        return {
            "message": (
                "Medicine step cannot be completed yet. Speak this same medicine again with all required details: "
                + ", ".join(missing)
                + ". Then call mark_repeat_step_complete with spoken_text plus medicine_name_spoken=true, dose_spoken=true, timing_or_instruction_spoken=true, and period_spoken=true."
            ),
            "required_fields": list(required_flags.keys()),
            "missing_fields": missing,
            "step_key": step_key,
        }

    variables = step.get("variables") if isinstance(step.get("variables"), dict) else {}
    spoken_text = _clean_text(
        details.get("spoken_text")
        or details.get("customer_facing_text")
        or details.get("speech_text")
        or details.get("what_was_spoken")
    )
    text_block = _validate_medicine_spoken_text(step_key, variables, spoken_text)
    if text_block:
        return text_block

    expected_name = _clean_text(variables.get("medicine_name"))
    spoken_name = _clean_text(details.get("medicine_name") or details.get("medicine"))
    if expected_name and spoken_name and expected_name.lower() not in spoken_name.lower() and spoken_name.lower() not in expected_name.lower():
        return {
            "message": f"Wrong medicine completion. Active medicine is {expected_name}. Continue this same medicine; do not move ahead.",
            "required_fields": ["medicine_name"],
            "missing_fields": ["correct medicine name"],
            "expected_medicine_name": expected_name,
            "spoken_medicine_name": spoken_name,
            "step_key": step_key,
        }
    return {}


def _validate_minimum_step_speech_time(step: dict) -> dict:
    started_at = step.get("started_at")
    if not started_at:
        return {}
    try:
        elapsed_seconds = (now_datetime() - get_datetime(started_at)).total_seconds()
    except Exception:
        return {}
    required_seconds = _minimum_speech_seconds(step.get("speech_unit"))
    if elapsed_seconds >= required_seconds:
        return {}
    remaining = max(1, int(required_seconds - elapsed_seconds))
    return {
        "message": (
            "Medicine step cannot be completed yet. It was advanced too quickly to safely cover name, dose, timing/instruction and period. "
            "Speak this same medicine completely, then mark it complete."
        ),
        "required_fields": ["minimum_speech_time", "medicine_name_spoken", "dose_spoken", "timing_or_instruction_spoken", "period_spoken"],
        "missing_fields": [f"at least {remaining} more seconds of this medicine explanation"],
        "step_key": step.get("step_key"),
        "minimum_speech_seconds": required_seconds,
        "elapsed_seconds": round(elapsed_seconds, 2),
    }


def _minimum_speech_seconds(speech_unit: Any) -> int:
    text = _script_text(speech_unit)
    words = re.findall(r"[\w/.-]+", text)
    if not words:
        return 6
    # Medical dosage lines must not be marked complete after a tiny fragment.
    # This protects against realtime model/tool-call race conditions without
    # hardcoding any medicine name.
    return max(8, min(22, int(len(words) / 2.8)))


def _validate_medicine_spoken_text(step_key: str, variables: dict, spoken_text: str) -> dict:
    if not spoken_text:
        return {
            "message": (
                "Medicine step cannot be completed without spoken_text. Pass the exact customer-facing sentence you spoke, "
                "including medicine name, dose, timing/instruction and period."
            ),
            "required_fields": [
                "spoken_text",
                "medicine_name_spoken",
                "dose_spoken",
                "timing_or_instruction_spoken",
                "period_spoken",
            ],
            "missing_fields": ["spoken_text"],
            "step_key": step_key,
        }

    normalized = _normalize_for_loose_match(spoken_text)
    checks = {
        "medicine name": variables.get("medicine_name"),
        "dose": variables.get("medicine_dose"),
        "period": variables.get("medicine_period"),
    }
    instruction = variables.get("medicine_timing") or variables.get("medicine_instruction")
    missing = []
    for label, value in checks.items():
        value = _clean_text(value)
        if not value:
            continue
        if not _loose_contains(normalized, value):
            missing.append(label)
    if not missing:
        return {}
    return {
        "message": (
            "Medicine step cannot be completed because spoken_text does not contain: "
            + ", ".join(missing)
            + ". Speak this same medicine naturally but include those exact prescription details."
        ),
        "required_fields": ["spoken_text"],
        "missing_fields": missing,
        "step_key": step_key,
        "expected_medicine_name": variables.get("medicine_name") or "",
        "expected_dose": variables.get("medicine_dose") or "",
        "expected_period": variables.get("medicine_period") or "",
        "expected_instruction": instruction or "",
    }


def _normalize_for_loose_match(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("one", "1").replace("zero", "0")
    text = text.replace("ओने", "1").replace("वन", "1").replace("ज़ीरो", "0").replace("जीरो", "0")
    return re.sub(r"[^a-z0-9\u0900-\u097f]+", "", text)


def _loose_contains(normalized_text: str, expected: Any) -> bool:
    expected_text = _clean_text(expected)
    if not expected_text:
        return True
    variants = {expected_text}
    variants.add(expected_text.replace("-", " "))
    variants.add(expected_text.replace("-", ""))
    variants.add(expected_text.replace("/", " "))
    variants.add(expected_text.replace("/", ""))
    variants.add(re.sub(r"\([^)]*\)", "", expected_text).strip())
    for variant in variants:
        normalized_variant = _normalize_for_loose_match(variant)
        if normalized_variant and normalized_variant in normalized_text:
            return True
    words = [word for word in re.split(r"[^A-Za-z0-9\u0900-\u097f]+", expected_text) if len(word) >= 3]
    if words and all(_normalize_for_loose_match(word) in normalized_text for word in words[:4]):
        return True
    return False


def mark_repeat_step_interrupted(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Record an interruption without advancing the step pointer."""
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    state = _repeat_state_machine(workflow)
    step = _active_step(state)
    if not step:
        return {"status": "complete", "workflow": workflow.name}
    interruptions = step.get("interruptions")
    if not isinstance(interruptions, list):
        interruptions = []
    interruptions.append(
        {
            "at": frappe.utils.now(),
            "patient_text": _clean_text(arguments.get("patient_text") or arguments.get("utterance") or ""),
            "handled": 0,
        }
    )
    step["interruptions"] = interruptions[-20:]
    step["status"] = "RESUME_REQUIRED"
    state["active_step_key"] = step.get("step_key")
    _save_repeat_state_machine(workflow, state)
    _append_mcp_tool_usage(workflow, "mark_repeat_step_interrupted", task_id=task_id, status="success", detail={"step_key": step.get("step_key")})
    return {
        "status": "resume_required",
        "workflow": workflow.name,
        "current_step": step,
        "message": "Resume the same step after briefly answering the interruption.",
    }


def resume_repeat_pending_step(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Return the same pending step after interruption; does not advance."""
    workflow = _workflow_for_tool(arguments or {}, task_id)
    state = _repeat_state_machine(workflow)
    step = _active_step(state)
    if step and step.get("status") == "RESUME_REQUIRED":
        step["status"] = "IN_PROGRESS"
        _save_repeat_state_machine(workflow, state)
    _append_mcp_tool_usage(workflow, "resume_repeat_pending_step", task_id=task_id, status="success", detail={"step_key": step.get("step_key") if step else ""})
    return {
        "status": "success" if step else "complete",
        "workflow": workflow.name,
        "current_step": step,
        "speech_unit": step.get("speech_unit") if step else "",
    }


def get_repeat_encounter_full_data(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    workflow = _workflow_for_tool(arguments or {}, task_id)
    encounter = parse_json_object(workflow.encounter_json, "Encounter JSON")
    _append_mcp_tool_usage(workflow, "get_repeat_encounter_full_data", task_id=task_id, status="success")
    return {
        "status": "success",
        "workflow": workflow.name,
        "patient_encounter": workflow.patient_encounter,
        "patient_name": workflow.patient_name,
        "phone": workflow.patient_mobile,
        "awb_number": workflow.awb_number,
        "order_id": workflow.order_id,
        "encounter": encounter,
    }


def get_repeat_medicine_list(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Return the exact current prescription medicines for this repeat follow-up call.

    This is intentionally separate from the full encounter tool so the voice
    agent can fetch a small, deterministic source-of-truth before speaking
    medicine names/dosage.
    """
    workflow = _workflow_for_tool(arguments or {}, task_id)
    summary = _medicine_summary_for_workflow(workflow)
    medicines = _medicine_guard_items(summary)
    status = "success" if medicines else "missing_medicines"
    _append_mcp_tool_usage(
        workflow,
        "get_repeat_medicine_list",
        task_id=task_id,
        status=status,
        detail={"medicine_count": len(medicines)},
    )
    return {
        "status": status,
        "workflow": workflow.name,
        "patient_encounter": workflow.patient_encounter,
        "patient_name": workflow.patient_name,
        "medicine_count": len(medicines),
        "medicine_names": [item.get("display_name") for item in medicines],
        "medicines": medicines,
        "required_medicine_script": summary.get("required_medicine_script") or _required_medicine_script(summary),
        "patient_instruction": summary.get("sr_pe_instruction") or "",
        "source": "AI Repeat Follow Up Workflow.medicine_summary_json / Patient Encounter drug_prescription",
    }


def verify_repeat_medicine_in_prescription(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Verify a medicine name against the stored Patient Encounter prescription."""
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    query = _clean_text(arguments.get("medicine_name") or arguments.get("name") or arguments.get("query") or "")
    summary = _medicine_summary_for_workflow(workflow)
    medicines = _medicine_guard_items(summary)
    match = _find_medicine_match(query, medicines)
    status = "found" if match else "not_found"
    _append_mcp_tool_usage(
        workflow,
        "verify_repeat_medicine_in_prescription",
        task_id=task_id,
        status=status,
        detail={"query": query, "medicine": match.get("display_name") if match else ""},
    )
    if match:
        return {
            "status": "found",
            "workflow": workflow.name,
            "medicine_name": query,
            "medicine": match,
            "medicine_count": len(medicines),
            "medicine_names": [item.get("display_name") for item in medicines],
            "customer_safe_answer": (
                f"Haan ji, {match.get('display_name')} prescription list mein hai. "
                f"Dosage {match.get('dosage') or 'clear nahi'}; instruction {match.get('instruction') or 'clear nahi'}; "
                f"duration {match.get('period') or 'clear nahi'}."
            ),
        }
    return {
        "status": "not_found",
        "workflow": workflow.name,
        "medicine_name": query,
        "medicine_count": len(medicines),
        "medicine_names": [item.get("display_name") for item in medicines],
        "customer_safe_answer": "Is exact naam se medicine current prescription list mein nahi dikh rahi. Team prescription verify karegi; main guess nahi karungi.",
    }


def get_shipkia_tracking_status(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    return _fetch_shipkia_tracking_for_workflow(
        workflow,
        arguments=arguments,
        task_id=task_id,
        agent=agent,
        log_usage=True,
    )


def send_repeat_diet_chart_whatsapp(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    """Send the matching repeat-follow-up diet chart PDF to the customer on WhatsApp."""
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    settings = _workflow_settings(workflow)
    if not settings.diet_chart_whatsapp_enabled:
        result = {"status": "disabled", "message": "Diet chart WhatsApp sending is disabled for this workflow."}
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="disabled", detail=result)
        return result

    encounter = parse_json_object(workflow.encounter_json, "Encounter JSON")
    dept = _clean_text(
        arguments.get("department")
        or arguments.get("sr_pe_deptt")
        or _first_path(encounter, _field_names(settings.diet_chart_dept_field_names))
        or encounter.get("sr_pe_deptt")
    )
    if not dept:
        result = {"status": "missing_department", "message": "Patient Encounter department is not available."}
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="missing_department", detail=result)
        return result

    doc = _find_diet_chart_document(company=workflow.company, department=dept)
    if not doc:
        result = {
            "status": "no_matching_diet_chart",
            "department": dept,
            "message": "No matching diet chart document is configured for this department.",
        }
        workflow.diet_chart_dept = dept
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="no_match", detail=result)
        return result

    media_url = _customer_pdf_url(doc, settings)
    if not media_url:
        result = {
            "status": "missing_pdf",
            "department": dept,
            "knowledge_document": doc.name,
            "message": "A matching diet chart exists, but no customer PDF/URL is attached.",
        }
        workflow.diet_chart_dept = dept
        workflow.diet_chart_knowledge_document = doc.name
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="missing_pdf", detail=result)
        return result

    phone = _normalize_phone(arguments.get("phone") or arguments.get("customer_phone") or workflow.patient_mobile)
    caption = _clean_text(arguments.get("caption") or doc.get("whatsapp_caption")) or "Aapka diet chart yahan attach hai. Kripya isse follow kijiye."
    file_name = _diet_chart_file_name(doc, dept)
    template_map = _clean_text(arguments.get("template_map") or arguments.get("whatsapp_template_map") or settings.get("diet_chart_whatsapp_template_map"))
    try:
        if template_map:
            send_result = _send_diet_chart_with_template_map(
                template_map=template_map,
                phone=phone,
                workflow=workflow,
                department=dept,
                caption=caption,
                media_url=media_url,
                file_name=file_name,
                task_id=task_id,
                agent=agent,
            )
            channel_account = send_result.get("channel_account") or template_map
        else:
            channel_account = _clean_text(arguments.get("channel_account") or settings.diet_chart_whatsapp_channel_account)
            if not channel_account:
                result = {
                    "status": "missing_whatsapp_channel",
                    "department": dept,
                    "knowledge_document": doc.name,
                    "pdf_url": media_url,
                    "message": "Diet chart PDF is configured, but WhatsApp channel account is missing.",
                }
                workflow.diet_chart_dept = dept
                workflow.diet_chart_knowledge_document = doc.name
                workflow.diet_chart_pdf_file = media_url
                _store_diet_chart_result(workflow, result)
                _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="missing_channel", detail=result)
                return result
            send_result = _send_whatsapp_document(
                phone=phone,
                customer_name=workflow.patient_name,
                channel_account=channel_account,
                body=caption,
                media_url=media_url,
                file_name=file_name,
                remote_mcp_server=settings.diet_chart_whatsapp_remote_mcp_server,
                send_method=settings.diet_chart_whatsapp_method,
                settings=settings,
                department=dept,
                caption=caption,
                task_id=task_id,
            )
        result = {
            "status": "success",
            "department": dept,
            "knowledge_document": doc.name,
            "pdf_url": media_url,
            "whatsapp_channel_account": channel_account,
            "whatsapp_template_map": template_map,
            "delivery_status": send_result.get("delivery_status") or send_result.get("result", {}).get("delivery_status"),
            "message": "Diet chart PDF sent on WhatsApp.",
            "result": send_result,
        }
        record_provider_event(
            provider="WhatsApp",
            operation="send_repeat_diet_chart_whatsapp",
            status="Succeeded",
            agent=agent,
            task=task_id,
            request={
                "workflow": workflow.name,
                "phone": phone,
                "department": dept,
                "knowledge_document": doc.name,
                "channel_account": channel_account,
                "media_url": media_url,
            },
            response=result,
        )
        workflow.diet_chart_dept = dept
        workflow.diet_chart_knowledge_document = doc.name
        workflow.diet_chart_pdf_file = media_url
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="success", detail={"department": dept, "document": doc.name})
        return result
    except Exception as exc:
        result = {
            "status": "error",
            "department": dept,
            "knowledge_document": doc.name,
            "pdf_url": media_url,
            "message": "Unable to send diet chart PDF on WhatsApp right now.",
            "error": str(exc),
        }
        workflow.diet_chart_dept = dept
        workflow.diet_chart_knowledge_document = doc.name
        workflow.diet_chart_pdf_file = media_url
        _store_diet_chart_result(workflow, result)
        _append_mcp_tool_usage(workflow, "send_repeat_diet_chart_whatsapp", task_id=task_id, status="error", detail={"department": dept, "document": doc.name})
        create_error("Repeat Follow Up Diet Chart WhatsApp", str(exc), source="repeat_followup", task=task_id, agent=agent, payload=result, exc=exc)
        return result


def _fetch_shipkia_tracking_for_workflow(
    workflow,
    *,
    arguments: dict | None = None,
    task_id: str | None = None,
    agent: str | None = None,
    log_usage: bool = False,
) -> dict:
    arguments = arguments or {}
    settings = _workflow_settings(workflow)
    if not settings.shipkia_tracking_enabled:
        result = {"status": "disabled", "message": "Shipkia tracking is disabled for this workflow."}
        if log_usage:
            _append_mcp_tool_usage(workflow, "get_shipkia_tracking_status", task_id=task_id, status="disabled", detail=result)
        return result

    awb = _clean_text(arguments.get("awb") or arguments.get("awb_number") or arguments.get("tracking_id") or workflow.awb_number)
    if not awb:
        encounter = parse_json_object(workflow.encounter_json, "Encounter JSON")
        awb = _clean_text(_first_path(encounter, _field_names(settings.awb_field_names)))
    if not awb:
        result = {"status": "missing_awb", "message": "AWB number is not available for this workflow."}
        workflow.shipkia_result_json = as_json(result)
        workflow.save(ignore_permissions=True)
        if log_usage:
            _append_mcp_tool_usage(workflow, "get_shipkia_tracking_status", task_id=task_id, status="missing_awb", detail=result)
        return result

    url = (settings.shipkia_tracking_api_url or DEFAULT_SHIPKIA_URL).strip()
    request_payload = {"tracking_id": awb}
    try:
        response = requests.post(
            url,
            data=request_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=20,
        )
        raw = response.json() if response.text else {}
        compact = _compact_shipkia_result(raw, awb=awb, status_code=response.status_code, ok=response.ok)
        record_provider_event(
            provider="Shipkia",
            operation="track_awb",
            status="Succeeded" if compact.get("status") == "success" else "Failed",
            agent=agent,
            task=task_id,
            request={"url": url, "tracking_id": awb},
            response=compact,
            error=None if compact.get("status") == "success" else compact.get("message"),
        )
    except Exception as exc:
        compact = {"status": "error", "tracking_id": awb, "message": "Unable to fetch tracking details."}
        create_error("Shipkia Tracking", str(exc), source="repeat_followup", task=task_id, agent=agent, payload={"tracking_id": awb}, exc=exc)

    workflow.shipkia_result_json = as_json(compact)
    if compact.get("status") == "success":
        workflow.awb_number = compact.get("awb_number") or awb
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    if log_usage:
        _append_mcp_tool_usage(workflow, "get_shipkia_tracking_status", task_id=task_id, status=compact.get("status") or "unknown", detail={"tracking_id": awb})
    return compact


def log_repeat_followup_outcome(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    state = _repeat_state_machine(workflow)
    pending_step = {} if _is_simple_followup_mode(workflow) else _pending_required_steps_before(state, "outcome_log")
    if pending_step:
        _append_mcp_tool_usage(
            workflow,
            "log_repeat_followup_outcome",
            task_id=task_id,
            status="blocked",
            detail={"pending_step": pending_step.get("step_key")},
        )
        return {
            "status": "blocked_incomplete_agent_1",
            "workflow": workflow.name,
            "pending_step": pending_step,
            "message": "Agent 1 required flow is incomplete. Finish the active required step before logging outcome or scheduling Agent 2.",
        }
    workflow.primary_outcome = _clean_text(arguments.get("primary_outcome") or arguments.get("outcome") or "")
    workflow.sub_outcome = _clean_text(arguments.get("sub_outcome") or "")
    workflow.customer_summary = _clean_text(arguments.get("customer_summary") or arguments.get("summary") or arguments.get("notes") or "")
    workflow.agent_notes = _clean_text(arguments.get("agent_notes") or "")
    workflow.next_action = _clean_text(arguments.get("next_action") or "")
    structured = arguments.get("structured_details_json") or arguments.get("structured_details") or arguments.get("details") or {}
    if not isinstance(structured, dict):
        structured = {"value": structured}
    workflow.structured_details_json = as_json(structured)
    if arguments.get("shipkia_result") and isinstance(arguments.get("shipkia_result"), dict):
        workflow.shipkia_result_json = as_json(arguments.get("shipkia_result"))
    _complete_terminal_state_steps(state)
    context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
    context[STATE_MACHINE_CONTEXT_KEY] = state
    workflow.context_json = as_json(context)
    _sync_state_machine_child_tables(workflow, state)
    _append_mcp_tool_usage(workflow, "log_repeat_followup_outcome", task_id=task_id, status="success", detail={"primary_outcome": workflow.primary_outcome})

    real_conversation = _truthy(arguments.get("real_conversation"))
    if "real_conversation" not in arguments:
        real_conversation = _normalize_outcome(workflow.primary_outcome) not in MISSED_OUTCOMES

    if _normalize_outcome(workflow.primary_outcome) in {"unclear", "unclear_conversation", "unknown"}:
        workflow.status = "Unclear Conversation"
    else:
        workflow.status = "Completed"
    workflow.active_call_timeout_at = None
    workflow.next_scheduled_call_time = None
    workflow.next_scheduled_call_stage = "Outcome logged"
    workflow.timer_status = "Agent 1 outcome logged"
    workflow.last_error = ""
    workflow.save(ignore_permissions=True)
    frappe.db.commit()

    schedule_result = schedule_agent_2(workflow.name, real_conversation=real_conversation)
    return {"status": "success", "workflow": workflow.name, "agent_2": schedule_result}


def trigger_repeat_renewal_n8n(arguments: dict | None = None, *, task_id: str | None = None, agent: str | None = None) -> dict:
    arguments = arguments or {}
    workflow = _workflow_for_tool(arguments, task_id)
    webhook_url = _clean_text(workflow.renewal_webhook_url or _settings().get("renewal_webhook_url"))
    if not webhook_url:
        result = {"status": "missing_webhook_url", "workflow": workflow.name}
        _append_mcp_tool_usage(workflow, "trigger_repeat_renewal_n8n", task_id=task_id, status="missing_webhook_url", detail=result)
        frappe.throw("Renewal Webhook URL is not configured.")

    consent = _truthy(arguments.get("renewal_consent") or arguments.get("consent") or arguments.get("confirmed"))
    if not consent:
        result = {"status": "blocked_missing_consent", "workflow": workflow.name}
        _append_mcp_tool_usage(workflow, "trigger_repeat_renewal_n8n", task_id=task_id, status="blocked", detail=result)
        return {**result, "message": "Renewal webhook was not triggered because renewal_consent was not true."}

    structured = arguments.get("structured_details") or arguments.get("details") or {}
    if not isinstance(structured, dict):
        structured = {"value": structured}

    customer_said = _clean_text(
        arguments.get("customer_said")
        or arguments.get("customer_exact_words")
        or arguments.get("customer_message")
        or arguments.get("customer_consent_text")
        or arguments.get("what_customer_said")
        or structured.get("customer_said")
        or structured.get("customer_exact_words")
        or structured.get("customer_message")
        or structured.get("customer_consent_text")
    )
    customer_summary = _clean_text(
        arguments.get("customer_summary")
        or arguments.get("short_summary")
        or arguments.get("summary")
        or structured.get("customer_summary")
        or structured.get("short_summary")
        or structured.get("summary")
    )
    medicine_status = _clean_text(arguments.get("medicine_status") or structured.get("medicine_status"))
    medicine_name = _clean_text(
        arguments.get("medicine_name")
        or arguments.get("medicine_names")
        or structured.get("medicine_name")
        or structured.get("medicine_names")
    )
    medicine_duration = _clean_text(
        arguments.get("medicine_duration")
        or arguments.get("duration")
        or arguments.get("course_duration")
        or structured.get("medicine_duration")
        or structured.get("duration")
        or structured.get("course_duration")
    )
    renewal_reason = _clean_text(arguments.get("renewal_reason") or structured.get("renewal_reason"))
    consent_summary = _clean_text(arguments.get("consent_summary") or structured.get("consent_summary"))
    if customer_said and not customer_summary:
        customer_summary = customer_said
    for key, value in {
        "customer_said": customer_said,
        "customer_summary": customer_summary,
        "medicine_status": medicine_status,
        "medicine_name": medicine_name,
        "medicine_duration": medicine_duration,
        "renewal_reason": renewal_reason,
        "consent_summary": consent_summary,
    }.items():
        if value and not structured.get(key):
            structured[key] = value

    payload = {
        "event": "repeat_renewal_requested",
        "workflow": workflow.name,
        "company": workflow.company,
        "patient_encounter": workflow.patient_encounter,
        "patient_name": workflow.patient_name,
        "customer_name": workflow.patient_name,
        "customer_phone": workflow.patient_mobile,
        "phone": workflow.patient_mobile,
        "awb_number": workflow.awb_number,
        "order_id": workflow.order_id,
        "agent": agent,
        "task": task_id,
        "renewal_consent": True,
        "customer_said": customer_said,
        "customer_summary": customer_summary,
        "medicine_status": medicine_status,
        "medicine_name": medicine_name,
        "medicine_duration": medicine_duration,
        "renewal_reason": renewal_reason,
        "consent_summary": consent_summary,
        "structured_details": structured,
        "source": "confluence_ai_repeat_followup",
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    auth_header = _clean_text(workflow.renewal_webhook_auth_header or _settings().get("renewal_webhook_auth_header"))
    auth_token = _password_value(workflow, "renewal_webhook_auth_token") or _password_value(_settings(), "renewal_webhook_auth_token")
    if auth_header and auth_token and _is_valid_http_header_name(auth_header):
        headers[auth_header] = auth_token
    elif auth_token:
        headers["Authorization"] = auth_token

    request_log = {
        "url": webhook_url,
        "payload": payload,
        "headers": sorted(headers.keys()),
    }
    try:
        response = requests.post(webhook_url, json=payload, headers=headers, timeout=30)
        try:
            body = response.json() if response.text else {}
        except Exception:
            body = {"text": response.text[:2000]}
        result = {
            "status": "success" if response.ok else "failed",
            "workflow": workflow.name,
            "status_code": response.status_code,
            "ok": response.ok,
            "body": body,
        }
        record_provider_event(
            provider="N8N",
            operation="trigger_repeat_renewal_n8n",
            status="Succeeded" if response.ok else "Failed",
            company=workflow.company,
            agent=agent,
            task=task_id,
            request=request_log,
            response=result,
            error=None if response.ok else str(body)[:1000],
        )
        if not response.ok:
            _append_mcp_tool_usage(workflow, "trigger_repeat_renewal_n8n", task_id=task_id, status="failed", detail=result)
            frappe.throw(f"Renewal webhook failed with status {response.status_code}.")
    except Exception as exc:
        if not isinstance(exc, frappe.ValidationError):
            record_provider_event(
                provider="N8N",
                operation="trigger_repeat_renewal_n8n",
                status="Failed",
                company=workflow.company,
                agent=agent,
                task=task_id,
                request=request_log,
                response={},
                error=str(exc),
            )
            create_error(
                "Repeat Renewal Webhook Failed",
                str(exc),
                company=workflow.company,
                source="repeat_followup",
                task=task_id,
                agent=agent,
                payload={"workflow": workflow.name, "webhook_url": webhook_url},
                exc=exc,
            )
        raise

    workflow.renewal_triggered_at = now_datetime()
    workflow.renewal_result_json = as_json(result)
    workflow.last_error = ""
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    _append_mcp_tool_usage(workflow, "trigger_repeat_renewal_n8n", task_id=task_id, status="success", detail={"status_code": result["status_code"]})
    return result


def handle_voice_result(workflow: str | None = None, task: str | None = None, outcome: str | None = None, notes: str | None = None) -> dict:
    doc = _find_workflow(workflow=workflow, task=task)
    if not doc:
        return {"status": "ignored", "reason": "no_active_workflow"}
    if doc.status in FINAL_STATES:
        return {"status": "ignored", "reason": "final_state", "workflow": doc.name}

    normalized = _normalize_outcome(outcome)
    if normalized in MISSED_OUTCOMES:
        return mark_call_missed(doc.name, notes or outcome)

    if not _is_simple_followup_mode(doc):
        state = _repeat_state_machine(doc)
        pending_step = _pending_required_steps_before(state, "outcome_log")
        if pending_step:
            return mark_agent_1_incomplete(doc.name, notes or f"Call ended before completing {pending_step.get('step_key')}.")

    if notes and not doc.customer_summary:
        doc.customer_summary = _clean_text(notes)
    if not doc.primary_outcome:
        doc.primary_outcome = outcome or "unclear"
    doc.status = "Unclear Conversation"
    doc.active_call_timeout_at = None
    doc.next_scheduled_call_time = None
    doc.next_scheduled_call_stage = "Call completed"
    doc.timer_status = "Call completed; outcome was not explicitly logged"
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    schedule_result = schedule_agent_2(doc.name, real_conversation=True)
    return {"status": "unclear_logged", "workflow": doc.name, "agent_2": schedule_result}


def _is_simple_followup_mode(workflow) -> bool:
    try:
        context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
    except Exception:
        context = {}
    return _truthy(context.get("simple_followup_mode"))


def mark_agent_1_incomplete(workflow_name: str, notes: str | None = None, *, ended_at=None) -> dict:
    workflow = frappe.get_doc(WORKFLOW, workflow_name)
    settings = _workflow_settings(workflow)
    _mark_voice_task_missed(workflow.voice_task)
    if notes:
        workflow.agent_notes = _clean_text(notes)

    max_attempts = int(workflow.max_retry_count or settings.max_agent_1_attempts or 3)
    if int(workflow.retry_count or 0) >= max_attempts:
        workflow.status = "Failed"
        workflow.primary_outcome = workflow.primary_outcome or "agent_1_incomplete"
        workflow.active_call_timeout_at = None
        workflow.next_scheduled_call_time = None
        workflow.next_scheduled_call_stage = "Stopped"
        workflow.timer_status = "Agent 1 ended before completing required flow; no attempts remaining"
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "failed_incomplete_after_retries", "workflow": workflow.name}

    retry_time = add_to_date(
        ended_at or now_datetime(),
        minutes=int(settings.retry_delay_minutes or 60),
        as_datetime=True,
    )
    workflow.status = "Retry Queued"
    workflow.primary_outcome = workflow.primary_outcome or "agent_1_incomplete"
    workflow.next_call_time = retry_time
    workflow.active_call_timeout_at = None
    workflow.next_scheduled_call_time = retry_time
    workflow.next_scheduled_call_stage = "Agent 1 retry - incomplete required flow"
    workflow.timer_status = f"Agent 1 incomplete; retry scheduled for {retry_time}"
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "retry_queued_incomplete", "workflow": workflow.name, "next_call_time": retry_time}


def wait_for_voice_transcript(workflow_name: str, notes: str | None = None) -> dict:
    doc = frappe.get_doc(WORKFLOW, workflow_name)
    if doc.status in FINAL_STATES:
        return {"status": "ignored", "reason": "final_state", "workflow": doc.name}
    if notes:
        doc.agent_notes = _clean_text(notes)
    doc.status = "Call Running"
    doc.active_call_timeout_at = None
    doc.next_scheduled_call_time = None
    doc.next_scheduled_call_stage = "Waiting for transcript"
    doc.timer_status = "Call completed; waiting for provider transcript/outcome"
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "waiting_for_transcript", "workflow": doc.name}


def mark_call_missed(workflow_name: str, notes: str | None = None, *, missed_at=None) -> dict:
    workflow = frappe.get_doc(WORKFLOW, workflow_name)
    settings = _workflow_settings(workflow)
    _mark_voice_task_missed(workflow.voice_task)
    if notes:
        workflow.agent_notes = _clean_text(notes)

    max_attempts = int(workflow.max_retry_count or settings.max_agent_1_attempts or 3)
    if int(workflow.retry_count or 0) >= max_attempts:
        workflow.status = "Missed After Retries"
        workflow.primary_outcome = workflow.primary_outcome or "missed"
        workflow.active_call_timeout_at = None
        workflow.next_scheduled_call_time = None
        workflow.next_scheduled_call_stage = "Stopped"
        workflow.timer_status = "Agent 1 missed after configured retries"
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "missed_after_retries", "workflow": workflow.name}

    workflow.status = "Retry Queued"
    workflow.next_call_time = add_to_date(
        missed_at or now_datetime(),
        minutes=int(settings.retry_delay_minutes or 60),
        as_datetime=True,
    )
    workflow.active_call_timeout_at = None
    workflow.next_scheduled_call_time = workflow.next_call_time
    workflow.next_scheduled_call_stage = "Agent 1 retry"
    workflow.timer_status = f"Retry scheduled for {workflow.next_call_time}"
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "retry_queued", "workflow": workflow.name, "next_call_time": workflow.next_call_time}


def schedule_agent_2(workflow_name: str, *, real_conversation: bool) -> dict:
    workflow = frappe.get_doc(WORKFLOW, workflow_name)
    settings = _workflow_settings(workflow)
    if settings.schedule_agent_2_only_after_conversation and not real_conversation:
        workflow.active_call_timeout_at = None
        workflow.next_scheduled_call_time = None
        workflow.next_scheduled_call_stage = "No follow-up scheduled"
        workflow.timer_status = "Agent 2 skipped because there was no real conversation"
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "skipped", "reason": "no_real_conversation"}
    if workflow.agent_2_scheduled_at or workflow.agent_2_task:
        return {"status": "already_scheduled", "scheduled_at": workflow.agent_2_scheduled_at, "task": workflow.agent_2_task}

    agent_2 = workflow.agent_2 or settings.agent_2
    workflow.agent_2 = agent_2
    if not agent_2 or not frappe.db.exists("AI Agent", agent_2) or not frappe.db.get_value("AI Agent", agent_2, "enabled"):
        workflow.status = "Agent 2 Pending Config"
        workflow.active_call_timeout_at = None
        workflow.next_scheduled_call_time = None
        workflow.next_scheduled_call_stage = "Agent 2 pending config"
        workflow.timer_status = "Agent 2 is missing or disabled"
        _sync_journey_stage_to_workflow(workflow, "AGENT_2", "PENDING_CONFIG")
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "pending_config"}

    workflow.status = "Agent 2 Scheduled"
    workflow.agent_2_scheduled_at = add_to_date(now_datetime(), days=int(settings.agent_2_delay_days or 7), as_datetime=True)
    workflow.active_call_timeout_at = None
    workflow.next_scheduled_call_time = workflow.agent_2_scheduled_at
    workflow.next_scheduled_call_stage = "Agent 2"
    workflow.timer_status = f"Agent 2 scheduled for {workflow.agent_2_scheduled_at}"
    _sync_journey_stage_to_workflow(workflow, "AGENT_2", "SCHEDULED", scheduled_at=workflow.agent_2_scheduled_at)
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "scheduled", "scheduled_at": workflow.agent_2_scheduled_at}


def process_due_workflows() -> dict:
    now_value = now_datetime()
    retried = 0
    missed = 0
    agent_2_queued = 0

    for name in frappe.get_all(WORKFLOW, filters={"status": "Retry Queued", "next_call_time": ["<=", now_value]}, pluck="name", limit=200):
        try:
            if queue_agent_1_call(name).get("status") == "queued":
                retried += 1
        except Exception as exc:
            _mark_failed(name, exc)

    for name in _stale_voice_workflows(now_value):
        try:
            mark_call_missed(name, "Voice call timed out without pickup/provider callback.")
            missed += 1
        except Exception as exc:
            _mark_failed(name, exc)

    for name in _missed_voice_task_workflows():
        try:
            mark_call_missed(name, "Voice task was marked deadline missed before provider callback.")
            missed += 1
        except Exception as exc:
            _mark_failed(name, exc)

    for name in frappe.get_all(WORKFLOW, filters={"status": "Agent 2 Scheduled", "agent_2_scheduled_at": ["<=", now_value]}, pluck="name", limit=200):
        try:
            if queue_agent_2_call(name).get("status") == "queued":
                agent_2_queued += 1
        except Exception as exc:
            _mark_failed(name, exc)

    return {"retried": retried, "missed": missed, "agent_2_queued": agent_2_queued}


def queue_agent_2_call(workflow_name: str) -> dict:
    workflow = frappe.get_doc(WORKFLOW, workflow_name)
    settings = _workflow_settings(workflow)
    agent_2 = workflow.agent_2 or settings.agent_2
    if not agent_2 or not frappe.db.exists("AI Agent", agent_2) or not frappe.db.get_value("AI Agent", agent_2, "enabled"):
        workflow.status = "Agent 2 Pending Config"
        workflow.timer_status = "Agent 2 is missing or disabled"
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "pending_config", "workflow": workflow.name}

    task_template = settings.voice_task_template or _template_by_key("repeat_followup_voice") or _ensure_task_template()
    context = _agent_2_workflow_context(workflow)
    context["agent_2_from_workflow"] = workflow.name
    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "company": workflow.company,
            "status": "Queued",
            "source_system": "AI Repeat Follow Up",
            "batch_label": f"{workflow.name}:agent2",
            "idempotency_key": f"{workflow.name}:agent2",
            "task_template": task_template,
            "target_agent": agent_2,
            "priority": "Normal",
            "source_payload_json": workflow.source_payload_json,
        }
    )
    batch.insert(ignore_permissions=True)
    task = frappe.new_doc("AI Task")
    deadline = add_to_date(now_datetime(), minutes=int(settings.voice_call_timeout_minutes or 5), as_datetime=True)
    task.update(
        {
            "company": workflow.company,
            "status": "Queued",
            "task_batch": batch.name,
            "task_template": task_template,
            "target_agent": agent_2,
            "assigned_agent": agent_2,
            "channel": "Voice",
            "priority": "Normal",
            "deadline": deadline,
            "external_record_id": workflow.name,
            "external_record_type": WORKFLOW,
            "idempotency_key": f"{workflow.name}:agent2",
            "context_json": as_json(context),
        }
    )
    task.insert(ignore_permissions=True)
    refresh_batch_counts(batch.name)
    workflow.status = "Agent 2 Queued"
    workflow.agent_2_task = task.name
    workflow.active_call_timeout_at = deadline
    workflow.next_scheduled_call_time = deadline
    workflow.next_scheduled_call_stage = "Agent 2 timeout check"
    workflow.timer_status = "Agent 2 queued"
    _sync_journey_stage_to_workflow(workflow, "AGENT_2", "QUEUED", started_at=now_datetime())
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    enqueue_task_execution(task.name, "Voice", enqueue_after_commit=False)
    return {"status": "queued", "workflow": workflow.name, "task": task.name}


def _agent_2_workflow_context(workflow) -> dict:
    context = _workflow_context(workflow)
    for key in (
        STATE_MACHINE_CONTEXT_KEY,
        "active_stage_id",
        "active_stage_name",
        "current_journey_stage",
        "current_rag_filters",
        "current_speech_unit",
        "current_step_key",
        "current_step_label",
        "diet_chart_summary",
        "next_stage_after_diet",
        "next_stage_after_medicine",
        "next_stage_after_order",
        "required_diet_script",
        "required_medicine_script",
        "required_order_script",
        "simple_followup_script",
        "stage_sequence",
        "strict_followup_script",
    ):
        context.pop(key, None)

    medicine_summary = context.get("medicine_summary")
    if isinstance(medicine_summary, dict):
        medicine_summary = medicine_summary.copy()
        medicine_summary.pop("required_medicine_script", None)
        context["medicine_summary"] = medicine_summary

    context.update(
        {
            "event": "repeat_followup",
            "simple_followup_mode": 0,
            "workflow": workflow.name,
            "company": workflow.company,
            "patient_name": workflow.patient_name,
            "customer_name": workflow.patient_name,
            "phone": workflow.patient_mobile,
            "customer_phone": workflow.patient_mobile,
            "patient_encounter": workflow.patient_encounter,
            "awb_number": workflow.awb_number,
            "order_id": workflow.order_id,
            "active_stage_id": "AGENT_2",
            "active_stage_name": "Agent 2 follow-up",
            "current_journey_stage": "AGENT_2",
            "repeat_followup_compacted": 1,
            "full_encounter_available_via_tool": 1,
        }
    )
    return context


def _settings() -> frappe._dict:
    if not frappe.db.exists("DocType", SETTINGS):
        frappe.throw(f"{SETTINGS} DocType is not installed.")
    return frappe.get_single(SETTINGS)


def _settings_config(settings) -> frappe._dict:
    return frappe._dict(
        {
            "scenario_key": "",
            "company": settings.company or "sriaas",
            "agent_1": settings.agent_1,
            "agent_2": settings.agent_2,
            "livekit_channel_account_fallback": settings.livekit_channel_account_fallback,
            "voice_channel_account": settings.livekit_channel_account_fallback,
            "voice_channel_source": "Settings fallback" if settings.livekit_channel_account_fallback else "",
            "voice_task_template": settings.voice_task_template,
            "default_knowledge_document": settings.default_knowledge_document,
            "medicine_summary_field_names": settings.get("medicine_summary_field_names")
            or "drug_prescription,sr_allopathy_drug_prescription,sr_homeopathy_drug_prescription,sr_pe_order_items,sr_medication_template,sr_pe_instruction,sr_pe_disease,sr_diagnosis,sr_complaints",
            "max_retry_count": int(settings.max_agent_1_attempts or 3),
            "retry_delay_minutes": int(settings.retry_delay_minutes or 60),
            "voice_call_timeout_minutes": int(settings.voice_call_timeout_minutes or 5),
            "agent_2_delay_days": int(settings.agent_2_delay_days or 7),
            "schedule_agent_2_only_after_conversation": 1 if settings.schedule_agent_2_only_after_conversation else 0,
            "shipkia_tracking_enabled": 1 if settings.shipkia_tracking_enabled else 0,
            "shipkia_prefetch_before_call": 1 if settings.get("shipkia_prefetch_before_call") else 0,
            "shipkia_tracking_api_url": settings.shipkia_tracking_api_url or DEFAULT_SHIPKIA_URL,
            "diet_chart_whatsapp_enabled": 1 if settings.get("diet_chart_whatsapp_enabled") else 0,
            "diet_chart_prefetch_before_call": 1 if settings.get("diet_chart_prefetch_before_call") else 0,
            "diet_chart_auto_send_before_call": 1 if settings.get("diet_chart_auto_send_before_call") else 0,
            "diet_chart_dept_field_names": settings.get("diet_chart_dept_field_names")
            or "sr_pe_deptt,patient_encounter.sr_pe_deptt,data.patient_encounter.sr_pe_deptt,body.encounter.sr_pe_deptt,encounter.sr_pe_deptt",
            "diet_chart_public_file_base_url": settings.get("diet_chart_public_file_base_url") or "",
            "diet_chart_whatsapp_channel_account": settings.get("diet_chart_whatsapp_channel_account") or "",
            "diet_chart_whatsapp_template_map": settings.get("diet_chart_whatsapp_template_map") or "",
            "diet_chart_whatsapp_remote_mcp_server": settings.get("diet_chart_whatsapp_remote_mcp_server") or "",
            "diet_chart_whatsapp_method": settings.get("diet_chart_whatsapp_method") or "wa_chat_hub.api.runtime.send_reply",
            "diet_chart_whatsapp_send_strategy": settings.get("diet_chart_whatsapp_send_strategy") or "Free-form then Template",
            "diet_chart_whatsapp_template_name": settings.get("diet_chart_whatsapp_template_name") or "",
            "diet_chart_whatsapp_template_language": settings.get("diet_chart_whatsapp_template_language") or "en",
            "diet_chart_whatsapp_template_method": settings.get("diet_chart_whatsapp_template_method") or "wa_chat_hub.api.runtime.send_template_message",
            "diet_chart_template_header_values_json": settings.get("diet_chart_template_header_values_json") or '["{media_url}"]',
            "diet_chart_template_body_values_json": settings.get("diet_chart_template_body_values_json") or '["{patient_name}", "{department}"]',
            "diet_chart_template_button_values_json": settings.get("diet_chart_template_button_values_json") or "{}",
            "outcome_logging_required": 1 if settings.outcome_logging_required else 0,
            "idempotency_key_field_names": settings.idempotency_key_field_names,
            "phone_field_names": settings.phone_field_names,
            "awb_field_names": settings.awb_field_names,
        }
    )


def _start_config(settings, payload: dict | None) -> frappe._dict:
    config = _settings_config(settings)
    payload = payload or {}
    overrides = _payload_config_overrides(payload)
    scenario_key = _payload_scenario_key(payload, overrides)
    scenario_config = _scenario_config_overrides(scenario_key)
    if scenario_config:
        for fieldname in WORKFLOW_CONFIG_FIELDS:
            value = scenario_config.get(fieldname)
            if value not in (None, ""):
                config[fieldname] = value
        if scenario_config.get("max_agent_1_attempts") not in (None, ""):
            config.max_retry_count = scenario_config.get("max_agent_1_attempts")
        if scenario_config.get("voice_channel_account"):
            config.voice_channel_account = scenario_config.get("voice_channel_account")
            config.livekit_channel_account_fallback = scenario_config.get("livekit_channel_account_fallback") or config.voice_channel_account
            config.voice_channel_source = "Scenario Config"
    if overrides:
        minimum_voice_timeout = _int_or_default(config.get("voice_call_timeout_minutes"), 5)
        for fieldname in WORKFLOW_CONFIG_FIELDS:
            value = overrides.get(fieldname)
            if value not in (None, ""):
                if fieldname == "voice_call_timeout_minutes":
                    override_timeout = _int_or_default(value, minimum_voice_timeout)
                    if override_timeout < minimum_voice_timeout:
                        continue
                    value = override_timeout
                config[fieldname] = value
        if overrides.get("max_agent_1_attempts") not in (None, ""):
            config.max_retry_count = overrides.get("max_agent_1_attempts")
        override_channel = overrides.get("voice_channel_account") or overrides.get("livekit_channel_account") or overrides.get("livekit_channel_account_fallback")
        if override_channel:
            config.voice_channel_account = override_channel
            config.livekit_channel_account_fallback = override_channel
            config.voice_channel_source = "workflow_config"
    config.scenario_key = _clean_text(
        config.get("scenario_key")
        or scenario_key
        or ""
    )
    if not config.voice_channel_account:
        agent_channel = _agent_channel_account(config.agent_1)
        if agent_channel:
            config.voice_channel_account = agent_channel
            config.voice_channel_source = "Agent 1"
    elif config.voice_channel_source not in ("workflow_config", "Scenario Config"):
        config.voice_channel_source = "Settings fallback"
    return config


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _payload_config_overrides(payload: dict) -> dict:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    for key in ("repeat_followup_config", "workflow_config", "repeat_followup_settings"):
        value = payload.get(key)
        if value in (None, ""):
            value = body.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip().startswith("{"):
            return parse_json_object(value, f"{key} JSON")
    return {}


def _payload_scenario_key(payload: dict, overrides: dict | None = None) -> str:
    payload = payload or {}
    overrides = overrides or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    return _clean_text(
        overrides.get("scenario_key")
        or overrides.get("workflow_key")
        or overrides.get("scenario")
        or payload.get("scenario_key")
        or payload.get("workflow_key")
        or payload.get("scenario")
        or body.get("scenario_key")
        or body.get("workflow_key")
        or body.get("scenario")
        or data.get("scenario_key")
        or data.get("workflow_key")
        or data.get("scenario")
        or ""
    )


def _scenario_config_overrides(scenario_key: str) -> dict:
    scenario_key = _clean_text(scenario_key)
    if not scenario_key or not frappe.db.exists("DocType", WORKFLOW):
        return {}
    name = frappe.db.get_value(
        WORKFLOW,
        {
            "workflow_type": "Scenario Config",
            "enabled": 1,
            "scenario_key": scenario_key,
        },
        "name",
        order_by="modified desc",
    )
    if not name:
        return {}
    doc = frappe.get_doc(WORKFLOW, name)
    values = {"scenario_config_workflow": doc.name}
    for fieldname in WORKFLOW_CONFIG_FIELDS:
        value = doc.get(fieldname)
        if value not in (None, ""):
            values[fieldname] = value
    if doc.get("max_retry_count") not in (None, ""):
        values["max_retry_count"] = doc.max_retry_count
        values["max_agent_1_attempts"] = doc.max_retry_count
    return values


def _workflow_config_values(config: frappe._dict) -> dict:
    values = {}
    for fieldname in WORKFLOW_CONFIG_FIELDS:
        value = config.get(fieldname)
        if value not in (None, ""):
            values[fieldname] = value
    values["max_retry_count"] = int(values.get("max_retry_count") or 3)
    values["retry_delay_minutes"] = int(values.get("retry_delay_minutes") or 60)
    values["voice_call_timeout_minutes"] = int(values.get("voice_call_timeout_minutes") or 5)
    values["agent_2_delay_days"] = int(values.get("agent_2_delay_days") or 7)
    values["schedule_agent_2_only_after_conversation"] = 1 if _truthy(values.get("schedule_agent_2_only_after_conversation")) else 0
    values["shipkia_tracking_enabled"] = 1 if _truthy(values.get("shipkia_tracking_enabled")) else 0
    values["shipkia_prefetch_before_call"] = 1 if _truthy(values.get("shipkia_prefetch_before_call")) else 0
    values["diet_chart_whatsapp_enabled"] = 1 if _truthy(values.get("diet_chart_whatsapp_enabled")) else 0
    values["diet_chart_prefetch_before_call"] = 1 if _truthy(values.get("diet_chart_prefetch_before_call")) else 0
    values["diet_chart_auto_send_before_call"] = 1 if _truthy(values.get("diet_chart_auto_send_before_call")) else 0
    values["outcome_logging_required"] = 1 if _truthy(values.get("outcome_logging_required")) else 0
    values["shipkia_tracking_api_url"] = values.get("shipkia_tracking_api_url") or DEFAULT_SHIPKIA_URL
    values["diet_chart_whatsapp_method"] = values.get("diet_chart_whatsapp_method") or "wa_chat_hub.api.runtime.send_reply"
    values["diet_chart_whatsapp_send_strategy"] = values.get("diet_chart_whatsapp_send_strategy") or "Free-form then Template"
    values["diet_chart_whatsapp_template_language"] = values.get("diet_chart_whatsapp_template_language") or "en"
    values["diet_chart_whatsapp_template_method"] = values.get("diet_chart_whatsapp_template_method") or "wa_chat_hub.api.runtime.send_template_message"
    values["diet_chart_template_header_values_json"] = values.get("diet_chart_template_header_values_json") or '["{media_url}"]'
    values["diet_chart_template_body_values_json"] = values.get("diet_chart_template_body_values_json") or '["{patient_name}", "{department}"]'
    values["diet_chart_template_button_values_json"] = values.get("diet_chart_template_button_values_json") or "{}"
    return values


def _workflow_settings(workflow) -> frappe._dict:
    config = _settings_config(_settings())
    for fieldname in WORKFLOW_CONFIG_FIELDS:
        value = workflow.get(fieldname)
        if value not in (None, ""):
            config[fieldname] = value
    if workflow.get("max_retry_count") not in (None, ""):
        config.max_retry_count = workflow.max_retry_count
    config.max_agent_1_attempts = config.max_retry_count
    return config


def _agent_channel_account(agent_name: str | None) -> str:
    if not agent_name or not frappe.db.exists("AI Agent", agent_name):
        return ""
    return frappe.db.get_value("AI Agent", agent_name, "allowed_channel_account") or ""


def _prefetch_shipkia_before_call(workflow) -> None:
    settings = _workflow_settings(workflow)
    if not settings.shipkia_tracking_enabled or not settings.shipkia_prefetch_before_call:
        return
    try:
        result = _fetch_shipkia_tracking_for_workflow(workflow, log_usage=False)
        workflow.reload()
        context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
        summary = _tracking_summary(result)
        if summary:
            context["tracking_summary"] = summary
            workflow.context_json = as_json(context)
            workflow.save(ignore_permissions=True)
            frappe.db.commit()
    except Exception as exc:
        create_error("Shipkia Prefetch", str(exc), source="repeat_followup", payload={"workflow": workflow.name, "awb_number": workflow.awb_number}, exc=exc)


def _prepare_diet_chart_before_call(workflow) -> None:
    settings = _workflow_settings(workflow)
    if not settings.diet_chart_prefetch_before_call:
        return
    try:
        result = _resolve_diet_chart_for_workflow(workflow, settings=settings)
        workflow.reload()
        context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
        summary = _diet_chart_summary(result)
        if summary:
            context["diet_chart_summary"] = summary
            workflow.context_json = as_json(context)
            workflow.save(ignore_permissions=True)
            frappe.db.commit()
    except Exception as exc:
        create_error("Diet Chart Prepare Before Call", str(exc), source="repeat_followup", payload={"workflow": workflow.name}, exc=exc)


def _resolve_diet_chart_for_workflow(workflow, *, settings=None) -> dict:
    settings = settings or _workflow_settings(workflow)
    encounter = parse_json_object(workflow.encounter_json, "Encounter JSON")
    dept = _clean_text(
        workflow.diet_chart_dept
        or _first_path(encounter, _field_names(settings.diet_chart_dept_field_names))
        or encounter.get("sr_pe_deptt")
    )
    if not dept:
        result = {"status": "missing_department", "message": "Patient Encounter department is not available."}
        workflow.diet_chart_summary_json = as_json(_diet_chart_summary(result))
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return result
    doc = _find_diet_chart_document(company=workflow.company, department=dept)
    if not doc:
        result = {
            "status": "no_matching_diet_chart",
            "department": dept,
            "message": "No matching diet chart document is configured for this department.",
        }
        workflow.diet_chart_dept = dept
        workflow.diet_chart_summary_json = as_json(_diet_chart_summary(result))
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
        return result

    full_doc = frappe.get_doc("AI Knowledge Document", doc.name)
    media_url = _customer_pdf_url(doc, settings)
    result = {
        "status": "ready" if media_url else "missing_pdf",
        "department": dept,
        "knowledge_document": doc.name,
        "knowledge_document_title": doc.get("title"),
        "pdf_url": media_url,
        "diet_explanation_script": _diet_chart_explanation_script(full_doc),
        "message": "Diet chart matched." if media_url else "A matching diet chart exists, but no customer PDF/URL is attached.",
    }
    workflow.diet_chart_dept = dept
    workflow.diet_chart_knowledge_document = doc.name
    workflow.diet_chart_pdf_file = media_url
    workflow.diet_chart_summary_json = as_json(_diet_chart_summary(result))
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return result


def _tracking_summary(result: dict | None) -> dict:
    result = result or {}
    if not result:
        return {}
    return {
        key: result.get(key)
        for key in (
            "status",
            "tracking_id",
            "awb_number",
            "shipment_status",
            "estimated_delivery",
            "delivered_on",
            "delivery_partner",
            "delivery_location",
            "latest_status",
            "latest_detail",
            "latest_location",
            "latest_time",
            "message",
        )
        if result.get(key) not in (None, "", [], {})
    }


def _diet_chart_summary(result: dict | None) -> dict:
    result = result or {}
    if not result:
        return {}
    return {
        key: result.get(key)
        for key in (
            "status",
            "department",
            "knowledge_document",
            "knowledge_document_title",
            "pdf_url",
            "diet_explanation_script",
            "whatsapp_channel_account",
            "delivery_status",
            "message",
        )
        if result.get(key) not in (None, "", [], {})
    }


def _diet_chart_explanation_script(doc) -> str:
    """Return patient-speakable diet content from the configured Knowledge Document.

    This intentionally uses the Knowledge Document content as the source, not hardcoded
    food rules. The prompt asks Radha to explain this content naturally.
    """
    content = _script_text(doc.get("content") or "")
    if not content:
        return ""
    # kbdoc-0787 is small enough for the current Agent 1 scope. Keep the full useful
    # diet content, but avoid unbounded prompt growth if future documents are huge.
    return content[:6500]


def _initialize_agent_1_state_machine(workflow) -> dict:
    context = _workflow_context(workflow)
    state = _build_agent_1_state_machine(workflow=workflow, context=context)
    _save_repeat_state_machine(workflow, state)
    return state


def _repeat_state_machine(workflow) -> dict:
    context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
    state = context.get(STATE_MACHINE_CONTEXT_KEY)
    if isinstance(state, dict) and state.get("runtime_version"):
        return state
    return _initialize_agent_1_state_machine(workflow)


def _save_repeat_state_machine(workflow, state: dict) -> None:
    context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
    context[STATE_MACHINE_CONTEXT_KEY] = state
    active = _active_step(state)
    context["current_journey_stage"] = ((state.get("journey") or {}).get("active_stage_key") or "AGENT_1")
    context["current_step_key"] = active.get("step_key") if active else ""
    context["current_step_label"] = active.get("step_label") if active else ""
    context["current_speech_unit"] = active.get("speech_unit") if active else ""
    context["current_rag_filters"] = active.get("rag_filters") if active else {}
    context["radha_runtime_version"] = RADHA_RUNTIME_VERSION
    workflow.context_json = as_json(context)
    if active:
        workflow.next_scheduled_call_stage = f"Agent 1 step: {active.get('step_label') or active.get('step_key')}"
        workflow.timer_status = f"Agent 1 active step: {active.get('step_label') or active.get('step_key')}"
    else:
        workflow.next_scheduled_call_stage = "Agent 1 steps complete"
        workflow.timer_status = "Agent 1 required step machine completed"
    _sync_state_machine_child_tables(workflow, state)
    workflow.save(ignore_permissions=True)
    frappe.db.commit()


def _build_agent_1_state_machine(*, workflow, context: dict) -> dict:
    medicine_summary = context.get("medicine_summary") or {}
    prescriptions = medicine_summary.get("drug_prescription") if isinstance(medicine_summary, dict) else []
    if not isinstance(prescriptions, list):
        prescriptions = []
    department = _clean_text(context.get("patient_department") or workflow.diet_chart_dept)
    diet_summary = context.get("diet_chart_summary") if isinstance(context.get("diet_chart_summary"), dict) else {}
    if not diet_summary and workflow.get("diet_chart_summary_json"):
        diet_summary = parse_json_object(workflow.diet_chart_summary_json, "Diet Chart Summary JSON")
    tracking_summary = context.get("tracking_summary") or {}
    patient_name = _clean_text(context.get("patient_name") or workflow.patient_name) or "customer"
    medicine_count = len(prescriptions)
    steps: list[dict] = []

    def add_step(
        step_key: str,
        label: str,
        speech_unit: str,
        *,
        required: bool = True,
        can_skip: bool = False,
        variables: dict | None = None,
        rag_filters: dict | None = None,
        tool_to_call: str = "",
        completion_condition: str = "mark_repeat_step_complete",
        agent_instruction: str = "",
    ) -> None:
        steps.append(
            {
                "step_key": step_key,
                "step_label": label,
                "stage_key": "AGENT_1",
                "order": len(steps) + 1,
                "status": "PENDING",
                "required": 1 if required else 0,
                "can_skip": 1 if can_skip else 0,
                "resume_policy": "resume_same_step_after_interrupt",
                "completion_condition": completion_condition,
                "tool_to_call": tool_to_call,
                "variables": variables or {},
                "rag_filters": rag_filters or {},
                "speech_unit": _script_text(speech_unit),
                "agent_instruction": agent_instruction
                or "Speak only this step. Do not move to the next step until mark_repeat_step_complete succeeds.",
            }
        )

    add_step(
        "opening",
        "Opening plus delivery question",
        f"Namaste {patient_name} ji, main Radha sriaas treatment-support team se bol rahi hoon. Aapke medicine package follow-up ke liye call kiya hai. Sabse pehle confirm kar leti hoon: aapko medicine package receive ho gaya hai?",
        variables={"awb_number": workflow.awb_number or "", "order_id": workflow.order_id or ""},
        agent_instruction=(
            "This first step must never be only an intro. Greet briefly and ask the delivery question in the same reply. "
            "After asking this question, wait for the customer's delivery answer; do not go silent after just the greeting."
        ),
    )
    add_step(
        "delivery_check",
        "Medicine/order delivery check",
        "Sabse pehle confirm kar leti hoon: aapko medicine package receive ho gaya hai?",
        variables={"awb_number": workflow.awb_number or "", "order_id": workflow.order_id or ""},
        agent_instruction=(
            "Ask delivery status first. If received, complete with structured_details.order_received=true. "
            "If not received, complete with structured_details.order_received=false so tracking step unlocks."
        ),
    )
    add_step(
        "order_tracking_if_needed",
        "Order tracking if not received",
        _required_order_script(
            encounter=parse_json_object(workflow.encounter_json, "Encounter JSON") if workflow.encounter_json else {},
            awb=workflow.awb_number,
            order_id=workflow.order_id,
            tracking_summary=tracking_summary,
        ),
        can_skip=True,
        variables={"tracking_summary": tracking_summary, "awb_number": workflow.awb_number or "", "order_id": workflow.order_id or ""},
        tool_to_call="get_shipkia_tracking_status",
        completion_condition="complete after customer-safe tracking/fallback is explained",
        agent_instruction=(
            "Use get_shipkia_tracking_status if tracking_summary is missing or stale. "
            "Explain status/location/detail/expected delivery clearly in Hindi/Hinglish. "
            "If AWB/tracking is missing, say team will check delivery details. Do not claim delivered unless verified."
        ),
    )
    if medicine_count:
        add_step(
            "medicine_intro",
            "Medicine total count intro",
            f"Aapko total {medicine_count} medicines prescribe hui hain. Main ek-ek medicine ka naam, dose, timing aur period clear kar deti hoon, taaki start karte waqt confusion na rahe.",
            variables={"medicine_count": medicine_count},
        )
        for index, item in enumerate(prescriptions, start=1):
            if not isinstance(item, dict):
                continue
            med = _medicine_variables(index, medicine_count, item)
            add_step(
                f"medicine_item_{index}",
                f"Medicine {index} of {medicine_count}",
                _medicine_speech_unit(med),
                variables=med,
                rag_filters={
                    "stage": "AGENT_1",
                    "step": "medicine_explanation",
                    "content_type": "PRESCRIPTION_ONLY_MEDICINE",
                    "medicine_name": med.get("medicine_name"),
                    "allowed_for_patient_speech": True,
                },
                completion_condition="complete only after name, dose, timing/instruction and period are spoken or missing fields are explicitly marked pending",
                agent_instruction=(
                    "This is mandatory medical instruction. Do not skip this medicine. "
                    "Speak the medicine name, dose, timing/instruction and period exactly from variables. "
                    "If a value is missing, say you will not guess and mark it pending; do not invent. "
                    "Only after speaking all required details, call mark_repeat_step_complete with structured_details including "
                    "medicine_name, spoken_text, medicine_name_spoken=true, dose_spoken=true, timing_or_instruction_spoken=true, and period_spoken=true."
                ),
            )
        add_step(
            "medicine_recap",
            "Medicine completion recap",
            f"Toh ji, total {medicine_count} medicines ka naam aur use clear ho gaya. Agar kisi medicine ki timing ko lekar doubt ho to abhi pooch sakte hain; warna main ab diet clear kar deti hoon.",
            variables={"medicine_count": medicine_count},
        )
    else:
        add_step(
            "medicine_data_missing",
            "Medicine data missing fallback",
            "Mujhe active prescription ki complete medicine list is call context mein clear nahi mil rahi, isliye main dosage guess nahi karungi. Team prescription details verify karke medicine guidance clear karegi.",
            completion_condition="complete after missing prescription/medicine guidance is clearly marked pending",
        )
    add_step(
        "diet_explanation",
        "Department-matched diet explanation",
        _required_diet_script(department=department, diet_summary=diet_summary),
        variables={"department": department},
        rag_filters={
            "stage": "AGENT_1",
            "step": "diet_explanation",
            "department": department,
            "content_type": "DIET",
            "allowed_for_patient_speech": True,
        },
        completion_condition="complete only after allowed foods and parhej/avoid foods are explained from matched knowledge/chart",
        agent_instruction=(
            "Explain diet directly after medicine completion. Use retrieved department-matched knowledge, not generic diet words. "
            "Mention specific allowed and avoid foods from the chart. Do not say any WhatsApp chart has been sent unless a WhatsApp tool step exists and returns success."
        ),
    )
    if _truthy(workflow.get("diet_chart_whatsapp_enabled")):
        add_step(
            "whatsapp_diet_chart",
            "Send diet chart on WhatsApp",
            "Agar customer ne WhatsApp par diet chart maanga hai, to send confirmation milne ke baad hi bolna ki chart send ho gaya hai. Agar customer ne nahi maanga, is step ko no_customer_request ke saath complete karna hai.",
            tool_to_call="send_repeat_diet_chart_whatsapp",
            completion_condition="complete after WhatsApp send result is SUCCESS/FAILED/PENDING or no_customer_request is logged",
            agent_instruction=(
                "Only call send_repeat_diet_chart_whatsapp if the customer explicitly asks/agrees. If success, confirm sent. If failed/missing, say send confirmation nahi mili and log pending support. Never fake success."
            ),
        )
    add_step(
        "outcome_log",
        "Outcome logging",
        "Call ka short outcome save karna hai: delivery status, medicine explanation completion, diet explanation, WhatsApp result, customer summary, and next action.",
        tool_to_call="log_repeat_followup_outcome",
        completion_condition="complete only after log_repeat_followup_outcome succeeds",
        agent_instruction="Mandatory before closing any real conversation.",
    )
    add_step(
        "schedule_next_agent",
        "Schedule Agent 2",
        "Agent 1 conversation complete hone ke baad configured delay ke according Agent 2 follow-up schedule hoga.",
        completion_condition="backend schedules Agent 2 after outcome logging",
        agent_instruction="Do not say an exact future call time unless backend/tool result provides it.",
    )
    add_step(
        "close",
        "Supportive close",
        "Aapko medicine dekar chhoda nahi ja raha; follow-up mein progress step by step monitor hogi. Dhanyavaad ji.",
    )
    if steps:
        steps[0]["status"] = "IN_PROGRESS"
    return {
        "runtime_version": RADHA_RUNTIME_VERSION,
        "journey": {
            "workflow": workflow.name,
            "journey_state": "AGENT_1_IN_PROGRESS",
            "active_stage_key": "AGENT_1",
            "stage_schedule": [
                {"stage_key": "AGENT_1", "agent_field": "agent_1", "delay_after_previous": "immediate", "status": "IN_PROGRESS"},
                {"stage_key": "AGENT_2", "agent_field": "agent_2", "delay_after_previous_days": int((workflow.agent_2_delay_days or 7)), "status": "WAITING_FOR_AGENT_1_COMPLETION"},
                {"stage_key": "AGENT_3", "agent_field": "agent_3", "delay_after_previous_days": "workflow_config_future", "status": "PENDING_CONFIG"},
                {"stage_key": "AGENT_4", "agent_field": "agent_4", "delay_after_previous_days": "workflow_config_future", "status": "PENDING_CONFIG"},
            ],
        },
        "active_step_key": steps[0]["step_key"] if steps else "",
        "steps": steps,
        "rules": {
            "no_skip_policy": "Required steps cannot be skipped. Medicine items are generated per drug_prescription and must complete in order.",
            "interruption_policy": "Answer interruption briefly, then resume the same active step unless safety override applies.",
            "context_policy": "Live context gets only current step, variables, verified state, tool truth and filtered RAG. Full encounter is available through tool.",
            "rag_policy": "Retrieve by current stage, current step, department/disease and allowed_for_patient_speech. Never load all stages/diseases/docs together.",
            "tool_truth_policy": "Do not claim tracking/WhatsApp/outcome/schedule success until the corresponding tool returns SUCCESS.",
        },
        "created_at": frappe.utils.now(),
    }


def _medicine_variables(index: int, count: int, item: dict) -> dict:
    return {
        "medicine_index": index,
        "medicine_count": count,
        "medicine_name": _medicine_display_name(item) or f"Medicine {index}",
        "medicine_dose": _clean_text(item.get("dosage")),
        "medicine_timing": _medicine_timing_hindi(item),
        "medicine_period": _clean_text(item.get("period")),
        "medicine_instruction": _clean_text(item.get("sr_drug_instruction")),
        "medicine_form": _clean_text(item.get("dosage_form")),
        "source_row_name": _clean_text(item.get("name")),
    }


def _medicine_speech_unit(values: dict) -> str:
    index = int(values.get("medicine_index") or 0)
    count = int(values.get("medicine_count") or 0)
    ordinals = {
        1: "Pehli",
        2: "Doosri",
        3: "Teesri",
        4: "Chauthi",
        5: "Paanchvi",
        6: "Chhathi",
        7: "Saatvi",
        8: "Aathvi",
        9: "Nauvi",
        10: "Dasvi",
    }
    ordinal = ordinals.get(index, f"{index} number ki")
    name = values.get("medicine_name")
    form = values.get("medicine_form")
    dose = values.get("medicine_dose")
    timing = values.get("medicine_timing") or values.get("medicine_instruction")
    period = values.get("medicine_period")
    lines = [
        f"{ordinal} medicine {name} hai.",
    ]
    details = []
    if form:
        details.append(f"ye {form} form mein hai")
    if dose:
        details.append(f"iski prescribed dose {dose} hai")
    if timing:
        details.append(str(timing))
    if period:
        details.append(f"isko {period} tak follow karna hai")
    if details:
        lines.append(", ".join(details) + ".")
    if index and count and index < count:
        lines.append("Ab main next medicine par aati hoon.")
    elif index and count:
        lines.append("Ye last medicine thi; ab main short recap karke diet samjhaungi.")
    return "\n".join(lines)


def _active_step(state: dict | None) -> dict:
    if not isinstance(state, dict):
        return {}
    active_key = state.get("active_step_key")
    for step in state.get("steps") or []:
        if isinstance(step, dict) and step.get("step_key") == active_key and step.get("status") not in {"COMPLETED", "SKIPPED_ALLOWED"}:
            return step
    for step in state.get("steps") or []:
        if isinstance(step, dict) and step.get("status") in {"IN_PROGRESS", "RESUME_REQUIRED", "PENDING"}:
            state["active_step_key"] = step.get("step_key")
            if step.get("status") == "PENDING":
                step["status"] = "IN_PROGRESS"
                step["started_at"] = frappe.utils.now()
            return step
    return {}


def _advance_state_machine(state: dict) -> dict:
    next_step = {}
    for step in state.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("status") == "PENDING":
            step["status"] = "IN_PROGRESS"
            step["started_at"] = frappe.utils.now()
            state["active_step_key"] = step.get("step_key")
            next_step = step
            break
    if not next_step:
        state["active_step_key"] = ""
        journey = state.get("journey") if isinstance(state.get("journey"), dict) else {}
        journey["journey_state"] = "AGENT_1_STEPS_COMPLETED"
        state["journey"] = journey
        state["completed_at"] = frappe.utils.now()
    return next_step


def _apply_step_completion_side_effects(state: dict, step: dict, details: dict) -> None:
    if step.get("step_key") == "delivery_check" and _truthy(details.get("order_received")):
        for row in state.get("steps") or []:
            if isinstance(row, dict) and row.get("step_key") == "order_tracking_if_needed" and row.get("status") == "PENDING":
                row["status"] = "SKIPPED_ALLOWED"
                row["skip_reason"] = "Customer confirmed medicine/order received."
                row["completed_at"] = frappe.utils.now()


def _pending_required_steps_before(state: dict, stop_step_key: str) -> dict:
    for step in state.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("step_key") == stop_step_key:
            return {}
        if step.get("required") and step.get("status") not in {"COMPLETED", "SKIPPED_ALLOWED"}:
            return step
    return {}


def _complete_terminal_state_steps(state: dict) -> None:
    now_value = frappe.utils.now()
    terminal_started = False
    for step in state.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("step_key") == "outcome_log":
            terminal_started = True
        if terminal_started and step.get("status") not in {"COMPLETED", "SKIPPED_ALLOWED"}:
            step["status"] = "COMPLETED"
            step["completed_at"] = now_value
    state["active_step_key"] = ""
    journey = state.get("journey") if isinstance(state.get("journey"), dict) else {}
    journey["journey_state"] = "AGENT_1_COMPLETED"
    stage_schedule = journey.get("stage_schedule") if isinstance(journey.get("stage_schedule"), list) else []
    for stage in stage_schedule:
        if not isinstance(stage, dict):
            continue
        if stage.get("stage_key") == "AGENT_1":
            stage["status"] = "COMPLETED"
        if stage.get("stage_key") == "AGENT_2" and stage.get("status") == "WAITING_FOR_AGENT_1_COMPLETION":
            stage["status"] = "SCHEDULING_PENDING"
    journey["stage_schedule"] = stage_schedule
    state["journey"] = journey
    state["completed_at"] = now_value


def _sync_state_machine_child_tables(workflow, state: dict) -> None:
    """Mirror the JSON state machine into UI child tables when those doctypes are installed."""
    try:
        if (
            workflow.meta.has_field("journey_stages")
            and frappe.db.exists("DocType", "AI Repeat Follow Up Journey Stage")
        ):
            workflow.set("journey_stages", [])
            journey = state.get("journey") if isinstance(state.get("journey"), dict) else {}
            for stage in journey.get("stage_schedule") or []:
                if not isinstance(stage, dict):
                    continue
                agent_field = _clean_text(stage.get("agent_field"))
                workflow.append(
                    "journey_stages",
                    {
                        "stage_key": _clean_text(stage.get("stage_key")),
                        "stage_label": _journey_stage_label(stage.get("stage_key")),
                        "agent_field": agent_field,
                        "agent": workflow.get(agent_field) if agent_field and workflow.meta.has_field(agent_field) else "",
                        "status": _clean_text(stage.get("status")),
                        "delay_after_previous_days": _clean_text(
                            stage.get("delay_after_previous_days")
                            or stage.get("delay_after_previous")
                            or ""
                        ),
                        "scheduled_at": stage.get("scheduled_at"),
                        "started_at": stage.get("started_at"),
                        "completed_at": stage.get("completed_at"),
                        "notes": _clean_text(stage.get("notes")),
                    },
                )
        if (
            workflow.meta.has_field("step_runs")
            and frappe.db.exists("DocType", "AI Repeat Follow Up Step Run")
        ):
            workflow.set("step_runs", [])
            for step in state.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                workflow.append(
                    "step_runs",
                    {
                        "step_key": _clean_text(step.get("step_key")),
                        "step_label": _clean_text(step.get("step_label")),
                        "stage_key": _clean_text(step.get("stage_key")),
                        "step_order": int(step.get("order") or 0),
                        "status": _clean_text(step.get("status")),
                        "required": 1 if step.get("required") else 0,
                        "can_skip": 1 if step.get("can_skip") else 0,
                        "resume_policy": _clean_text(step.get("resume_policy")),
                        "tool_to_call": _clean_text(step.get("tool_to_call")),
                        "completion_condition": _clean_text(step.get("completion_condition")),
                        "speech_unit": step.get("speech_unit") or "",
                        "variables_json": as_json(step.get("variables") or {}),
                        "rag_filters_json": as_json(step.get("rag_filters") or {}),
                        "agent_instruction": _clean_text(step.get("agent_instruction")),
                        "started_at": step.get("started_at"),
                        "completed_at": step.get("completed_at"),
                        "interrupt_count": len(step.get("interruptions") or []) if isinstance(step.get("interruptions"), list) else 0,
                        "completion_details_json": as_json(step.get("completion_details") or {}),
                    },
                )
    except Exception as exc:
        create_error(
            "Repeat Follow Up State Machine Table Sync",
            str(exc),
            source="repeat_followup",
            payload={"workflow": workflow.name},
            exc=exc,
        )


def _sync_journey_stage_to_workflow(
    workflow,
    stage_key: str,
    status: str,
    *,
    scheduled_at=None,
    started_at=None,
    completed_at=None,
    notes: str | None = None,
) -> None:
    try:
        state = _repeat_state_machine(workflow)
        journey = state.get("journey") if isinstance(state.get("journey"), dict) else {}
        stages = journey.get("stage_schedule") if isinstance(journey.get("stage_schedule"), list) else []
        found = False
        for stage in stages:
            if not isinstance(stage, dict) or stage.get("stage_key") != stage_key:
                continue
            stage["status"] = status
            if scheduled_at:
                stage["scheduled_at"] = scheduled_at
            if started_at:
                stage["started_at"] = started_at
            if completed_at:
                stage["completed_at"] = completed_at
            if notes:
                stage["notes"] = notes
            found = True
            break
        if not found:
            stages.append(
                {
                    "stage_key": stage_key,
                    "agent_field": stage_key.lower(),
                    "status": status,
                    "scheduled_at": scheduled_at,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "notes": notes or "",
                }
            )
        journey["stage_schedule"] = stages
        state["journey"] = journey
        context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.context_json else {}
        context[STATE_MACHINE_CONTEXT_KEY] = state
        workflow.context_json = as_json(context)
        _sync_state_machine_child_tables(workflow, state)
    except Exception as exc:
        create_error(
            "Repeat Follow Up Journey Stage Sync",
            str(exc),
            source="repeat_followup",
            payload={"workflow": workflow.name, "stage_key": stage_key, "status": status},
            exc=exc,
        )


def _journey_stage_label(stage_key: Any) -> str:
    labels = {
        "AGENT_1": "Agent 1 - Delivery + Medicine + Diet onboarding",
        "AGENT_2": "Agent 2 - First progress follow-up",
        "AGENT_3": "Agent 3 - Trust/progress/adherence",
        "AGENT_4": "Agent 4 - Repeat order / continuation",
    }
    return labels.get(_clean_text(stage_key), _clean_text(stage_key))


def _call_state_summary(state: dict) -> dict:
    steps = [step for step in (state.get("steps") or []) if isinstance(step, dict)]
    return {
        "active_step_key": state.get("active_step_key"),
        "total_steps": len(steps),
        "completed_steps": len([s for s in steps if s.get("status") == "COMPLETED"]),
        "skipped_allowed_steps": len([s for s in steps if s.get("status") == "SKIPPED_ALLOWED"]),
        "pending_steps": len([s for s in steps if s.get("status") in {"PENDING", "IN_PROGRESS", "RESUME_REQUIRED"}]),
    }


def _compact_medicine_summary(encounter: dict, paths: list[str]) -> dict:
    summary = {}
    for path in paths:
        value = _first_path(encounter, [path])
        if value in (None, "", [], {}):
            continue
        max_items = 100 if path in {"drug_prescription", "sr_allopathy_drug_prescription", "sr_homeopathy_drug_prescription"} else 20
        summary[path] = _compact_value(value, max_items=max_items)
    prescriptions = summary.get("drug_prescription")
    if isinstance(prescriptions, list):
        summary["medicine_count"] = len(prescriptions)
        summary["medicine_names"] = [_medicine_display_name(item) for item in prescriptions if _medicine_display_name(item)]
        summary["medicine_explanation_lines"] = [
            _medicine_explanation_line(index, item)
            for index, item in enumerate(prescriptions, start=1)
            if isinstance(item, dict)
        ]
        summary["required_medicine_script"] = _required_medicine_script(summary)
    return summary


def _medicine_display_name(item: dict | None) -> str:
    if not isinstance(item, dict):
        return ""
    return _clean_text(
        item.get("sr_medication_name_print")
        or item.get("drug_name")
        or item.get("medication")
        or item.get("drug_code")
    )


def _medicine_explanation_line(index: int, item: dict) -> str:
    name = _medicine_display_name(item) or f"Medicine {index}"
    dosage = _clean_text(item.get("dosage"))
    instruction = _clean_text(item.get("sr_drug_instruction"))
    period = _clean_text(item.get("period"))
    dosage_form = _clean_text(item.get("dosage_form"))
    parts = [f"{index}. {name}"]
    if dosage_form:
        parts.append(f"form: {dosage_form}")
    if dosage:
        parts.append(f"dosage: {dosage}")
    if instruction:
        parts.append(f"instruction: {instruction}")
    if period:
        parts.append(f"duration: {period}")
    return "; ".join(parts)


def _required_medicine_script(summary: dict) -> str:
    prescriptions = summary.get("drug_prescription")
    if not isinstance(prescriptions, list) or not prescriptions:
        return ""

    lines = [
        f"Aapko total {len(prescriptions)} medicines milengi. Main ek-ek karke sab medicines ka naam aur kaise leni hai bata deti hoon."
    ]
    for index, item in enumerate(prescriptions, start=1):
        if not isinstance(item, dict):
            continue
        name = _medicine_display_name(item) or f"Medicine {index}"
        dosage = _clean_text(item.get("dosage"))
        dosage_form = _clean_text(item.get("dosage_form"))
        instruction = _clean_text(item.get("sr_drug_instruction"))
        timing = _medicine_timing_hindi(item)
        duration = _clean_text(item.get("period"))
        parts = [f"{index}. {name}"]
        if dosage_form:
            parts.append(f"Form {dosage_form}")
        if dosage:
            parts.append(f"Prescription dosage {dosage}")
        if timing:
            parts.append(timing)
        if instruction and instruction not in timing:
            parts.append(f"Doctor instruction: {instruction}")
        if duration:
            parts.append(f"Duration {duration} tak")
        lines.append(". ".join(parts) + ".")

    instruction = _clean_text(summary.get("sr_pe_instruction"))
    if instruction:
        lines.append(f"Important instruction: {instruction}.")
    lines.append("Ye saari medicines complete ho gayi. Ab main aapko diet ke baare mein samjha deti hoon.")
    return "\n".join(lines)


def _required_order_script(*, encounter: dict | None = None, awb: str | None = None, order_id: str | None = None, tracking_summary: dict | None = None) -> str:
    encounter = encounter or {}
    tracking_summary = tracking_summary or {}
    awb = _clean_text(awb or tracking_summary.get("tracking_id") or tracking_summary.get("awb_number") or encounter.get("pe_shipkia_awb_number"))
    order_id = _clean_text(order_id or encounter.get("pe_shipkia_order_id"))
    status = _clean_text(
        tracking_summary.get("shipment_status")
        or tracking_summary.get("latest_status")
        or encounter.get("pe_shipkia_status")
        or encounter.get("pe_shipkia_stage")
    )
    latest_location = _clean_text(tracking_summary.get("latest_location") or tracking_summary.get("delivery_location"))
    latest_detail = _clean_text(tracking_summary.get("latest_detail"))
    partner = _clean_text(tracking_summary.get("delivery_partner") or encounter.get("pe_delivery_partner"))
    expected = _clean_text(tracking_summary.get("estimated_delivery") or encounter.get("pe_shipkia_estimated_delivery"))

    lines = ["Sabse pehle main aapko order ka status bata deti hoon."]
    if awb:
        lines.append(f"Aapka AWB/tracking number {awb} hai.")
    if order_id:
        lines.append(f"Order ID {order_id} hai.")
    if status:
        lines.append(f"Current order status {status} hai.")
        if status.lower() in {"in transit", "in-transit", "transit"}:
            lines.append("Iska matlab order nikal chuka hai aur delivery process mein hai.")
    if latest_location:
        lines.append(f"Latest location/update {latest_location} dikh raha hai.")
    if latest_detail:
        lines.append(f"Latest detail: {latest_detail}.")
    if partner:
        lines.append(f"Courier partner {partner} hai.")
    if expected:
        lines.append(f"Expected delivery {expected} tak dikh rahi hai.")
    if len(lines) == 1:
        lines.append("Abhi tracking detail available nahi hai; team order detail check kar degi.")
    lines.append("Order status ka part complete ho gaya. Ab main medicines ka part start karti hoon.")
    return "\n".join(lines)


def _required_diet_script(*, department: str | None = None, diet_summary: dict | None = None) -> str:
    dept = _clean_text(department)
    diet_summary = diet_summary or {}
    explanation = _script_text(diet_summary.get("diet_explanation_script"))
    customer_script = _customer_facing_diet_script(department=dept, diet_text=explanation)
    if customer_script:
        return customer_script
    lines = []
    if dept:
        lines.append(f"Ab main {dept} ke hisaab se diet samjha deti hoon.")
    else:
        lines.append("Ab main aapko diet ke baare mein samjha deti hoon.")
    lines.append("Diet section mein pehle kya kha sakte hain bataana hai, phir kya parhej karna hai bataana hai. Generic words jaise sirf 'fruits' ya 'sabzi' bolkar skip nahi karna.")
    if explanation:
        lines.append("Use this exact Knowledge Document content as source for diet explanation:")
        lines.append(explanation)
    else:
        lines.append("Diet details matching Knowledge Document / diet chart se bolni hain. Agar chart content context mein missing ho, say team will verify diet chart; do not invent.")
    return "\n".join(lines)


def _customer_facing_diet_script(*, department: str | None = None, diet_text: str | None = None) -> str:
    text = _script_text(diet_text)
    if not text:
        return ""
    allowed = _extract_diet_items(text, start_patterns=[r"food items allowed", r"allowed"], stop_patterns=[r"food items not allowed", r"not allowed", r"foods to avoid"])
    avoid = _extract_diet_items(text, start_patterns=[r"food items not allowed", r"not allowed", r"foods to avoid"], stop_patterns=[r"meal", r"note:", r"## "])
    if not allowed and not avoid:
        return ""
    dept = _clean_text(department)
    lines = [f"Ab main {dept + ' ke hisaab se ' if dept else ''}diet samjha deti hoon."]
    if allowed:
        lines.append("Aap ye cheezein le sakte hain: " + ", ".join(allowed[:28]) + ".")
    if avoid:
        lines.append("Parhej mein ye avoid karna hai: " + ", ".join(avoid[:28]) + ".")
    lines.append("Oil kam rakhein, overeating na karein, aur agar diabetes ya koi special condition ho to team/doctor se confirm karke fruit choose karein.")
    return "\n".join(lines)


def _extract_diet_items(text: str, *, start_patterns: list[str], stop_patterns: list[str]) -> list[str]:
    lines = text.splitlines()
    collecting = False
    items: list[str] = []
    skip_words = {
        "food items allowed",
        "food items not allowed",
        "allowed",
        "not allowed",
        "fruits for diabetic patients",
        "fruits for non-diabetic patients",
        "cereals and grains",
        "vegetables",
        "pulses",
        "beverages",
        "dairy products",
        "cooking oil / fat",
        "non-vegetarian foods",
        "spices",
        "rice",
        "nuts",
        "seasoning",
        "salad and steamed vegetables",
    }
    for raw in lines:
        cleaned = raw.strip().strip("#").strip()
        lowered = cleaned.lower()
        if not collecting and any(re.search(pattern, lowered) for pattern in start_patterns):
            collecting = True
            continue
        if collecting and any(re.search(pattern, lowered) for pattern in stop_patterns):
            if items:
                break
        if not collecting:
            continue
        candidate = cleaned.lstrip("-•0123456789. ").strip()
        if not candidate:
            continue
        candidate_l = candidate.lower()
        if candidate_l in skip_words or candidate_l.startswith("note"):
            continue
        if len(candidate) > 55:
            continue
        if candidate not in items:
            items.append(candidate)
        if len(items) >= 40:
            break
    return items


def _strict_followup_script(order_script: str | None, medicine_summary: dict | None, diet_script: str | None) -> str:
    medicine_script = ""
    if isinstance(medicine_summary, dict):
        medicine_script = _script_text(medicine_summary.get("required_medicine_script"))
    sections = [
        "STRICT FOLLOW-UP SCRIPT. Follow in this exact order. Do not skip any section.",
        "SECTION 1 - ORDER STATUS:",
        _script_text(order_script),
        "SECTION 2 - MEDICINES:",
        medicine_script or "Medicine data is missing. Tell customer team will check medicine details before advising.",
        "SECTION 3 - DIET:",
        _script_text(diet_script),
    ]
    return "\n\n".join(section for section in sections if section)


def _simple_followup_script(order_script: str | None, medicine_summary: dict | None, diet_script: str | None) -> str:
    medicine_script = ""
    if isinstance(medicine_summary, dict):
        medicine_script = _script_text(medicine_summary.get("required_medicine_script"))
    sections = [
        "Radha simple straight flow. Speak naturally, not like field reading.",
        "1) Opening: Namaste <patient_name> ji, main Radha sriaas treatment-support team se bol rahi hoon. Aapko medicine package receive ho gaya hai?",
        "2) If customer says package received: acknowledge once and go directly to medicine explanation. If customer says not received or asks location/date: explain this order status, then go to medicine:",
        _script_text(order_script),
        "3) Medicine explanation: Tell total count first. Then explain every medicine in order. Do not skip any medicine. Use natural Hindi/Hinglish:",
        medicine_script or "Medicine list missing hai; dosage guess mat karna. Team verify karegi.",
        "4) Diet explanation: after all medicines, explain these diet points directly:",
        _script_text(diet_script),
        "5) Close: ask if any medicine or diet doubt remains. If not, say follow-up team will stay connected and close politely.",
    ]
    return "\n\n".join(section for section in sections if section)


def _script_text(value: Any) -> str:
    if value in (None, [], {}):
        return ""
    lines = []
    for line in str(value).splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _medicine_timing_hindi(item: dict) -> str:
    dosage = _clean_text(item.get("dosage"))
    instruction = _clean_text(item.get("sr_drug_instruction"))
    dosage_form = _clean_text(item.get("dosage_form")).lower()
    instruction_l = instruction.lower()

    if "apply" in instruction_l or "affected area" in instruction_l or dosage_form in {"oil", "cream", "ointment", "gel"}:
        return "Isse affected area par lagani hai" if not instruction else f"Instruction: {instruction}"

    timing = ""
    if dosage == "1-0-1":
        timing = "Subah aur shaam leni hai"
    elif dosage == "1-0-0":
        timing = "Sirf subah leni hai"
    elif dosage == "0-1-0":
        timing = "Sirf dopahar leni hai"
    elif dosage == "0-0-1":
        timing = "Sirf shaam/raat leni hai"
    elif dosage:
        timing = f"Dosage {dosage} hai"

    if instruction:
        if "after food" in instruction_l and timing:
            if instruction_l.strip() == "after food":
                return f"{timing}, khaane ke baad"
            return f"{timing}, khaane ke baad. Full instruction: {instruction}"
        return f"{timing}. Instruction: {instruction}" if timing else f"Instruction: {instruction}"
    return timing


def _medicine_summary_for_workflow(workflow) -> dict:
    summary = parse_json_object(workflow.medicine_summary_json, "Medicine Summary JSON") if workflow.medicine_summary_json else {}
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(summary.get("drug_prescription"), list) or not summary.get("drug_prescription"):
        encounter = parse_json_object(workflow.encounter_json, "Encounter JSON") if workflow.encounter_json else {}
        settings = _workflow_settings(workflow)
        summary = _compact_medicine_summary(encounter, _field_names(settings.medicine_summary_field_names))
    if not summary.get("required_medicine_script"):
        summary["required_medicine_script"] = _required_medicine_script(summary)
    return summary


def _medicine_guard_items(summary: dict) -> list[dict]:
    prescriptions = summary.get("drug_prescription") if isinstance(summary, dict) else []
    if not isinstance(prescriptions, list):
        return []
    items: list[dict] = []
    for index, item in enumerate(prescriptions, start=1):
        if not isinstance(item, dict):
            continue
        display_name = _medicine_display_name(item) or f"Medicine {index}"
        guard = {
            "index": index,
            "display_name": display_name,
            "drug_name": _clean_text(item.get("drug_name")),
            "medication": _clean_text(item.get("medication")),
            "print_name": _clean_text(item.get("sr_medication_name_print")),
            "dosage": _clean_text(item.get("dosage")),
            "dosage_form": _clean_text(item.get("dosage_form")),
            "instruction": _clean_text(item.get("sr_drug_instruction")),
            "period": _clean_text(item.get("period")),
            "speech_line": _medicine_explanation_line(index, item),
        }
        items.append(guard)
    return items


def _medicine_match_keys(item: dict) -> list[str]:
    values = [
        item.get("display_name"),
        item.get("drug_name"),
        item.get("medication"),
        item.get("print_name"),
    ]
    keys = []
    for value in values:
        text = _clean_text(value)
        if text:
            keys.append(text)
            keys.append(re.sub(r"\([^)]*\)", "", text).strip())
    return [key for key in keys if key]


def _normalise_medicine_lookup(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("oil", "oil").replace("ओआईएल", "oil")
    return re.sub(r"[^a-z0-9]+", "", text)


def _find_medicine_match(query: str, medicines: list[dict]) -> dict | None:
    needle = _normalise_medicine_lookup(query)
    if not needle:
        return None
    for item in medicines:
        for key in _medicine_match_keys(item):
            hay = _normalise_medicine_lookup(key)
            if needle == hay or needle in hay or hay in needle:
                return item
    return None


def _compact_value(value: Any, *, max_items: int = 8, max_text: int = 700):
    if isinstance(value, list):
        compacted = []
        for item in value[:max_items]:
            compacted.append(_compact_value(item, max_items=max_items, max_text=max_text))
        return compacted
    if isinstance(value, dict):
        preferred = [
            "drug_name",
            "medication",
            "sr_medication_name_print",
            "dosage",
            "period",
            "sr_drug_instruction",
            "dosage_form",
            "sr_item_name",
            "sr_item_qty",
            "sr_item_description",
            "sr_item_amount",
            "sr_item_rate",
            "sr_item_uom",
            "sr_pe_instruction",
            "sr_pe_disease",
            "diagnosis",
            "complaint",
            "symptom",
        ]
        result = {key: value.get(key) for key in preferred if value.get(key) not in (None, "", [], {})}
        if result:
            return result
        return {key: _compact_value(val, max_items=max_items, max_text=max_text) for key, val in list(value.items())[:12] if val not in (None, "", [], {})}
    text = _clean_text(value)
    return text[:max_text] if len(text) > max_text else text


def _find_diet_chart_document(*, company: str | None, department: str):
    if not frappe.db.exists("DocType", "AI Knowledge Document"):
        return None
    dept_norm = _match_key(department)
    if not dept_norm:
        return None
    filters = {"enabled": 1, "status": "Published"}
    if company:
        filters["company"] = company
    fields = ["name", "title", "tags"]
    meta = frappe.get_meta("AI Knowledge Document")
    for fieldname in ("repeat_followup_document_type", "department_match_values", "customer_pdf_file", "customer_attachment_url", "whatsapp_caption"):
        if meta.has_field(fieldname):
            fields.append(fieldname)
    rows = frappe.get_all(
        "AI Knowledge Document",
        filters=filters,
        fields=fields,
        order_by="modified desc",
        limit=200,
    )
    fallback = None
    for row in rows:
        row_dict = dict(row)
        doc_type = _match_key(row_dict.get("repeat_followup_document_type"))
        title = _match_key(row_dict.get("title"))
        tags = _match_key(row_dict.get("tags"))
        match_values = _department_match_keys(row_dict.get("department_match_values"))

        if doc_type and doc_type != "dietchart":
            continue
        looks_like_diet = doc_type == "dietchart" or "diet" in title or "diet" in tags or "chart" in title
        if not looks_like_diet:
            continue
        if dept_norm in match_values:
            return frappe._dict(row_dict)
        if any(value and (value in dept_norm or dept_norm in value) for value in match_values):
            fallback = fallback or frappe._dict(row_dict)
        elif not match_values and (dept_norm in title or dept_norm in tags):
            fallback = fallback or frappe._dict(row_dict)
    return fallback


def _department_match_keys(value: Any) -> set[str]:
    if value in (None, "", [], {}):
        return set()
    if isinstance(value, list):
        return {_match_key(item) for item in value if _match_key(item)}
    return {_match_key(item) for item in str(value or "").split(",") if _match_key(item)}


def _match_key(value: Any) -> str:
    text = _clean_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _customer_pdf_url(doc, settings=None) -> str:
    raw_url = _clean_text(doc.get("customer_attachment_url") or doc.get("customer_pdf_file"))
    if not raw_url:
        return ""
    if raw_url.startswith(("http://", "https://")):
        return _safe_media_url(raw_url)
    public_base = _clean_text(settings.get("diet_chart_public_file_base_url") if settings else "")
    if public_base:
        return _safe_media_url(public_base.rstrip("/") + "/" + raw_url.lstrip("/"))
    return _safe_media_url(get_url(raw_url))


def _safe_media_url(url: str) -> str:
    if not url:
        return ""
    if " " not in url:
        return url
    return quote(url, safe="/:%?&=#%")


def _diet_chart_file_name(doc, department: str) -> str:
    raw_url = _clean_text(doc.get("customer_pdf_file") or doc.get("customer_attachment_url") or "")
    filename = raw_url.split("?", 1)[0].rstrip("/").split("/")[-1]
    if filename:
        return filename
    dept = re.sub(r"[^A-Za-z0-9]+", "-", department or "diet-chart").strip("-").lower() or "diet-chart"
    return f"{dept}-diet-chart.pdf"


def _send_diet_chart_with_template_map(
    *,
    template_map: str,
    phone: str,
    workflow,
    department: str,
    caption: str,
    media_url: str,
    file_name: str,
    task_id: str | None,
    agent: str | None,
) -> dict:
    from confluence_ai.services.whatsapp_templates import send_mapped_whatsapp_template

    message = " ".join(f"{caption} Diet chart PDF: {media_url}".split())
    result = send_mapped_whatsapp_template(
        {
            "template_map": template_map,
            "phone": phone,
            "customer_name": workflow.patient_name,
            "intent": "Information",
            "disease_or_concern": department,
            "message": message,
            "media_url": media_url,
            "file_name": file_name,
            "department": department,
            "workflow": workflow.name,
            "customer_requested": True,
        },
        task_id=task_id,
        agent=agent,
    )
    mapping = frappe.get_doc("AI WhatsApp Template Map", template_map)
    return {
        **result,
        "channel_account": mapping.channel_account,
        "template_name": mapping.template_name,
        "delivery_status": result.get("delivery_status") or (result.get("result") or {}).get("delivery_status"),
    }


def _send_whatsapp_document(
    *,
    phone: str,
    customer_name: str | None,
    channel_account: str,
    body: str,
    media_url: str,
    file_name: str,
    remote_mcp_server: str | None,
    send_method: str | None,
    settings,
    department: str,
    caption: str,
    task_id: str | None,
) -> dict:
    strategy = _clean_text(settings.get("diet_chart_whatsapp_send_strategy") if settings else "") or "Free-form then Template"
    strategy_key = _match_key(strategy)
    if strategy_key == "templatedocument":
        return _send_whatsapp_document_template(
            phone=phone,
            customer_name=customer_name,
            channel_account=channel_account,
            body=body,
            media_url=media_url,
            file_name=file_name,
            remote_mcp_server=remote_mcp_server,
            settings=settings,
            department=department,
            caption=caption,
            task_id=task_id,
        )
    try:
        return _send_whatsapp_document_freeform(
            phone=phone,
            customer_name=customer_name,
            channel_account=channel_account,
            body=body,
            media_url=media_url,
            file_name=file_name,
            remote_mcp_server=remote_mcp_server,
            send_method=send_method,
            task_id=task_id,
        )
    except Exception as exc:
        if strategy_key != "freeformthentemplate" or not _is_whatsapp_window_closed_error(exc):
            raise
        template_result = _send_whatsapp_document_template(
            phone=phone,
            customer_name=customer_name,
            channel_account=channel_account,
            body=body,
            media_url=media_url,
            file_name=file_name,
            remote_mcp_server=remote_mcp_server,
            settings=settings,
            department=department,
            caption=caption,
            task_id=task_id,
        )
        return {"fallback_from": "freeform_document", "fallback_reason": str(exc), **template_result}


def _send_whatsapp_document_freeform(
    *,
    phone: str,
    customer_name: str | None,
    channel_account: str,
    body: str,
    media_url: str,
    file_name: str,
    remote_mcp_server: str | None,
    send_method: str | None,
    task_id: str | None,
) -> dict:
    if remote_mcp_server:
        from confluence_ai.services import whatsapp_templates

        contact = whatsapp_templates._remote_find_or_create_chat_contact(
            remote_mcp_server,
            phone=phone,
            display_name=customer_name or phone,
        )
        conversation = whatsapp_templates._remote_find_or_create_chat_conversation(
            remote_mcp_server,
            channel_account=channel_account,
            contact=contact,
        )
        return whatsapp_templates._remote_frappe_method(
            remote_mcp_server,
            send_method or "wa_chat_hub.api.runtime.send_reply",
            {
                "conversation": conversation,
                "body": body,
                "content_type": "Document",
                "media_url": media_url,
                "display_media_url": media_url,
                "file_name": file_name,
                "sender_type": "AI",
                "source": "confluence_repeat_followup",
                "task": task_id,
            },
        )

    from confluence_ai.services.whatsapp_mcp import send_whatsapp_message

    return send_whatsapp_message(
        {
            "phone_number": phone,
            "customer_name": customer_name or phone,
            "channel_account": channel_account,
            "message": body,
            "content_type": "Document",
            "media_url": media_url,
            "file_name": file_name,
        },
        task_id=task_id,
    )


def _send_whatsapp_document_template(
    *,
    phone: str,
    customer_name: str | None,
    channel_account: str,
    body: str,
    media_url: str,
    file_name: str,
    remote_mcp_server: str | None,
    settings,
    department: str,
    caption: str,
    task_id: str | None,
) -> dict:
    template_name = _clean_text(settings.get("diet_chart_whatsapp_template_name") if settings else "")
    if not template_name:
        frappe.throw("Diet chart WhatsApp template name is required when the messaging window is closed.")
    template_payload = _diet_chart_template_payload(
        settings=settings,
        template_name=template_name,
        customer_name=customer_name,
        phone=phone,
        department=department,
        caption=caption or body,
        media_url=media_url,
        file_name=file_name,
        task_id=task_id,
    )
    if remote_mcp_server:
        from confluence_ai.services import whatsapp_templates

        contact = whatsapp_templates._remote_find_or_create_chat_contact(
            remote_mcp_server,
            phone=phone,
            display_name=customer_name or phone,
        )
        conversation = whatsapp_templates._remote_find_or_create_chat_conversation(
            remote_mcp_server,
            channel_account=channel_account,
            contact=contact,
        )
        template_payload["conversation"] = conversation
        return whatsapp_templates._remote_frappe_method(
            remote_mcp_server,
            settings.get("diet_chart_whatsapp_template_method") or "wa_chat_hub.api.runtime.send_template_message",
            template_payload,
        )

    from confluence_ai.services.whatsapp_mcp import send_whatsapp_message

    return send_whatsapp_message(
        {
            "phone_number": phone,
            "customer_name": customer_name or phone,
            "channel_account": channel_account,
            **template_payload,
        },
        task_id=task_id,
    )


def _diet_chart_template_payload(
    *,
    settings,
    template_name: str,
    customer_name: str | None,
    phone: str,
    department: str,
    caption: str,
    media_url: str,
    file_name: str,
    task_id: str | None,
) -> dict:
    values = {
        "patient_name": customer_name or "",
        "customer_name": customer_name or "",
        "phone": phone or "",
        "customer_phone": phone or "",
        "department": department or "",
        "caption": caption or "",
        "media_url": media_url or "",
        "file_name": file_name or "",
        "task": task_id or "",
    }
    return {
        "template_name": template_name,
        "language_code": settings.get("diet_chart_whatsapp_template_language") or "en",
        "header_values": _render_template_json(settings.get("diet_chart_template_header_values_json") or '["{media_url}"]', values, default=[]),
        "body_values": _render_template_json(settings.get("diet_chart_template_body_values_json") or '["{patient_name}", "{department}"]', values, default=[]),
        "button_values": _render_template_json(settings.get("diet_chart_template_button_values_json") or "{}", values, default={}),
        "message": caption or f"Template: {template_name}",
        "file_name": file_name,
        "callback_data": as_json({"task": task_id, "source": "repeat_followup_diet_chart"}),
    }


def _render_template_json(raw: str, values: dict, *, default):
    try:
        parsed = json.loads(raw) if raw else default
    except Exception:
        parsed = default

    def render(value):
        if isinstance(value, str):
            rendered = value
            for key, replacement in values.items():
                rendered = rendered.replace("{" + key + "}", str(replacement))
            return " ".join(rendered.split())
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, dict):
            return {key: render(val) for key, val in value.items()}
        return value

    return render(parsed)


def _is_whatsapp_window_closed_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return any(token in message for token in ("window_closed", "24 hour", "24-hour", "messaging window", "outside the messaging window", "use an approved template"))


def _store_diet_chart_result(workflow, result: dict) -> None:
    workflow.diet_chart_whatsapp_result_json = as_json(result)
    workflow.diet_chart_summary_json = as_json(_diet_chart_summary(result))
    workflow.save(ignore_permissions=True)
    frappe.db.commit()


def _append_mcp_tool_usage(workflow, tool_name: str, *, task_id: str | None = None, status: str = "success", detail: dict | None = None) -> None:
    try:
        usage = parse_json_object(workflow.mcp_tools_used_json, "MCP Tools Used JSON") if workflow.mcp_tools_used_json else {}
        rows = usage.get("events") if isinstance(usage, dict) else []
        if not isinstance(rows, list):
            rows = []
        item = {
            "tool": tool_name,
            "status": status,
            "task": task_id or "",
            "used_at": frappe.utils.now(),
        }
        if detail:
            item["detail"] = detail
        rows.append(item)
        workflow.mcp_tools_used_json = as_json({"events": rows[-50:]})
        workflow.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:
        create_error("Repeat Follow Up MCP Usage Log", str(exc), source="repeat_followup", task=task_id, agent=None, payload={"workflow": workflow.name, "tool": tool_name}, exc=exc)


def _normalize_payload(payload: dict, settings) -> dict:
    payload = dict(payload or {})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    encounter = _encounter_from_payload(payload)
    phone_paths = _field_names(settings.phone_field_names)
    awb_paths = _field_names(settings.awb_field_names)
    phone = _normalize_phone(
        _first_path(payload, phone_paths)
        or _first_path(body, phone_paths)
        or _first_path(data, phone_paths)
        or _first_path(encounter, phone_paths)
        or encounter.get("sr_pe_mobile")
        or encounter.get("sr_pe_mobile_norm")
        or encounter.get("mobile")
        or encounter.get("phone")
    )
    patient_name = _clean_text(
        payload.get("patient_name")
        or payload.get("customer_name")
        or body.get("patient_name")
        or body.get("customer_name")
        or data.get("patient_name")
        or data.get("customer_name")
        or encounter.get("patient_name")
        or encounter.get("patient")
    )
    encounter_id = _clean_text(
        payload.get("patient_encounter_id")
        or payload.get("encounter_id")
        or body.get("patient_encounter_id")
        or body.get("encounter_id")
        or data.get("patient_encounter_id")
        or data.get("encounter_id")
        or (payload.get("patient_encounter") if isinstance(payload.get("patient_encounter"), str) else None)
        or (body.get("encounter") if isinstance(body.get("encounter"), str) else None)
        or encounter.get("name")
    )
    awb = _clean_text(
        _first_path(payload, awb_paths)
        or _first_path(body, awb_paths)
        or _first_path(data, awb_paths)
        or _first_path(encounter, awb_paths)
        or encounter.get("pe_shipkia_awb_number")
        or encounter.get("awb_number")
        or encounter.get("awb")
    )
    order_id = _clean_text(
        payload.get("order_id")
        or payload.get("external_order_id")
        or body.get("order_id")
        or body.get("external_order_id")
        or data.get("order_id")
        or data.get("external_order_id")
        or encounter.get("pe_shipkia_order_id")
        or encounter.get("order_id")
    )
    department = _clean_text(
        _first_path(payload, _field_names(settings.diet_chart_dept_field_names))
        or _first_path(body, _field_names(settings.diet_chart_dept_field_names))
        or _first_path(data, _field_names(settings.diet_chart_dept_field_names))
        or _first_path(encounter, _field_names(settings.diet_chart_dept_field_names))
        or encounter.get("sr_pe_deptt")
    )
    medicine_summary = _compact_medicine_summary(encounter, _field_names(settings.medicine_summary_field_names))
    order_script = _required_order_script(encounter=encounter, awb=awb, order_id=order_id)
    diet_script = _required_diet_script(department=department)
    return {
        "company": payload.get("company") or body.get("company") or data.get("company") or encounter.get("company") or settings.company or "sriaas",
        "scenario_key": settings.get("scenario_key") or payload.get("scenario_key") or body.get("scenario_key") or data.get("scenario_key") or "",
        "phone": phone,
        "patient_name": patient_name,
        "patient_department": department,
        "medicine_summary": medicine_summary,
        "required_order_script": order_script,
        "required_medicine_script": medicine_summary.get("required_medicine_script") if isinstance(medicine_summary, dict) else "",
        "required_diet_script": diet_script,
        "strict_followup_script": _strict_followup_script(order_script, medicine_summary, diet_script),
        "encounter_id": encounter_id,
        "awb_number": awb,
        "order_id": order_id,
        "voice_channel_account": settings.get("voice_channel_account") or "",
        "livekit_channel_account_fallback": settings.get("livekit_channel_account_fallback") or "",
        "encounter": encounter,
        "payload": payload,
    }


def _encounter_from_payload(payload: dict) -> dict:
    for key in ("patient_encounter", "encounter", "patient_encounter_data"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip().startswith("{"):
            return parse_json_object(value, f"{key} JSON")
    if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("patient_encounter"), dict):
        return payload["data"]["patient_encounter"]
    if isinstance(payload.get("body"), dict):
        body = payload["body"]
        for key in ("patient_encounter", "encounter", "patient_encounter_data"):
            value = body.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip().startswith("{"):
                return parse_json_object(value, f"body.{key} JSON")
    return {}


def _voice_bootstrap_context(workflow, context: dict) -> dict:
    state = context.get(STATE_MACHINE_CONTEXT_KEY) or (
        (parse_json_object(workflow.context_json, "Workflow Context JSON") or {}).get(STATE_MACHINE_CONTEXT_KEY)
        if workflow and workflow.context_json
        else {}
    )
    active_step = _active_step(state) if isinstance(state, dict) else {}
    medicine_summary = context.get("medicine_summary") or (
        parse_json_object(workflow.medicine_summary_json, "Medicine Summary JSON") if workflow and workflow.medicine_summary_json else {}
    )
    diet_summary = context.get("diet_chart_summary") or _diet_chart_summary(
        parse_json_object(workflow.diet_chart_summary_json, "Diet Chart Summary JSON") if workflow and workflow.diet_chart_summary_json else {}
    )
    tracking_summary = context.get("tracking_summary") or (
        _tracking_summary(parse_json_object(workflow.shipkia_result_json, "Shipkia Result JSON")) if workflow and workflow.shipkia_result_json else {}
    )
    order_script = _required_order_script(
        encounter=parse_json_object(workflow.encounter_json, "Encounter JSON") if workflow and workflow.encounter_json else {},
        awb=(workflow.awb_number if workflow else context.get("awb_number")),
        order_id=(workflow.order_id if workflow else context.get("order_id")),
        tracking_summary=tracking_summary,
    )
    diet_script = _required_diet_script(
        department=context.get("patient_department") or (workflow.diet_chart_dept if workflow else ""),
        diet_summary=diet_summary,
    )
    medicine_script = context.get("required_medicine_script") or (medicine_summary.get("required_medicine_script") if isinstance(medicine_summary, dict) else "")
    return {
        "event": "repeat_followup",
        "simple_followup_mode": 1,
        "workflow": workflow.name if workflow else context.get("workflow"),
        "company": context.get("company") or (workflow.company if workflow else "sriaas"),
        "scenario_key": context.get("scenario_key") or (workflow.scenario_key if workflow else ""),
        "patient_name": context.get("patient_name") or (workflow.patient_name if workflow else ""),
        "customer_name": context.get("patient_name") or (workflow.patient_name if workflow else ""),
        "patient_department": context.get("patient_department") or (workflow.diet_chart_dept if workflow else ""),
        "phone": context.get("phone") or (workflow.patient_mobile if workflow else ""),
        "customer_phone": context.get("phone") or (workflow.patient_mobile if workflow else ""),
        "patient_encounter": context.get("encounter_id") or (workflow.patient_encounter if workflow else ""),
        "awb_number": context.get("awb_number") or (workflow.awb_number if workflow else ""),
        "order_id": context.get("order_id") or (workflow.order_id if workflow else ""),
        "tracking_summary": tracking_summary,
        "medicine_summary": medicine_summary,
        "required_order_script": order_script,
        "required_medicine_script": medicine_script,
        "required_diet_script": diet_script,
        "strict_followup_script": _strict_followup_script(order_script, medicine_summary, diet_script),
        "simple_followup_script": _simple_followup_script(order_script, medicine_summary, diet_script),
        "diet_chart_summary": diet_summary,
        "radha_runtime_version": RADHA_RUNTIME_VERSION,
        "active_stage_id": "SIMPLE_FOLLOWUP",
        "active_stage_name": "Simple Agent 1 delivery medicine diet flow",
        "stage_sequence": "DELIVERY -> MEDICINE -> DIET -> CLOSE",
        "next_stage_after_order": "MEDICINE_EXPLANATION",
        "next_stage_after_medicine": "DIET_EXPLANATION",
        "next_stage_after_diet": "OUTCOME_CLOSE",
        "current_journey_stage": ((state.get("journey") or {}).get("active_stage_key") if isinstance(state, dict) else "") or context.get("current_journey_stage") or "AGENT_1",
        "current_step_key": active_step.get("step_key") or context.get("current_step_key") or "",
        "current_step_label": active_step.get("step_label") or context.get("current_step_label") or "",
        "current_speech_unit": active_step.get("speech_unit") or context.get("current_speech_unit") or "",
        "current_rag_filters": active_step.get("rag_filters") or context.get("current_rag_filters") or {},
        "state_machine_required": 0,
        "stage_prompt_loading_required": 0,
        "voice_channel_account": context.get("voice_channel_account") or (workflow.voice_channel_account if workflow else ""),
        "livekit_channel_account_fallback": context.get("livekit_channel_account_fallback") or (workflow.livekit_channel_account_fallback if workflow else ""),
        "repeat_followup_compacted": 1,
        "full_encounter_available_via_tool": 1,
    }


def _workflow_context(workflow) -> dict:
    context = parse_json_object(workflow.context_json, "Workflow Context JSON")
    diet_summary = _diet_chart_summary(parse_json_object(workflow.diet_chart_summary_json, "Diet Chart Summary JSON") if workflow.diet_chart_summary_json else {})
    patient_department = workflow.diet_chart_dept or _clean_text(
        _first_path(
            parse_json_object(workflow.encounter_json, "Encounter JSON") if workflow.encounter_json else {},
            _field_names(_workflow_settings(workflow).diet_chart_dept_field_names),
        )
    )
    context.update(
        {
            "workflow": workflow.name,
            "company": workflow.company,
            "scenario_key": workflow.scenario_key,
            "patient_name": workflow.patient_name,
            "customer_name": workflow.patient_name,
            "patient_department": patient_department,
            "phone": workflow.patient_mobile,
            "customer_phone": workflow.patient_mobile,
            "patient_encounter": workflow.patient_encounter,
            "awb_number": workflow.awb_number,
            "order_id": workflow.order_id,
            "tracking_summary": _tracking_summary(parse_json_object(workflow.shipkia_result_json, "Shipkia Result JSON") if workflow.shipkia_result_json else {}),
            "medicine_summary": parse_json_object(workflow.medicine_summary_json, "Medicine Summary JSON") if workflow.medicine_summary_json else {},
            "diet_chart_summary": diet_summary,
            "required_diet_script": _required_diet_script(department=patient_department, diet_summary=diet_summary),
            "voice_channel_account": workflow.voice_channel_account,
            "livekit_channel_account_fallback": workflow.livekit_channel_account_fallback,
        }
    )
    return context


def _field_names(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _first_path(source: dict, paths: list[str]) -> Any:
    for path in paths:
        value = _get_path(source, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _get_path(source: dict, path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _idempotency_key(context: dict, settings) -> str | None:
    value = _first_path(context["payload"], _field_names(settings.idempotency_key_field_names))
    if value is None:
        value = context.get("encounter_id")
    if not value:
        return None
    return f"repeat-followup:{_clean_text(value)}"


def _workflow_for_tool(arguments: dict, task_id: str | None):
    workflow_name = arguments.get("workflow") or arguments.get("workflow_name")
    if workflow_name and frappe.db.exists(WORKFLOW, workflow_name):
        return frappe.get_doc(WORKFLOW, workflow_name)
    if task_id and frappe.db.exists("AI Task", task_id):
        task = frappe.get_doc("AI Task", task_id)
        if task.external_record_type == WORKFLOW and task.external_record_id and frappe.db.exists(WORKFLOW, task.external_record_id):
            return frappe.get_doc(WORKFLOW, task.external_record_id)
    frappe.throw("Active repeat follow-up workflow was not found for this task.")


def _find_workflow(workflow: str | None = None, task: str | None = None):
    if workflow and frappe.db.exists(WORKFLOW, workflow):
        return frappe.get_doc(WORKFLOW, workflow)
    if task:
        name = frappe.db.get_value(WORKFLOW, {"voice_task": task}, "name") or frappe.db.get_value(WORKFLOW, {"agent_2_task": task}, "name")
        return frappe.get_doc(WORKFLOW, name) if name else None
    return None


def _compact_shipkia_result(raw: dict, *, awb: str, status_code: int, ok: bool) -> dict:
    if not ok:
        return {"status": "error", "tracking_id": awb, "status_code": status_code, "message": str(raw)[:500]}
    if not raw.get("success"):
        return {"status": "not_found", "tracking_id": awb, "message": raw.get("message") or "Tracking details were not found."}
    result = raw.get("result") or {}
    details = result.get("order_details") or {}
    timeline = result.get("shipment_timeline") or []
    latest = timeline[-1] if timeline else {}
    delivered_on = result.get("delivered_on") or ""
    if not delivered_on:
        delivered = next((row for row in timeline if row.get("status") == "Delivered"), None)
        delivered_on = (delivered or {}).get("date_time") or ""
    return {
        "status": "success",
        "tracking_id": result.get("tracking_id") or awb,
        "awb_number": details.get("awb_number") or awb,
        "shipment_status": result.get("status"),
        "estimated_delivery": result.get("estimated_delivery") or "",
        "delivered_on": delivered_on,
        "delivery_partner": details.get("delivery_partner") or "",
        "delivery_location": details.get("delivery_location") or "",
        "latest_status": latest.get("status") or result.get("status") or "",
        "latest_detail": latest.get("detail") or "",
        "latest_location": latest.get("location") or "",
        "latest_time": latest.get("date_time") or "",
    }


def _workflow_voice_trunk_id(workflow, settings=None) -> str | None:
    channel_account = (
        workflow.get("voice_channel_account")
        or workflow.get("livekit_channel_account_fallback")
        or (settings.get("livekit_channel_account_fallback") if settings else None)
    )
    if not channel_account or not frappe.db.exists("AI Channel Account", channel_account):
        return None
    return frappe.db.get_value("AI Channel Account", channel_account, "trunk_id")


def _stale_voice_workflows(now_value) -> list[str]:
    return frappe.db.sql(
        """
        select workflow.name
        from `tabAI Repeat Follow Up Workflow` workflow
        inner join `tabAI Task` task on task.name = workflow.voice_task
        where workflow.status in ('Call Queued', 'Call Running')
            and workflow.voice_task is not null
            and workflow.voice_task != ''
            and task.channel = 'Voice'
            and task.status in ('Queued', 'Waiting', 'Running')
            and task.modified <= date_sub(%s, interval coalesce(nullif(workflow.voice_call_timeout_minutes, 0), 5) minute)
        order by workflow.modified asc
        limit 200
        """,
        now_value,
        pluck=True,
    ) or []


def _missed_voice_task_workflows() -> list[str]:
    return frappe.db.sql(
        """
        select workflow.name
        from `tabAI Repeat Follow Up Workflow` workflow
        inner join `tabAI Task` task on task.name = workflow.voice_task
        where workflow.status in ('Call Queued', 'Call Running')
            and task.channel = 'Voice'
            and task.status = 'Deadline Missed'
        order by workflow.modified asc
        limit 200
        """,
        pluck=True,
    ) or []


def _mark_voice_task_missed(task_name: str | None) -> None:
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return
    frappe.db.set_value(
        "AI Task",
        task_name,
        {"status": "Deadline Missed", "last_error": "Repeat follow-up call timed out or was missed."},
        update_modified=True,
    )
    attempt_name = frappe.db.get_value("AI Task Attempt", {"task": task_name, "status": "Started"}, "name", order_by="creation desc")
    if attempt_name:
        frappe.db.set_value(
            "AI Task Attempt",
            attempt_name,
            {"status": "Failed", "error_message": "Repeat follow-up call timed out or was missed.", "ended_at": frappe.utils.now()},
            update_modified=True,
        )


def _mark_failed(name: str, exc: Exception) -> None:
    doc = frappe.get_doc(WORKFLOW, name)
    doc.status = "Failed"
    doc.last_error = str(exc)
    doc.save(ignore_permissions=True)
    create_error("AI Repeat Follow Up Workflow", str(exc), source="repeat_followup", payload={"workflow": name}, exc=exc)


def _ensure_task_template() -> str:
    existing = _template_by_key("repeat_followup_voice")
    if existing:
        return existing
    doc = frappe.new_doc("AI Task Template")
    doc.update(
        {
            "enabled": 1,
            "company": "sriaas",
            "template_key": "repeat_followup_voice",
            "template_name": "Repeat Follow Up Voice",
            "task_type": "Repeat Follow Up",
            "objective_prompt": "Call the customer for configurable repeat follow-up.",
            "default_channel": "Voice",
            "default_priority": "High",
            "default_timeout_seconds": 900,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_agent_1() -> str:
    existing = _agent_by_label(DEFAULT_AGENT_1_NAME)
    if existing:
        try:
            doc = frappe.get_doc("AI Agent", existing)
            changed = False
            prompt = doc.system_prompt or ""
            if (
                "RADHA_REPEAT_AGENT1_STATE_LOCK_V5" not in prompt
                or "RADHA_REPEAT_AGENT1_MULTISTAGE_V4" not in prompt
                or "get_repeat_encounter_full_data" not in prompt
            ):
                doc.system_prompt = _default_agent_prompt()
                changed = True
            if doc.get("agent_type") != "Multi-Stage State Machine":
                doc.agent_type = "Multi-Stage State Machine"
                changed = True
            changed = _sync_agent_1_stage_prompts(doc) or changed
            if not doc.get("audio_name"):
                doc.audio_name = "Kore"
                changed = True
            if changed:
                doc.save(ignore_permissions=True)
        except Exception as exc:
            create_error("Repeat Follow Up Agent Update", str(exc), source="repeat_followup", payload={"agent": existing}, exc=exc)
        return existing
    doc = frappe.new_doc("AI Agent")
    doc.update(
        {
            "enabled": 1,
            "company": "sriaas",
            "agent_name": DEFAULT_AGENT_1_NAME,
            "personality": "Warm, practical sriaas follow-up voice assistant.",
            "system_prompt": _default_agent_prompt(),
            "language": "Hinglish",
            "audio_name": "Kore",
            "primary_provider": "Gemini",
            "fallback_provider": "OpenAI",
            "agent_type": "Multi-Stage State Machine",
            "max_concurrency": 5,
        }
    )
    _sync_agent_1_stage_prompts(doc)
    doc.insert(ignore_permissions=True)
    return doc.name


def _sync_agent_1_stage_prompts(doc) -> bool:
    """Keep Agent 1 stage prompts deterministic and editable in the Agent UI.

    The important change here is that the realtime worker receives only the
    active stage prompt at first. Later stages are loaded on demand, so Gemini
    is not tempted to jump from order to medicine/diet.
    """
    desired = _agent_1_stage_prompts()
    existing = [
        {
            "stage_id": row.stage_id,
            "stage_name": row.stage_name,
            "is_orchestrator": int(row.is_orchestrator or 0),
            "system_prompt": row.system_prompt or "",
        }
        for row in (doc.get("stage_prompts") or [])
    ]
    if existing == desired:
        return False
    doc.set("stage_prompts", [])
    for row in desired:
        doc.append("stage_prompts", row)
    return True


def _agent_1_stage_prompts() -> list[dict]:
    return [
        {
            "stage_id": "ORDER_STATUS",
            "stage_name": "1. Order delivery status only",
            "is_orchestrator": 0,
            "system_prompt": _agent_1_order_stage_prompt(),
        },
        {
            "stage_id": "MEDICINE_EXPLANATION",
            "stage_name": "2. Full medicine explanation",
            "is_orchestrator": 0,
            "system_prompt": _agent_1_medicine_stage_prompt(),
        },
        {
            "stage_id": "DIET_EXPLANATION",
            "stage_name": "3. Diet chart explanation",
            "is_orchestrator": 0,
            "system_prompt": _agent_1_diet_stage_prompt(),
        },
        {
            "stage_id": "OUTCOME_CLOSE",
            "stage_name": "4. Outcome and safe close",
            "is_orchestrator": 0,
            "system_prompt": _agent_1_close_stage_prompt(),
        },
    ]


def _ensure_knowledge_document(agent_name: str) -> str | None:
    existing = frappe.db.get_value("AI Knowledge Document", {"title": DEFAULT_KB_TITLE, "company": "sriaas"}, "name")
    if existing:
        try:
            doc = frappe.get_doc("AI Knowledge Document", existing)
            if "## Medicine And Diet Questions" not in (doc.content or ""):
                doc.content = ((doc.content or "").rstrip() + "\n\n" + _diet_chart_knowledge_addendum()).strip()
                doc.save(ignore_permissions=True)
        except Exception as exc:
            create_error("Repeat Follow Up Knowledge Update", str(exc), source="repeat_followup", payload={"knowledge_document": existing}, exc=exc)
        return existing
    if not frappe.db.exists("DocType", "AI Knowledge Document"):
        return None
    doc = frappe.new_doc("AI Knowledge Document")
    doc.update(
        {
            "enabled": 1,
            "company": "sriaas",
            "title": DEFAULT_KB_TITLE,
            "status": "Published",
            "source_type": "Manual",
            "tags": "repeat follow up, medicine delivery, shipkia, sriaas",
            "content": _default_knowledge_content(),
            "chunk_size": 1200,
            "chunk_overlap": 150,
        }
    )
    if agent_name and frappe.db.exists("AI Agent", agent_name):
        doc.append("agent_visibility", {"agent": agent_name})
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_tools() -> list[str]:
    tools = [
        (
            "get_repeat_workflow_state",
            "Fetch the deterministic multi-call journey and current Agent 1 step state for the active repeat follow-up task.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "get_current_required_step",
            "Fetch the current unlocked Agent 1 step. Required steps and medicine items must be completed in order.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "get_current_speech_unit",
            "Fetch the current step's exact speech unit, variables, tool requirement and RAG filters.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "mark_repeat_step_complete",
            "Mark the active repeat-follow-up step complete and unlock the next step. Out-of-order completion is blocked.",
            [
                ("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used."),
                ("step_key", "string", 0, "The active step key being completed."),
                (
                    "structured_details",
                    "object",
                    0,
                    "Step-specific facts. For medicine_item steps include medicine_name, spoken_text, medicine_name_spoken, dose_spoken, timing_or_instruction_spoken, and period_spoken.",
                ),
            ],
        ),
        (
            "mark_repeat_step_interrupted",
            "Record that the patient interrupted the active step. The same step remains pending and must be resumed.",
            [
                ("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used."),
                ("patient_text", "string", 0, "What the patient said during interruption."),
            ],
        ),
        (
            "resume_repeat_pending_step",
            "Resume the same pending step after an interruption without advancing the state machine.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "get_repeat_encounter_full_data",
            "Fetch the full Patient Encounter payload stored for the active repeat follow-up workflow.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "get_repeat_medicine_list",
            "Fetch the exact current Patient Encounter drug_prescription medicine list for the active repeat follow-up task. Use before speaking medicine names or dosage.",
            [("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used.")],
        ),
        (
            "verify_repeat_medicine_in_prescription",
            "Verify whether a customer-mentioned medicine exists in the active workflow drug_prescription list. Use for questions like 'Neuro M Oil milega?'.",
            [
                ("medicine_name", "string", 1, "Medicine name or partial name mentioned by the customer."),
                ("workflow", "string", 0, "Optional workflow id. Usually omit; active task scope is used."),
            ],
        ),
        (
            "get_shipkia_tracking_status",
            "Fetch customer-safe Shipkia shipment status by AWB/tracking id for the active repeat follow-up workflow.",
            [("awb_number", "string", 0, "Optional AWB. If omitted, the workflow encounter AWB is used.")],
        ),
        (
            "send_repeat_diet_chart_whatsapp",
            "Send the diet chart PDF that matches the active workflow Patient Encounter department (sr_pe_deptt) on WhatsApp.",
            [
                ("department", "string", 0, "Optional department. If omitted, sr_pe_deptt from the full encounter is used."),
                ("phone", "string", 0, "Optional customer phone. If omitted, workflow phone is used."),
                ("caption", "string", 0, "Optional customer-facing WhatsApp caption."),
                ("channel_account", "string", 0, "Optional WhatsApp channel account override."),
                ("template_map", "string", 0, "Optional AI WhatsApp Template Map. If omitted, workflow/template map config is used."),
            ],
        ),
        (
            "send_mapped_whatsapp_template",
            "Shared WhatsApp template sender. Repeat follow-up uses this only after explicit customer request and with a complete customer-facing message.",
            [
                ("message", "string", 1, "Complete customer-facing message to place in the approved template variable."),
                ("customer_requested", "boolean", 1, "True only when the customer explicitly asked or agreed to receive WhatsApp."),
                ("phone", "string", 0, "Optional customer phone. If omitted, task/workflow context is used."),
                ("template_map", "string", 0, "Optional AI WhatsApp Template Map such as the configured generic map."),
            ],
        ),
        (
            "log_repeat_followup_outcome",
            "Log flexible repeat follow-up outcome, summary, next action and structured details before closing the call.",
            [
                ("primary_outcome", "string", 1, "Main call outcome, e.g. medicine_received, not_received, unclear."),
                ("sub_outcome", "string", 0, "Optional detailed outcome."),
                ("customer_summary", "string", 1, "Short customer-facing summary of the conversation."),
                ("agent_notes", "string", 0, "Internal notes for the team."),
                ("next_action", "string", 0, "Next action to take."),
                ("structured_details", "object", 0, "Flexible structured details for future Agent 1 tasks."),
                ("real_conversation", "boolean", 0, "True if the customer actually spoke or gave a usable answer."),
            ],
        ),
    ]
    docnames = []
    for tool_name, description, params in tools:
        docname = frappe.db.get_value("AI MCP Tool", {"tool_name": tool_name}, "name")
        if docname:
            tool = frappe.get_doc("AI MCP Tool", docname)
            changed = False
            if not tool.enabled:
                tool.enabled = 1
                changed = True
            if not tool.company:
                tool.company = "sriaas"
                changed = True
            if tool.description != description:
                tool.description = description
                changed = True
            existing_params = {
                row.parameter_name: row
                for row in (tool.get("input_parameters") or [])
                if row.get("parameter_name")
            }
            desired_names = []
            for parameter_name, typ, required, param_description in params:
                desired_names.append(parameter_name)
                row = existing_params.get(parameter_name)
                if row:
                    if row.type != typ:
                        row.type = typ
                        changed = True
                    if int(row.required or 0) != int(required or 0):
                        row.required = required
                        changed = True
                    if row.description != param_description:
                        row.description = param_description
                        changed = True
                else:
                    tool.append(
                        "input_parameters",
                        {
                            "parameter_name": parameter_name,
                            "type": typ,
                            "required": required,
                            "description": param_description,
                        },
                    )
                    changed = True
            for row in list(tool.get("input_parameters") or []):
                if row.get("parameter_name") not in desired_names:
                    tool.remove(row)
                    changed = True
            if changed:
                tool.save(ignore_permissions=True)
            if tool_name in REPEAT_MCP_TOOL_NAMES:
                docnames.append(tool.name)
            continue
        tool = frappe.new_doc("AI MCP Tool")
        tool.update({"enabled": 1, "company": "sriaas", "tool_name": tool_name, "description": description, "operation_type": "Read"})
        for parameter_name, typ, required, param_description in params:
            tool.append(
                "input_parameters",
                {
                    "parameter_name": parameter_name,
                    "type": typ,
                    "required": required,
                    "description": param_description,
                },
            )
        tool.insert(ignore_permissions=True)
        if tool_name in REPEAT_MCP_TOOL_NAMES:
            docnames.append(tool.name)
    return docnames


def _attach_agent_tools(agent_name: str, tool_docnames: list[str]) -> None:
    if not agent_name or not frappe.db.exists("AI Agent", agent_name):
        return
    agent = frappe.get_doc("AI Agent", agent_name)
    desired_tool_names = set(REPEAT_MCP_TOOL_NAMES)
    desired_docnames = set(tool_docnames)
    existing_rows = []
    for row in agent.get("allowed_mcp_tools") or []:
        tool_name = frappe.db.get_value("AI MCP Tool", row.tool, "tool_name") if row.tool else ""
        if row.tool in desired_docnames or (tool_name and tool_name not in _repeat_tool_name_set()):
            existing_rows.append(row)
    if len(existing_rows) != len(agent.get("allowed_mcp_tools") or []):
        agent.set("allowed_mcp_tools", existing_rows)
    existing = {row.tool for row in agent.get("allowed_mcp_tools") or [] if row.tool}
    changed = False
    for tool in tool_docnames:
        if tool not in existing:
            agent.append("allowed_mcp_tools", {"tool": tool, "calling_condition": _tool_condition(tool)})
            changed = True
    tool_names = frappe.get_all("AI MCP Tool", filters={"name": ["in", list(desired_docnames)]}, pluck="tool_name")
    tool_names = [name for name in tool_names if name in desired_tool_names]
    agent.allowed_tools = ",".join(tool_names)
    changed = True
    if changed:
        agent.save(ignore_permissions=True)


def _repeat_tool_name_set() -> set[str]:
    return {
        "get_repeat_workflow_state",
        "get_current_required_step",
        "get_current_speech_unit",
        "mark_repeat_step_complete",
        "mark_repeat_step_interrupted",
        "resume_repeat_pending_step",
        "get_repeat_encounter_full_data",
        "get_repeat_medicine_list",
        "verify_repeat_medicine_in_prescription",
        "get_shipkia_tracking_status",
        "send_repeat_diet_chart_whatsapp",
        "trigger_repeat_renewal_n8n",
        "log_repeat_followup_outcome",
    }


def _tool_condition(tool_docname: str) -> str:
    tool_name = frappe.db.get_value("AI MCP Tool", tool_docname, "tool_name") or ""
    if tool_name == "get_repeat_workflow_state":
        return "Call near the start to understand the current journey/stage state. Do not use it as patient speech."
    if tool_name == "get_current_required_step":
        return "Call before deciding what to say next. The returned active step is mandatory."
    if tool_name == "get_current_speech_unit":
        return "Call before speaking each controlled step. Speak only the returned speech unit and relevant retrieved content."
    if tool_name == "mark_repeat_step_complete":
        return "Call only after the active step has truly been spoken/handled. Never complete a future step out of order."
    if tool_name == "mark_repeat_step_interrupted":
        return "Call when the patient interrupts or changes topic before the current required step is complete."
    if tool_name == "resume_repeat_pending_step":
        return "Call after answering a brief interruption so the same pending step continues."
    if tool_name == "get_repeat_encounter_full_data":
        return "Call once near the start before discussing detailed repeat follow-up context."
    if tool_name == "get_repeat_medicine_list":
        return "Mandatory before speaking medicine names/dosage in Repeat Follow Up Agent 1. Use this as the only medicine source of truth."
    if tool_name == "verify_repeat_medicine_in_prescription":
        return "Mandatory when the customer asks whether a specific medicine is included. Never answer from memory."
    if tool_name == "get_shipkia_tracking_status":
        return "Call when the customer says the medicine/order has not arrived or asks delivery status."
    if tool_name == "send_repeat_diet_chart_whatsapp":
        return "Call when the customer asks for a diet chart, says they did not receive the diet chart, or asks for food/diet guidance that should be sent as PDF. Match using sr_pe_deptt from the full encounter."
    if tool_name == "send_mapped_whatsapp_template":
        return "Call only after explicit WhatsApp request/consent. Pass customer_requested=true and a complete medicine/order message; never pass a placeholder."
    if tool_name == "trigger_repeat_renewal_n8n":
        return "Call only after the patient says medicine is khatam/empty/khatam hone wali and clearly agrees to repeat order. This triggers the configured Renewal Webhook URL."
    if tool_name == "log_repeat_followup_outcome":
        return "Mandatory before closing any real conversation."
    return "Use only when needed for the repeat follow-up flow."


def _template_by_key(key: str) -> str | None:
    return frappe.db.get_value("AI Task Template", {"template_key": key}, "name")


def _agent_by_label(label: str) -> str | None:
    return frappe.db.get_value("AI Agent", {"agent_name": label}, "name")


def _workflow_lock(workflow_name: str):
    return filelock(f"repeat_followup_workflow_{workflow_name}", timeout=60)


def _normalize_phone(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) >= 10 and text.startswith("+"):
        return f"+{digits}"
    return text


def _clean_text(value: Any) -> str:
    if value in (None, [], {}):
        return ""
    return " ".join(str(value).strip().split())


def _normalize_outcome(value: Any) -> str:
    return _clean_text(value).lower().replace("-", "_").replace(" ", "_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "haan", "ha"}


def _password_value(doc, fieldname: str) -> str:
    if not doc:
        return ""
    try:
        if hasattr(doc, "get_password"):
            value = doc.get_password(fieldname)
            if value:
                return str(value)
    except Exception:
        pass
    return _clean_text(doc.get(fieldname))


def _is_valid_http_header_name(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$", value or ""))


def _diet_chart_prompt_addendum() -> str:
    return """
Additional medicine/diet guidance:
- If the customer asks about medicine use, precautions, basic diet, or general instructions, answer from the configured sriaas Knowledge Documents/Chunks. Keep it safe and practical; for medical uncertainty, ask them to follow the doctor's prescription/team guidance.
- For this Agent 1 version, do not send WhatsApp. Explain the matched diet chart content in the call. The matching diet chart is selected from sr_pe_deptt in the full Patient Encounter.
""".strip()


def _diet_chart_knowledge_addendum() -> str:
    return """
## Medicine And Diet Questions
If the customer asks about medicine or how to take it, answer only from the uploaded sriaas knowledge. Keep the answer short and remind the customer to follow the prescription/doctor instructions for dosage or medical doubts.

For diet guidance, use the active Patient Encounter department field `sr_pe_deptt` to select the matching Knowledge Document. In this Agent 1 version, explain the chart content in the call and do not send WhatsApp.
""".strip()


def _default_agent_prompt() -> str:
    return """
RADHA_REPEAT_AGENT1_SIMPLE_STRAIGHT_V1
Compatibility marker: RADHA_REPEAT_AGENT1_STATE_LOCK_V5 RADHA_REPEAT_AGENT1_MULTISTAGE_V4

You are Radha, a warm female sriaas treatment-support voice agent.

Your job is simple and straight:
1. Ask whether the customer received the medicine package.
2. If received, acknowledge and go to medicines. If not received or customer asks location/date, explain order status from the provided order context.
3. Explain every medicine from the provided Patient Encounter medicine script in order.
4. Explain diet from the provided diet script.
5. Ask for any doubt and close politely.

Use only the provided context:
- required_order_script
- required_medicine_script
- required_diet_script
- simple_followup_script
- tracking_summary
- medicine_summary

Voice rules:
- Speak natural Hindi/Hinglish. Radha is female: say "bol rahi hoon", "samjha deti hoon", "kar deti hoon".
- Never say "raha hoon".
- Do not sound like field reading. Do not say labels like JSON, tool, stage, prompt, metadata, or RAG.
- Do not ask permission before medicine or diet explanation. Continue naturally.
- Do not jump from order to diet. Medicine always comes before diet.
- If customer interrupts, answer that exact question briefly, then continue the same unfinished section.
- If customer says "haan", "ok", "hmm", continue with the next missing point.
- Do not offer WhatsApp proactively in this Agent 1 version.

Medicine safety:
- Medicine names, dose, timing/instruction, and period must come only from required_medicine_script / medicine_summary.
- Tell total medicine count before details.
- Speak every medicine in order.
- Never invent medicine names.
- If customer asks about a medicine name, answer from the current medicine_summary only. If not visible, say team will verify.

Diet safety:
- Use required_diet_script only.
- Mention specific allowed foods and specific parhej/avoid foods.
- If customer asks about a food, answer from required_diet_script if visible. If unsure, say team will verify; do not guess.

Do not call state/progress tools during the call. Speak the flow naturally.
""".strip()


def _agent_1_order_stage_prompt() -> str:
    return """
## ACTIVE STAGE: ORDER_STATUS

You are currently in the order/delivery stage only.
Do not explain medicines yet. Do not explain diet yet.

Start:
- Greet briefly as Radha from sriaas treatment-support team.
- Confirm you are speaking with {{ patient_name or customer_name or "aap" }}.
- Ask: "Aapko medicine/order receive ho gaya hai?"

If customer says received:
- Acknowledge.
- This stage is complete. Move to MEDICINE_EXPLANATION by loading that stage prompt.

If customer says not received, asks "kahan pahucha", "kab aayega", "location", "tracking":
- Use this order/tracking context first:
{{ required_order_script }}
- If tracking_summary is missing/incomplete and get_shipkia_tracking_status is available, call it with AWB {{ awb_number }}.
- Explain in Hindi/Hinglish clearly: current status, latest location/detail, expected delivery if available, partner/courier if available.
- Translate status naturally. Example: "In Transit" = "order nikal chuka hai / raste mein hai".
- If tracking is unavailable, say: "Mujhe abhi exact tracking confirmation nahi dikh rahi, main delivery team ko check karne ke liye note kar deti hoon."
- Then this stage is complete. Move to MEDICINE_EXPLANATION by loading that stage prompt.

Interruption rule:
- If customer interrupts during order explanation, answer that order-related question.
- If it is not order-related, answer in one short line, then return to order status.
- Do not move to medicine until order received/not-received/tracking fallback has been handled.
""".strip()


def _agent_1_medicine_stage_prompt() -> str:
    return """
## ACTIVE STAGE: MEDICINE_EXPLANATION

You are currently in the medicine explanation stage only.
Do not discuss diet until this stage is fully complete.

Mandatory source:
Use the exact medicine list below. It is generated from Patient Encounter drug_prescription. Do not invent, summarize, merge, or skip.
If get_active_repeat_medicine_list or get_repeat_medicine_list is available, call it before speaking medicine names. Use the returned list as final source of truth.
If customer asks about a specific medicine name, call verify_active_repeat_medicine or verify_repeat_medicine_in_prescription before answering.
Never use any sales/liver KB medicine names in this repeat follow-up medicine stage.

{{ required_medicine_script }}

Required speaking order:
1. First tell total count: "Aapko total <count> medicines milengi. Main ek-ek karke kaise leni hai samjha deti hoon."
2. Explain every medicine in the same order as the list.
3. For each medicine, speak:
   - medicine name
   - form/type if present
   - dose/timing/how to take if present
   - instruction such as before/after food if present
   - period/duration if present
4. In state-machine mode, speak only one medicine item per assistant turn, then call mark_repeat_step_complete for that exact medicine step.
5. If a detail is missing, say only for that medicine: "Iski exact instruction prescription mein clear nahi dikh rahi, team verify kar degi." Do not guess.

Non-skipping rule:
- You must maintain internal medicine_index.
- Never jump from medicine 1 to medicine 5.
- Never move to DIET_EXPLANATION until every medicine name in the list has been spoken with its available dosage/timing/period.
- If customer says "haan", "ok", "hmm", continue with the next unspoken medicine; do not treat it as permission to jump.

Interruption rule:
- If customer interrupts with a clear question, answer briefly.
- Then say naturally: "Ji, ab main wahi medicine continue karti hoon jahan se baat ruk gayi thi."
- Resume from the same medicine if it was not fully spoken, otherwise next unspoken medicine.
- If customer says random/filler words, do not change topic. Continue the same medicine explanation.

WhatsApp request:
- If the customer asks to receive medicine details on WhatsApp, use send_mapped_whatsapp_template after explicit request.
- Pass customer_requested=true.
- Pass message as complete customer-facing medicine details with all medicines from get_repeat_medicine_list/current prescription.
- Do not say WhatsApp cannot be sent just because the call version previously did not send diet charts.

Completion condition:
- This stage is complete only after all medicines from drug_prescription have been explained.
- Then move to DIET_EXPLANATION by loading that stage prompt.
""".strip()


def _agent_1_diet_stage_prompt() -> str:
    return """
## ACTIVE STAGE: DIET_EXPLANATION

You are currently in the diet chart explanation stage only.

Mandatory source:
Use only the matched diet chart content below. It is selected from sr_pe_deptt / patient department.

{{ required_diet_script }}

Required speaking behavior:
- Explain diet directly. Do not ask "kya main diet bataun?"
- Say: "Ab main aapko diet ke baare mein samjha deti hoon."
- Clearly explain:
  1. kya kha sakte hain, with specific food names from the chart
  2. kya avoid/parhej karna hai, with specific food names from the chart
  3. important routine/timing/quantity rules if chart contains them
- Do not use generic words like "fruits kha lijiye" unless you also name the specific allowed fruits from the chart.
- If chart content is missing or does not match the patient's department, say: "Aapke department ka correct diet chart mujhe abhi clear nahi dikh raha, main team ko verify karne ke liye note kar deti hoon." Do not use another department's chart.

Interruption rule:
- If customer interrupts, answer that question briefly from the diet chart if possible.
- Then continue the diet explanation from the same unfinished diet point.

Completion condition:
- This stage is complete only after allowed foods and avoid/parhej foods have both been explained.
- Then move to OUTCOME_CLOSE by loading that stage prompt.
""".strip()


def _agent_1_close_stage_prompt() -> str:
    return """
## ACTIVE STAGE: OUTCOME_CLOSE

You are currently in the close/outcome stage.

Before ending:
- Briefly summarize customer-safe outcome: delivery status handled, medicines explained, diet explained.
- If log_repeat_followup_outcome is available, call it before closing.
- Say a warm closing line from sriaas.

Do not introduce new medicine, diet, WhatsApp, or Agent 2 discussion here unless customer asks.
""".strip()


def _default_knowledge_content() -> str:
    return """
# Radha Repeat Agent Sriaas 1 Follow Up Guide

## Purpose
Radha handles repeat follow-up calls for sriaas customers. The first configured job is medicine/order delivery follow-up, but the backend outcome fields are flexible so more Agent 1 responsibilities can be added later through prompt and knowledge updates.

## Delivery Follow-Up
Confirm that you are speaking to the right customer, then ask naturally whether the medicine/order has been received. If received, acknowledge and ask a short check-in about whether they need any help. If not received, use the Shipkia tracking tool and explain the current shipment status in customer-friendly language.

## Medicine And Diet Questions
If the customer asks about medicine or how to take it, answer only from the uploaded sriaas knowledge. Keep the answer short and remind the customer to follow the prescription/doctor instructions for dosage or medical doubts.

For diet guidance, use the active Patient Encounter department field `sr_pe_deptt` to select the matching Knowledge Document. In this Agent 1 version, explain the chart content in the call and do not send WhatsApp.

## Safe Close
Always log the outcome before closing. Use concise summaries and never expose internal record ids unless the customer already knows them or asks for order/tracking reference.
""".strip()
