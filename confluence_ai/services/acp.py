from __future__ import annotations

import frappe

from confluence_ai.services.utils import as_json


def create_event(
    event_type: str,
    *,
    from_agent: str | None = None,
    to_agent: str | None = None,
    to_group: str | None = None,
    task: str | None = None,
    message: str | None = None,
    context: dict | None = None,
) -> str:
    event = frappe.new_doc("AI ACP Event")
    event.update(
        {
            "status": "Queued",
            "event_type": event_type,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "to_group": to_group,
            "task": task,
            "message": message,
            "context_json": as_json(context or {}),
        }
    )
    event.insert(ignore_permissions=True)
    return event.name


def deliver_event(event_name: str) -> dict:
    event = frappe.get_doc("AI ACP Event", event_name)
    event.status = "Delivered"
    event.save(ignore_permissions=True)
    return {"delivered": event_name}
