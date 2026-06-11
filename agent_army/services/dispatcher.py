from __future__ import annotations

import frappe

from agent_army.services.callbacks import post_batch_callback
from agent_army.services.scheduler import is_agent_available
from agent_army.services.utils import get_queue_name, get_setting


CHANNEL_QUEUE_FIELD = {
    "WhatsApp": ("whatsapp_queue", "agent_whatsapp"),
    "Voice": ("voice_queue", "agent_voice"),
    "LLM": ("llm_queue", "agent_llm"),
    "MCP": ("llm_queue", "agent_llm"),
    "API": ("dispatch_queue", "agent_dispatch"),
}


def enqueue_ready_batches() -> dict:
    batch_size = int(get_setting("dispatch_batch_size", 500) or 500)
    batches = frappe.get_all(
        "AI Task Batch",
        filters={"status": ["in", ["Queued", "Running", "Dispatching"]]},
        pluck="name",
        limit=batch_size,
    )
    queue = get_queue_name("dispatch_queue", "agent_dispatch")
    for batch_name in batches:
        frappe.enqueue(
            "agent_army.services.dispatcher.dispatch_batch",
            queue=queue,
            batch_name=batch_name,
            enqueue_after_commit=True,
        )
    return {"queued": len(batches)}


def dispatch_batch(batch_name: str, limit: int | None = None) -> dict:
    if not frappe.db.exists("AI Task Batch", batch_name):
        return {"missing": batch_name}

    batch = frappe.get_doc("AI Task Batch", batch_name)
    if batch.status in {"Paused", "Completed", "Failed", "Cancelled"}:
        return {"skipped": batch.status}

    batch.status = "Dispatching"
    batch.save(ignore_permissions=True)

    task_names = frappe.get_all(
        "AI Task",
        filters={"task_batch": batch_name, "status": "Queued"},
        pluck="name",
        limit=limit or int(get_setting("dispatch_batch_size", 500) or 500),
        order_by="priority desc, creation asc",
    )

    dispatched = 0
    waiting = 0
    for task_name in task_names:
        task = frappe.get_doc("AI Task", task_name)
        agent_name = assign_agent(task)
        if not agent_name:
            task.status = "Waiting"
            task.last_error = "No available agent"
            task.save(ignore_permissions=True)
            waiting += 1
            continue

        task.assigned_agent = agent_name
        task.status = "Running"
        task.save(ignore_permissions=True)
        enqueue_task_execution(task.name, task.channel)
        dispatched += 1

    refresh_batch_counts(batch_name)
    batch.reload()
    if batch.completed_count + batch.failed_count + batch.cancelled_count >= batch.record_count and batch.record_count:
        batch.status = "Completed" if not batch.failed_count else "Failed"
        batch.save(ignore_permissions=True)
        post_batch_callback(batch.name, "batch_completed", {"status": batch.status})
    elif batch.status == "Dispatching":
        batch.status = "Running"
        batch.save(ignore_permissions=True)

    return {"dispatched": dispatched, "waiting": waiting}


def assign_agent(task) -> str | None:
    if task.target_agent and frappe.db.exists("AI Agent", task.target_agent) and is_agent_available(task.target_agent):
        return task.target_agent

    if task.target_group:
        agents = _agents_for_group(task.target_group)
        for agent_name in agents:
            if is_agent_available(agent_name) and _active_task_count(agent_name) < _max_concurrency(agent_name):
                return agent_name

    agents = frappe.get_all("AI Agent", filters={"enabled": 1}, pluck="name", limit=100)
    for agent_name in agents:
        if is_agent_available(agent_name) and _active_task_count(agent_name) < _max_concurrency(agent_name):
            return agent_name
    return None


def _agents_for_group(group_name: str) -> list[str]:
    group = frappe.get_doc("AI Agent Group", group_name)
    if not group.enabled:
        return []
    return frappe.get_all("AI Agent", filters={"enabled": 1}, pluck="name", limit=5000)


def _active_task_count(agent_name: str) -> int:
    return frappe.db.count("AI Task", {"assigned_agent": agent_name, "status": ["in", ["Running", "Waiting"]]})


def _max_concurrency(agent_name: str) -> int:
    value = frappe.db.get_value("AI Agent", agent_name, "max_concurrency") or 1
    return max(1, int(value))


def enqueue_task_execution(task_name: str, channel: str) -> None:
    queue_field, default_queue = CHANNEL_QUEUE_FIELD.get(channel or "API", ("dispatch_queue", "agent_dispatch"))
    frappe.enqueue(
        "agent_army.services.executor.execute_task",
        queue=get_queue_name(queue_field, default_queue),
        task_name=task_name,
        enqueue_after_commit=True,
    )


def refresh_batch_counts(batch_name: str) -> None:
    counts = {
        "record_count": frappe.db.count("AI Task", {"task_batch": batch_name}),
        "queued_count": frappe.db.count("AI Task", {"task_batch": batch_name, "status": "Queued"}),
        "running_count": frappe.db.count("AI Task", {"task_batch": batch_name, "status": "Running"}),
        "completed_count": frappe.db.count("AI Task", {"task_batch": batch_name, "status": "Completed"}),
        "failed_count": frappe.db.count("AI Task", {"task_batch": batch_name, "status": "Failed"}),
        "cancelled_count": frappe.db.count("AI Task", {"task_batch": batch_name, "status": "Cancelled"}),
    }
    frappe.db.set_value("AI Task Batch", batch_name, counts, update_modified=True)
