from __future__ import annotations

import frappe

from confluence_ai.services.auth import require_access
from confluence_ai.services.inbound_sales import resolve_latest_inbound_metadata
from confluence_ai.services.utils import as_json, get_request_json


@frappe.whitelist(allow_guest=True, methods=["POST"])
def resolve_call() -> dict:
    """Resolve prepared inbound-call metadata for the LiveKit worker."""
    require_access("mcp")
    payload = get_request_json()
    webhook_event = None
    if frappe.db.exists("DocType", "AI Webhook Event"):
        event = frappe.new_doc("AI Webhook Event")
        event.update(
            {
                "status": "Queued",
                "direction": "Inbound",
                "event_type": "inbound_resolve",
                "source": "livekit_resolver",
                "signature_valid": 1,
                "payload_json": as_json(payload),
            }
        )
        event.insert(ignore_permissions=True)
        webhook_event = event.name

    try:
        result = resolve_latest_inbound_metadata(payload)
    except Exception as exc:
        if webhook_event:
            frappe.db.set_value(
                "AI Webhook Event",
                webhook_event,
                {
                    "status": "Failed",
                    "error_message": str(exc),
                    "response_json": as_json({"status": "error", "message": str(exc)}),
                },
            )
        raise

    if webhook_event:
        values = {"status": "Processed", "response_json": as_json(result)}
        task_name = result.get("task") if isinstance(result, dict) else None
        if task_name and frappe.db.exists("AI Task", task_name):
            values["task"] = task_name
        frappe.db.set_value("AI Webhook Event", webhook_event, values)

    return result
