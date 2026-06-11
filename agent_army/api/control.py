from __future__ import annotations

import frappe

from agent_army.services.auth import require_access
from agent_army.services.dispatcher import enqueue_task_execution, refresh_batch_counts


@frappe.whitelist(allow_guest=True, methods=["POST"])
def pause_batch(batch_name: str) -> dict:
    require_access("control")
    frappe.db.set_value("AI Task Batch", batch_name, "status", "Paused")
    return {"status": "paused", "batch": batch_name}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def resume_batch(batch_name: str) -> dict:
    require_access("control")
    frappe.db.set_value("AI Task Batch", batch_name, "status", "Queued")
    frappe.enqueue(
        "agent_army.services.dispatcher.dispatch_batch",
        queue="agent_dispatch",
        batch_name=batch_name,
        enqueue_after_commit=True,
    )
    return {"status": "queued", "batch": batch_name}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def cancel_batch(batch_name: str) -> dict:
    require_access("control")
    frappe.db.set_value("AI Task Batch", batch_name, "status", "Cancelled")
    for task_name in frappe.get_all("AI Task", filters={"task_batch": batch_name, "status": ["!=", "Completed"]}, pluck="name"):
        frappe.db.set_value("AI Task", task_name, "status", "Cancelled")
    refresh_batch_counts(batch_name)
    return {"status": "cancelled", "batch": batch_name}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def retry_task(task_name: str) -> dict:
    require_access("control")
    task = frappe.get_doc("AI Task", task_name)
    task.status = "Queued"
    task.last_error = ""
    task.save(ignore_permissions=True)
    enqueue_task_execution(task.name, task.channel)
    return {"status": "queued", "task": task.name}
