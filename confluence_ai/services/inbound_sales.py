from __future__ import annotations

import json

import frappe

from confluence_ai.services.dispatcher import refresh_batch_counts
from confluence_ai.services.livekit import build_voice_metadata
from confluence_ai.services.sales_disease_router import apply_sales_route_context, resolve_inbound_sales_route
from confluence_ai.services.utils import as_json, now


INBOUND_EVENT = "inbound-sales-call"


def handle_vobiz_inbound_call(payload: dict) -> dict:
    """Create/update an inbound sales task from a Vobiz CallInitiated webhook."""
    if not _is_inbound_call_start(payload):
        return {"status": "ignored", "reason": "not_inbound_call_start"}

    selection = resolve_inbound_sales_route(payload)
    if not selection:
        return {"status": "no_route", "message": "No AI Sales Disease Route matched this inbound TrunkID."}

    call_uuid = _payload_call_uuid(payload)
    idem_key = f"inbound-vobiz:{call_uuid}" if call_uuid else None
    if idem_key:
        existing = frappe.db.exists("AI Task", {"idempotency_key": idem_key})
        if existing:
            task = frappe.get_doc("AI Task", existing)
            metadata = build_voice_metadata(task.name, _task_context(task))
            return {"status": "duplicate", "task": task.name, "metadata": metadata}

    target_agent = selection.get("target_agent")
    agent = frappe.get_doc("AI Agent", target_agent) if target_agent else None
    task_template = _resolve_task_template()
    if not target_agent or not task_template:
        return {
            "status": "error",
            "message": "Inbound route needs target agent and at least one AI Task Template.",
            "route": selection.get("route"),
        }

    context = _context_from_vobiz_payload(payload, selection)
    context = apply_sales_route_context(context, selection)
    # Inbound callers are waiting on the line while LiveKit asks Confluence AI
    # for metadata. Keep this path lightweight: route/agent/disease context is
    # enough for the first greeting, and the voice agent can use MCP/KB tools
    # during the conversation for patient lookup and deeper disease data.
    context["sales_context_mode"] = "deferred_for_inbound"

    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "status": "Running",
            "source_system": context.get("source_system") or "Vobiz Inbound Sales",
            "batch_label": f"Inbound Sales - {selection.get('route_name') or selection.get('disease_key') or 'Route'}",
            "task_template": task_template,
            "target_agent": target_agent,
            "priority": "High",
            "source_payload_json": as_json(payload),
        }
    )
    batch.insert(ignore_permissions=True)

    task = frappe.new_doc("AI Task")
    task.update(
        {
            "status": "Running",
            "task_batch": batch.name,
            "task_template": task_template,
            "target_agent": target_agent,
            "assigned_agent": target_agent,
            "channel": "Voice",
            "priority": "High",
            "external_record_id": call_uuid,
            "external_record_type": "Vobiz Inbound Call",
            "idempotency_key": idem_key,
            "call_uuid": call_uuid,
            "trunk_id": payload.get("TrunkID") or payload.get("trunk_id"),
            "telephony_status": payload.get("Status") or payload.get("status") or payload.get("Event"),
            "vobiz_initiated_payload": as_json(payload),
            "context_json": as_json(context),
        }
    )
    task.insert(ignore_permissions=True)

    attempt = frappe.new_doc("AI Task Attempt")
    attempt.update(
        {
            "status": "Started",
            "task": task.name,
            "task_batch": batch.name,
            "agent": target_agent,
            "channel": "Voice",
            "provider": "Vobiz/LiveKit",
            "started_at": now(),
            "external_id": call_uuid,
            "call_uuid": call_uuid,
            "trunk_id": task.trunk_id,
            "telephony_status": task.telephony_status,
            "vobiz_initiated_payload": as_json(payload),
            "request_json": as_json(context),
        }
    )
    attempt.insert(ignore_permissions=True)

    refresh_batch_counts(batch.name)
    metadata = build_voice_metadata(task.name, context)
    return {
        "status": "routed",
        "task": task.name,
        "attempt": attempt.name,
        "batch": batch.name,
        "route": selection.get("route"),
        "target_agent": target_agent,
        "metadata": metadata,
    }


