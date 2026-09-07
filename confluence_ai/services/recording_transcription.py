from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import frappe
import requests

from confluence_ai.services.utils import as_json, create_error, get_queue_name, record_provider_event
from confluence_ai.services.vobiz import _transcript_from_payload, _vobiz_account_id, _vobiz_media_auth_candidates


DEFAULT_LOOKBACK_MINUTES = 360
DEFAULT_LIMIT = 50
DEFAULT_WAIT_MINUTES = 2
DEFAULT_MAX_AUDIO_MB = 25


@dataclass(frozen=True)
class RecordingTranscriptionConfig:
    enabled: bool
    provider: str
    model: str
    api_key: str
    base_url: str
    path: str
    timeout: int
    lookback_minutes: int
    limit: int
    wait_minutes: int
    max_audio_mb: int


class RecordingTranscriptionSkipped(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def process_missing_recording_transcripts(minutes: int | None = None, limit: int | None = None) -> dict:
    """Recover recent call transcripts when Vobiz transcript callbacks are missing."""
    if not _call_log_has_transcript_fields():
        return {"status": "skipped", "reason": "ai_call_log_transcript_fields_not_migrated"}

    config = get_recording_transcription_config()
    if not config.enabled:
        return {"status": "skipped", "reason": "recording_transcription_fallback_disabled"}
    lookback = int(minutes or config.lookback_minutes or DEFAULT_LOOKBACK_MINUTES)
    row_limit = int(limit or config.limit or DEFAULT_LIMIT)
    lookback = max(lookback, 5)
    row_limit = max(1, min(row_limit, 200))
    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-lookback)
    wait_cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-max(config.wait_minutes, 0))

    rows = frappe.db.sql(
        """
        select name
        from `tabAI Call Log`
        where modified >= %(cutoff)s
          and modified <= %(wait_cutoff)s
          and coalesce(transcript, '') = ''
          and coalesce(transcript_summary, '') = ''
          and coalesce(nullif(recording_url, ''), nullif(external_recording_url, ''), '') != ''
          and coalesce(status, '') in ('Completed', 'Unknown', 'In Progress')
        order by
          case when coalesce(task, '') != '' then 0 else 1 end,
          modified asc
        limit %(limit)s
        """,
        {"cutoff": cutoff, "wait_cutoff": wait_cutoff, "limit": row_limit},
        as_dict=True,
    )

    processed: list[dict] = []
    for row in rows:
        processed.append(process_call_log_recording_transcript(row.name, config=config))

    return {
        "status": "success",
        "processed_count": len(processed),
        "processed": processed,
    }


