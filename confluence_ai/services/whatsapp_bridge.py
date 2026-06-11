from __future__ import annotations

from urllib.parse import urljoin

import requests
import frappe

from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event


def send_message(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    account_name = agent.allowed_channel_account if agent else None

    if not account_name:
        return {"status": "skipped", "reason": "no_channel_account"}

    account = frappe.get_doc("AI Channel Account", account_name)
    endpoints = parse_json_object(account.endpoint_paths_json, "Endpoint Paths JSON")
    path = endpoints.get("send") or "/api/method/send"
    url = urljoin((account.base_url or "").rstrip("/") + "/", path.lstrip("/"))
    request_payload = {
        "task": task.name,
        "agent": agent_name,
        "channel": "WhatsApp",
        "context": payload,
    }

    headers = {"Content-Type": "application/json"}
    token = account.get_password("api_key")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.post(url, data=as_json(request_payload), headers=headers, timeout=30)
        result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:2000]}
        record_provider_event(
            provider=account.provider_type or "WhatsApp Bridge",
            operation="send_message",
            status="Succeeded" if response.ok else "Failed",
            agent=agent_name,
            task=task.name,
            request=request_payload,
            response=result,
        )
        if not response.ok:
            frappe.throw(f"WhatsApp bridge failed with HTTP {response.status_code}")
        return result
    except Exception as exc:
        create_error("WhatsApp Bridge", str(exc), source="whatsapp_bridge", task=task.name, agent=agent_name, exc=exc)
        raise


def handle_callback(payload: dict) -> dict:
    task_name = payload.get("task") or payload.get("task_name")
    if task_name and frappe.db.exists("AI Task", task_name):
        task = frappe.get_doc("AI Task", task_name)
        status = payload.get("status")
        if status in {"delivered", "read", "sent"}:
            task.status = "Completed"
        elif status == "failed":
            task.status = "Failed"
            task.last_error = payload.get("error") or "WhatsApp callback failed"
        task.result_json = as_json(payload)
        task.save(ignore_permissions=True)
    return {"ok": True, "task": task_name}
