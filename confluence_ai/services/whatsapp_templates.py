from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import frappe
import requests
from frappe.utils.password import get_decrypted_password

from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event


def send_mapped_whatsapp_template(
    arguments: dict,
    *,
    task_id: str | None = None,
    agent: str | None = None,
) -> dict:
    phone = _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile"))
    intent = str(arguments.get("intent") or arguments.get("template_intent") or "").strip()
    disease = str(arguments.get("disease_or_concern") or arguments.get("disease") or arguments.get("topic") or "").strip()
    message = str(arguments.get("message") or arguments.get("details") or arguments.get("content") or "").strip()

    context = _task_context(task_id)
    called_number = _clean_phone(
        arguments.get("did_number")
        or arguments.get("from_number")
        or context.get("outbound_phone_number")
        or context.get("called_number")
        or context.get("inbound_phone_number")
    )
    profile_key = str(arguments.get("profile_key") or context.get("profile_key") or "").strip()

    mapping = _find_template_map(
        did_number=called_number,
        profile_key=profile_key,
        disease=disease or str(context.get("disease_or_concern") or ""),
        intent=intent,
    )
    if not mapping:
        frappe.throw("No active AI WhatsApp Template Map matched this request.")

    body_values = _render_json_values(mapping.body_values_json, message=message, arguments=arguments, context=context)
    if not body_values and (message or mapping.default_message):
        body_values = {"1": message or mapping.default_message}
    header_values = _render_json_values(mapping.header_values_json, message=message, arguments=arguments, context=context)
    button_values = _render_json_values(mapping.button_values_json, message=message, arguments=arguments, context=context)
    extra_payload = _render_json_values(mapping.extra_payload_json, message=message, arguments=arguments, context=context)

    payload = {
        "channel_account": mapping.channel_account,
        "template_name": mapping.template_name,
        "language_code": mapping.language_code or "en",
        "to": phone or _clean_phone(context.get("customer_phone") or context.get("phone")),
        "body_values": body_values,
        "header_values": header_values,
        "button_values": button_values,
        "message": message or mapping.default_message,
        "task": task_id,
        "agent": agent,
    }
    if isinstance(extra_payload, dict):
        payload.update(extra_payload)

    try:
        result = _send_template(mapping, payload)
        record_provider_event(
            provider="WhatsApp",
            operation="send_mapped_whatsapp_template",
            status="Succeeded",
            agent=agent,
            task=task_id,
            request={
                "map": mapping.name,
                "phone": payload.get("to"),
                "template_name": mapping.template_name,
                "intent": intent,
            },
            response=result,
        )
        return {"status": "success", "map": mapping.name, "result": result}
    except Exception as exc:
        create_error(
            "WhatsApp Template Send Failed",
            str(exc),
            source="WhatsApp",
            task=task_id,
            agent=agent,
            payload={"map": mapping.name, "request": payload},
            exc=exc,
        )
        raise


def _send_template(mapping, payload: dict) -> dict:
    remote_server = getattr(mapping, "remote_mcp_server", None)
    method_path = getattr(mapping, "remote_send_method", None) or mapping.send_method

    if not remote_server and str(method_path or "").startswith("wa_chat_hub."):
        remote_server = _default_remote_frappe_server()

    if remote_server:
        return _call_remote_whatsapp_method(remote_server, method_path, payload)

    return _call_send_method(mapping.send_method, payload)


def _call_remote_whatsapp_method(server_name: str, method_path: str, payload: dict) -> dict:
    if not method_path:
        frappe.throw("WhatsApp template map has no remote send method configured.")

    phone = _clean_phone(payload.get("to"))
    if not phone:
        frappe.throw("WhatsApp recipient phone number is required.")
    if not payload.get("channel_account"):
        frappe.throw("WhatsApp channel account is required.")

    conversation = payload.get("conversation")
    if not conversation:
        contact = _remote_find_or_create_chat_contact(
            server_name,
            phone=phone,
            display_name=payload.get("customer_name") or payload.get("patient_name") or payload.get("name"),
        )
        conversation = _remote_find_or_create_chat_conversation(
            server_name,
            channel_account=payload["channel_account"],
            contact=contact,
        )

    remote_payload = {
        **payload,
        "conversation": conversation,
        "template_name": payload.get("template_name"),
        "name": payload.get("template_name"),
        "body": payload.get("message") or f"Template: {payload.get('template_name')}",
        "sender_type": payload.get("sender_type") or "Agent",
    }
    return _remote_frappe_method(server_name, method_path, remote_payload)