def process_call_log_recording_transcript(
    call_log: str,
    *,
    force: bool = False,
    config: RecordingTranscriptionConfig | None = None,
) -> dict:
    if not call_log or not frappe.db.exists("AI Call Log", call_log):
        return {"status": "skipped", "reason": "missing_call_log"}
    if not _call_log_has_transcript_fields():
        return {"status": "skipped", "reason": "ai_call_log_transcript_fields_not_migrated"}

    doc = frappe.get_doc("AI Call Log", call_log)
    if not force and (doc.get("transcript") or doc.get("transcript_summary")):
        return {"status": "skipped", "reason": "transcript_already_present", "call_log": doc.name}

    recording_url = doc.get("recording_url") or doc.get("external_recording_url")
    if not recording_url:
        return {"status": "skipped", "reason": "recording_missing", "call_log": doc.name}

    config = config or get_recording_transcription_config()
    if not config.enabled:
        return {"status": "skipped", "reason": "recording_transcription_fallback_disabled", "call_log": doc.name}
    try:
        vobiz_result = fetch_vobiz_transcript_for_call_log(doc)
        if vobiz_result.get("status") == "success":
            transcript = str(vobiz_result.get("transcript") or "").strip()
            summary = str(vobiz_result.get("summary") or transcript[:1000]).strip()
            payload = _fallback_transcript_payload(doc, transcript, summary)
            payload["source"] = "vobiz_transcript_pull"
            payload["vobiz_transcription_id"] = vobiz_result.get("transcription_id")
            _save_transcript(doc, transcript, summary, payload)
            record_provider_event(
                provider="Vobiz",
                operation="vobiz_transcript_pull",
                status="Succeeded",
                company=doc.get("company"),
                agent=doc.get("agent"),
                task=doc.get("task"),
                request={"call_log": doc.name, "call_ids": vobiz_result.get("searched_call_ids")},
                response={"transcript_chars": len(transcript), "summary": summary},
            )
            callback_result = emit_synthetic_transcript_callback(doc.name, transcript, summary)
            return {
                "status": "success",
                "source": "vobiz_transcript_pull",
                "call_log": doc.name,
                "transcript_chars": len(transcript),
                "callback": callback_result,
            }

        if not _ai_recording_transcription_enabled():
            return {
                "status": "skipped",
                "reason": vobiz_result.get("reason") or "vobiz_transcript_not_ready",
                "source": "vobiz_transcript_pull",
                "call_log": doc.name,
            }

        if not config.api_key:
            return {"status": "skipped", "reason": "recording_transcription_api_key_missing", "call_log": doc.name}

        audio_bytes, mime_type = fetch_call_recording_audio(doc, max_audio_mb=config.max_audio_mb)
        transcript = str(transcribe_recording_audio(audio_bytes, mime_type=mime_type, config=config) or "").strip()
        if not transcript:
            return {"status": "skipped", "reason": "empty_transcript", "call_log": doc.name}

        summary = transcript[:1000]
        payload = {
            "event": "transcription.completed",
            "source": "ai_recording_transcription_fallback",
            "provider": config.provider,
            "model": config.model,
            "call_log": doc.name,
            "task": doc.get("task"),
            "company": doc.get("company"),
            "CallUUID": doc.get("call_uuid"),
            "SIPCallID": doc.get("sip_call_id"),
            "transcript_chars": len(transcript),
            "summary": summary,
        }

        _save_transcript(doc, transcript, summary, payload)
        record_provider_event(
            provider=config.provider,
            operation="recording_transcription_fallback",
            status="Succeeded",
            company=doc.get("company"),
            agent=doc.get("agent"),
            task=doc.get("task"),
            request={"call_log": doc.name, "model": config.model, "recording_url_present": True},
            response={"transcript_chars": len(transcript), "summary": summary},
        )
        callback_result = emit_synthetic_transcript_callback(doc.name, transcript, summary)
        return {
            "status": "success",
            "call_log": doc.name,
            "transcript_chars": len(transcript),
            "callback": callback_result,
        }
    except RecordingTranscriptionSkipped as exc:
        _save_transcription_skip_state(
            doc.name,
            {
                "reason": exc.reason,
                "message": exc.message,
                "recording_url_present": bool(recording_url),
            },
        )
        record_provider_event(
            provider=config.provider,
            operation="recording_transcription_fallback",
            status="Skipped",
            company=doc.get("company"),
            agent=doc.get("agent"),
            task=doc.get("task"),
            request={"call_log": doc.name, "model": config.model, "recording_url_present": True},
            response={"reason": exc.reason, "message": exc.message},
        )
        return {
            "status": "skipped",
            "call_log": doc.name,
            "reason": exc.reason,
            "message": exc.message,
        }
    except Exception as exc:
        create_error(
            "Recording Transcription Fallback",
            str(exc),
            source="recording_transcription",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name, "recording_url_present": bool(recording_url)},
            exc=exc,
        )
        record_provider_event(
            provider=config.provider,
            operation="recording_transcription_fallback",
            status="Failed",
            company=doc.get("company"),
            agent=doc.get("agent"),
            task=doc.get("task"),
            request={"call_log": doc.name, "model": config.model, "recording_url_present": True},
            response={"error": str(exc)[:500]},
            error=str(exc)[:500],
        )
        return {"status": "failed", "call_log": doc.name, "error": str(exc)}


