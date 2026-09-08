"""One call per attempt; external identifiers are aliases, never phone matches."""

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe

from confluence_ai.services.call_identity import call_phone, is_internal_call_id


def identity_key(company, kind, value):
    return hashlib.sha256(json.dumps([company, kind, str(value)], separators=(",", ":")).encode()).hexdigest()


def aliases(payload):
    result = set()
    for kind, keys in (
        # Vobiz explicitly relates its customer leg to the SIP leg seen by LiveKit.
        ("sip", ("SIPCallID", "sip_call_id", "BridgeUUID", "bridge_uuid")),
        ("uuid", ("CallUUID", "call_uuid", "BridgeUUID", "bridge_uuid")),
        ("room", ("room_name", "room")),
        ("attempt", ("attempt", "attempt_id")),
    ):
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value and (kind in {"room", "attempt"} or not is_internal_call_id(value)):
                result.add((kind, value))
    return sorted(result)


def company_for(payload, task=None):
    if task:
        if payload.get("company") and payload["company"] != task.company:
            frappe.throw("Call company does not match task company.")
        return task.company
    from confluence_ai.services.vobiz import _candidate_channel_accounts
    accounts = _candidate_channel_accounts(payload)
    companies = {frappe.db.get_value("AI Channel Account", name, "company") for name in accounts} - {None, ""}
    if payload.get("company"):
        if companies and payload["company"] not in companies:
            frappe.throw("Callback company does not match telephony account.")
        return payload["company"]
    return next(iter(companies)) if len(companies) == 1 else None


def find_call(payload, company):
    if not company:
        return None
    names = set()
    for kind, value in aliases(payload):
        name = frappe.db.get_value("AI Call Identity", identity_key(company, kind, value), "call_log")
        if name:
            names.add(name)
        # Exact legacy identifiers are accepted, but room names are not provider IDs.
        fields = {"sip": ("sip_call_id",), "uuid": ("call_uuid",), "attempt": ("attempt",), "room": ()}[kind]
        for field in fields:
            names.update(frappe.get_all("AI Call Log", filters={"company": company, field: value}, pluck="name"))
    if len(names) > 1:
        frappe.throw("Conflicting call identities; review required, no automatic phone merge.")
    return next(iter(names)) if names else None


def exact_attempt(task, payload, attempt=None):
    name = payload.get("attempt") or payload.get("attempt_id") or getattr(attempt, "name", None)
    if name:
        doc = frappe.get_doc("AI Task Attempt", name)
        if doc.task != task.name or doc.company != task.company:
            frappe.throw("Call attempt does not belong to this task/company.")
        return doc
    # Legacy workers are safe only when the task has exactly one attempt.
    rows = frappe.get_all("AI Task Attempt", filters={"task": task.name}, pluck="name", limit=2)
    return frappe.get_doc("AI Task Attempt", rows[0]) if len(rows) == 1 else None


def resolve_call(payload, task=None, attempt=None):
    if task:
        frappe.db.get_value("AI Task", task.name, "name", for_update=True)
    company = company_for(payload, task)
    existing = find_call(payload, company)
    if existing:
        doc = frappe.get_doc("AI Call Log", existing)
        if task and doc.task and doc.task != task.name:
            frappe.throw("Call identity belongs to another task.")
        if doc.attempt:
            attempt = frappe.get_doc("AI Task Attempt", doc.attempt)
            if task is None:
                task = frappe.get_doc("AI Task", attempt.task)
                frappe.db.get_value("AI Task", task.name, "name", for_update=True)
    if task:
        attempt = exact_attempt(task, payload, attempt)
        if not attempt:
            return None
        frappe.db.get_value("AI Task Attempt", attempt.name, "name", for_update=True)
        by_attempt = frappe.db.get_value("AI Call Log", {"company": company, "attempt": attempt.name}, "name", for_update=True)
        if not by_attempt:
            attempts = frappe.get_all("AI Task Attempt", filters={"task": task.name}, pluck="name", limit=2)
            legacy = frappe.get_all("AI Call Log", filters={"company": company, "task": task.name, "attempt": ["is", "not set"]}, pluck="name", limit=2)
            if len(attempts) == 1 and len(legacy) == 1:
                by_attempt = legacy[0]
        if existing and by_attempt and existing != by_attempt:
            frappe.throw("Attempt and provider identity refer to different call logs.")
        existing = existing or by_attempt
    if not existing and not attempt:
        return None
    doc = frappe.get_doc("AI Call Log", existing, for_update=True) if existing else frappe.new_doc("AI Call Log")
    doc.company = company
    if attempt:
        doc.attempt = attempt.name
        doc.task = attempt.task
        doc.call_reference = identity_key(company, "attempt", attempt.name)
    if task:
        doc.agent = task.assigned_agent or task.target_agent
    if is_internal_call_id(doc.call_uuid):
        doc.call_uuid = None
    if is_internal_call_id(doc.sip_call_id):
        doc.sip_call_id = None
    if not doc.call_reference:
        doc.call_reference = identity_key(company, "legacy", doc.name)
    return doc


