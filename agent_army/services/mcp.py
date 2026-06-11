from __future__ import annotations

from urllib.parse import urljoin

import requests
import frappe

from agent_army.services.utils import as_json, parse_json_object, record_provider_event


def assert_tool_allowed(tool_name: str, agent: str | None = None, agent_group: str | None = None) -> None:
    filters = {"tool": tool_name, "enabled": 1}
    if agent:
        filters["agent"] = agent
    if agent_group:
        filters["agent_group"] = agent_group
    if not frappe.db.exists("AI Tool Permission", filters):
        raise frappe.PermissionError(f"Tool not allowed for agent: {tool_name}")


def call_tool(tool_name: str, arguments: dict, *, agent: str | None = None, agent_group: str | None = None, task: str | None = None) -> dict:
    assert_tool_allowed(tool_name, agent=agent, agent_group=agent_group)
    tool = frappe.get_doc("AI MCP Tool", tool_name)
    server = frappe.get_doc("AI MCP Server", tool.server) if tool.server else None
    base = server.server_url if server else ""
    url = tool.endpoint_url if tool.endpoint_url.startswith("http") else urljoin(base.rstrip("/") + "/", tool.endpoint_url.lstrip("/"))

    headers = {"Content-Type": "application/json"}
    if server:
        token = server.get_password("bearer_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    method = (tool.http_method or "POST").lower()
    response = requests.request(method, url, data=as_json(arguments), headers=headers, timeout=60)
    result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:4000]}
    record_provider_event(
        provider="MCP",
        operation=tool_name,
        status="Succeeded" if response.ok else "Failed",
        agent=agent,
        task=task,
        request=arguments,
        response=result,
    )
    if not response.ok:
        frappe.throw(f"MCP tool failed with HTTP {response.status_code}: {tool_name}")
    return result


def run_mcp_task(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    arguments = parse_json_object(payload.get("arguments") or payload, "MCP Arguments")
    return call_tool(
        payload.get("tool") or payload.get("tool_name"),
        arguments,
        agent=task.assigned_agent or task.target_agent,
        agent_group=task.target_group,
        task=task.name,
    )