def enqueue_call_log_recording_transcript(call_log: str | None) -> dict:
    if not call_log:
        return {"status": "skipped", "reason": "missing_call_log"}
    frappe.enqueue(
        "confluence_ai.services.recording_transcription.process_call_log_recording_transcript",
        queue=get_queue_name("llm_queue", "agent_llm"),
        call_log=call_log,
    )
    return {"status": "queued", "call_log": call_log}


def emit_synthetic_transcript_callback(call_log: str, transcript: str, summary: str | None = None) -> dict:
    doc = frappe.get_doc("AI Call Log", call_log)
    summary = summary or transcript[:1000]
    try:
        _sync_transcript_to_related_docs(doc, transcript, summary)
        workflow_result = _notify_task_workflow_transcript(doc, transcript, summary)
        _enqueue_disposition_after_transcript(doc.name)
        return {
            "status": "success",
            "source": "recording_transcription_fallback",
            "call_log": doc.name,
            "task": doc.get("task"),
            "attempt": doc.get("attempt"),
            "workflow": workflow_result,
            "ai_disposition": "queued",
        }
    except Exception as exc:
        create_error(
            "Recording Transcript Sync",
            str(exc),
            source="recording_transcription",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name},
            exc=exc,
        )
        _enqueue_disposition_after_transcript(doc.name)
        return {"status": "partial", "call_log": doc.name, "error": str(exc)}


def _sync_transcript_to_related_docs(doc, transcript: str, summary: str) -> None:
    payload = {
        "event": "transcription.completed",
        "source": "recording_transcription_fallback",
        "call_log": doc.name,
        "task": doc.get("task"),
        "attempt": doc.get("attempt"),
        "company": doc.get("company"),
        "transcript_chars": len(transcript),
        "summary": summary,
    }
    _set_transcript_fields("AI Task", doc.get("task"), transcript, payload)
    _set_transcript_fields("AI Task Attempt", doc.get("attempt"), transcript, payload)
    frappe.db.commit()


def _notify_task_workflow_transcript(doc, transcript: str, summary: str) -> dict:
    task_name = doc.get("task")
    if not task_name or not frappe.db.exists("AI Task", task_name):
        return {"status": "skipped", "reason": "missing_task"}

    task = frappe.get_doc("AI Task", task_name)
    payload = _fallback_transcript_payload(doc, transcript, summary)
    try:
        from confluence_ai.services import vobiz

        return {
            "order_confirmation": vobiz._handle_order_confirmation_callback(task, payload, "transcription.completed"),
            "repeat_followup": vobiz._handle_repeat_followup_callback(task, payload, "transcription.completed"),
            "fresh_followup": vobiz._handle_fresh_followup_callback(task, payload, "transcription.completed"),
        }
    except Exception as exc:
        create_error(
            "Recording Transcript Workflow Notify",
            str(exc),
            source="recording_transcription",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name},
            exc=exc,
        )
        return {"status": "failed", "error": str(exc)}


def _fallback_transcript_payload(doc, transcript: str, summary: str) -> dict:
    return {
        "event": "transcription.completed",
        "Event": "transcription.completed",
        "source": "recording_transcription_fallback",
        "company": doc.get("company"),
        "task": doc.get("task"),
        "task_name": doc.get("task"),
        "CallUUID": doc.get("call_uuid"),
        "call_uuid": doc.get("call_uuid"),
        "SIPCallID": doc.get("sip_call_id"),
        "sip_call_id": doc.get("sip_call_id"),
        "TrunkID": doc.get("trunk_id"),
        "trunk_id": doc.get("trunk_id"),
        "Direction": doc.get("direction"),
        "direction": doc.get("direction"),
        "CallStatus": doc.get("status") or "completed",
        "status": doc.get("status") or "completed",
        "from_number": doc.get("from_number"),
        "to_number": doc.get("to_number"),
        "From": doc.get("from_number"),
        "To": doc.get("to_number"),
        "customer_phone": doc.get("customer_phone"),
        "phone": doc.get("customer_phone"),
        "recording_url": doc.get("recording_url") or doc.get("external_recording_url"),
        "url": doc.get("recording_url") or doc.get("external_recording_url"),
        "transcript": transcript,
        "transcription_text": transcript,
        "summary": summary,
        "transcription_summary": summary,
    }


