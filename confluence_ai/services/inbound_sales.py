from __future__ import annotations

import hashlib
import json

import frappe

from confluence_ai.services.dispatcher import refresh_batch_counts
from confluence_ai.services.livekit import build_voice_metadata
from confluence_ai.services.sales_disease_router import apply_sales_route_context, resolve_inbound_sales_route
from confluence_ai.services.sales_context import enrich_sales_context, enrich_start_context_tools
from confluence_ai.services.utils import as_json, create_error, now
from confluence_ai.services.call_identity import is_internal_call_id


INBOUND_EVENT = "inbound-sales-call"


def handle_vobiz_inbound_call(payload: dict) -> dict:
    """Create/update an inbound sales task from a Vobiz CallInitiated webhook."""
    if not _is_inbound_call_start(payload):
        return {"status": "ignored", "reason": "not_inbound_call_start"}

    selection = resolve_inbound_sales_route(payload)
    if not selection:
        selection = _selection_from_dispatch_metadata(payload)
    if not selection:
        return {"status": "no_route", "message": "No AI Sales Disease Route matched this inbound TrunkID."}

    call_uuid = _payload_call_uuid(payload)
    if not call_uuid or is_internal_call_id(call_uuid):
        return {"status": "pending_identity", "reason": "provider_call_id_required"}
    lock_key = "inbound-task:" + hashlib.sha256(call_uuid.encode()).hexdigest()
    # Keep the reservation across helper commits; unrelated calls use different locks.
    with frappe.cache.lock(lock_key, timeout=120, blocking_timeout=30):
        try:
            result = _handle_identified_inbound_call(payload, selection)
            frappe.db.commit()
            return result
        except Exception:
            frappe.db.rollback()
            raise


