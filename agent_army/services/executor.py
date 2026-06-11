from __future__ import annotations

import frappe

from agent_army.services import livekit, llm, mcp, whatsapp_bridge
from agent_army.services.callbacks import post_batch_callback
from agent_army.services.dispatcher import refresh_batch_counts
from agent_army.services.utils import as_json, create_error, now, parse_json_object


def execute_task(task_name: str) -> dict:
    if not frappe.db.exists("AI Task", task_name):
        return {"missing": task_name}

    task = frappe.get_doc("AI Task", task_name)
    if task.status in {"Completed", "Failed", "Cancelled", "Deadline Missed"}:
        return {"skipped": task.status}

    task.status = "Running"
    task.attempt_count = int(task.attempt_count or 0) + 1
    task.save(ignore_permissions=True)
    post_batch_callback(task.task_batch, "task_started", {"task": task.name})

    attempt = frappe.new_doc("AI Task Attempt")
    attempt.update(
        {
            "status": "Started",
            "task": task.name,
            "task_batch": task.task_batch,
            "agent": task.assigned_agent,
            "channel": task.channel,
            "started_at": now(),
            "request_json": task.context_json,
        }
    )
    attempt.insert(ignore_permissions=True)

    try:
        payload = parse_json_object(task.context_json, "Task Context JSON")
        result = _run_channel(task, payload)
        task.status = "Completed"
        task.result_json = as_json(result)
        task.last_error = ""
        task.save(ignore_permissions=True)
        attempt.status = "Succeeded"
        attempt.response_json = as_json(result)
        attempt.ended_at = now()
        attempt.save(ignore_permissions=True)
        refresh_batch_counts(task.task_batch)
        post_batch_callback(task.task_batch, "task_completed", {"task": task.name, "result": result})
        return result
    except Exception as exc:
        message = str(exc)
        task.status = "Failed"
        task.last_error = message
        task.save(ignore_permissions=True)
        attempt.status = "Failed"
        attempt.error_message = message
        attempt.ended_at = now()
        attempt.save(ignore_permissions=True)
        refresh_batch_counts(task.task_batch)
        create_error("Task Execution", message, source="executor", task=task.name, task_batch=task.task_batch, agent=task.assigned_agent, exc=exc)
        post_batch_callback(task.task_batch, "task_failed", {"task": task.name, "error": message})
        raise


def _run_channel(task, payload: dict) -> dict:
    if task.channel == "WhatsApp":
        return whatsapp_bridge.send_message(task.name, payload)
    if task.channel == "Voice":
        return livekit.start_voice_task(task.name, payload)
    if task.channel == "LLM":
        return llm.run_llm_task(task.name, payload)
    if task.channel == "MCP":
        return mcp.run_mcp_task(task.name, payload)
    return {"status": "completed", "channel": task.channel or "API", "payload": payload}
