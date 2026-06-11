from __future__ import annotations

import frappe

from agent_army.services.auth import require_access
from agent_army.services.callbacks import post_batch_callback
from agent_army.services.dispatcher import dispatch_batch, enqueue_task_execution, refresh_batch_counts
from agent_army.services.utils import as_json, get_queue_name, get_request_json, parse_json_list


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_task_batch(**kwargs) -> dict:
    require_access("ingest")
    payload = get_request_json()
    payload.update(kwargs or {})

    source_system = payload.get("source_system") or payload.get("source") or "external"
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key:
        existing = frappe.db.exists("AI Task Batch", {"source_system": source_system, "idempotency_key": idempotency_key})
        if existing:
            return {"status": "exists", "batch": existing}

    records = parse_json_list(payload.get("records"), "records")
    if not records:
        frappe.throw("records must contain at least one item")

    template = payload.get("task_template")
    if not template or not frappe.db.exists("AI Task Template", template):
        frappe.throw("Valid task_template is required")

    template_doc = frappe.get_doc("AI Task Template", template)
    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "status": "Queued",
            "source_system": source_system,
            "idempotency_key": idempotency_key,
            "task_template": template,
            "target_agent": payload.get("target_agent"),
            "target_group": payload.get("target_group"),
            "priority": payload.get("priority") or template_doc.default_priority or "Normal",
            "deadline": payload.get("deadline"),
            "callback_url": payload.get("callback_url"),
            "source_payload_json": as_json(payload),
        }
    )
    batch.insert(ignore_permissions=True)

    for index, record in enumerate(records, start=1):
        task = frappe.new_doc("AI Task")
        record_id = record.get("external_record_id") or record.get("name") or record.get("id") or str(index)
        task.update(
            {
                "status": "Queued",
                "task_batch": batch.name,
                "task_template": template,
                "target_agent": payload.get("target_agent"),
                "target_group": payload.get("target_group"),
                "channel": record.get("channel") or payload.get("channel") or template_doc.default_channel or "WhatsApp",
                "priority": record.get("priority") or payload.get("priority") or template_doc.default_priority or "Normal",
                "deadline": record.get("deadline") or payload.get("deadline"),
                "external_record_id": record_id,
                "external_record_type": record.get("external_record_type") or payload.get("external_record_type"),
                "idempotency_key": _task_idempotency_key(batch.name, idempotency_key, record_id),
                "context_json": as_json(record),
            }
        )
        task.insert(ignore_permissions=True)

    refresh_batch_counts(batch.name)
    frappe.enqueue(
        "agent_army.services.dispatcher.dispatch_batch",
        queue=get_queue_name("dispatch_queue", "agent_dispatch"),
        batch_name=batch.name,
        enqueue_after_commit=True,
    )
    post_batch_callback(batch.name, "batch_accepted", {"batch": batch.name, "records": len(records)})
    return {"status": "queued", "batch": batch.name, "records": len(records)}


def _task_idempotency_key(batch_name: str, batch_key: str | None, record_id: str) -> str:
    prefix = batch_key or batch_name
    return f"{prefix}:{record_id}"


@frappe.whitelist(methods=["POST"])
def dispatch_now(batch_name: str) -> dict:
    return dispatch_batch(batch_name)


@frappe.whitelist(methods=["POST"])
def enqueue_task(task_name: str) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    enqueue_task_execution(task.name, task.channel)
    return {"status": "queued", "task": task.name}
