from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.synchronization import filelock

from confluence_ai.services.dispatcher import enqueue_task_execution, refresh_batch_counts
from confluence_ai.services.utils import as_json, create_error, parse_json_object

WORKFLOW = "AI Fresh Follow Up Workflow"
WORKFLOW_AGENT = "AI Fresh Follow Up Workflow Agent"
SETTINGS = "AI Fresh Follow Up Settings"

FINAL_STATES = {"Completed", "Missed After Attempts", "Failed", "Cancelled"}
MISSED_OUTCOMES = {"missed", "no_answer", "no answer", "busy", "failed", "timeout", "cancelled", "canceled"}
FOLLOWUP_REQUIRED_KEYS = {
    "follow_up_required",
    "followup_required",
    "fresh_followup_required",
    "requires_follow_up",
    "requires_followup",
}
FOLLOWUP_REASON_KEYS = {
    "follow_up_reason",
    "followup_reason",
    "fresh_followup_reason",
    "reason",
}
FOLLOWUP_NEXT_TIME_KEYS = {
    "next_follow_up_at",
    "next_followup_at",
    "next_call_time",
    "next_follow_up_time",
    "follow_up_at",
    "followup_at",
}
ENCOUNTER_CREATE_OPERATIONS = {
    "create_draft_patient_encounter_from_sales",
    "mcp_create_draft_patient_encounter_from_sales",
}


def start_from_event(payload: dict | list | None) -> dict:
    payload = _coerce_payload(payload)
    settings = _settings_for_payload(payload)
    if not settings:
        frappe.throw("No enabled AI Fresh Follow Up Settings found.")

    plan = _settings_agent_plan(settings)
    if not plan:
        frappe.throw("AI Fresh Follow Up Settings must have at least one enabled agent row.")

    context = _normalize_payload(payload, settings)
    if not context.get("phone"):
        frappe.throw("Fresh follow-up payload must include phone or mobile.")

    idem_key = _first_path(payload, _field_names(settings.idempotency_key_field_names)) or context.get("phone")
    existing = frappe.db.exists(WORKFLOW, {"idempotency_key": idem_key}) if idem_key else None
    if existing:
        return {"status": "duplicate", "workflow": existing, "idempotency_key": idem_key}

    workflow = frappe.new_doc(WORKFLOW)
    workflow.update(
        {
            "company": settings.company,
            "enabled": 1,
            "status": "Draft",
            "settings": settings.name,
            "idempotency_key": idem_key,
            "customer_name": context.get("customer_name"),
            "customer_phone": context.get("phone"),
            "source_reference_type": context.get("source_reference_type"),
            "source_reference_name": context.get("source_reference_name"),
            "current_agent_no": 0,
            "next_agent_no": 1,
            "voice_task_template": settings.voice_task_template,
            "livekit_channel_account_fallback": settings.livekit_channel_account_fallback,
            "voice_call_timeout_minutes": _safe_int(settings.voice_call_timeout_minutes, 5),
            "minimum_connected_seconds": _safe_int(settings.get("minimum_connected_seconds"), 20),
            "source_payload_json": as_json(payload),
            "context_json": as_json(context),
            "task_history_json": "[]",
        }
    )
    for agent_no, row in enumerate(plan, start=1):
        workflow.append(
            "agents",
            {
                "enabled": 1,
                "agent_no": agent_no,
                "agent": row["agent"],
                "status": "Pending",
                "followup_timing_mode": row["followup_timing_mode"],
                "followup_after_value": row["followup_after_value"],
                "followup_after_unit": row["followup_after_unit"],
                "max_attempts": row["max_attempts"],
                "retry_after_value": row["retry_after_value"],
                "retry_after_unit": row["retry_after_unit"],
            },
        )

    workflow.insert(ignore_permissions=True)
    frappe.db.commit()
    return queue_agent_call(workflow.name, 1)