def register_call(doc, payload):
    values = set(aliases(payload))
    if doc.attempt:
        values.add(("attempt", doc.attempt))
    for kind, value in sorted(values):
        key = identity_key(doc.company, kind, value)
        existing = frappe.db.get_value("AI Call Identity", key, "call_log", for_update=True)
        if existing and existing != doc.name:
            frappe.throw("Call identity is already assigned to a different call.")
        if not existing:
            frappe.get_doc({"doctype": "AI Call Identity", "name": key, "company": doc.company,
                "identity_kind": kind, "identity_value": value, "call_log": doc.name}).insert(ignore_permissions=True)


def reserve_call(task, attempt, context):
    payload = {"attempt": attempt.name, "room_name": f"agent-army-{task.name}-{attempt.name}"}
    doc = resolve_call(payload, task, attempt)
    doc.direction = "Outbound"
    doc.customer_phone = call_phone(context.get("customer_phone") or context.get("phone") or context.get("to"))
    doc.from_number = call_phone(context.get("outbound_phone_number"))
    doc.to_number = doc.customer_phone
    doc.status = "Initiated"
    doc.save(ignore_permissions=True)
    register_call(doc, payload)
    return payload


def apply_event_state(doc, payload):
    event = str(payload.get("event") or payload.get("Event") or payload.get("event_type") or "").lower()
    category = event_category(event)
    if category:
        doc.set(category + "_event_status", "Applied")
    if not doc.recording_received_at and (category == "recording" or (
        doc.get("recording_url") or doc.get("external_recording_url")
    )):
        doc.recording_received_at = payload.get("_received_at") or frappe.utils.now_datetime()
    if category == "hangup" and not doc.call_end_received_at:
        doc.call_end_received_at = payload.get("_received_at") or frappe.utils.now_datetime()
    if category == "transcript" and doc.transcript:
        doc.transcript_recovery_status = "Transcript received"
    for field, keys in (("started_at", ("StartTime", "started_at")), ("ended_at", ("EndTime", "ended_at"))):
        value = next((payload[k] for k in keys if payload.get(k)), None)
        if value and category in {"initiate", "hangup"}:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo:
                moment = moment.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)
            doc.set(field, moment)


def event_category(event):
    if event in {"callinitiated", "initiated", "dial", "ringing"}:
        return "initiate"
    if event in {"hangup", "completed", "call_ended", "room_finished", "failed", "busy", "no_answer", "cancel", "timeout"}:
        return "hangup"
    if event in {"recording", "recording.completed", "recording_ready", "call_recording"}:
        return "recording"
    if event in {"transcript", "transcription.completed", "transcript_ready", "call_transcript"}:
        return "transcript"
    return None


def duration_seconds(payload):
    for key in ("duration_sec", "Duration", "duration", "duration_ms", "recording_duration_sec"):
        if payload.get(key) is not None:
            return float(payload[key]) / (1000 if key == "duration_ms" else 1)
    return None


def record_receipt_state(payload, company, state):
    name = find_call(payload, company)
    category = event_category(str(payload.get("event") or payload.get("Event") or payload.get("event_type") or "").lower())
    if name and category:
        field = category + "_event_status"
        # A failed duplicate must not hide data already applied successfully.
        if frappe.db.get_value("AI Call Log", name, field) != "Applied":
            frappe.db.set_value("AI Call Log", name, field, state)
