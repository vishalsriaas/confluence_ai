from __future__ import annotations

import frappe

from confluence_ai.services import livekit, whatsapp_bridge, vobiz
from confluence_ai.services import event_router
from confluence_ai.services.auth import require_access
from confluence_ai.services.utils import as_json, get_request_json


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_vobiz() -> dict:
    payload = get_request_json()
    event = _record_inbound("vobiz", payload)
    result = vobiz.handle_callback(payload)
    _mark_webhook_processed(event, result)
    return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_whatsapp() -> dict:
    require_access("webhook")
    payload = get_request_json()
    event = _record_inbound("whatsapp", payload)
    result = whatsapp_bridge.handle_callback(payload)
    frappe.db.set_value("AI Webhook Event", event, {"status": "Processed", "response_json": as_json(result)})
    return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_livekit() -> dict:
    require_access("webhook")
    payload = get_request_json()
    event = _record_inbound("livekit", payload)
    result = livekit.handle_callback(payload)
    frappe.db.set_value("AI Webhook Event", event, {"status": "Processed", "response_json": as_json(result)})
    return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_event(source_system: str | None = None) -> dict:
    """
    Universal inbound webhook endpoint for external systems (ERP, CRM, etc.).

    Routing is driven entirely by AI Event Route records in the UI — no code
    changes needed to support new event types.

    Optional query param:
        ?source_system=ERPNext   — used to disambiguate same event from multiple sources
    """
    payload = get_request_json()

    if _is_vobiz_payload(payload):
        webhook_event = _record_inbound("vobiz", payload)
        try:
            result = vobiz.handle_callback(payload)
            _mark_webhook_processed(webhook_event, result)
            return result
        except Exception as exc:
            frappe.db.set_value(
                "AI Webhook Event",
                webhook_event,
                {"status": "Failed", "response_json": as_json({"error": str(exc)})},
            )
            raise

    webhook_event = _record_inbound(source_system or "external", payload)

    # Find matching route
    route = event_router.find_matching_route(payload, source_system=source_system)

    if not route:
        event_key = payload.get("event") or payload.get("event_type") or "unknown"
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": "no_matching_route", "event": event_key})},
        )
        frappe.log_error(
            title="AI Event Route: no matching route",
            message=f"No enabled AI Event Route found for payload: {payload}",
        )
        return {"status": "no_route", "event": event_key, "message": "No matching AI Event Route configured for this event."}

    # Per-route optional auth: validate X-Webhook-Secret header if secret is configured
    if not event_router.validate_route_auth(route, frappe.request.headers):
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": "invalid_webhook_secret"})},
        )
        frappe.throw("Invalid or missing X-Webhook-Secret header", frappe.AuthenticationError)

    try:
        result = event_router.dispatch_from_route(route, payload)
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Processed", "response_json": as_json(result)},
        )
        return result
    except Exception as exc:
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": str(exc)})},
        )
        raise


def _is_vobiz_payload(payload: dict) -> bool:
    event_type = str(payload.get("event") or payload.get("Event") or payload.get("event_type") or "").lower()
    if event_type in {"hangup", "callinitiated", "recording.completed", "transcription.completed"}:
        return True
    return bool(
        payload.get("CallUUID")
        or payload.get("SIPCallID")
        or payload.get("recording_id")
        or payload.get("transcription_id")
        or payload.get("account_id")
        or payload.get("AccountId")
    )


def _record_inbound(source: str, payload: dict) -> str:
    doc = frappe.new_doc("AI Webhook Event")
    doc.update(
        {
            "status": "Queued",
            "direction": "Inbound",
            "event_type": payload.get("event") or payload.get("event_type") or payload.get("status") or "unknown",
            "source": source,
            "task": payload.get("task") or payload.get("task_name"),
            "task_batch": payload.get("batch") or payload.get("task_batch"),
            "signature_valid": 1,
            "payload_json": as_json(payload),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _mark_webhook_processed(webhook_event: str, result: dict) -> None:
    values = {"status": "Processed", "response_json": as_json(result)}
    if isinstance(result, dict):
        if result.get("task") and frappe.db.exists("AI Task", result.get("task")):
            values["task"] = result.get("task")
        if result.get("batch") and frappe.db.exists("AI Task Batch", result.get("batch")):
            values["task_batch"] = result.get("batch")
    frappe.db.set_value("AI Webhook Event", webhook_event, values)