def _set_transcript_fields(doctype: str, name: str | None, transcript: str, payload: dict) -> None:
    if not name or not frappe.db.exists(doctype, name):
        return
    try:
        meta = frappe.get_meta(doctype)
    except Exception:
        return

    values = {}
    if meta.has_field("transcript"):
        values["transcript"] = transcript
    if meta.has_field("vobiz_transcript_payload"):
        values["vobiz_transcript_payload"] = as_json(payload)

    if values:
        frappe.db.set_value(doctype, name, values, update_modified=True)


def _enqueue_disposition_after_transcript(call_log: str) -> None:
    try:
        from confluence_ai.services.call_disposition import enqueue_call_disposition

        enqueue_call_disposition(call_log)
    except Exception:
        pass


def fetch_vobiz_transcript_for_call_log(doc) -> dict:
    """Pull a missed Vobiz transcript callback without re-transcribing audio."""
    recording_url = doc.get("recording_url") or doc.get("external_recording_url")
    payload = {
        "recording_url": recording_url,
        "url": recording_url,
        "AccountId": _vobiz_account_id({}, recording_url),
        "TrunkID": doc.get("trunk_id"),
        "trunk_id": doc.get("trunk_id"),
    }
    task = frappe.get_doc("AI Task", doc.get("task")) if doc.get("task") and frappe.db.exists("AI Task", doc.get("task")) else None
    account_id = _vobiz_account_id(payload, recording_url)
    call_ids = _vobiz_transcript_call_ids(doc)
    if not call_ids:
        return {"status": "skipped", "reason": "missing_vobiz_call_id"}

    auth_candidates = _vobiz_media_auth_candidates(payload, task=task, account_id=account_id)
    if not auth_candidates:
        return {"status": "skipped", "reason": "missing_vobiz_auth", "searched_call_ids": call_ids}

    last_error = ""
    for headers in auth_candidates:
        auth_id = headers.get("X-Auth-ID") or account_id
        if not auth_id:
            continue
        url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Transcriptions/"
        for call_id in call_ids:
            try:
                response = requests.get(
                    url,
                    headers={**headers, "Accept": "application/json"},
                    params={"call_uuid": call_id, "limit": 5},
                    timeout=30,
                )
            except Exception as exc:
                last_error = str(exc)
                continue
            if response.status_code == 404:
                continue
            if not response.ok:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                continue

            row = _select_vobiz_transcription(_vobiz_transcription_rows(response), call_id)
            if not row:
                continue
            transcript = str(_transcript_from_payload(row) or "").strip()
            if not transcript:
                continue
            summary = str(row.get("summary") or transcript[:1000]).strip()
            return {
                "status": "success",
                "source": "vobiz_transcript_pull",
                "transcript": transcript,
                "summary": summary,
                "transcription_id": row.get("transcription_id") or row.get("id"),
                "matched_call_id": call_id,
                "searched_call_ids": call_ids,
            }

    return {
        "status": "skipped",
        "reason": "vobiz_transcript_not_ready",
        "searched_call_ids": call_ids,
        "last_error": last_error,
    }


def _vobiz_transcription_rows(response) -> list[dict]:
    data = response.json()
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    rows = data.get("objects") or data.get("data") or data.get("results") or []
    if isinstance(rows, dict):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)]


