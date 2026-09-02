from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

import frappe
import requests
from frappe.utils import add_to_date, now_datetime

from confluence_ai.services.utils import as_json, create_error, get_queue_name, parse_json_object, record_provider_event


DISPOSITION_OPTIONS = {
    "Duplicate",
    "Existing Patient",
    "Financial Issue",
    "Follow up",
    "Fresh",
    "Not Answered",
    "Order Placed",
    "Other Disease",
    "Not Interested",
}

FINAL_NO_ANSWER_STATUSES = {"No Answer", "Busy", "Rejected", "Cancelled"}
MISSING_TRANSCRIPT_FALLBACK_MINUTES = 10
MISSING_TRANSCRIPT_FALLBACK_LIMIT = 50
FINAL_CALL_STATUSES = {"completed", "failed", "no answer", "busy", "rejected", "cancelled", "timeout"}
FINAL_CALL_EVENT_TYPES = {
    "hangup",
    "call_ended",
    "room_finished",
    "participant_left",
    "completed",
    "failed",
    "room_failed",
    "call_failed",
    "no_answer",
    "busy",
    "cancel",
    "timeout",
}

DEFAULT_DISPOSITION_INSTRUCTIONS = (
    "Read the voice call transcript and choose exactly one CRM Lead status. "
    "Use only the transcript and context. Do not invent. "
    "Allowed statuses: Duplicate, Existing Patient, Financial Issue, Follow up, Fresh, Not Answered, Order Placed, Other Disease, Not Interested. "
    "Return JSON only with keys: ai_disposition, ai_disposition_reason, ai_disposition_confidence, ai_disposition_summary. "
    "If company-specific rules ask for a custom ERP disposition field, also return custom_vobiz_disposition with the exact disposition label. "
    "Use Order Placed only if customer clearly agreed to order/treatment or payment/order was confirmed. "
    "Use Follow up if customer asked to call later, needs family discussion, is busy, or decision is pending. "
    "Use Financial Issue if price/payment is the main blocker. "
    "Use Not Interested only if customer clearly refused. "
    "Use Fresh for normal new enquiry without decision. "
    "Use Existing Patient when the conversation is mainly with/about an old patient or previous order. "
    "Use Other Disease when the concern is outside the configured campaign disease/department. "
    "Confidence must be 0 to 1."
)


@dataclass(frozen=True)
class DispositionConfig:
    enabled: bool
    provider: str
    model: str
    api_key: str
    base_url: str
    path: str
    timeout: int
    update_mcp_tool_name: str


def enqueue_call_disposition(call_log: str | None) -> dict:
    if not call_log:
        return {"status": "skipped", "reason": "missing_call_log"}
    if not _call_log_has_disposition_fields():
        return {"status": "skipped", "reason": "ai_call_log_fields_not_migrated"}

    frappe.enqueue(
        "confluence_ai.services.call_disposition.process_call_log",
        queue=get_queue_name("llm_queue", "agent_llm"),
        call_log=call_log,
    )
    return {"status": "queued", "call_log": call_log}


def process_call_log(call_log: str, force: bool = False) -> dict:
    if not call_log or not frappe.db.exists("AI Call Log", call_log):
        return {"status": "skipped", "reason": "missing_call_log"}
    if not _call_log_has_disposition_fields():
        return {"status": "skipped", "reason": "ai_call_log_fields_not_migrated"}

    doc = frappe.get_doc("AI Call Log", call_log)
    if not force and doc.get("erp_status_update_status") == "Succeeded":
        return {"status": "skipped", "reason": "already_updated", "call_log": call_log}

    try:
        config = get_disposition_config()
        if not config.enabled:
            _save_update_state(doc, "Skipped", {"reason": "ai_disposition_disabled"})
            return {"status": "skipped", "reason": "disabled", "call_log": doc.name}

        transcript = _clean_text(doc.get("transcript") or doc.get("transcript_summary"))
        if not transcript:
            if doc.get("status") in FINAL_NO_ANSWER_STATUSES:
                decision = {
                    "ai_disposition": "Not Answered",
                    "ai_disposition_reason": f"Call ended with status {doc.get('status')}.",
                    "ai_disposition_confidence": 0.95,
                    "ai_disposition_summary": "Customer did not complete a usable conversation.",
                }
            else:
                _save_update_state(doc, "Skipped", {"reason": "waiting_for_transcript"})
                return {"status": "skipped", "reason": "waiting_for_transcript", "call_log": doc.name}
        else:
            decision = classify_transcript(doc, transcript, config)

        _save_disposition(doc, decision)
        update_result = update_crm_lead_status(doc, decision, config)
        _save_update_state(doc, update_result.get("erp_status_update_status") or "Skipped", update_result)
        return {"status": "success", "call_log": doc.name, "decision": decision, "erp_update": update_result}
    except Exception as exc:
        create_error(
            "AI Call Disposition",
            str(exc),
            source="call_disposition",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name},
            exc=exc,
        )
        _save_update_state(doc, "Failed", {"error": str(exc)[:1000]})
        return {"status": "failed", "call_log": doc.name, "error": str(exc)}


