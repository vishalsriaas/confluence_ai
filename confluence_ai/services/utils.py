from __future__ import annotations

import json
from typing import Any

import frappe


def parse_json_object(value: str | dict | None, label: str = "JSON") -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise frappe.ValidationError(f"{label} must be a JSON object")
    return parsed


def parse_json_list(value: str | list | None, label: str = "JSON") -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise frappe.ValidationError(f"{label} must be a JSON array")
    return parsed


def as_json(value: Any) -> str:
    return frappe.as_json(value or {}, indent=2)


def now() -> str:
    return frappe.utils.now()


def get_request_json() -> dict:
    data = frappe.local.form_dict or {}
    if getattr(frappe.local, "request", None):
        try:
            request_json = frappe.local.request.get_json(silent=True)
            if isinstance(request_json, dict):
                return request_json
        except Exception:
            pass
    return dict(data)


def get_setting(fieldname: str, default: str | int | None = None):
    try:
        value = frappe.db.get_single_value("Confluence AI Settings", fieldname)
        return value if value not in (None, "") else default
    except Exception:
        return default


_FRAPPE_VALID_QUEUES = {"short", "default", "long"}

_CUSTOM_QUEUE_MAP = {
    # Map custom agent-army queue aliases to valid Frappe queue names
    "agent_dispatch": "default",
    "agent_voice": "long",
    "agent_whatsapp": "default",
    "agent_llm": "long",
}


def get_queue_name(fieldname: str, default: str) -> str:
    """Return a valid Frappe queue name (short / default / long).

    The Confluence AI Settings may store a custom alias like 'agent_dispatch'.
    This function maps those aliases to the correct Frappe queue so that
    ``frappe.enqueue`` does not raise a ValidationError.
    """
    raw = str(get_setting(fieldname, default) or default).strip() or default
    if raw in _FRAPPE_VALID_QUEUES:
        return raw
    return _CUSTOM_QUEUE_MAP.get(raw, "default")


def create_error(
    error_type: str,
    message: str,
    *,
    source: str | None = None,
    task: str | None = None,
    task_batch: str | None = None,
    agent: str | None = None,
    queue_name: str | None = None,
    payload: dict | None = None,
    exc: Exception | None = None,
) -> str | None:
    try:
        doc = frappe.new_doc("AI Error Log")
        doc.update(
            {
                "status": "Open",
                "severity": "Error",
                "error_type": error_type,
                "source": source,
                "agent": agent,
                "task": task,
                "task_batch": task_batch,
                "queue_name": queue_name,
                "message": message,
                "traceback": frappe.get_traceback() if exc else "",
                "payload_json": as_json(payload or {}),
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Confluence AI Error Log Failed")
        return None


def record_provider_event(
    *,
    provider: str,
    operation: str,
    status: str,
    agent: str | None = None,
    task: str | None = None,
    request: dict | None = None,
    response: dict | None = None,
    error: str | None = None,
    external_id: str | None = None,
) -> str | None:
    try:
        doc = frappe.new_doc("AI Provider Event")
        doc.update(
            {
                "status": status,
                "provider": provider,
                "operation": operation,
                "agent": agent,
                "task": task,
                "external_id": external_id,
                "request_json": as_json(request or {}),
                "response_json": as_json(response or {}),
                "error_message": error,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Confluence AI Provider Event Failed")
        return None