def _remote_find_or_create_chat_contact(server_name: str, *, phone: str, display_name: str | None = None) -> str:
    variants = _phone_variants(phone)
    for value in variants:
        result = _remote_frappe_list(
            server_name,
            "Chat Contact",
            filters=[["phone_number", "=", value]],
            fields=["name", "phone_number"],
            limit=1,
        )
        if result.get("ok") and result.get("data"):
            return result["data"][0].get("name")

    create_result = _remote_frappe_create(
        server_name,
        "Chat Contact",
        {
            "phone_number": phone,
            "display_name": display_name or phone,
            "source_doctype": "Confluence AI",
            "source_name": "AI WhatsApp Template Map",
        },
    )
    if create_result.get("ok") and isinstance(create_result.get("data"), dict):
        return create_result["data"].get("name") or phone

    retry = _remote_frappe_list(
        server_name,
        "Chat Contact",
        filters=[["phone_number", "=", phone]],
        fields=["name", "phone_number"],
        limit=1,
    )
    if retry.get("ok") and retry.get("data"):
        return retry["data"][0].get("name")

    frappe.throw(f"Remote WhatsApp contact create failed: {create_result.get('error')}")


def _remote_find_or_create_chat_conversation(server_name: str, *, channel_account: str, contact: str) -> str:
    result = _remote_frappe_list(
        server_name,
        "Chat Conversation",
        filters=[["channel_account", "=", channel_account], ["contact", "=", contact], ["status", "!=", "Closed"]],
        fields=["name", "channel_account", "contact"],
        limit=1,
    )
    if result.get("ok") and result.get("data"):
        return result["data"][0].get("name")

    create_result = _remote_frappe_create(
        server_name,
        "Chat Conversation",
        {
            "channel_account": channel_account,
            "contact": contact,
            "status": "Open",
            "priority": "Medium",
        },
    )
    if create_result.get("ok") and isinstance(create_result.get("data"), dict):
        return create_result["data"].get("name")
    frappe.throw(f"Remote WhatsApp conversation create failed: {create_result.get('error')}")


def _task_context(task_id: str | None) -> dict:
    if not task_id or not frappe.db.exists("AI Task", task_id):
        return {}
    task = frappe.get_doc("AI Task", task_id)
    return parse_json_object(task.context_json, "AI Task Context") if task.context_json else {}


def _default_remote_frappe_server() -> str | None:
    preferred = frappe.db.get_value("AI MCP Server", {"server_name": "Remote Dev SR Frappe", "enabled": 1}, "name")
    if preferred:
        return preferred
    return frappe.db.get_value("AI MCP Server", {"enabled": 1}, "name")


def _mcp_server_connection(server_name: str) -> tuple[str, dict]:
    values = frappe.db.get_value("AI MCP Server", server_name, ["server_url", "api_key"], as_dict=True)
    if not values:
        return "", {"Content-Type": "application/json"}

    headers = {"Content-Type": "application/json"}
    api_key = values.get("api_key")
    api_secret = get_decrypted_password("AI MCP Server", server_name, "api_secret", raise_exception=False)
    if api_key and api_secret:
        headers["Authorization"] = f"token {api_key}:{api_secret}"
    else:
        bearer = get_decrypted_password("AI MCP Server", server_name, "bearer_token", raise_exception=False)
        if bearer:
            headers["Authorization"] = bearer if bearer.startswith(("Bearer", "token")) else f"Bearer {bearer}"
    return (values.get("server_url") or "").strip(), headers