def maybe_start_from_task(task, payload: dict | None = None, context: dict | None = None) -> dict | None:
    """Attach a normal voice task as Agent 1 of a fresh follow-up workflow.

    This keeps the first call on the normal Vobiz/LiveKit start path and only
    uses Fresh Follow Up to schedule the later agents after Agent 1 completes.
    """
    if isinstance(task, str):
        if not frappe.db.exists("AI Task", task):
            return None
        task = frappe.get_doc("AI Task", task)

    if not task or task.channel != "Voice":
        return None

    if task.external_record_type in {WORKFLOW, "AI Repeat Follow Up Workflow", "Order Confirmation Workflow"}:
        return None

    agent_name = task.assigned_agent or task.target_agent
    if not agent_name or not frappe.db.exists("AI Agent", agent_name):
        return None

    company = task.company or frappe.db.get_value("AI Agent", agent_name, "company")
    if not company:
        return None

    settings = _settings_for_payload({"company": company})
    if not settings:
        return None

    plan = _settings_agent_plan(settings)
    if not plan or plan[0].get("agent") != agent_name:
        return None

    existing_parent = frappe.db.get_value(WORKFLOW_AGENT, {"task": task.name}, "parent")
    if existing_parent:
        return {"status": "duplicate", "workflow": existing_parent, "task": task.name}

    task_context = parse_json_object(task.context_json, "Task Context JSON") if task.context_json else {}
    source_payload = {}
    for item in (payload or {}, context or {}, task_context):
        if isinstance(item, dict):
            source_payload.update(item)
    source_payload["company"] = company
    source_payload.setdefault("task", task.name)
    source_payload.setdefault("source_reference_type", task.external_record_type or "AI Task")
    source_payload.setdefault("source_reference_name", task.external_record_id or task.name)
    if task.call_uuid:
        source_payload.setdefault("call_uuid", task.call_uuid)

    normalized = _normalize_payload(source_payload, settings)
    if not normalized.get("phone"):
        return {"status": "ignored", "reason": "missing_phone", "task": task.name}
    normalized["fresh_followup_outcome_contract"] = _fresh_followup_outcome_contract()

    base_idem = task.idempotency_key or task.call_uuid or task.name
    idem_key = f"fresh-followup:{base_idem}"
    existing = frappe.db.exists(WORKFLOW, {"idempotency_key": idem_key})
    if existing:
        return {"status": "duplicate", "workflow": existing, "idempotency_key": idem_key, "task": task.name}

    workflow = frappe.new_doc(WORKFLOW)
    deadline = task.deadline or add_to_date(now_datetime(), minutes=_safe_int(settings.voice_call_timeout_minutes, 5), as_datetime=True)
    workflow.update(
        {
            "company": settings.company,
            "enabled": 1,
            "status": "Queued",
            "settings": settings.name,
            "idempotency_key": idem_key,
            "customer_name": normalized.get("customer_name"),
            "customer_phone": normalized.get("phone"),
            "source_reference_type": normalized.get("source_reference_type") or task.external_record_type or "AI Task",
            "source_reference_name": normalized.get("source_reference_name") or task.external_record_id or task.name,
            "current_agent_no": 1,
            "next_agent_no": 1,
            "next_call_time": deadline,
            "active_call_timeout_at": deadline,
            "timer_status": "Agent 1 running from normal Vobiz/LiveKit task.",
            "voice_task_template": settings.voice_task_template,
            "livekit_channel_account_fallback": settings.livekit_channel_account_fallback,
            "voice_call_timeout_minutes": _safe_int(settings.voice_call_timeout_minutes, 5),
            "minimum_connected_seconds": _safe_int(settings.get("minimum_connected_seconds"), 20),
            "source_payload_json": as_json(source_payload),
            "context_json": as_json(normalized),
            "task_history_json": "[]",
        }
    )

    for agent_no, row in enumerate(plan, start=1):
        workflow.append(
            "agents",
            {
                "enabled": 1,
                "agent_no": agent_no,
                "agent": row["agent"],
                "status": "Queued" if agent_no == 1 else "Pending",
                "task": task.name if agent_no == 1 else None,
                "scheduled_at": now_datetime() if agent_no == 1 else None,
                "attempt_count": 1 if agent_no == 1 else 0,
                "followup_timing_mode": row["followup_timing_mode"],
                "followup_after_value": row["followup_after_value"],
                "followup_after_unit": row["followup_after_unit"],
                "max_attempts": row["max_attempts"],
                "retry_after_value": row["retry_after_value"],
                "retry_after_unit": row["retry_after_unit"],
            },
        )

    _append_task_history(workflow, 1, 1, task.name, "attached")
    workflow.insert(ignore_permissions=True)
    _attach_outcome_contract_to_task(task.name, workflow.name)
    return {"status": "attached", "workflow": workflow.name, "agent_no": 1, "task": task.name}


