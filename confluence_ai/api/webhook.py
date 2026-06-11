from __future__ import annotations

import frappe

from confluence_ai.services import livekit, whatsapp_bridge
from confluence_ai.services.auth import require_access
from confluence_ai.services.utils import as_json, get_request_json


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
