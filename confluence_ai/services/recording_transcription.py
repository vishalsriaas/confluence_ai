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
from confluence_ai.services.vobiz import _vobiz_account_id, _vobiz_media_auth_candidates


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


def process_missing_recording_transcripts(minutes: int | None = None, limit: int | None = None) -> dict:
    """Transcribe recent recorded calls when Vobiz transcript callbacks are missing."""
    if not _call_log_has_transcript_fields():
        return {"status": "skipped", "reason": "ai_call_log_transcript_fields_not_migrated"}

    config = get_recording_transcription_config()
    if not config.enabled:
        return {"status": "skipped", "reason": "recording_transcription_fallback_disabled"}
    if not config.api_key:
        return {"status": "skipped", "reason": "recording_transcription_api_key_missing"}

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
        order by modified asc
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
    if not config.api_key:
        return {"status": "skipped", "reason": "recording_transcription_api_key_missing", "call_log": doc.name}

    try:
        audio_bytes, mime_type = fetch_call_recording_audio(doc, max_audio_mb=config.max_audio_mb)
        transcript = str(transcribe_recording_audio(audio_bytes, mime_type=mime_type, config=config) or "").strip()
        if not transcript:
            return {"status": "skipped", "reason": "empty_transcript", "call_log": doc.name}

        summary = transcript[:1000]
        payload = {
            "event": "transcription.completed",
            "source": "recording_transcription_fallback",
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
    payload = {
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
        "recording_url": doc.get("recording_url") or doc.get("external_recording_url"),
        "url": doc.get("recording_url") or doc.get("external_recording_url"),
        "transcript": transcript,
        "transcription_text": transcript,
        "summary": summary or transcript[:1000],
        "transcription_summary": summary or transcript[:1000],
    }

    try:
        from confluence_ai.services import vobiz

        return vobiz.handle_callback(payload)
    except Exception as exc:
        create_error(
            "Recording Transcript Callback Replay",
            str(exc),
            source="recording_transcription",
            task=doc.get("task"),
            agent=doc.get("agent"),
            company=doc.get("company"),
            payload={"call_log": doc.name},
            exc=exc,
        )
        try:
            from confluence_ai.services.call_disposition import enqueue_call_disposition

            enqueue_call_disposition(doc.name)
        except Exception:
            pass
        return {"status": "failed", "error": str(exc)}


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
    current = frappe.get_doc("AI Call Log", doc.name)
    current.transcript = transcript
    current.transcript_summary = summary
    current.transcript_payload_json = as_json(payload)
    current.flags.ignore_ai_disposition_auto_sync = True
    current.save(ignore_permissions=True)
    frappe.db.commit()


def _read_site_file(file_url: str, *, max_audio_mb: int) -> bytes:
    parts = [part for part in str(file_url).lstrip("/").split("/") if part]
    content = frappe.get_site_path(*parts)
    with open(content, "rb") as file:
        return _checked_audio_bytes(file.read(), max_audio_mb)


def _checked_audio_bytes(content: bytes, max_audio_mb: int) -> bytes:
    max_bytes = int(max_audio_mb) * 1024 * 1024
    if len(content or b"") <= 0:
        frappe.throw("Downloaded recording is empty.")
    if len(content) > max_bytes:
        frappe.throw(f"Recording is too large for transcription fallback ({len(content)} bytes > {max_bytes} bytes).")
    return content


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