def queue_agent_call(workflow_name: str, agent_no: int | None = None) -> dict:
    with _workflow_lock(workflow_name):
        workflow = frappe.get_doc(WORKFLOW, workflow_name)
        if not workflow.enabled:
            return {"status": "skipped", "reason": "disabled", "workflow": workflow.name}
        if workflow.status in FINAL_STATES:
            return {"status": "skipped", "reason": "final_state", "workflow": workflow.name}

        row = _agent_row(workflow, agent_no or workflow.next_agent_no or 1)
        if not row:
            workflow.status = "Completed"
            workflow.final_reason = "No follow-up agent row is available."
            workflow.next_agent_no = 0
            workflow.next_call_time = None
            workflow.active_call_timeout_at = None
            workflow.save(ignore_permissions=True)
            frappe.db.commit()
            return {"status": "completed", "workflow": workflow.name}

        agent_no = int(row.agent_no or row.idx or 1)
        if not row.agent or not frappe.db.exists("AI Agent", row.agent) or not frappe.db.get_value("AI Agent", row.agent, "enabled"):
            return _mark_pending_config(workflow, row)

        if row.task:
            task_status = frappe.db.get_value("AI Task", row.task, "status")
            if task_status in {"Queued", "Waiting", "Running"}:
                return {"status": "skipped", "reason": "agent_task_already_active", "workflow": workflow.name, "task": row.task}

        attempt_number = _safe_int(row.attempt_count, 0) + 1
        max_attempts = max(1, _safe_int(row.max_attempts, 1))
        if attempt_number > max_attempts:
            workflow.status = "Missed After Attempts"
            workflow.final_reason = f"Agent {agent_no} attempts exhausted."
            workflow.next_agent_no = 0
            workflow.next_call_time = None
            workflow.active_call_timeout_at = None
            row.status = "Missed"
            workflow.save(ignore_permissions=True)
            frappe.db.commit()
            return {"status": "missed_after_attempts", "workflow": workflow.name, "agent_no": agent_no}

        task_template = workflow.voice_task_template
        if not task_template or not frappe.db.exists("AI Task Template", task_template):
            frappe.throw("AI Fresh Follow Up Settings must have a valid Voice Task Template.")

        context = _workflow_context(workflow, agent_no)
        batch = frappe.new_doc("AI Task Batch")
        idempotency_key = f"{workflow.name}:agent{agent_no}:attempt{attempt_number}"
        batch.update(
            {
                "company": workflow.company,
                "status": "Queued",
                "source_system": "AI Fresh Follow Up",
                "batch_label": f"{workflow.name}:agent{agent_no}",
                "idempotency_key": idempotency_key,
                "task_template": task_template,
                "target_agent": row.agent,
                "priority": "Normal" if agent_no > 1 else "High",
                "source_payload_json": workflow.source_payload_json,
            }
        )
        batch.insert(ignore_permissions=True)

        deadline = add_to_date(now_datetime(), minutes=_safe_int(workflow.voice_call_timeout_minutes, 5), as_datetime=True)
        task = frappe.new_doc("AI Task")
        task.update(
            {
                "company": workflow.company,
                "status": "Queued",
                "task_batch": batch.name,
                "task_template": task_template,
                "target_agent": row.agent,
                "assigned_agent": row.agent,
                "channel": "Voice",
                "priority": "Normal" if agent_no > 1 else "High",
                "deadline": deadline,
                "external_record_id": workflow.name,
                "external_record_type": WORKFLOW,
                "idempotency_key": idempotency_key,
                "context_json": as_json(context),
            }
        )
        task.insert(ignore_permissions=True)
        refresh_batch_counts(batch.name)

        row.attempt_count = attempt_number
        row.task = task.name
        row.status = "Queued"
        row.scheduled_at = now_datetime()
        workflow.status = "Queued"
        workflow.current_agent_no = agent_no
        workflow.next_agent_no = agent_no
        workflow.next_call_time = deadline
        workflow.active_call_timeout_at = deadline
        workflow.timer_status = f"Agent {agent_no} attempt {attempt_number} queued."
        _append_task_history(workflow, agent_no, attempt_number, task.name, "queued")
        workflow.save(ignore_permissions=True)
        frappe.db.commit()

        enqueue_task_execution(task.name, "Voice", enqueue_after_commit=False)
        return {"status": "queued", "workflow": workflow.name, "agent_no": agent_no, "task": task.name}


