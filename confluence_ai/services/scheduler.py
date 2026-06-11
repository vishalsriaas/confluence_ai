from __future__ import annotations

from datetime import time

import frappe

from confluence_ai.services.utils import get_queue_name, parse_json_object


def is_agent_available(agent_name: str) -> bool:
    agent = frappe.get_doc("AI Agent", agent_name)
    if not agent.enabled:
        return False

    timezone = agent.timezone or "Asia/Kolkata"
    current = frappe.utils.get_datetime_in_timezone(timezone)
    day_name = current.strftime("%A")

    working_days = {item.strip() for item in (agent.working_days or "").split(",") if item.strip()}
    if working_days and day_name not in working_days:
        return False

    schedule = parse_json_object(agent.schedule_json, "Schedule JSON")
    windows = schedule.get(day_name) or []
    if not windows:
        return True

    current_time = current.time()
    for window in windows:
        start = _parse_time(window.get("start"))
        end = _parse_time(window.get("end"))
        if start and end and start <= current_time <= end:
            return True
    return False


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    parts = [int(part) for part in value.split(":")[:2]]
    return time(parts[0], parts[1])


def process_deadlines() -> dict:
    queue = get_queue_name("deadline_queue", "agent_deadline")
    overdue = frappe.get_all(
        "AI Task",
        filters={"status": ["in", ["Queued", "Waiting", "Running"]], "deadline": ["<", frappe.utils.now()]},
        pluck="name",
        limit=1000,
    )
    for task_name in overdue:
        frappe.enqueue(
            "confluence_ai.services.scheduler.mark_deadline_missed",
            queue=queue,
            task_name=task_name,
            enqueue_after_commit=True,
        )
    return {"queued": len(overdue)}


def mark_deadline_missed(task_name: str) -> None:
    if not frappe.db.exists("AI Task", task_name):
        return
    task = frappe.get_doc("AI Task", task_name)
    if task.status in {"Completed", "Failed", "Cancelled", "Deadline Missed"}:
        return
    task.status = "Deadline Missed"
    task.save(ignore_permissions=True)
    frappe.db.commit()
