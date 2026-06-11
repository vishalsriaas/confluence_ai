from __future__ import annotations

import requests
import frappe

from confluence_ai.services.auth import sign_payload
from confluence_ai.services.utils import as_json, create_error


def post_batch_callback(batch_name: str, event_type: str, payload: dict) -> dict:
    batch = frappe.get_doc("AI Task Batch", batch_name)
    if not batch.callback_url:
        return {"skipped": True, "reason": "no_callback_url"}

    body = as_json({"event": event_type, "batch": batch_name, "payload": payload})
    secret = frappe.conf.get("confluence_ai_callback_secret") or ""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Agent-Army-Signature"] = sign_payload(secret, body)

    event = frappe.new_doc("AI Webhook Event")
    event.update(
        {
            "direction": "Outbound",
            "event_type": event_type,
            "source": "callback",
            "task_batch": batch_name,
            "payload_json": body,
        }
    )
    event.insert(ignore_permissions=True)

    try:
        response = requests.post(batch.callback_url, data=body, headers=headers, timeout=20)
        event.status = "Processed" if response.ok else "Failed"
        event.response_json = as_json({"status_code": response.status_code, "body": response.text[:2000]})
        event.save(ignore_permissions=True)
        return {"status_code": response.status_code, "ok": response.ok}
    except Exception as exc:
        event.status = "Failed"
        event.error_message = str(exc)
        event.save(ignore_permissions=True)
        create_error("Callback", str(exc), source="callbacks", task_batch=batch_name, exc=exc)
        return {"ok": False, "error": str(exc)}