def handle_voice_result(
    workflow: str | None = None,
    task: str | None = None,
    outcome: str | None = None,
    notes: str | None = None,
    transcript: str | None = None,
    result: dict | None = None,
) -> dict:
    doc = _find_workflow(workflow=workflow, task=task)
    if not doc:
        frappe.throw("Fresh follow-up workflow not found.")

    row = _agent_row_for_task_or_status(doc, task)
    if not row:
        frappe.throw("Fresh follow-up agent row not found.")

    outcome_key = str(outcome or "").strip().lower()
    if outcome_key in MISSED_OUTCOMES:
        return mark_call_missed(doc.name, notes or transcript or outcome, task=task)

    if _call_was_too_short(doc, task=task, result=result):
        return mark_call_missed(
            doc.name,
            f"Connected call was shorter than minimum {doc.get('minimum_connected_seconds') or 20} seconds; retry same agent.",
            task=task,
        )

    transcript_text = transcript or notes or _task_transcript(task) or ""
    if transcript_text:
        row.transcript = transcript_text
    row.status = "Completed"
    row.last_notes = notes or ""
    if task and frappe.db.exists("AI Task", task):
        frappe.db.set_value("AI Task", task, {"status": "Completed", "last_error": ""}, update_modified=True)

    agent_no = int(row.agent_no or row.idx or 1)
    doc.active_call_timeout_at = None
    doc.timer_status = f"Agent {agent_no} completed."
    decision = _fresh_followup_decision(result=result, task=task, outcome=outcome)
    doc.result_json = as_json(
        {
            "outcome": outcome,
            "notes": notes,
            "transcript": transcript_text,
            "raw_result": result or {},
            "fresh_followup_decision": decision,
        }
    )
    _append_task_history(doc, agent_no, _safe_int(row.attempt_count, 0), task, "completed")
    if decision.get("follow_up_required"):
        next_result = _schedule_next_agent(doc, agent_no, decision=decision)
    else:
        next_result = _complete_without_next_followup(doc, agent_no, decision)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "completed", "workflow": doc.name, "agent_no": agent_no, "next": next_result}


def wait_for_voice_transcript(workflow_name: str, notes: str | None = None) -> dict:
    doc = frappe.get_doc(WORKFLOW, workflow_name)
    if doc.status in FINAL_STATES:
        return {"status": "ignored", "reason": "final_state", "workflow": doc.name}
    if notes:
        doc.timer_status = notes
    doc.active_call_timeout_at = None
    doc.timer_status = doc.timer_status or "Call completed; waiting for provider transcript/outcome."
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "waiting_for_transcript", "workflow": doc.name}


def mark_call_missed(workflow: str, notes: str | None = None, task: str | None = None) -> dict:
    doc = _find_workflow(workflow=workflow, task=task)
    if not doc:
        frappe.throw("Fresh follow-up workflow not found.")

    row = _agent_row_for_task_or_status(doc, task)
    if not row:
        frappe.throw("Fresh follow-up agent row not found.")

    task_name = task or row.task
    if task_name and frappe.db.exists("AI Task", task_name):
        frappe.db.set_value("AI Task", task_name, {"status": "Deadline Missed", "last_error": notes or "Call missed."}, update_modified=True)

    agent_no = int(row.agent_no or row.idx or 1)
    attempts = _safe_int(row.attempt_count, 0)
    max_attempts = max(1, _safe_int(row.max_attempts, 1))
    row.last_notes = notes or ""
    _append_task_history(doc, agent_no, attempts, task_name, "missed", notes=notes)
    doc.active_call_timeout_at = None

    if attempts >= max_attempts:
        doc.status = "Missed After Attempts"
        doc.next_agent_no = 0
        doc.next_call_time = None
        doc.final_reason = f"Agent {agent_no} missed after {attempts} attempts."
        doc.timer_status = doc.final_reason
        row.status = "Missed"
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "missed_after_attempts", "workflow": doc.name, "agent_no": agent_no}

    retry_at = _add_duration(now_datetime(), row.retry_after_value, row.retry_after_unit, default_value=3, default_unit="Hours")
    doc.status = "Scheduled"
    doc.next_agent_no = agent_no
    doc.next_call_time = retry_at
    doc.timer_status = f"Agent {agent_no} retry scheduled for {retry_at}."
    row.status = "Scheduled"
    row.scheduled_at = retry_at
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "retry_scheduled", "workflow": doc.name, "agent_no": agent_no, "scheduled_at": retry_at}