def process_stale_missing_transcript_dispositions(
    minutes: int = MISSING_TRANSCRIPT_FALLBACK_MINUTES,
    limit: int = MISSING_TRANSCRIPT_FALLBACK_LIMIT,
) -> dict:
    if not _call_log_has_disposition_fields():
        return {"status": "skipped", "reason": "ai_call_log_fields_not_migrated"}

    minutes = max(int(minutes or MISSING_TRANSCRIPT_FALLBACK_MINUTES), 1)
    limit = max(min(int(limit or MISSING_TRANSCRIPT_FALLBACK_LIMIT), 200), 1)
    cutoff = add_to_date(now_datetime(), minutes=-minutes)
    rows = frappe.get_all(
        "AI Call Log",
        filters={
            "erp_status_update_status": "Skipped",
            "modified": ["<=", cutoff],
        },
        fields=[
            "name",
            "status",
            "event_type",
            "transcript",
            "transcript_summary",
            "ai_disposition",
            "erp_status_update_response",
        ],
        order_by="modified asc",
        limit_page_length=limit,
    )

    processed = []
    skipped = 0
    for row in rows:
        if row.get("transcript") or row.get("transcript_summary") or row.get("ai_disposition"):
            skipped += 1
            continue
        if not _is_waiting_for_transcript_response(row.get("erp_status_update_response")):
            skipped += 1
            continue
        if not _is_final_call_for_missing_transcript(row):
            skipped += 1
            continue
        processed.append(process_missing_transcript_fallback(row.get("name")))

    return {"status": "success", "processed_count": len(processed), "skipped_count": skipped, "processed": processed}


def process_missing_transcript_fallback(call_log: str, force: bool = False) -> dict:
    if not call_log or not frappe.db.exists("AI Call Log", call_log):
        return {"status": "skipped", "reason": "missing_call_log"}
    if not _call_log_has_disposition_fields():
        return {"status": "skipped", "reason": "ai_call_log_fields_not_migrated"}

    doc = frappe.get_doc("AI Call Log", call_log)
    if not force and doc.get("erp_status_update_status") == "Succeeded":
        return {"status": "skipped", "reason": "already_updated", "call_log": doc.name}

    transcript = _clean_text(doc.get("transcript") or doc.get("transcript_summary"))
    if transcript:
        return process_call_log(doc.name, force=force)
    if not force and not _is_waiting_for_transcript_response(doc.get("erp_status_update_response")):
        return {"status": "skipped", "reason": "not_waiting_for_transcript", "call_log": doc.name}
    if not force and not _is_final_call_for_missing_transcript(doc):
        return {"status": "skipped", "reason": "call_not_final", "call_log": doc.name}

    try:
        config = get_disposition_config()
        if not config.enabled:
            _save_update_state(doc, "Skipped", {"reason": "ai_disposition_disabled"})
            return {"status": "skipped", "reason": "disabled", "call_log": doc.name}

        decision = _missing_transcript_fallback_decision(doc)
        _save_disposition(doc, decision)
        update_result = update_crm_lead_status(doc, decision, config)
        _save_update_state(doc, update_result.get("erp_status_update_status") or "Skipped", update_result)
        return {"status": "success", "call_log": doc.name, "decision": decision, "erp_update": update_result}
    except Exception as exc:
        create_error(
            "AI Call Disposition Missing Transcript Fallback",
            str(exc),
            source="call_disposition",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name},
            exc=exc,
        )
        _save_update_state(doc, "Failed", {"error": str(exc)[:1000]})
        return {"status": "failed", "call_log": doc.name, "error": str(exc)}


