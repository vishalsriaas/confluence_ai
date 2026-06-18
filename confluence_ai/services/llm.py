from __future__ import annotations

from urllib.parse import urljoin

import requests
import frappe

from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event


def run_llm_task(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    providers = [agent.primary_provider if agent else "Gemini", agent.fallback_provider if agent else "OpenAI"]
    last_error: Exception | None = None

    for provider in [provider for provider in providers if provider]:
        try:
            return _run_provider(provider, task, agent, payload)
        except Exception as exc:
            last_error = exc
            create_error("LLM", str(exc), source=provider, task=task.name, agent=agent_name, exc=exc)

    if last_error:
        raise last_error
    return {"status": "skipped", "reason": "no_provider"}


def _run_provider(provider: str, task, agent, payload: dict) -> dict:
    model_config = parse_json_object(agent.model_config, "Model Config") if agent else {}
    provider_config = model_config.get(provider) or model_config.get(provider.lower()) or {}
    base_url = provider_config.get("base_url")
    if not base_url:
        result = {"status": "planned", "provider": provider, "reason": "provider_base_url_not_configured"}
        record_provider_event(provider=provider, operation="llm_task", status="Succeeded", agent=agent.name, task=task.name, response=result)
        return result

    request_payload = {
        "model": provider_config.get("model"),
        "system_prompt": agent.get_system_prompt() if agent else "",
        "personality": agent.personality if agent else "",
        "task_context": payload,
    }
    headers = {"Content-Type": "application/json"}
    if provider_config.get("api_key"):
        headers["Authorization"] = f"Bearer {provider_config.get('api_key')}"
    url = urljoin(base_url.rstrip("/") + "/", (provider_config.get("path") or "").lstrip("/"))
    response = requests.post(url, data=as_json(request_payload), headers=headers, timeout=int(provider_config.get("timeout") or 60))
    result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:4000]}
    record_provider_event(
        provider=provider,
        operation="llm_task",
        status="Succeeded" if response.ok else "Failed",
        agent=agent.name if agent else None,
        task=task.name,
        request=request_payload,
        response=result,
    )
    if not response.ok:
        frappe.throw(f"{provider} failed with HTTP {response.status_code}")
    return result