def process_due_workflows() -> dict:
    now_value = now_datetime()
    queued = 0
    missed = 0

    for name in frappe.get_all(
        WORKFLOW,
        filters={"enabled": 1, "status": "Scheduled", "next_call_time": ["<=", now_value]},
        pluck="name",
        limit=200,
    ):
        try:
            result = queue_agent_call(name)
            if result.get("status") == "queued":
                queued += 1
        except Exception as exc:
            _mark_failed(name, exc)

    for name in frappe.get_all(
        WORKFLOW,
        filters={"enabled": 1, "status": "Queued", "active_call_timeout_at": ["<=", now_value]},
        pluck="name",
        limit=200,
    ):
        try:
            mark_call_missed(name, "Voice call timed out without callback.")
            missed += 1
        except Exception as exc:
            _mark_failed(name, exc)

    for row in _missed_task_rows():
        try:
            mark_call_missed(row.parent, "Voice task was marked deadline missed.", task=row.task)
            missed += 1
        except Exception as exc:
            _mark_failed(row.parent, exc)

    return {"queued": queued, "missed": missed}


def _settings_agent_plan(settings) -> list[dict]:
    plan = []
    for row in settings.get("agents") or []:
        if not row.enabled or not row.agent:
            continue
        plan.append(
            {
                "agent": row.agent,
                "followup_timing_mode": row.get("followup_timing_mode") or "Manual",
                "followup_after_value": _safe_int(row.followup_after_value, 0),
                "followup_after_unit": row.followup_after_unit or "Minutes",
                "max_attempts": max(1, _safe_int(row.max_attempts, 1)),
                "retry_after_value": _safe_int(row.retry_after_value, 3),
                "retry_after_unit": row.retry_after_unit or "Hours",
            }
        )
    return plan


def _schedule_next_agent(workflow, completed_agent_no: int, decision: dict | None = None) -> dict:
    next_row = _next_agent_row(workflow, completed_agent_no)
    if not next_row:
        workflow.status = "Completed"
        workflow.next_agent_no = 0
        workflow.next_call_time = None
        workflow.final_reason = "All configured follow-up agents completed."
        return {"status": "completed"}

    next_agent_no = int(next_row.agent_no or next_row.idx or 1)
    if not next_row.agent or not frappe.db.exists("AI Agent", next_row.agent) or not frappe.db.get_value("AI Agent", next_row.agent, "enabled"):
        workflow.status = "Pending Config"
        workflow.next_agent_no = next_agent_no
        workflow.next_call_time = None
        workflow.timer_status = f"Agent {next_agent_no} is missing or disabled."
        next_row.status = "Pending Config"
        return {"status": "pending_config", "agent_no": next_agent_no}

    scheduled_at = _resolve_next_call_time(next_row, decision)
    workflow.status = "Scheduled"
    workflow.next_agent_no = next_agent_no
    workflow.next_call_time = scheduled_at
    workflow.timer_status = f"Agent {next_agent_no} scheduled for {scheduled_at}."
    next_row.status = "Scheduled"
    next_row.scheduled_at = scheduled_at
    if decision and decision.get("next_follow_up_at"):
        next_row.suggested_next_call_time = decision.get("next_follow_up_at")
    return {"status": "scheduled", "agent_no": next_agent_no, "scheduled_at": scheduled_at}


def _mark_pending_config(workflow, row) -> dict:
    agent_no = int(row.agent_no or row.idx or 1)
    workflow.status = "Pending Config"
    workflow.next_agent_no = agent_no
    workflow.next_call_time = None
    workflow.active_call_timeout_at = None
    workflow.timer_status = f"Agent {agent_no} is missing or disabled."
    row.status = "Pending Config"
    workflow.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "pending_config", "workflow": workflow.name, "agent_no": agent_no}


def _settings_for_payload(payload: dict):
    company = str(payload.get("company") or payload.get("ai_company") or "").strip()
    filters = {"enabled": 1}
    if company:
        filters["company"] = company
    name = frappe.db.get_value(SETTINGS, filters, "name", order_by="modified desc")
    if not name and not company:
        name = frappe.db.get_value(SETTINGS, {"enabled": 1}, "name", order_by="modified desc")
    return frappe.get_doc(SETTINGS, name) if name else None