def classify_transcript(doc, transcript: str, config: DispositionConfig) -> dict:
    if not config.api_key:
        frappe.throw("AI disposition model API key is not configured.")

    payload = _classification_payload(doc, transcript)
    if config.provider in {"OpenAI", "OpenAI Compatible"}:
        raw = _classify_openai_compatible(payload, config)
    elif config.provider == "Gemini":
        raw = _classify_gemini(payload, config)
    else:
        frappe.throw(f"Unsupported AI disposition provider: {config.provider}")

    decision = _extract_json_object(raw)
    disposition = _normalize_disposition(decision.get("ai_disposition") or decision.get("disposition"))
    reason = _clean_text(decision.get("ai_disposition_reason") or decision.get("reason"))[:1000]
    summary = _clean_text(decision.get("ai_disposition_summary") or decision.get("summary"))[:2000]
    confidence = _safe_confidence(decision.get("ai_disposition_confidence") or decision.get("confidence"))

    result = {
        "ai_disposition": disposition,
        "ai_disposition_reason": reason or "Disposition classified from call transcript.",
        "ai_disposition_confidence": confidence,
        "ai_disposition_summary": summary or transcript[:1000],
    }
    custom_vobiz_disposition = _clean_text(decision.get("custom_vobiz_disposition"))
    if custom_vobiz_disposition:
        result["custom_vobiz_disposition"] = custom_vobiz_disposition
    record_provider_event(
        provider=config.provider,
        operation="ai_call_disposition",
        status="Succeeded",
        company=doc.get("company"),
        agent=doc.get("agent"),
        task=doc.get("task"),
        request={"call_log": doc.name, "model": config.model},
        response=result,
    )
    return result


def update_crm_lead_status(doc, decision: dict, config: DispositionConfig) -> dict:
    tool = _resolve_update_tool(doc, config)
    if not tool:
        return {
            "erp_status_update_status": "Skipped",
            "reason": "update_crm_lead_status MCP tool is not configured.",
        }

    context = _task_context(doc.get("task"))
    lead_id = _lead_id_from_context(doc, context)
    phone = _phone_from_context(doc, context)
    if not lead_id:
        lead_id = _find_remote_lead_id_by_phone(tool, phone)

    if _tool_uses_lead_id_filter(tool) and not lead_id:
        return {
            "erp_status_update_status": "Skipped",
            "reason": "No CRM lead id was available for the configured update filter.",
            "phone": phone,
        }

    if not lead_id and not phone:
        return {
            "erp_status_update_status": "Skipped",
            "reason": "No CRM lead id or phone was available for update.",
        }

    phone_values = _phone_argument_values(phone)
    custom_vobiz_disposition = _resolve_custom_vobiz_disposition(tool, decision)
    if _tool_writes_custom_vobiz_disposition(tool) and not custom_vobiz_disposition:
        return {
            "erp_status_update_status": "Skipped",
            "reason": "No valid SR Lead Disposition was available for custom_vobiz_disposition.",
            "phone": phone,
        }
    arguments = {
        "lead_id": lead_id,
        "name": lead_id,
        "crm_lead": lead_id,
        "phone": phone,
        "phone_e164": phone_values.get("phone_e164"),
        "phone_last10": phone_values.get("phone_last10"),
        "phone_91": phone_values.get("phone_91"),
        "mobile_no": phone_values.get("mobile_no") or phone,
        "normalized_phone": phone_values.get("phone_91") or phone,
        "customer_phone": phone,
        "status": decision.get("ai_disposition"),
        "ai_disposition": decision.get("ai_disposition"),
        "custom_vobiz_disposition": custom_vobiz_disposition,
        "custom_vobiz_disposition_label": _clean_text(decision.get("custom_vobiz_disposition")),
        "ai_disposition_reason": decision.get("ai_disposition_reason"),
        "ai_disposition_confidence": decision.get("ai_disposition_confidence"),
        "ai_disposition_summary": decision.get("ai_disposition_summary"),
        "call_log": doc.name,
        "task": doc.get("task"),
    }

    from confluence_ai.api.mcp import execute_mcp_tool

    result = execute_mcp_tool(tool, arguments, doc.get("task"))
    return {
        "erp_status_update_status": "Succeeded",
        "tool": tool.name,
        "tool_name": tool.tool_name,
        "lead_id": lead_id,
        "phone": phone,
        "custom_vobiz_disposition": custom_vobiz_disposition,
        "result": result,
    }