def _handle_identified_inbound_call(payload: dict, selection: dict) -> dict:
    call_uuid = _payload_call_uuid(payload)
    idem_key = f"inbound-vobiz:{call_uuid}" if call_uuid else None
    if idem_key:
        existing = frappe.db.get_value("AI Task", {"idempotency_key": idem_key}, "name", for_update=True)
        if existing:
            task = frappe.get_doc("AI Task", existing)
            if selection.get("company") and task.company != selection["company"]:
                frappe.throw("Inbound call identity belongs to a different company.")
            metadata = build_voice_metadata(task.name, _task_context(task))
            return {"status": "duplicate", "task": task.name, "metadata": metadata}

    existing_livekit_task = _find_latest_inbound_task(payload)
    if existing_livekit_task:
        if selection.get("company") and existing_livekit_task.company != selection["company"]:
            frappe.throw("Inbound call identity belongs to a different company.")
        _attach_vobiz_payload_to_existing_task(existing_livekit_task, payload, selection, idem_key)
        metadata = build_voice_metadata(existing_livekit_task.name, _task_context(existing_livekit_task))
        return {
            "status": "duplicate",
            "reason": "matched_existing_livekit_inbound_task",
            "task": existing_livekit_task.name,
            "metadata": metadata,
        }

    target_agent = selection.get("target_agent")
    agent = frappe.get_doc("AI Agent", target_agent) if target_agent else None
    company = selection.get("company") or (agent.company if agent else None)
    task_template = _resolve_task_template(selection, payload)
    if not target_agent or not task_template:
        return {
            "status": "error",
            "message": "Inbound route needs target agent and at least one AI Task Template.",
            "route": selection.get("route"),
        }

    context = _context_from_vobiz_payload(payload, selection)
    context = apply_sales_route_context(context, selection)

    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "status": "Running",
            "source_system": context.get("source_system") or "Vobiz Inbound Sales",
            "batch_label": f"Inbound Sales - {selection.get('route_name') or selection.get('disease_key') or 'Route'}",
            "task_template": task_template,
            "target_agent": target_agent,
            "company": company,
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
            "company": company,
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

    context = enrich_start_context_tools(context, agent=agent, task_id=task.name)
    context = enrich_sales_context(context, agent=agent, task_id=task.name)
    context["sales_context_mode"] = "prepared_for_inbound"
    context.pop("inbound_sales_context_deferred", None)
    task.context_json = as_json(context)
    task.save(ignore_permissions=True)
    fresh_followup_result = _maybe_start_fresh_followup_from_task(task, payload, context)

    attempt = frappe.new_doc("AI Task Attempt")
    attempt.update(
        {
            "status": "Started",
            "task": task.name,
            "task_batch": batch.name,
            "agent": target_agent,
            "company": company,
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

    # Create the human-facing call log as soon as the inbound call is resolved.
    # Waiting for the agent's first callback loses the call entirely when agent
    # startup or callback correlation fails.
    from confluence_ai.services.vobiz import upsert_call_log

    call_log = upsert_call_log(payload, task=task, attempt=attempt)

    refresh_batch_counts(batch.name)
    metadata = build_voice_metadata(task.name, context)
    return {
        "status": "routed",
        "task": task.name,
        "attempt": attempt.name,
        "batch": batch.name,
        "route": selection.get("route"),
        "target_agent": target_agent,
        "channel_account": selection.get("channel_account"),
        "call_log": call_log,
        "metadata": metadata,
        "fresh_followup": fresh_followup_result,
    }


def _maybe_start_fresh_followup_from_task(task, payload: dict, context: dict) -> dict | None:
    try:
        from confluence_ai.services import fresh_followup

        return fresh_followup.maybe_start_from_task(task, payload=payload, context=context)
    except Exception as exc:
        create_error("Fresh Follow Up Attach", str(exc), source="inbound_sales", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _attach_vobiz_payload_to_existing_task(task, payload: dict, selection: dict, idem_key: str | None = None) -> None:
    """Attach the real Vobiz webhook to a task already created by LiveKit resolver.

    Vobiz and LiveKit can race on inbound SIP calls. When LiveKit asks for
    metadata first, a task already exists before the Vobiz CallInitiated webhook
    arrives. In that case, update the existing task instead of creating a second
    task for the same phone/called-number room.
    """
    context = _task_context(task)
    incoming_context = _context_from_vobiz_payload(payload, selection)
    context.update({key: value for key, value in incoming_context.items() if value not in (None, "", [], {})})
    context = apply_sales_route_context(context, selection)
    agent = frappe.get_doc("AI Agent", selection.get("target_agent")) if selection.get("target_agent") else None
    context = enrich_start_context_tools(context, agent=agent, task_id=task.name)

    call_uuid = _payload_call_uuid(payload)
    values = {
        "status": "Running",
        "external_record_type": "Vobiz Inbound Call",
        "external_record_id": call_uuid or task.external_record_id,
        "idempotency_key": idem_key or task.idempotency_key,
        "call_uuid": call_uuid or task.call_uuid,
        "trunk_id": payload.get("TrunkID") or payload.get("trunk_id") or task.trunk_id,
        "telephony_status": payload.get("Status") or payload.get("status") or payload.get("Event") or task.telephony_status,
        "vobiz_initiated_payload": as_json(payload),
        "context_json": as_json(context),
    }
    task.update({key: value for key, value in values.items() if value not in (None, "", [], {})})
    task.save(ignore_permissions=True)

    attempts = frappe.get_all(
        "AI Task Attempt",
        filters={"task": task.name},
        order_by="creation desc",
        limit=1,
    )
    attempt = frappe.get_doc("AI Task Attempt", attempts[0].name) if attempts else None
    if attempt:
        attempt.call_uuid = call_uuid or attempt.call_uuid
        attempt.trunk_id = task.trunk_id or attempt.trunk_id
        attempt.telephony_status = task.telephony_status or attempt.telephony_status
        attempt.vobiz_initiated_payload = as_json(payload)
        attempt.request_json = as_json(context)
        attempt.save(ignore_permissions=True)

    from confluence_ai.services.vobiz import upsert_call_log

    upsert_call_log(payload, task=task, attempt=attempt)
    if task.task_batch:
        refresh_batch_counts(task.task_batch)


def resolve_latest_inbound_metadata(payload: dict) -> dict:
    """Resolve metadata for a LiveKit inbound room that arrived without metadata."""
    # Older workers send SCL_* at the top level but include the full SIP ID here.
    provider_ids = {
        str((participant.get("attributes") or {}).get("sip.callIDFull") or "").strip()
        for participant in (payload.get("participants") or [])
        if isinstance(participant, dict) and isinstance(participant.get("attributes"), dict)
    } - {""}
    supplied_id = _payload_call_uuid(payload)
    if supplied_id and not is_internal_call_id(supplied_id):
        provider_ids.add(supplied_id)
    if len(provider_ids) != 1:
        return {"status": "pending_identity", "reason": "provider_call_id_missing_or_ambiguous"}
    provider_id = provider_ids.pop()
    if is_internal_call_id(provider_id):
        return {"status": "pending_identity", "reason": "provider_call_id_required"}
    payload = {**payload, "call_uuid": provider_id, "CallUUID": provider_id}
    created = _create_task_from_livekit_resolve_payload(payload)
    if created.get("status") not in {"routed", "duplicate"} or not created.get("task"):
        return created
    return {
        "status": "resolved",
        "task": created["task"],
        "metadata": created["metadata"],
    }


def _create_task_from_livekit_resolve_payload(payload: dict) -> dict:
    """Create an inbound sales task directly from LiveKit SIP metadata.

    LiveKit can connect the SIP room before the Vobiz recording/status webhook
    reaches Confluence AI. In that case the worker asks this resolver for room
    metadata and previously received ``no_task``, causing a generic no-prompt
    session. This fallback creates the same inbound task shape using the SIP
    metadata already present in the LiveKit room.
    """
    caller = payload.get("caller_phone") or payload.get("From") or payload.get("from")
    called = payload.get("called_number") or payload.get("To") or payload.get("to")
    call_uuid = _payload_call_uuid(payload)
    trunk_id = payload.get("trunk_id") or payload.get("TrunkID")
    domain = payload.get("domain") or payload.get("Domain")

    if not (caller or called or call_uuid or trunk_id):
        return {"status": "no_task"}

    synthesized = {
        "Direction": "inbound",
        "Event": "CallInitiated",
        "Status": "initiated",
        "From": caller,
        "To": called,
        "CallUUID": call_uuid,
        "RequestID": call_uuid,
        "SIPCallID": call_uuid,
        "TrunkID": trunk_id,
        "Domain": domain,
        "source": "livekit_inbound_resolver",
    }
    synthesized = {key: value for key, value in synthesized.items() if value not in (None, "", [], {})}
    return handle_vobiz_inbound_call(synthesized)


def _find_latest_inbound_task(payload: dict):
    call_uuid = _payload_call_uuid(payload)
    if call_uuid and not is_internal_call_id(call_uuid):
        name = frappe.db.get_value("AI Task", {"call_uuid": call_uuid}, "name", for_update=True)
        if name:
            return frappe.get_doc("AI Task", name)
    return None


def _context_from_vobiz_payload(payload: dict, selection: dict) -> dict:
    raw_caller = payload.get("From") or payload.get("from") or payload.get("caller_phone")
    raw_called = payload.get("To") or payload.get("to") or payload.get("called_number")
    caller = _phone_for_context(raw_caller, prefer_ten_digit=True)
    called = _phone_for_context(raw_called)
    agent = frappe.get_doc("AI Agent", selection.get("target_agent")) if selection.get("target_agent") else None
    build_sales_context = 1 if agent and getattr(agent, "enable_sales_context", 0) else 0
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
        "channel_account": selection.get("channel_account") or payload.get("channel_account"),
        "ai_agent": selection.get("target_agent") or payload.get("ai_agent"),
        "business_unit": payload.get("business_unit"),
        "disease_or_concern": selection.get("disease_key"),
        "profile_key": selection.get("profile_key"),
        "payload_json": dict(payload),
    }
    if build_sales_context:
        context["build_sales_context"] = 1
        context["inbound_sales_context_deferred"] = 1
    if raw_caller and raw_caller != caller:
        context["raw_customer_phone"] = raw_caller
    if raw_called and raw_called != called:
        context["raw_called_number"] = raw_called
    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def _resolve_task_template(selection: dict | None = None, payload: dict | None = None) -> str | None:
    selection = selection or {}
    payload = payload or {}

    explicit_template = payload.get("task_template")
    if explicit_template and frappe.db.exists("AI Task Template", explicit_template):
        return explicit_template

    explicit_route = payload.get("event_route")
    if explicit_route and frappe.db.exists("AI Event Route", explicit_route):
        template = frappe.db.get_value("AI Event Route", explicit_route, "task_template")
        if template:
            return template

    target_agent = selection.get("target_agent") or payload.get("ai_agent")
    if target_agent:
        route_template = frappe.db.get_value(
            "AI Event Route",
            {"enabled": 1, "target_agent": target_agent, "channel": "Voice"},
            "task_template",
        )
        if route_template:
            return route_template

    route_template = frappe.db.get_value("AI Event Route", {"event_value": "sales-call-required", "enabled": 1}, "task_template")
    if route_template:
        return route_template
    return frappe.db.get_value("AI Task Template", {}, "name")


def _selection_from_dispatch_metadata(payload: dict) -> dict:
    """Use authenticated LiveKit dispatch metadata as an explicit route hint."""
    channel_account = str(payload.get("channel_account") or "").strip()
    target_agent = str(payload.get("ai_agent") or payload.get("agent") or "").strip()
    if not channel_account or not frappe.db.exists("AI Channel Account", channel_account):
        return {}

    account = frappe.get_doc("AI Channel Account", channel_account)
    if not account.enabled:
        return {}

    if not target_agent:
        target_agent = frappe.db.get_value(
            "AI Agent",
            {"enabled": 1, "allowed_channel_account": channel_account},
            "name",
        )
    if not target_agent or not frappe.db.exists("AI Agent", target_agent):
        return {}

    agent = frappe.get_doc("AI Agent", target_agent)
    if not agent.enabled or agent.allowed_channel_account != channel_account:
        return {}

    return {
        "route": None,
        "route_name": f"LiveKit Dispatch - {channel_account}",
        "disease_key": payload.get("disease_or_concern") or "liver",
        "matched_alias": "livekit_dispatch_metadata",
        "target_agent": target_agent,
        "channel_account": channel_account,
        "profile_key": payload.get("profile_key") or payload.get("business_unit"),
        "outbound_phone_number": payload.get("outbound_phone_number"),
        "sip_trunk_id": payload.get("outbound_sip_trunk_id") or payload.get("sip_trunk_id"),
        "sip_uri": payload.get("sip_uri") or payload.get("inbound_domain"),
    }


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
    value = payload.get("SIPCallID") or payload.get("sip_call_id") or payload.get("CallUUID") or payload.get("call_uuid")
    return str(value).strip() if value else None


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone_for_context(value, *, prefer_ten_digit: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    digits = _digits(text)
    if digits.startswith("00") and len(digits) > 10:
        digits = digits[2:]
    if len(digits) >= 10:
        ten_digit = digits[-10:]
        if prefer_ten_digit:
            return ten_digit
        return f"+91{ten_digit}" if len(ten_digit) == 10 else f"+{digits}"
    return text