def _select_vobiz_transcription(rows: list[dict], call_id: str) -> dict | None:
    call_id = str(call_id or "").strip()
    if not rows:
        return None
    for row in rows:
        if str(row.get("call_uuid") or "").strip() == call_id and _transcript_from_payload(row):
            return row
    for row in rows:
        if str(row.get("transcription_id") or row.get("id") or "").strip() == call_id and _transcript_from_payload(row):
            return row
    for row in rows:
        if _transcript_from_payload(row):
            return row
    return None


def _vobiz_transcript_call_ids(doc) -> list[str]:
    candidates = [
        doc.get("call_uuid"),
        doc.get("sip_call_id"),
    ]
    recording_id = _recording_id_from_url(doc.get("recording_url") or doc.get("external_recording_url"))
    if recording_id:
        candidates.append(recording_id)

    unique = []
    seen = set()
    for value in candidates:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _recording_id_from_url(recording_url: str | None) -> str:
    if not recording_url:
        return ""
    tail = str(recording_url).split("?")[0].rstrip("/").split("/")[-1]
    if tail.lower().endswith(".wav"):
        tail = tail[:-4]
    return tail.strip()


def _ai_recording_transcription_enabled() -> bool:
    truthy = {1, "1", True, "true", "True", "yes", "Yes", "on", "On"}
    for fieldname in ("enable_ai_recording_transcription_fallback", "allow_ai_recording_transcription_fallback"):
        try:
            settings = frappe.get_single("Confluence AI Settings")
            if settings.meta.has_field(fieldname):
                return settings.get(fieldname) in truthy
        except Exception:
            pass
    try:
        return frappe.conf.get("enable_ai_recording_transcription_fallback") in truthy
    except Exception:
        return False


def fetch_call_recording_audio(doc, *, max_audio_mb: int = DEFAULT_MAX_AUDIO_MB) -> tuple[bytes, str]:
    recording_url = doc.get("external_recording_url") or doc.get("recording_url")
    if not recording_url:
        frappe.throw("No recording URL found for transcription.")

    if str(recording_url).startswith(("/private/files/", "/files/")):
        return _read_site_file(recording_url, max_audio_mb=max_audio_mb), _guess_mime_type(recording_url)

    payload = {
        "recording_url": recording_url,
        "url": recording_url,
        "AccountId": _vobiz_account_id({}, recording_url),
        "TrunkID": doc.get("trunk_id"),
        "trunk_id": doc.get("trunk_id"),
    }
    task = frappe.get_doc("AI Task", doc.get("task")) if doc.get("task") and frappe.db.exists("AI Task", doc.get("task")) else None
    auth_candidates = _vobiz_media_auth_candidates(payload, task=task, account_id=payload.get("AccountId"))
    last_response = None
    for headers in auth_candidates or [{}]:
        response = requests.get(recording_url, headers=headers, timeout=60)
        if response.ok:
            return _checked_audio_bytes(response.content, max_audio_mb), response.headers.get("Content-Type") or _guess_mime_type(recording_url)
        last_response = response

    status = last_response.status_code if last_response is not None else "unknown"
    detail = last_response.text[:300] if last_response is not None else "No response"
    frappe.throw(f"Recording download failed for transcription with HTTP {status}: {detail}")


def transcribe_recording_audio(
    audio_bytes: bytes,
    *,
    mime_type: str,
    config: RecordingTranscriptionConfig,
) -> str:
    if config.provider in {"OpenAI", "OpenAI Compatible"}:
        return _transcribe_openai_compatible(audio_bytes, mime_type=mime_type, config=config)
    if config.provider == "Gemini":
        return _transcribe_gemini(audio_bytes, mime_type=mime_type, config=config)
    frappe.throw(f"Unsupported recording transcription provider: {config.provider}")


