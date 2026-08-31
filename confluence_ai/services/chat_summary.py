from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import frappe
import requests

from confluence_ai.services.utils import create_error, record_provider_event


@dataclass(frozen=True)
class ChatSummaryConfig:
    enabled: bool
    provider: str
    model: str
    api_key: str
    base_url: str
    path: str
    timeout: int
    max_messages: int
    max_input_chars: int
    max_output_chars: int


def summarize_whatsapp_chat_with_ai(
    record: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    agent: str | None = None,
    company: str | None = None,
) -> str:
    """Create a compact LLM summary of WhatsApp history for call prompts.

    If AI summary is disabled or not configured, return an empty string so the
    caller can fall back to the deterministic compact summary.
    """
    if not isinstance(messages, list) or not messages:
        return ""

    try:
        config = get_chat_summary_config()
    except Exception:
        return ""

    if not config.enabled or not config.api_key:
        return ""

    prepared_messages = _prepare_messages_for_summary(messages, config.max_messages, config.max_input_chars)
    if not prepared_messages:
        return ""

    request_payload = _summary_request_payload(record, prepared_messages, config)
    try:
        if config.provider in {"OpenAI", "OpenAI Compatible"}:
            summary = _summarize_openai_compatible(request_payload, config)
        elif config.provider == "Gemini":
            summary = _summarize_gemini(request_payload, config)
        else:
            return ""
    except Exception as exc:
        create_error(
            "WhatsApp AI Summary Failed",
            str(exc),
            source=config.provider,
            task=task_id,
            agent=agent,
            company=company,
            payload={"record": _safe_record_context(record), "messages": len(prepared_messages)},
            exc=exc,
        )
        record_provider_event(
            provider=config.provider,
            operation="whatsapp_chat_ai_summary",
            status="Failed",
            task=task_id,
            agent=agent,
            company=company,
            request={"record": _safe_record_context(record), "messages": len(prepared_messages), "model": config.model},
            response={"error": str(exc)[:500]},
            error=str(exc)[:500],
        )
        return ""

    summary = _clean_summary(summary)
    if not summary:
        return ""

    summary = summary[: config.max_output_chars]
    record_provider_event(
        provider=config.provider,
        operation="whatsapp_chat_ai_summary",
        status="Succeeded",
        task=task_id,
        agent=agent,
        company=company,
        request={"record": _safe_record_context(record), "messages": len(prepared_messages), "model": config.model},
        response={"chat_summary": summary},
        chat_summary=summary,
    )
    return summary


def get_chat_summary_config() -> ChatSummaryConfig:
    settings = frappe.get_single("Confluence AI Settings")
    provider = (settings.get("whatsapp_summary_provider") or "Gemini").strip()
    model = (settings.get("whatsapp_summary_model") or "").strip()
    base_url = (settings.get("whatsapp_summary_base_url") or "").strip()
    path = (settings.get("whatsapp_summary_path") or "").strip()
    timeout = max(int(settings.get("whatsapp_summary_timeout_seconds") or 20), 5)
    max_messages = max(1, min(int(settings.get("whatsapp_summary_max_messages") or 100), 500))
    max_input_chars = max(1000, min(int(settings.get("whatsapp_summary_max_input_chars") or 12000), 50000))
    max_output_chars = max(300, min(int(settings.get("whatsapp_summary_max_output_chars") or 1500), 3000))

    if provider == "OpenAI":
        model = model or "gpt-4.1-mini"
        base_url = base_url or "https://api.openai.com/v1"
        path = path or "/chat/completions"
        api_key = (
            settings.get_password("whatsapp_summary_api_key", raise_exception=False)
            or frappe.conf.get("openai_api_key")
            or ""
        )
    elif provider == "OpenAI Compatible":
        api_key = settings.get_password("whatsapp_summary_api_key", raise_exception=False) or ""
        path = path or "/chat/completions"
    elif provider == "Gemini":
        model = model or "gemini-2.5-flash"
        base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"
        api_key = (
            settings.get_password("whatsapp_summary_api_key", raise_exception=False)
            or frappe.conf.get("gemini_api_key")
            or frappe.conf.get("google_api_key")
            or ""
        )
    else:
        return ChatSummaryConfig(False, provider, model, "", base_url, path, timeout, max_messages, max_input_chars, max_output_chars)

    enabled = settings.get("enable_whatsapp_ai_summary") in (1, "1", True, "true", "True")
    return ChatSummaryConfig(
        enabled=bool(enabled),
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        path=path,
        timeout=timeout,
        max_messages=max_messages,
        max_input_chars=max_input_chars,
        max_output_chars=max_output_chars,
    )