def resolve_latest_inbound_metadata(payload: dict) -> dict:
    """Resolve metadata for a LiveKit inbound room that arrived without metadata."""
    task = _find_latest_inbound_task(payload)
    if not task:
        return {"status": "no_task"}

    context = _task_context(task)
    metadata = build_voice_metadata(task.name, context)
    return {
        "status": "resolved",
        "task": task.name,
        "metadata": metadata,
    }


def _find_latest_inbound_task(payload: dict):
    call_uuid = _payload_call_uuid(payload)
    if call_uuid:
        name = frappe.db.exists("AI Task", {"call_uuid": call_uuid})
        if name:
            return frappe.get_doc("AI Task", name)

    caller = _digits(payload.get("caller_phone") or payload.get("From") or payload.get("from"))
    trunk_id = str(payload.get("TrunkID") or payload.get("trunk_id") or "").strip()
    filters = {
        "channel": "Voice",
        "external_record_type": "Vobiz Inbound Call",
        "status": ["in", ["Queued", "Running", "Waiting"]],
    }

    # Vobiz and LiveKit use different trunk IDs for the same physical call:
    # Vobiz sends its account trunk UUID, while LiveKit sends ST_*. Do not make
    # trunk a hard filter here; prefer matching by caller/called phone and only
    # use trunk as an optional tie-breaker.
    candidates = frappe.get_all(
        "AI Task",
        filters=filters,
        fields=["name", "context_json", "trunk_id"],
        order_by="creation desc",
        limit=40,
    )

    trunk_matches = []
    phone_matches = []
    for row in candidates:
        context_text = row.context_json or ""
        if trunk_id and row.get("trunk_id") == trunk_id:
            trunk_matches.append(row)
        if caller and caller[-10:] and caller[-10:] in _digits(context_text):
            phone_matches.append(row)

    for row in trunk_matches + phone_matches + candidates:
        if not caller:
            return frappe.get_doc("AI Task", row.name)
        if row in phone_matches:
            return frappe.get_doc("AI Task", row.name)
    return None


def _context_from_vobiz_payload(payload: dict, selection: dict) -> dict:
    caller = payload.get("From") or payload.get("from") or payload.get("caller_phone")
    called = payload.get("To") or payload.get("to") or payload.get("called_number")
    context = {
        "event": INBOUND_EVENT,
        "source_system": "Vobiz Inbound Sales",
        "direction": "Inbound",
        "customer_phone": caller,
        "phone": caller,
        "called_number": called,
        "inbound_phone_number": called,
        "call_uuid": _payload_call_uuid(payload),
        "vobiz_trunk_id": payload.get("TrunkID") or payload.get("trunk_id"),
        "vobiz_domain": payload.get("Domain") or payload.get("domain"),
        "disease_or_concern": selection.get("disease_key"),
        "profile_key": selection.get("profile_key"),
        "build_sales_context": 1,
        "inbound_sales_context_deferred": 1,
        "payload_json": dict(payload),
    }
    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def _resolve_task_template() -> str | None:
    route_template = frappe.db.get_value("AI Event Route", {"event_value": "sales-call-required", "enabled": 1}, "task_template")
    if route_template:
        return route_template
    return frappe.db.get_value("AI Task Template", {}, "name")


def _task_context(task) -> dict:
    try:
        context = json.loads(task.context_json or "{}")
    except Exception:
        context = {}
    return context if isinstance(context, dict) else {}


def _is_inbound_call_start(payload: dict) -> bool:
    direction = str(payload.get("Direction") or payload.get("direction") or "").lower()
    event = str(payload.get("Event") or payload.get("event") or payload.get("event_type") or "").lower()
    status = str(payload.get("Status") or payload.get("status") or "").lower()
    return direction == "inbound" and event in {"callinitiated", "initiated", "dial", "ringing"} and status in {
        "",
        "initiated",
        "ringing",
        "dialing",
    }


def _payload_call_uuid(payload: dict) -> str | None:
    value = payload.get("CallUUID") or payload.get("call_uuid") or payload.get("RequestID") or payload.get("SIPCallID")
    return str(value).strip() if value else None


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())