def _remote_frappe_list(
    server_name: str,
    doctype: str,
    *,
    filters: list,
    fields: list[str],
    limit: int = 5,
) -> dict:
    try:
        server_url, headers = _mcp_server_connection(server_name)
        if not server_url:
            return {"ok": False, "error": f"MCP server {server_name} has no URL"}
        response = requests.get(
            urljoin(server_url.rstrip("/") + "/", f"api/resource/{doctype}"),
            headers=headers,
            params={
                "filters": json.dumps(filters),
                "fields": json.dumps(fields),
                "limit_page_length": limit,
            },
            timeout=20,
        )
        if not response.ok:
            return {"ok": False, "error": f"{doctype} lookup HTTP {response.status_code}: {response.text[:500]}"}
        return {"ok": True, "data": response.json().get("data") or []}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _remote_frappe_create(server_name: str, doctype: str, payload: dict) -> dict:
    try:
        server_url, headers = _mcp_server_connection(server_name)
        if not server_url:
            return {"ok": False, "error": f"MCP server {server_name} has no URL"}
        response = requests.post(
            urljoin(server_url.rstrip("/") + "/", f"api/resource/{doctype}"),
            headers=headers,
            json=payload,
            timeout=30,
        )
        if not response.ok:
            return {"ok": False, "error": f"{doctype} create HTTP {response.status_code}: {response.text[:800]}"}
        return {"ok": True, "data": response.json().get("data") or {}}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _remote_frappe_method(server_name: str, method_path: str, payload: dict) -> dict:
    server_url, headers = _mcp_server_connection(server_name)
    if not server_url:
        frappe.throw(f"MCP server {server_name} has no URL")
    response = requests.post(
        urljoin(server_url.rstrip("/") + "/", f"api/method/{method_path}"),
        headers=headers,
        json=payload,
        timeout=30,
    )
    result = {
        "status_code": response.status_code,
        "ok": response.ok,
        "body": response.json() if _looks_like_json(response) else response.text[:2000],
    }
    if not response.ok:
        frappe.throw(f"Remote WhatsApp send failed HTTP {response.status_code}: {response.text[:500]}")
    body = result["body"]
    if isinstance(body, dict) and body.get("exc"):
        frappe.throw(f"Remote WhatsApp send failed: {body.get('exception') or body.get('exc')}")
    return result


def _looks_like_json(response) -> bool:
    content_type = response.headers.get("Content-Type", "")
    return "json" in content_type.lower()


def _find_template_map(*, did_number: str, profile_key: str, disease: str, intent: str):
    filters = {"enabled": 1}
    rows = frappe.get_all(
        "AI WhatsApp Template Map",
        filters=filters,
        fields=["name", "priority"],
        order_by="priority desc, modified desc",
        limit=200,
    )
    disease_l = (disease or "").lower()
    intent_l = (intent or "").lower()
    for row in rows:
        doc = frappe.get_doc("AI WhatsApp Template Map", row.name)
        if profile_key and doc.profile_key and doc.profile_key != profile_key:
            continue
        if did_number and doc.did_number and _clean_phone(doc.did_number) != did_number:
            continue
        if disease_l and doc.disease_or_topic and doc.disease_or_topic.lower() not in disease_l and disease_l not in doc.disease_or_topic.lower():
            continue
        if intent_l and doc.intent and doc.intent.lower() not in intent_l and intent_l not in doc.intent.lower():
            continue
        return doc
    return None


def _render_json_values(raw: str | None, *, message: str, arguments: dict, context: dict) -> Any:
    if not raw:
        value: Any = {}
    else:
        try:
            value = json.loads(raw)
        except Exception:
            frappe.throw("WhatsApp Template Values must be valid JSON")
        if not isinstance(value, (dict, list)):
            frappe.throw("WhatsApp Template Values must be a JSON object or array")
    values = {"message": message, **{k: v for k, v in context.items() if isinstance(v, (str, int, float))}}
    values.update({k: v for k, v in arguments.items() if isinstance(v, (str, int, float))})

    def render(item):
        if isinstance(item, str):
            rendered = item
            for key, val in values.items():
                rendered = rendered.replace("{" + key + "}", str(val))
            return rendered
        if isinstance(item, list):
            return [render(x) for x in item]
        if isinstance(item, dict):
            return {k: render(v) for k, v in item.items()}
        return item

    return render(value)


def _call_send_method(method_path: str, payload: dict) -> dict:
    if not method_path:
        frappe.throw("WhatsApp template map has no send method configured.")
    fn = frappe.get_attr(method_path)
    result = fn(**payload)
    if isinstance(result, dict):
        return result
    return {"result": result}


def _clean_phone(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone_variants(phone: str) -> list[str]:
    digits = _clean_phone(phone)
    if not digits:
        return []
    values = []
    for value in (phone, digits, f"+{digits}"):
        if value and value not in values:
            values.append(value)
    if digits.startswith("91") and len(digits) == 12:
        ten_digit = digits[-10:]
        for value in (ten_digit, f"91{ten_digit}", f"+91{ten_digit}"):
            if value not in values:
                values.append(value)
    elif len(digits) == 10:
        for value in (digits, f"91{digits}", f"+91{digits}"):
            if value not in values:
                values.append(value)
    return values
