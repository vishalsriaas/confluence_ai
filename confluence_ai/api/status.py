from __future__ import annotations

import frappe

from confluence_ai.services.auth import require_access


@frappe.whitelist(allow_guest=True)
def get_batch(batch_name: str) -> dict:
    require_access("status")
    batch = frappe.get_doc("AI Task Batch", batch_name)
    return {
        "name": batch.name,
        "status": batch.status,
        "source_system": batch.source_system,
        "task_template": batch.task_template,
        "record_count": batch.record_count,
        "queued_count": batch.queued_count,
        "running_count": batch.running_count,
        "completed_count": batch.completed_count,
        "failed_count": batch.failed_count,
        "cancelled_count": batch.cancelled_count,
        "deadline": batch.deadline,
    }


@frappe.whitelist(allow_guest=True)
def get_task(task_name: str) -> dict:
    require_access("status")
    task = frappe.get_doc("AI Task", task_name)
    return {
        "name": task.name,
        "status": task.status,
        "task_batch": task.task_batch,
        "assigned_agent": task.assigned_agent,
        "channel": task.channel,
        "attempt_count": task.attempt_count,
        "deadline": task.deadline,
        "last_error": task.last_error,
        "result_json": task.result_json,
    }
