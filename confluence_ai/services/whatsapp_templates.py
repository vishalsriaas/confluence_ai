from __future__ import annotations

import json
from typing import Any

import frappe

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
        result = _call_send_method(mapping.send_method, payload)
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


def _task_context(task_id: str | None) -> dict:
    if not task_id or not frappe.db.exists("AI Task", task_id):
        return {}
    task = frappe.get_doc("AI Task", task_id)
    return parse_json_object(task.context_json, "AI Task Context") if task.context_json else {}


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
    value = parse_json_object(raw or "[]", "WhatsApp Template Values")
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