def _workflow_context(workflow, agent_no: int) -> dict:
    base_context = parse_json_object(workflow.context_json, "Workflow Context JSON")
    previous_transcripts = {
        f"agent_{int(row.agent_no or row.idx)}": row.transcript
        for row in workflow.agents
        if row.enabled and _safe_int(row.agent_no or row.idx, 0) < agent_no and row.transcript
    }
    base_context.update(
        {
            "event": "fresh_followup",
            "workflow": workflow.name,
            "company": workflow.company,
            "fresh_followup_agent_no": agent_no,
            "customer_name": workflow.customer_name,
            "patient_name": workflow.customer_name,
            "phone": workflow.customer_phone,
            "customer_phone": workflow.customer_phone,
            "source_reference_type": workflow.source_reference_type,
            "source_reference_name": workflow.source_reference_name,
            "previous_agent_transcripts": previous_transcripts,
            "previous_transcript_summary": _compact_transcripts(previous_transcripts),
            "fresh_followup_outcome_contract": _fresh_followup_outcome_contract(),
            "minimum_connected_seconds": workflow.get("minimum_connected_seconds") or 20,
            "voice_channel_account": "",
            "livekit_channel_account_fallback": workflow.livekit_channel_account_fallback,
        }
    )
    return base_context


def _normalize_payload(payload: dict, settings) -> dict:
    phone = _normalize_phone(_first_path(payload, _field_names(settings.phone_field_names)))
    customer_name = _clean_text(_first_path(payload, _field_names(settings.customer_name_field_names)))
    source_reference_type = _clean_text(payload.get("source_reference_type") or payload.get("reference_type") or payload.get("doctype"))
    source_reference_name = _clean_text(payload.get("source_reference_name") or payload.get("reference_name") or payload.get("name"))
    return {
        "company": settings.company,
        "phone": phone,
        "customer_name": customer_name,
        "source_reference_type": source_reference_type,
        "source_reference_name": source_reference_name,
        "payload": payload,
    }


def _agent_row(workflow, agent_no: int | str | None):
    requested = _safe_int(agent_no, 1)
    for row in workflow.get("agents") or []:
        if row.enabled and _safe_int(row.agent_no or row.idx, 0) == requested:
            return row
    return None


def _next_agent_row(workflow, after_agent_no: int):
    candidates = [
        row
        for row in workflow.get("agents") or []
        if row.enabled and _safe_int(row.agent_no or row.idx, 0) > after_agent_no
    ]
    return sorted(candidates, key=lambda row: _safe_int(row.agent_no or row.idx, 0))[0] if candidates else None


def _complete_without_next_followup(workflow, completed_agent_no: int, decision: dict) -> dict:
    workflow.status = "Completed"
    workflow.next_agent_no = 0
    workflow.next_call_time = None
    workflow.active_call_timeout_at = None
    reason = decision.get("reason") or "follow_up_not_required"
    workflow.final_reason = f"Agent {completed_agent_no} completed; no next follow-up scheduled ({reason})."
    workflow.timer_status = workflow.final_reason
    for row in workflow.get("agents") or []:
        if row.enabled and _safe_int(row.agent_no or row.idx, 0) > completed_agent_no and row.status in {"Pending", "Scheduled"}:
            row.status = "Cancelled"
    return {"status": "completed", "reason": reason}


def _resolve_next_call_time(next_row, decision: dict | None):
    mode = str(next_row.get("followup_timing_mode") or "Manual").strip().lower()
    suggested = _parse_next_call_time((decision or {}).get("next_follow_up_at")) if mode == "agent" else None
    if suggested:
        return suggested
    return _add_duration(
        now_datetime(),
        next_row.followup_after_value,
        next_row.followup_after_unit,
        default_value=0,
        default_unit="Minutes",
    )


def _fresh_followup_decision(result: dict | None, task: str | None = None, outcome: str | None = None) -> dict:
    structured = _extract_structured_followup_decision(result if isinstance(result, dict) else {})
    if structured is None and task:
        structured = _extract_structured_followup_decision(_task_result_json(task))
    if structured is None and task:
        structured = _latest_structured_outcome_event(task)
    if structured is not None:
        return structured

    encounter_status = _encounter_create_status(task)
    if encounter_status == "Succeeded":
        return {
            "follow_up_required": False,
            "reason": "encounter_created",
            "source": "provider_event",
        }
    if encounter_status in {"Failed", "Timeout", "Rate Limited"}:
        return {
            "follow_up_required": True,
            "reason": "encounter_not_created",
            "source": "provider_event",
        }

    return {
        "follow_up_required": False,
        "reason": "no_structured_followup_outcome",
        "source": "default",
        "outcome": outcome or "",
    }