def get_recording_transcription_config() -> RecordingTranscriptionConfig:
    settings = frappe.get_single("Confluence AI Settings")
    provider = _settings_value(settings, "recording_transcription_provider", "ai_disposition_provider", "whatsapp_summary_provider") or "Gemini"
    provider = str(provider).strip()
    model = _settings_value(settings, "recording_transcription_model") or ""
    base_url = _settings_value(settings, "recording_transcription_base_url") or ""
    path = _settings_value(settings, "recording_transcription_path") or ""
    timeout = int(_settings_value(settings, "recording_transcription_timeout_seconds") or 60)
    lookback_minutes = int(_settings_value(settings, "recording_transcription_lookback_minutes") or DEFAULT_LOOKBACK_MINUTES)
    limit = int(_settings_value(settings, "recording_transcription_limit") or DEFAULT_LIMIT)
    wait_minutes = int(_settings_value(settings, "recording_transcription_wait_minutes") or DEFAULT_WAIT_MINUTES)
    max_audio_mb = int(_settings_value(settings, "recording_transcription_max_audio_mb") or DEFAULT_MAX_AUDIO_MB)

    if provider == "OpenAI":
        model = model or "whisper-1"
        base_url = base_url or _settings_value(settings, "ai_disposition_base_url", "whatsapp_summary_base_url") or "https://api.openai.com/v1"
        path = path or "/audio/transcriptions"
        api_key = (
            _settings_password(settings, "recording_transcription_api_key")
            or _settings_password(settings, "ai_disposition_api_key")
            or _settings_password(settings, "whatsapp_summary_api_key")
            or frappe.conf.get("openai_api_key")
            or ""
        )
    elif provider == "OpenAI Compatible":
        model = model or "whisper-1"
        base_url = base_url or _settings_value(settings, "ai_disposition_base_url", "whatsapp_summary_base_url") or "https://api.openai.com/v1"
        path = path or "/audio/transcriptions"
        api_key = (
            _settings_password(settings, "recording_transcription_api_key")
            or _settings_password(settings, "ai_disposition_api_key")
            or _settings_password(settings, "whatsapp_summary_api_key")
            or frappe.conf.get("openai_api_key")
            or ""
        )
    elif provider == "Gemini":
        model = model or "gemini-2.5-flash"
        base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"
        api_key = (
            _settings_password(settings, "recording_transcription_api_key")
            or _settings_password(settings, "ai_disposition_api_key")
            or _settings_password(settings, "whatsapp_summary_api_key")
            or frappe.conf.get("gemini_api_key")
            or frappe.conf.get("google_api_key")
            or ""
        )
    else:
        api_key = ""

    enabled = _settings_value(settings, "enable_recording_transcription_fallback")
    if enabled in (None, ""):
        enabled = 1

    return RecordingTranscriptionConfig(
        enabled=enabled in (1, "1", True, "true", "True", "yes", "Yes"),
        provider=provider,
        model=str(model).strip(),
        api_key=str(api_key).strip(),
        base_url=str(base_url).strip().rstrip("/"),
        path=str(path).strip(),
        timeout=max(timeout, 10),
        lookback_minutes=max(lookback_minutes, 5),
        limit=max(1, min(limit, 200)),
        wait_minutes=max(wait_minutes, 0),
        max_audio_mb=max(1, min(max_audio_mb, 100)),
    )


def _transcribe_openai_compatible(audio_bytes: bytes, *, mime_type: str, config: RecordingTranscriptionConfig) -> str:
    url = f"{config.base_url}/{config.path.lstrip('/')}"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        data={
            "model": config.model,
            "response_format": "text",
            "temperature": "0",
            "prompt": "Transcribe this Indian phone call accurately. Preserve Hindi/Hinglish wording.",
        },
        files={"file": (_audio_filename(mime_type), audio_bytes, mime_type or "audio/wav")},
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"Recording transcription failed with HTTP {response.status_code}: {response.text[:500]}")
    return response.text.strip()


def _transcribe_gemini(audio_bytes: bytes, *, mime_type: str, config: RecordingTranscriptionConfig) -> str:
    model_path = config.model if config.model.startswith("models/") else f"models/{config.model}"
    url = f"{config.base_url}/{model_path}:generateContent?{urlencode({'key': config.api_key})}"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 8192,
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Transcribe this phone call accurately. Preserve Hindi/Hinglish wording. "
                                "If you can identify speaker turns, use [AGENT]: and [CUSTOMER]: labels. "
                                "Do not summarize, do not translate to English, and do not add facts."
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": mime_type or "audio/wav",
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
        },
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"Recording transcription failed with HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected Gemini transcription response: {data}") from exc