def get_disposition_config() -> DispositionConfig:
    settings = frappe.get_single("Confluence AI Settings")
    provider = _settings_value(settings, "ai_disposition_provider", "whatsapp_summary_provider") or "Gemini"
    provider = str(provider).strip()
    model = _settings_value(settings, "ai_disposition_model", "whatsapp_summary_model") or ""
    base_url = _settings_value(settings, "ai_disposition_base_url", "whatsapp_summary_base_url") or ""
    path = _settings_value(settings, "ai_disposition_path", "whatsapp_summary_path") or ""
    timeout = int(_settings_value(settings, "ai_disposition_timeout_seconds", "whatsapp_summary_timeout_seconds") or 20)
    timeout = max(timeout, 5)
    update_mcp_tool_name = _settings_value(settings, "ai_disposition_update_mcp_tool_name") or ""

    if provider == "OpenAI":
        model = model or "gpt-4.1-mini"
        base_url = base_url or "https://api.openai.com/v1"
        path = path or "/chat/completions"
        api_key = _settings_password(settings, "ai_disposition_api_key") or frappe.conf.get("openai_api_key") or ""
    elif provider == "OpenAI Compatible":
        path = path or "/chat/completions"
        api_key = _settings_password(settings, "ai_disposition_api_key") or _settings_password(settings, "whatsapp_summary_api_key") or ""
    elif provider == "Gemini":
        model = model or "gemini-2.5-flash"
        base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"
        api_key = (
            _settings_password(settings, "ai_disposition_api_key")
            or _settings_password(settings, "whatsapp_summary_api_key")
            or frappe.conf.get("gemini_api_key")
            or frappe.conf.get("google_api_key")
            or ""
        )
    else:
        api_key = ""

    enabled = _settings_value(settings, "enable_ai_disposition")
    if enabled in (None, ""):
        enabled = 1

    return DispositionConfig(
        enabled=enabled in (1, "1", True, "true", "True"),
        provider=provider,
        model=str(model).strip(),
        api_key=str(api_key).strip(),
        base_url=str(base_url).strip().rstrip("/"),
        path=str(path).strip(),
        timeout=timeout,
        update_mcp_tool_name=str(update_mcp_tool_name).strip(),
    )


def _classification_payload(doc, transcript: str) -> dict:
    context = _task_context(doc.get("task"))
    instructions = _classification_instructions(doc)
    return {
        "instructions": instructions,
        "content": {
            "call_log": doc.name,
            "company": doc.get("company"),
            "agent": doc.get("agent"),
            "customer_name": doc.get("customer_name"),
            "customer_phone": doc.get("customer_phone"),
            "task_context": _compact_context(context),
            "transcript": transcript[:12000],
        },
    }


def _classification_instructions(doc) -> str:
    return _build_disposition_instructions(_company_disposition_prompt(doc.get("company")))


def _build_disposition_instructions(company_prompt: str | None = None) -> str:
    prompt = _clean_text(company_prompt)
    if not prompt:
        return DEFAULT_DISPOSITION_INSTRUCTIONS
    return (
        DEFAULT_DISPOSITION_INSTRUCTIONS
        + "\n\nCompany-specific disposition rules:\n"
        + prompt
        + "\n\nWhen company-specific rules conflict with the default examples, follow the company-specific rules."
    )


def _company_disposition_prompt(company: str | None) -> str:
    company = _clean_text(company)
    if not company:
        return ""
    try:
        settings = frappe.get_single("Confluence AI Settings")
        if not settings.meta.has_field("ai_disposition_company_prompts"):
            return ""

        company_key = _company_key(company).lower()
        for row in settings.get("ai_disposition_company_prompts") or []:
            row_company = _clean_text(row.get("company"))
            if not row_company:
                continue
            row_company_key = _company_key(row_company).lower()
            if row_company.lower() == company.lower() or (company_key and row_company_key == company_key):
                return _clean_text(row.get("ai_disposition_prompt"))
    except Exception:
        return ""
    return ""


