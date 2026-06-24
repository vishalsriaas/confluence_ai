from __future__ import annotations

import frappe

from confluence_ai.services import livekit, llm, mcp, whatsapp_bridge
from confluence_ai.services.callbacks import post_batch_callback
from confluence_ai.services.dispatcher import refresh_batch_counts
from confluence_ai.services.utils import as_json, create_error, now, parse_json_object


def execute_task(task_name: str) -> dict:
    if not frappe.db.exists("AI Task", task_name):
        return {"missing": task_name}

    # Lock the task row to prevent concurrent execution
    status = frappe.db.get_value("AI Task", task_name, "status", for_update=True)
    if status in {"Completed", "Failed", "Cancelled", "Deadline Missed"}:
        return {"skipped": status}

    # Check if there is already an active attempt in progress
    if frappe.db.exists("AI Task Attempt", {"task": task_name, "status": "Started"}):
        return {"skipped": "already_started"}

    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    trunk_id = None
    if agent_name:
        agent = frappe.get_doc("AI Agent", agent_name)
        if agent.allowed_channel_account:
            trunk_id = frappe.db.get_value("AI Channel Account", agent.allowed_channel_account, "trunk_id")

    task.assigned_agent = agent_name
    task.status = "Running"
    task.attempt_count = int(task.attempt_count or 0) + 1
    task.trunk_id = trunk_id
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
            "trunk_id": trunk_id,
            "started_at": now(),
            "request_json": task.context_json,
        }
    )
    attempt.insert(ignore_permissions=True)

    # Commit immediately to release database locks and make the status update visible to other transactions/workers
    frappe.db.commit()

    try:
        payload = parse_json_object(task.context_json, "Task Context JSON")
        result = _run_channel(task, payload)
        
        # Reload docs to get any potential updates from concurrent callbacks
        task = frappe.get_doc("AI Task", task_name)
        attempt = frappe.get_doc("AI Task Attempt", attempt.name)

        if task.channel == "Voice":
            task.status = "Running"
            task.result_json = as_json(result)
            task.last_error = ""
            task.save(ignore_permissions=True)
            
            attempt.status = "Started"
            attempt.response_json = as_json(result)
            if isinstance(result, dict):
                attempt.external_id = result.get("sip_call_sid") or result.get("room_sid")
            attempt.save(ignore_permissions=True)
        else:
            task.status = "Completed"
            task.result_json = as_json(result)
            task.last_error = ""
            task.save(ignore_permissions=True)
            
            attempt.status = "Succeeded"
            attempt.response_json = as_json(result)
            if isinstance(result, dict):
                attempt.external_id = result.get("sip_call_sid") or result.get("room_sid")
            attempt.ended_at = now()
            attempt.save(ignore_permissions=True)
            
        frappe.db.commit()
        refresh_batch_counts(task.task_batch)
        post_batch_callback(task.task_batch, "task_completed", {"task": task.name, "result": result})
        return result
    except Exception as exc:
        message = str(exc)
        
        try:
            task = frappe.get_doc("AI Task", task_name)
            attempt = frappe.get_doc("AI Task Attempt", attempt.name)
        except Exception:
            pass
            
        task.status = "Failed"
        task.last_error = message
        task.save(ignore_permissions=True)
        attempt.status = "Failed"
        attempt.error_message = message
        attempt.ended_at = now()
        attempt.save(ignore_permissions=True)
        
        frappe.db.commit()
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


def run_task_5():
    tasks = frappe.get_all("AI Task", filters={"name": ["like", "%5"]})
    if not tasks:
        print("No task containing '5' found.")
        return
    task_name = tasks[0].name
    print(f"Found task: {task_name}. Resetting status to Queued and executing...")
    frappe.db.set_value("AI Task", task_name, "status", "Queued")
    frappe.db.commit()
    result = execute_task(task_name)
    print(f"Result: {result}")