def _save_transcript(doc, transcript: str, summary: str, payload: dict) -> None:
    frappe.db.set_value(
        "AI Call Log",
        doc.name,
        {
            "transcript": transcript,
            "transcript_summary": summary,
            "transcript_payload_json": as_json(payload),
        },
        update_modified=True,
    )
    frappe.db.commit()


def _save_transcription_skip_state(call_log: str, response: dict) -> None:
    values = {"erp_status_update_response": as_json(response)}
    try:
        if frappe.get_meta("AI Call Log").has_field("erp_status_update_status"):
            values["erp_status_update_status"] = "Skipped"
    except Exception:
        pass
    frappe.db.set_value("AI Call Log", call_log, values, update_modified=True)
    frappe.db.commit()


def _read_site_file(file_url: str, *, max_audio_mb: int) -> bytes:
    parts = [part for part in str(file_url).lstrip("/").split("/") if part]
    content = frappe.get_site_path(*parts)
    with open(content, "rb") as file:
        return _checked_audio_bytes(file.read(), max_audio_mb)


def _checked_audio_bytes(content: bytes, max_audio_mb: int) -> bytes:
    max_bytes = int(max_audio_mb) * 1024 * 1024
    if len(content or b"") <= 0:
        raise RecordingTranscriptionSkipped("recording_audio_empty", "Downloaded recording is empty.")
    if _looks_like_empty_wav(content):
        raise RecordingTranscriptionSkipped(
            "recording_audio_empty",
            "Recording file exists, but it contains no audio samples.",
        )
    if len(content) > max_bytes:
        raise RecordingTranscriptionSkipped(
            "recording_audio_too_large",
            f"Recording is too large for transcription fallback ({len(content)} bytes > {max_bytes} bytes).",
        )
    if len(content) < 512:
        raise RecordingTranscriptionSkipped(
            "recording_audio_too_short",
            "Recording file is too short for transcription fallback.",
        )
    return content


def _looks_like_empty_wav(content: bytes) -> bool:
    if len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        return False
    offset = 12
    while offset + 8 <= len(content):
        chunk_id = content[offset : offset + 4]
        chunk_size = int.from_bytes(content[offset + 4 : offset + 8], "little", signed=False)
        if chunk_id == b"data":
            return chunk_size == 0
        offset += 8 + chunk_size + (chunk_size % 2)
    return False


def _audio_filename(mime_type: str) -> str:
    if "mpeg" in (mime_type or "") or "mp3" in (mime_type or ""):
        return "recording.mp3"
    if "ogg" in (mime_type or ""):
        return "recording.ogg"
    if "webm" in (mime_type or ""):
        return "recording.webm"
    return "recording.wav"


def _guess_mime_type(url: str) -> str:
    guessed, _ = mimetypes.guess_type(str(url))
    return guessed or "audio/wav"


def _settings_value(settings, *fieldnames: str):
    for fieldname in fieldnames:
        try:
            if settings.meta.has_field(fieldname):
                value = settings.get(fieldname)
                if value not in (None, ""):
                    return value
        except Exception:
            continue
    return None


def _settings_password(settings, fieldname: str) -> str:
    try:
        if not settings.meta.has_field(fieldname):
            return ""
        return str(settings.get_password(fieldname, raise_exception=False) or "").strip()
    except TypeError:
        try:
            return str(settings.get_password(fieldname) or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""


def _call_log_has_transcript_fields() -> bool:
    try:
        meta = frappe.get_meta("AI Call Log")
        return meta.has_field("transcript") and meta.has_field("transcript_payload_json")
    except Exception:
        return False
