"""Join independently arriving call events only with an explicit SIP identity."""

import frappe
import re
from frappe.model.rename_doc import rename_doc


def is_internal_call_id(value) -> bool:
    return str(value or "").lower().startswith(("scl_", "agent-army-", "room_", "sip-", "task-", "batch-"))


def call_phone(value, existing=None):
    """Normalize an explicit international prefix without guessing a country."""
    text = str(value or "").strip()
    if not text:
        return existing
    digits = re.sub(r"\D", "", text)
    if not digits:
        return value
    if digits.startswith("00"):
        return "+" + digits[2:]
    if text.startswith("+"):
        return "+" + digits
    if existing and str(existing).startswith("+") and len(digits) >= 7 and str(existing).endswith(digits):
        return existing
    return digits


def bind_provider_identity(task, payload: dict) -> str | None:
    from confluence_ai.services.call_registry import resolve_call, register_call
    provider_id = str(payload.get("sip_call_id") or "").strip()
    if payload.get("identity_source") not in {"sip.callIDFull", "vobiz.SIPCallID"} or not provider_id or is_internal_call_id(provider_id):
        return None
    doc = resolve_call(payload, task)
    if not doc:
        return None
    if doc.sip_call_id and not is_internal_call_id(doc.sip_call_id) and doc.sip_call_id != provider_id:
        frappe.throw("Attempt is already linked to another provider identity.")
    doc.sip_call_id = provider_id
    if is_internal_call_id(doc.call_uuid):
        doc.call_uuid = None
    doc.direction = payload.get("direction") or doc.direction
    doc.save(ignore_permissions=True)
    register_call(doc, payload)
    return doc.name


def merge_legacy_provider_identity(task, payload: dict) -> str | None:
    """Explicit audited repair only. Never called by normal webhook processing."""
    provider_id = str(payload.get("sip_call_id") or "").strip()
    if payload.get("identity_source") not in {"sip.callIDFull", "vobiz.SIPCallID"} or not provider_id or is_internal_call_id(provider_id):
        return None
    frappe.db.get_value("AI Company", task.company, "name", for_update=True)
    frappe.db.get_value("AI Task", task.name, "name", for_update=True)
    from confluence_ai.services.call_registry import exact_attempt
    attempt = exact_attempt(task, payload)
    if not attempt:
        frappe.throw("Exact attempt required for legacy repair.")
    canonical = frappe.db.get_value("AI Call Log", {"attempt": attempt.name}, "name", for_update=True)
    if not canonical:
        candidates = frappe.get_all("AI Call Log", filters={"task": task.name, "attempt": ["is", "not set"]}, pluck="name", limit=2)
        if len(candidates) > 1:
            frappe.throw("Multiple legacy rows require explicit review.")
        canonical = candidates[0] if candidates else None
    rows = frappe.db.sql(
        """select name from `tabAI Call Log`
        where company=%s and (sip_call_id=%s or call_uuid=%s)
        order by name for update""", (task.company, provider_id, provider_id),
    )
    matches = [row[0] for row in rows]
    docs = [frappe.get_doc("AI Call Log", name, for_update=True) for name in sorted(matches)]
    if any(doc.task and doc.task != task.name for doc in docs):
        frappe.throw("Provider SIP identity is already linked to another task.")
    if not canonical and docs:
        canonical = docs[0].name
    target = frappe.get_doc("AI Call Log", canonical, for_update=True) if canonical else frappe.new_doc("AI Call Log")
    original_uuid = target.call_uuid
    if target.sip_call_id and not is_internal_call_id(target.sip_call_id) and target.sip_call_id != provider_id:
        frappe.throw("Task is already linked to a different provider SIP identity.")
    target.task = task.name
    target.company = task.company
    target.agent = task.assigned_agent or task.target_agent
    target.attempt = attempt.name
    target.sip_call_id = provider_id
    if not target.call_uuid or is_internal_call_id(target.call_uuid):
        target.call_uuid = provider_id
    target.direction = payload.get("direction") or target.direction
    fields = (
        "initiated_payload_json", "status_payload_json", "recording_payload_json", "transcript_payload_json",
        "recording_url", "external_recording_url", "transcript", "transcript_summary", "sentiment",
        "customer_phone", "customer_name", "from_number", "to_number", "trunk_id", "domain",
        "started_at", "ended_at", "duration_sec", "reason", "attempt",
        "ai_disposition", "ai_disposition_reason", "ai_disposition_confidence", "ai_disposition_summary",
        "erp_status_update_status", "erp_status_update_response",
    )
    terminal = {"Completed", "Failed", "Rejected", "No Answer", "Busy", "Cancelled"}
    merged_uuid = None
    for source in docs:
        if source.name == target.name:
            continue
        if source.call_uuid and target.call_uuid == provider_id:
            merged_uuid = source.call_uuid
        for field in fields:
            provider_payload = source.provider == "Vobiz" and field in {
                "from_number", "to_number", "started_at", "ended_at", "duration_sec",
                "initiated_payload_json", "status_payload_json", "recording_payload_json", "transcript_payload_json",
            }
            if source.get(field) not in (None, "", "{}") and (provider_payload or target.get(field) in (None, "", "{}")):
                target.set(field, source.get(field))
        if source.status in terminal and target.status not in terminal:
            target.status = source.status
        target.customer_phone = call_phone(source.customer_phone, target.customer_phone)
    target.status = target.status or "Unknown"
    if merged_uuid:
        # The source still owns the unique UUID until the merge is complete.
        target.call_uuid = original_uuid
    target.save(ignore_permissions=True)
    for source in docs:
        if source.name != target.name:
            frappe.get_doc({
                "doctype": "AI Webhook Event", "company": task.company, "task": task.name,
                "source": "call_identity", "event_type": "call_log_merged", "status": "Processed",
                "payload_json": frappe.as_json(source.as_dict()),
                "response_json": frappe.as_json({"canonical_call_log": target.name, "sip_call_id": provider_id}),
            }).insert(ignore_permissions=True)
            # Frappe updates all Link references; raw receipts remain in AI Webhook Event.
            rename_doc("AI Call Log", source.name, target.name, merge=True, force=True, ignore_permissions=True, show_alert=False, rebuild_search=False)
    if merged_uuid:
        frappe.db.set_value("AI Call Log", target.name, "call_uuid", merged_uuid)
    if target.transcript or target.recording_url:
        values = {}
        if target.transcript:
            values.update(transcript=target.transcript)
        if target.recording_url:
            values.update(recording_url=target.recording_url)
        frappe.db.set_value("AI Task", task.name, values)
        if target.attempt:
            frappe.db.set_value("AI Task Attempt", target.attempt, values)
    current = frappe.db.get_value("AI Task", task.name, "call_uuid")
    if not current or is_internal_call_id(current):
        frappe.db.set_value("AI Task", task.name, "call_uuid", provider_id)
    return target.name