def _classify_openai_compatible(payload: dict, config: DispositionConfig) -> str:
    url = f"{config.base_url}/{config.path.lstrip('/')}"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
        json={
            "model": config.model,
            "temperature": 0,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": payload["instructions"]},
                {"role": "user", "content": json.dumps(payload["content"], ensure_ascii=False)},
            ],
        },
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"AI disposition failed with HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected AI disposition response: {data}") from exc


def _classify_gemini(payload: dict, config: DispositionConfig) -> str:
    model_path = config.model if config.model.startswith("models/") else f"models/{config.model}"
    url = f"{config.base_url}/{model_path}:generateContent?{urlencode({'key': config.api_key})}"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 500,
                "responseMimeType": "application/json",
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": payload["instructions"]
                            + "\n\n"
                            + json.dumps(payload["content"], ensure_ascii=False)
                        }
                    ],
                }
            ],
        },
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"AI disposition failed with HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected AI disposition response: {data}") from exc


def _resolve_update_tool(doc, config: DispositionConfig):
    candidates = []
    if config.update_mcp_tool_name:
        candidates.append(config.update_mcp_tool_name)

    company_key = _company_key(doc.get("company"))
    if company_key:
        candidates.append(f"{company_key.upper()}_update_crm_lead_status")
        candidates.append(f"{company_key}_update_crm_lead_status")
    candidates.append("update_crm_lead_status")

    seen = set()
    for tool_name in candidates:
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        filters = {"tool_name": tool_name, "enabled": 1}
        if doc.get("company"):
            name = frappe.db.get_value("AI MCP Tool", {**filters, "company": doc.get("company")}, "name")
            if name:
                return frappe.get_doc("AI MCP Tool", name)
        name = frappe.db.get_value("AI MCP Tool", filters, "name")
        if name:
            return frappe.get_doc("AI MCP Tool", name)
    return None


def _find_remote_lead_id_by_phone(tool, phone: str | None) -> str | None:
    if not phone or not tool.get("server"):
        return None

    from confluence_ai.api.mcp import get_server_headers

    try:
        server = frappe.get_doc("AI MCP Server", tool.server)
        headers = get_server_headers(server)
        doctype = tool.client_doctype or "CRM Lead"
        url = urljoin((server.server_url or "").rstrip() + "/", f"api/resource/{doctype}")
        for fieldname in ("mobile_no", "phone", "whatsapp_no", "mobile", "contact_number"):
            for value in _phone_variants(phone):
                response = requests.get(
                    url,
                    headers=headers,
                    params={
                        "filters": json.dumps([[fieldname, "=", value]]),
                        "fields": json.dumps(["name"]),
                        "limit_page_length": 1,
                    },
                    timeout=15,
                )
                if not response.ok:
                    continue
                rows = response.json().get("data") or []
                if rows and rows[0].get("name"):
                    return rows[0].get("name")
    except Exception:
        return None
    return None


def _tool_uses_lead_id_filter(tool) -> bool:
    for mapping in tool.match_filters or []:
        if (mapping.value_source == "From Tool Arguments") and (
            (mapping.source_value or mapping.client_field) in {"lead_id", "name", "crm_lead"}
        ):
            return True
    return False


def _resolve_custom_vobiz_disposition(tool, decision: dict) -> str:
    if not _tool_writes_custom_vobiz_disposition(tool):
        return ""

    requested = _clean_text(decision.get("custom_vobiz_disposition"))
    if requested:
        resolved = _find_remote_disposition_id(tool, requested)
        if resolved:
            return resolved

    fallback = _fallback_vobiz_disposition_label(decision)
    if fallback:
        resolved = _find_remote_disposition_id(tool, fallback)
        if resolved:
            return resolved
    return ""


def _tool_writes_custom_vobiz_disposition(tool) -> bool:
    return any(mapping.client_field == "custom_vobiz_disposition" for mapping in (tool.fields_to_write or []))