def _extract_structured_followup_decision(source: Any) -> dict | None:
    if not isinstance(source, dict):
        return None
    for found in _walk_dicts(source):
        required_marker = _first_existing_value(found, FOLLOWUP_REQUIRED_KEYS)
        if required_marker is None:
            continue
        reason = _clean_text(_first_existing_value(found, FOLLOWUP_REASON_KEYS))
        next_follow_up_at = _parse_next_call_time(_first_existing_value(found, FOLLOWUP_NEXT_TIME_KEYS))
        return {
            "follow_up_required": _truthy(required_marker),
            "reason": reason or ("follow_up_required" if _truthy(required_marker) else "follow_up_not_required"),
            "next_follow_up_at": next_follow_up_at,
            "source": "structured_outcome",
        }
    return None


def _latest_structured_outcome_event(task: str) -> dict | None:
    if not task:
        return None
    rows = frappe.get_all(
        "AI Provider Event",
        filters={"task": task, "operation": ["in", ["log_sales_call_outcome", "mcp_log_sales_call_outcome"]]},
        fields=["request_json", "response_json"],
        order_by="modified desc",
        limit=3,
    )
    for row in rows:
        for raw in (row.get("request_json"), row.get("response_json")):
            parsed = parse_json_object(raw, "Fresh Follow Up Outcome Event") if raw else {}
            decision = _extract_structured_followup_decision(parsed)
            if decision:
                decision["source"] = "provider_event_structured_outcome"
                return decision
    return None


def _encounter_create_status(task: str | None) -> str:
    if not task:
        return ""
    return (
        frappe.db.get_value(
            "AI Provider Event",
            {"task": task, "operation": ["in", list(ENCOUNTER_CREATE_OPERATIONS)]},
            "status",
            order_by="modified desc",
        )
        or ""
    )


def _attach_outcome_contract_to_task(task_name: str, workflow_name: str) -> None:
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return
    task = frappe.get_doc("AI Task", task_name)
    context = parse_json_object(task.context_json, "Task Context JSON") if task.context_json else {}
    if not isinstance(context, dict):
        context = {}
    context["fresh_followup_workflow"] = workflow_name
    context["fresh_followup_agent_no"] = 1
    context["fresh_followup_outcome_contract"] = _fresh_followup_outcome_contract()
    task.context_json = as_json(context)
    task.save(ignore_permissions=True)


def _fresh_followup_outcome_contract() -> dict:
    return {
        "required": True,
        "fields": {
            "follow_up_required": "boolean",
            "follow_up_reason": "later_requested|encounter_not_created|issue|completed",
            "follow_up_summary": "short customer-safe reason",
            "next_follow_up_at": "required only when follow_up_required is true and customer gave a time; use site timezone datetime like YYYY-MM-DD HH:mm:ss",
        },
        "rule": "Set follow_up_required true only when the next follow-up call is needed. If work is completed, set false.",
    }


def _call_was_too_short(workflow, *, task: str | None = None, result: dict | None = None) -> bool:
    minimum = max(0, _safe_int(workflow.get("minimum_connected_seconds"), 20))
    if not minimum:
        return False
    duration = _duration_seconds_from_result(result)
    if duration is None and task and frappe.db.exists("AI Task", task):
        task_doc = frappe.get_doc("AI Task", task)
        duration = _safe_int(task_doc.get("duration"), 0) or None
        if duration is None and task_doc.result_json:
            duration = _duration_seconds_from_result(parse_json_object(task_doc.result_json, "Task Result JSON"))
    return duration is not None and 0 < duration < minimum