def _summary_request_payload(
    record: dict[str, Any],
    prepared_messages: list[dict[str, str]],
    config: ChatSummaryConfig,
) -> dict[str, Any]:
    context = _safe_record_context(record)
    instructions = (
        "Create a compact WhatsApp conversation summary for a voice agent. "
        "Use only the provided chat messages and record context. Do not invent. "
        "Summarize the useful customer/business context the next voice agent should know and can safely mention. "
        "Keep Hinglish/Hindi customer wording where useful. Include customer name, age, city/address/phone only if shared, "
        "concern, symptoms, duration, intent, objections, promised follow-up, order/payment/status details, "
        "last useful customer message, and the exact current pending point. "
        "Ignore automation noise such as repeated duplicate bot prompts, wrong confirmation-code loops, generic fallback lines, "
        "and accidental partial confirmations unless they changed the real customer state. "
        "Do not treat short replies like yes/ok as a name when a name was already known. "
        "Return one concise paragraph, no markdown heading."
    )
    content = {
        "record_context": context,
        "messages_chronological": prepared_messages,
        "max_output_chars": config.max_output_chars,
    }
    return {
        "instructions": instructions,
        "content": content,
    }


def _summarize_openai_compatible(payload: dict[str, Any], config: ChatSummaryConfig) -> str:
    url = f"{config.base_url}/{config.path.lstrip('/')}"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.model,
            "temperature": 0.1,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": payload["instructions"]},
                {"role": "user", "content": json.dumps(payload["content"], ensure_ascii=False)},
            ],
        },
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"WhatsApp AI summary failed with HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected OpenAI-compatible summary response: {data}") from exc


def _summarize_gemini(payload: dict[str, Any], config: ChatSummaryConfig) -> str:
    model_path = config.model if config.model.startswith("models/") else f"models/{config.model}"
    url = f"{config.base_url}/{model_path}:generateContent?{urlencode({'key': config.api_key})}"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 500,
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
        frappe.throw(f"WhatsApp AI summary failed with HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected Gemini summary response: {data}") from exc


def _prepare_messages_for_summary(
    messages: list[dict[str, Any]],
    max_messages: int,
    max_input_chars: int,
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    total_chars = 0
    for message in messages[-max_messages:]:
        if not isinstance(message, dict):
            continue
        text = _clean_chat_text(
            message.get("body") or message.get("message") or message.get("content") or message.get("text")
        )
        if not text:
            continue
        row = {
            "time": _clean_chat_text(message.get("creation") or message.get("modified"), max_chars=40),
            "speaker": _speaker_label(message),
            "text": text,
        }
        row_chars = sum(len(value or "") for value in row.values())
        if total_chars + row_chars > max_input_chars:
            break
        total_chars += row_chars
        prepared.append(row)
    return prepared


def _speaker_label(message: dict[str, Any]) -> str:
    direction = str(message.get("direction") or "").lower()
    sender = str(message.get("sender_type") or "").lower()
    if direction == "inbound" or sender == "customer":
        return "Customer"
    if direction == "outbound" or sender in {"ai", "agent", "system", "business"}:
        return "Business"
    return "Message"


def _safe_record_context(record: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "channel_account",
        "contact",
        "status",
        "lead_temperature",
        "lead_score",
        "lead_lan",
        "linked_crm_lead",
        "linked_reference_doctype",
        "linked_reference_name",
        "last_message_time",
        "last_message_preview",
        "ai_summary",
    )
    return {key: record.get(key) for key in allowed_keys if record.get(key) not in (None, "", [], {})}


def _clean_chat_text(value: Any, max_chars: int = 700) -> str:
    if value in (None, "", [], {}):
        return ""
    text = str(value)
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _clean_summary(value: Any) -> str:
    text = _clean_chat_text(value, max_chars=4000)
    text = re.sub(r"^summary\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()
