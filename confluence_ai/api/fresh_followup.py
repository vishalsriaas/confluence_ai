from __future__ import annotations

import json

import frappe

from confluence_ai.services import fresh_followup


def _request_payload() -> dict:
    if frappe.request and frappe.request.data:
        try:
            parsed = json.loads(frappe.request.data.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return parsed[0]
        except Exception:
            pass
    return dict(frappe.local.form_dict or {})


@frappe.whitelist()
def start(**kwargs) -> dict:
    payload = kwargs or _request_payload()
    return fresh_followup.start_from_event(payload)


@frappe.whitelist()
def receive(**kwargs) -> dict:
    return start(**kwargs)


@frappe.whitelist()
def voice_result(
    workflow: str | None = None,
    task: str | None = None,
    outcome: str | None = None,
    notes: str | None = None,
    transcript: str | None = None,
) -> dict:
    payload = _request_payload()
    return fresh_followup.handle_voice_result(
        workflow=workflow or payload.get("workflow"),
        task=task or payload.get("task"),
        outcome=outcome or payload.get("outcome"),
        notes=notes or payload.get("notes") or payload.get("summary"),
        transcript=transcript or payload.get("transcript"),
        result=payload,
    )


@frappe.whitelist()
def mark_call_missed(workflow: str, notes: str | None = None, task: str | None = None) -> dict:
    return fresh_followup.mark_call_missed(workflow, notes, task=task)


@frappe.whitelist()
def process_due() -> dict:
    return fresh_followup.process_due_workflows()