def _duration_seconds_from_result(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    value = (
        result.get("duration_sec")
        or result.get("duration_seconds")
        or result.get("Duration")
        or result.get("duration")
        or result.get("duration_ms")
    )
    if value is None and isinstance(result.get("last_vobiz_payload"), dict):
        return _duration_seconds_from_result(result["last_vobiz_payload"])
    if value is None and isinstance(result.get("last_livekit_payload"), dict):
        return _duration_seconds_from_result(result["last_livekit_payload"])
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if "ms" in str(value).lower() or number > 5000:
        return int(number / 1000)
    return int(number)


def _parse_next_call_time(value: Any):
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        return get_datetime(cleaned)
    except Exception:
        return None


def _task_result_json(task: str | None) -> dict:
    if not task or not frappe.db.exists("AI Task", task):
        return {}
    raw = frappe.db.get_value("AI Task", task, "result_json")
    parsed = parse_json_object(raw, "Task Result JSON") if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _first_existing_value(source: dict, keys: set[str]) -> Any:
    for key in keys:
        if key in source:
            return source.get(key)
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _agent_row_for_task_or_status(workflow, task: str | None = None):
    if task:
        for row in workflow.get("agents") or []:
            if row.task == task:
                return row
    return _agent_row(workflow, workflow.current_agent_no or workflow.next_agent_no or 1)


def _find_workflow(workflow: str | None = None, task: str | None = None):
    if workflow and frappe.db.exists(WORKFLOW, workflow):
        return frappe.get_doc(WORKFLOW, workflow)
    if task:
        row = frappe.db.get_value(WORKFLOW_AGENT, {"task": task}, ["parent"], as_dict=True)
        if row and row.parent:
            return frappe.get_doc(WORKFLOW, row.parent)
    return None


def _missed_task_rows() -> list:
    return frappe.db.sql(
        f"""
        select row.parent, row.task
        from `tab{WORKFLOW_AGENT}` row
        inner join `tab{WORKFLOW}` workflow on workflow.name = row.parent
        inner join `tabAI Task` task on task.name = row.task
        where workflow.enabled = 1
            and workflow.status = 'Queued'
            and row.status = 'Queued'
            and task.status = 'Deadline Missed'
        limit 200
        """,
        as_dict=True,
    )


def _task_transcript(task_name: str | None) -> str:
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return ""
    task = frappe.get_doc("AI Task", task_name)
    if task.transcript:
        return task.transcript
    result = parse_json_object(task.result_json, "Task Result JSON") if task.result_json else {}
    return _clean_text(result.get("transcript") or result.get("notes") or result.get("summary"))


def _append_task_history(workflow, agent_no: int, attempt: int, task: str | None, status: str, notes: str | None = None) -> None:
    history = []
    if workflow.task_history_json:
        try:
            parsed = json.loads(workflow.task_history_json)
            history = parsed if isinstance(parsed, list) else []
        except Exception:
            history = []
    history.append(
        {
            "at": now_datetime().isoformat(),
            "agent_no": agent_no,
            "attempt": attempt,
            "task": task,
            "status": status,
            "notes": notes or "",
        }
    )
    workflow.task_history_json = frappe.as_json(history[-100:], indent=2)


def _compact_transcripts(transcripts: dict[str, str]) -> str:
    parts = []
    for key, text in transcripts.items():
        compact = _clean_text(text)
        if compact:
            parts.append(f"{key}: {compact[:1200]}")
    return "\n\n".join(parts)


def _coerce_payload(payload: dict | list | None) -> dict:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return dict(payload[0])
    if isinstance(payload, dict):
        return dict(payload)
    return {}


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


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _normalize_phone(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[-10:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if text.startswith("+") and digits:
        return f"+{digits}"
    return text


def _add_duration(base, value, unit, *, default_value: int, default_unit: str):
    amount = max(0, _safe_int(value, default_value))
    unit_text = str(unit or default_unit or "Hours").strip().lower()
    kwargs = {"as_datetime": True}
    if unit_text.startswith("minute"):
        kwargs["minutes"] = amount
    elif unit_text.startswith("day"):
        kwargs["days"] = amount
    else:
        kwargs["hours"] = amount
    return add_to_date(get_datetime(base), **kwargs)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _mark_failed(workflow_name: str, exc: Exception) -> None:
    try:
        doc = frappe.get_doc(WORKFLOW, workflow_name)
        doc.status = "Failed"
        doc.final_reason = str(exc)
        doc.save(ignore_permissions=True)
        create_error("Fresh Follow Up", str(exc), source="fresh_followup", company=doc.company, payload={"workflow": workflow_name}, exc=exc)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "AI Fresh Follow Up mark failed error")


def _workflow_lock(workflow_name: str):
    return filelock(f"ai_fresh_followup_{workflow_name}", timeout=60)
