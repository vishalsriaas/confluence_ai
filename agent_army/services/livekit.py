from __future__ import annotations

from urllib.parse import urljoin

import requests
import frappe

from agent_army.services.utils import as_json, create_error, parse_json_object, record_provider_event


def start_voice_task(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    account_name = agent.allowed_channel_account if agent else None
    if not account_name:
        return {"status": "skipped", "reason": "no_livekit_account"}

    account = frappe.get_doc("AI Channel Account", account_name)
    endpoints = parse_json_object(account.endpoint_paths_json, "Endpoint Paths JSON")
    operation = "outbound_call" if payload.get("phone") or payload.get("to") else "create_room"
    path = endpoints.get(operation) or endpoints.get("create_room") or "/"
    url = urljoin((account.base_url or "").rstrip("/") + "/", path.lstrip("/"))
    request_payload = {
        "task": task.name,
        "agent": agent_name,
        "room": f"agent-army-{task.name}",
        "metadata": {
            "task": task.name,
            "agent": agent_name,
            "system_prompt": agent.system_prompt if agent else "",
            "personality": agent.personality if agent else "",
            "context": payload,
        },
        "to": payload.get("phone") or payload.get("to"),
        "from": account.default_from,
    }

    headers = {"Content-Type": "application/json"}
    api_key = account.get_password("api_key")
    api_secret = account.get_password("api_secret")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}:{api_secret or ''}"

    try:
        response = requests.post(url, data=as_json(request_payload), headers=headers, timeout=30)
        result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:2000]}
        record_provider_event(
            provider=account.provider_type or "LiveKit",
            operation=operation,
            status="Succeeded" if response.ok else "Failed",
            agent=agent_name,
            task=task.name,
            request=request_payload,
            response=result,
        )
        if not response.ok:
            frappe.throw(f"LiveKit bridge failed with HTTP {response.status_code}")
        return result
    except Exception as exc:
        create_error("LiveKit", str(exc), source="livekit", task=task.name, agent=agent_name, exc=exc)
        raise


def handle_callback(payload: dict) -> dict:
    task_name = payload.get("task") or payload.get("task_name")
    event_type = payload.get("event") or payload.get("event_type")
    if task_name and frappe.db.exists("AI Task", task_name):
        task = frappe.get_doc("AI Task", task_name)
        if event_type in {"call_ended", "recording_ready", "transcript_ready"}:
            task.status = "Completed"
        elif event_type in {"call_failed", "room_failed"}:
            task.status = "Failed"
            task.last_error = payload.get("error") or event_type
        task.result_json = as_json(payload)
        task.save(ignore_permissions=True)
    return {"ok": True, "task": task_name}