def _find_remote_disposition_id(tool, label: str) -> str:
    label = _clean_text(label)
    if not label or not tool.get("server"):
        return ""

    from confluence_ai.api.mcp import get_server_headers

    try:
        server = frappe.get_doc("AI MCP Server", tool.server)
        headers = get_server_headers(server)
        url = urljoin((server.server_url or "").rstrip() + "/", "api/resource/SR Lead Disposition")
        for fieldname in ("name", "sr_disposition_name"):
            response = requests.get(
                url,
                headers=headers,
                params={
                    "filters": json.dumps([[fieldname, "=", label]]),
                    "fields": json.dumps(["name"]),
                    "limit_page_length": 1,
                },
                timeout=15,
            )
            if not response.ok:
                continue
            rows = response.json().get("data") or []
            if rows and rows[0].get("name"):
                return rows[0].get("name")
    except Exception:
        return ""
    return ""


def _fallback_vobiz_disposition_label(decision: dict) -> str:
    disposition = _clean_text(decision.get("ai_disposition")).lower()
    reason = _clean_text(decision.get("ai_disposition_reason")).lower()
    summary = _clean_text(decision.get("ai_disposition_summary")).lower()
    text = f"{reason} {summary}"

    if disposition == "follow up":
        if "family" in text or "ghar" in text:
            return "Family Discussion"
        if "busy" in text:
            return "Busy"
        if "report" in text:
            return "Reports Pending"
        if "address" in text:
            return "Address Pending"
        if "payment" in text:
            return "Payment Pending"
        return "Call Back"
    if disposition == "financial issue":
        if "discount" in text:
            return "Want Discount"
        return "Can't Afford"
    if disposition == "not answered":
        if "switch" in text:
            return "Switch Off"
        if "reachable" in text or "out of service" in text:
            return "Not Reachable"
        if "busy" in text:
            return "Busy"
        return "Call Cut"
    if disposition == "existing patient":
        return "Existing Pt Query"
    if disposition == "not interested":
        return "Not Interested"
    if disposition == "duplicate":
        return "Fake Call"
    if disposition == "other disease":
        return "Non-treatable"
    if disposition == "fresh":
        return "General Queries"
    return ""


def _missing_transcript_fallback_decision(doc) -> dict:
    return {
        "ai_disposition": "Not Answered",
        "ai_disposition_reason": "No transcript was received within 10 minutes after the call ended.",
        "ai_disposition_confidence": 0.95,
        "ai_disposition_summary": "Call ended, but no usable transcript was received for disposition review.",
        "custom_vobiz_disposition": "Not Reachable",
    }


def _is_waiting_for_transcript_response(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("reason") == "waiting_for_transcript"
    try:
        data = json.loads(value or "{}")
    except Exception:
        return False
    return isinstance(data, dict) and data.get("reason") == "waiting_for_transcript"


def _is_final_call_for_missing_transcript(doc_or_row) -> bool:
    status = _clean_text(_get_doc_value(doc_or_row, "status")).lower()
    event_type = _clean_text(_get_doc_value(doc_or_row, "event_type")).lower()
    return status in FINAL_CALL_STATUSES or event_type in FINAL_CALL_EVENT_TYPES


def _get_doc_value(doc_or_row, fieldname: str) -> Any:
    if hasattr(doc_or_row, "get"):
        return doc_or_row.get(fieldname)
    return getattr(doc_or_row, fieldname, None)


def _save_disposition(doc, decision: dict) -> None:
    doc.ai_disposition = decision.get("ai_disposition")
    doc.ai_disposition_reason = decision.get("ai_disposition_reason")
    doc.ai_disposition_confidence = decision.get("ai_disposition_confidence")
    doc.ai_disposition_summary = decision.get("ai_disposition_summary")
    doc.erp_status_update_status = "Pending"
    doc.save(ignore_permissions=True)
    frappe.db.commit()


def _save_update_state(doc, status: str, response: dict) -> None:
    try:
        current = frappe.get_doc("AI Call Log", doc.name)
        current.erp_status_update_status = status
        current.erp_status_update_response = as_json(response)
        current.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:
        create_error(
            "AI Call Disposition Save",
            str(exc),
            source="call_disposition",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name, "status": status, "response": response},
            exc=exc,
        )


def _task_context(task_name: str | None) -> dict:
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return {}
    task = frappe.get_doc("AI Task", task_name)
    context = parse_json_object(task.context_json, "Task Context JSON") if task.context_json else {}
    result = parse_json_object(task.result_json, "Task Result JSON") if task.result_json else {}
    if isinstance(context, dict) and isinstance(result, dict):
        context["_task_result"] = result
    return context if isinstance(context, dict) else {}


def _lead_id_from_context(doc, context: dict) -> str | None:
    candidates = [
        doc.get("external_record_id"),
        context.get("lead_id"),
        context.get("crm_lead"),
        context.get("linked_crm_lead"),
        context.get("source_reference_name") if context.get("source_reference_type") == "CRM Lead" else None,
        context.get("external_record_id") if context.get("external_record_type") == "CRM Lead" else None,
    ]
    task_result = context.get("_task_result") if isinstance(context.get("_task_result"), dict) else {}
    candidates.extend(
        [
            task_result.get("lead_id"),
            task_result.get("crm_lead"),
            task_result.get("linked_crm_lead"),
        ]
    )
    for value in candidates:
        text = _clean_text(value)
        if text:
            return text
    return None


def _phone_from_context(doc, context: dict) -> str | None:
    for value in (
        doc.get("customer_phone"),
        context.get("customer_phone"),
        context.get("phone"),
        context.get("mobile_no"),
        context.get("to"),
    ):
        phone = _normalize_phone(value)
        if phone:
            return phone
    return None


def _compact_context(context: dict) -> dict:
    allowed = (
        "source_reference_type",
        "source_reference_name",
        "external_record_id",
        "external_record_type",
        "customer_name",
        "customer_phone",
        "phone",
        "disease_or_concern",
        "lead_id",
        "crm_lead",
        "linked_crm_lead",
        "whatsapp_conversation_summary",
    )
    return {key: context.get(key) for key in allowed if context.get(key) not in (None, "", [], {})}


def _settings_value(settings, *fieldnames: str) -> Any:
    for fieldname in fieldnames:
        if settings.meta.has_field(fieldname):
            value = settings.get(fieldname)
            if value not in (None, ""):
                return value
    return None


def _settings_password(settings, fieldname: str) -> str:
    if not settings.meta.has_field(fieldname):
        return ""
    return settings.get_password(fieldname, raise_exception=False) or ""


def _call_log_has_disposition_fields() -> bool:
    try:
        meta = frappe.get_meta("AI Call Log")
        return meta.has_field("ai_disposition") and meta.has_field("erp_status_update_status")
    except Exception:
        return False


def _company_key(company: str | None) -> str:
    if not company:
        return ""
    key = frappe.db.get_value("AI Company", company, "company_key") or company
    return re.sub(r"[^A-Za-z0-9]+", "_", str(key)).strip("_")


def _normalize_disposition(value: Any) -> str:
    text = _clean_text(value)
    lowered = text.lower()
    for option in DISPOSITION_OPTIONS:
        if lowered == option.lower():
            return option
    aliases = {
        "order": "Order Placed",
        "converted": "Order Placed",
        "followup": "Follow up",
        "follow-up": "Follow up",
        "callback": "Follow up",
        "financial": "Financial Issue",
        "price issue": "Financial Issue",
        "not answered": "Not Answered",
        "no answer": "Not Answered",
        "existing": "Existing Patient",
        "old patient": "Existing Patient",
        "not interested": "Not Interested",
    }
    return aliases.get(lowered, "Fresh")


def _safe_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(confidence, 1.0))


def _extract_json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    text = _clean_text(value)
    if not text:
        frappe.throw("AI disposition returned an empty response.")
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
    frappe.throw(f"AI disposition returned invalid JSON: {text[:300]}")


def _phone_variants(phone: str) -> list[str]:
    digits = re.sub(r"\D", "", str(phone or ""))
    values = [str(phone).strip()]
    if len(digits) >= 10:
        last10 = digits[-10:]
        values.extend([last10, f"91{last10}", f"+91{last10}", f"0{last10}"])
    return [value for index, value in enumerate(values) if value and value not in values[:index]]


def _phone_argument_values(phone: str | None) -> dict[str, str]:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 10:
        return {"phone_e164": _clean_text(phone), "mobile_no": _clean_text(phone)}
    last10 = digits[-10:]
    phone_91 = f"91{last10}"
    return {
        "phone_e164": f"+{phone_91}",
        "phone_91": phone_91,
        "phone_last10": last10,
        "mobile_no": phone_91,
    }


def _normalize_phone(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        last10 = digits[-10:]
        if digits.startswith("91") and len(digits) == 12:
            return f"+{digits}"
        return f"+91{last10}"
    return text


def _clean_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()
