from __future__ import annotations

import json
import re
import frappe
from datetime import datetime
from typing import Any

import requests

from confluence_ai.services.utils import as_json, now, record_provider_event


VOBIZ_TRANSCRIPT_EVENTS = {"transcript", "call_transcript", "transcript_ready", "transcription.completed"}
VOBIZ_RECORDING_EVENTS = {"recording", "call_recording", "recording_ready", "recording.completed"}
VOBIZ_RECORDING_BACKFILL_DEFAULT_LOOKBACK_MINUTES = 360
VOBIZ_RECORDING_BACKFILL_DEFAULT_LIMIT = 1000


def process_missing_recording_callbacks(minutes: int | None = None, limit: int | None = None) -> dict:
    """Poll Vobiz recordings and backfill call logs missed by webhooks.

    Vobiz recording webhooks are still the primary source. This safety job only
    recovers recent recordings for configured Vobiz channel accounts when the
    webhook was missed or not persisted.
    """
    if not _setting_enabled("enable_vobiz_recording_backfill", default=True):
        return {"status": "skipped", "reason": "vobiz_recording_backfill_disabled"}

    minutes = int(minutes or _setting_int("vobiz_recording_backfill_lookback_minutes", VOBIZ_RECORDING_BACKFILL_DEFAULT_LOOKBACK_MINUTES))
    limit = int(limit or _setting_int("vobiz_recording_backfill_limit", VOBIZ_RECORDING_BACKFILL_DEFAULT_LIMIT))
    minutes = max(minutes, 5)
    limit = max(1, min(limit, 2000))
    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-minutes)

    processed: list[dict] = []
    skipped = 0
    errors: list[dict] = []
    for channel in _vobiz_recording_channels():
        try:
            result = _backfill_recent_vobiz_recordings_for_channel(channel, cutoff=cutoff, limit=limit)
            processed.extend(result.get("processed") or [])
            skipped += int(result.get("skipped") or 0)
        except Exception as exc:
            errors.append({"channel": channel.name, "error": str(exc)})
            frappe.log_error(
                title="Vobiz recording backfill failed",
                message=f"Channel {channel.name}: {frappe.get_traceback()}",
            )

    return {
        "status": "success" if not errors else "partial",
        "processed_count": len(processed),
        "skipped_count": skipped,
        "errors": errors,
        "processed": processed[:50],
    }


def _backfill_recent_vobiz_recordings_for_channel(channel, *, cutoff, limit: int) -> dict:
    auth_id = str(channel.get("vobiz_auth_id") or "").strip()
    auth_token = _get_password(channel, "vobiz_auth_token")
    if not auth_id or not auth_token:
        return {"processed": [], "skipped": 0}

    recordings = _fetch_vobiz_recording_list(auth_id, auth_token, limit=limit, cutoff=cutoff)
    processed: list[dict] = []
    skipped = 0

    for recording in recordings:
        recording_dt = _parse_vobiz_datetime(recording.get("add_time"))
        if recording_dt and recording_dt < cutoff:
            skipped += 1
            continue
        if _recording_duration_sec(recording) <= 0:
            skipped += 1
            continue

        call_uuid = str(recording.get("call_uuid") or recording.get("recording_id") or "").strip()
        if not call_uuid:
            skipped += 1
            continue

        payload = _vobiz_recording_api_payload(recording, channel, auth_id)
        existing = frappe.db.exists("AI Call Log", {"call_uuid": call_uuid})
        if not existing:
            existing = _find_existing_call_log(payload)
        if existing:
            existing_doc = frappe.get_doc("AI Call Log", existing)
            if existing_doc.get("recording_url") or existing_doc.get("external_recording_url"):
                skipped += 1
                continue

        task_name, attempt_name = find_task_and_attempt(payload)
        task = frappe.get_doc("AI Task", task_name) if task_name else None
        attempt = frappe.get_doc("AI Task Attempt", attempt_name) if attempt_name else None
        if task and not attempt:
            attempts = frappe.get_all(
                "AI Task Attempt",
                filters={"task": task.name},
                order_by="creation desc",
                limit=1,
                pluck="name",
            )
            attempt = frappe.get_doc("AI Task Attempt", attempts[0]) if attempts else None

        call_log = upsert_call_log(payload, task=task, attempt=attempt)
        _attach_recording_to_task_docs(payload, task=task, attempt=attempt)
        _mark_call_log_waiting_for_transcript(call_log)
        if task:
            _handle_order_confirmation_callback(task, payload, "completed")
            _handle_repeat_followup_callback(task, payload, "completed")
            _handle_fresh_followup_callback(task, payload, "completed")

        processed.append({"call_log": call_log, "call_uuid": call_uuid, "channel": channel.name})

    if processed:
        record_provider_event(
            provider="Vobiz",
            operation="recording_backfill",
            status="Succeeded",
            company=channel.get("company"),
            request={"channel": channel.name, "lookback_limit": limit},
            response={"processed_count": len(processed), "skipped_count": skipped, "processed": processed[:10]},
        )

    frappe.db.commit()
    return {"processed": processed, "skipped": skipped}


def _fetch_vobiz_recording_list(auth_id: str, auth_token: str, *, limit: int, cutoff=None) -> list[dict]:
    url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Recording/"
    rows: list[dict] = []
    offset = 0
    page_size = min(max(limit, 1), 250)
    while len(rows) < limit:
        response = requests.get(
            url,
            headers={"X-Auth-ID": auth_id, "X-Auth-Token": auth_token, "Accept": "application/json"},
            params={"page": 1, "limit": page_size, "per_page": page_size, "offset": offset},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        batch = data.get("objects") if isinstance(data, dict) else data
        batch_rows = [row for row in (batch or []) if isinstance(row, dict)]
        if not batch_rows:
            break
        rows.extend(batch_rows[: max(limit - len(rows), 0)])

        if cutoff:
            oldest = _parse_vobiz_datetime(batch_rows[-1].get("add_time"))
            if oldest and oldest < cutoff:
                break

        meta = data.get("meta") if isinstance(data, dict) else {}
        if not isinstance(meta, dict) or not meta.get("next"):
            break
        offset += len(batch_rows)
        if len(batch_rows) < page_size:
            break
    return rows[:limit]


def _vobiz_recording_api_payload(recording: dict, channel, auth_id: str) -> dict:
    endpoints = _parse_json_object(channel.get("endpoint_paths_json"))
    duration_sec = _recording_duration_sec(recording)
    call_uuid = recording.get("call_uuid") or recording.get("recording_id")
    payload = {
        "event": "recording.completed",
        "Event": "recording.completed",
        "CallStatus": "completed",
        "status": "completed",
        "account_id": auth_id,
        "AccountId": auth_id,
        "CallUUID": call_uuid,
        "call_uuid": call_uuid,
        "recording_id": recording.get("recording_id") or call_uuid,
        "recording_url": recording.get("recording_url"),
        "url": recording.get("recording_url"),
        "recording_duration_sec": duration_sec,
        "Duration": duration_sec,
        "from_number": recording.get("from_number"),
        "to_number": recording.get("to_number"),
        "From": recording.get("from_number"),
        "To": recording.get("to_number"),
        "Direction": _infer_recording_direction(recording, channel, endpoints),
        "channel_account": channel.name,
        "company": channel.get("company"),
        "TrunkID": channel.get("trunk_id"),
        "trunk_id": channel.get("trunk_id"),
        "Domain": endpoints.get("sip_uri") or endpoints.get("inbound_domain"),
        "domain": endpoints.get("sip_uri") or endpoints.get("inbound_domain"),
        "recording_api_payload": recording,
        "recording_backfilled": 1,
    }
    if recording.get("add_time"):
        payload["started_at"] = recording.get("add_time")
        payload["ended_at"] = recording.get("add_time")
    return payload


def _attach_recording_to_task_docs(payload: dict, *, task=None, attempt=None) -> None:
    recording_url = payload.get("recording_url") or payload.get("url")
    if not recording_url:
        return
    if task:
        if not task.get("recording_url"):
            task.recording_url = recording_url
        if not task.get("call_uuid") and payload.get("CallUUID"):
            task.call_uuid = payload.get("CallUUID")
        task.vobiz_recording_payload = as_json(payload)
        if task.status in {"Queued", "Running", "Waiting"}:
            task.status = "Completed"
        task.save(ignore_permissions=True)
    if attempt:
        if not attempt.get("recording_url"):
            attempt.recording_url = recording_url
        if not attempt.get("call_uuid") and payload.get("CallUUID"):
            attempt.call_uuid = payload.get("CallUUID")
        attempt.vobiz_recording_payload = as_json(payload)
        if attempt.status in {"Started", "Retry Scheduled"}:
            attempt.status = "Succeeded"
        attempt.save(ignore_permissions=True)


def _mark_call_log_waiting_for_transcript(call_log: str | None) -> None:
    if not call_log or not frappe.db.exists("AI Call Log", call_log):
        return
    doc = frappe.get_doc("AI Call Log", call_log)
    if doc.get("transcript") or doc.get("transcript_summary"):
        return
    if doc.get("ai_disposition") and not _is_missing_transcript_fallback_disposition(doc):
        return
    if frappe.get_meta("AI Call Log").has_field("erp_status_update_status"):
        if _is_missing_transcript_fallback_disposition(doc):
            doc.ai_disposition = ""
            doc.ai_disposition_reason = ""
            doc.ai_disposition_confidence = 0
            doc.ai_disposition_summary = ""
        doc.erp_status_update_status = "Skipped"
        doc.erp_status_update_response = as_json(
            {
                "reason": "waiting_for_transcript",
                "recording_backfilled": True,
                "recording_available": bool(doc.get("recording_url") or doc.get("external_recording_url")),
            }
        )
        doc.flags.ignore_ai_disposition_auto_sync = True
        doc.save(ignore_permissions=True)


def _is_missing_transcript_fallback_disposition(doc) -> bool:
    disposition = str(doc.get("ai_disposition") or "").strip().lower()
    reason = str(doc.get("ai_disposition_reason") or "").strip().lower()
    response = _parse_json_object(doc.get("erp_status_update_response"))
    custom = str(response.get("custom_vobiz_disposition") or response.get("disposition") or "").strip().lower()
    return (
        disposition == "not answered"
        and ("no transcript" in reason or custom == "not reachable" or response.get("reason") == "missing_transcript_fallback")
    )


def _vobiz_recording_channels() -> list:
    channels = []
    if not frappe.db.exists("DocType", "AI Channel Account"):
        return channels
    rows = frappe.get_all(
        "AI Channel Account",
        filters={"enabled": 1, "channel_type": "LiveKit"},
        fields=["name"],
        limit_page_length=500,
    )
    for row in rows:
        try:
            channel = frappe.get_doc("AI Channel Account", row.name)
        except Exception:
            continue
        if channel.get("vobiz_auth_id") and _get_password(channel, "vobiz_auth_token"):
            channels.append(channel)
    return channels


def _recording_duration_sec(recording: dict) -> int:
    for key in ("recording_duration_ms", "duration_ms"):
        value = recording.get(key)
        if value not in (None, ""):
            try:
                return int(float(value) / 1000)
            except (TypeError, ValueError):
                pass
    for key in ("recording_duration_sec", "duration", "Duration"):
        value = recording.get(key)
        if value not in (None, ""):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                pass
    return 0


def _parse_vobiz_datetime(value: Any):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return frappe.utils.get_datetime(value)
        except Exception:
            return None


def _infer_recording_direction(recording: dict, channel, endpoints: dict) -> str:
    from_number = recording.get("from_number")
    to_number = recording.get("to_number")
    line_values = [
        channel.get("default_from"),
        endpoints.get("outbound_phone_number"),
        endpoints.get("inbound_phone_number"),
        endpoints.get("phone_number"),
        endpoints.get("vobiz_phone_number"),
    ]
    if any(_same_phone(from_number, line) for line in line_values if line):
        return "Outbound"
    if any(_same_phone(to_number, line) for line in line_values if line):
        return "Inbound"
    return str(recording.get("direction") or "").strip() or "Unknown"


def _setting_enabled(fieldname: str, *, default: bool = False) -> bool:
    value = _setting_value(fieldname, 1 if default else 0)
    return value in (1, "1", True, "true", "True", "yes", "Yes")


def _setting_int(fieldname: str, default: int) -> int:
    value = _setting_value(fieldname, default)
    try:
        return int(value)
    except Exception:
        return default


def _setting_value(fieldname: str, default=None):
    try:
        meta = frappe.get_meta("Confluence AI Settings")
        if not meta.has_field(fieldname):
            return default
        value = frappe.db.get_single_value("Confluence AI Settings", fieldname)
        return value if value not in (None, "") else default
    except Exception:
        return default


def download_vobiz_recording(recording_url: str, task) -> str | None:
    if not recording_url:
        return None
    if "storage.vobiz.ai" in recording_url or "test" in recording_url:
        return None

    # 1. Get channel account from task agent
    agent_name = task.assigned_agent or task.target_agent
    if not agent_name:
        return None

    try:
        agent = frappe.get_doc("AI Agent", agent_name)
    except Exception:
        return None

    account_name = agent.allowed_channel_account
    if not account_name:
        return None

    try:
        account = frappe.get_doc("AI Channel Account", account_name)
        api_key = account.get_password("api_key")
        api_secret = account.get_password("api_secret")
    except Exception:
        return None

    if not api_key or not api_secret:
        return None

    headers = {
        "X-Auth-ID": api_key,
        "X-Auth-Token": api_secret
    }

    try:
        response = requests.get(recording_url, headers=headers, timeout=30)
        response.raise_for_status()

        # Save to Frappe file manager
        from frappe.utils.file_manager import save_file
        call_uuid = task.call_uuid or "unknown"
        file_name = f"vobiz_recording_{call_uuid}.wav"

        file_doc = save_file(
            fname=file_name,
            content=response.content,
            dt="AI Task",
            dn=task.name,
            folder="Home/Attachments",
            is_private=1
        )
        return file_doc.file_url
    except Exception as e:
        frappe.log_error(
            title="Vobiz recording download failed",
            message=f"Failed to download recording from {recording_url}. Error: {str(e)}"
        )
        return None


def backfill_vobiz_recording_from_media(payload: dict, task=None, attempt=None, call_log: str | None = None) -> str | None:
    """Recover a Vobiz recording URL when the recording.completed webhook is missing."""
    call_log_doc = None
    if call_log and frappe.db.exists("AI Call Log", call_log):
        call_log_doc = frappe.get_doc("AI Call Log", call_log)
        if call_log_doc.recording_url or call_log_doc.external_recording_url:
            return call_log_doc.external_recording_url or call_log_doc.recording_url

    recording_url = _expected_vobiz_recording_url(payload, task)
    if not recording_url:
        return None

    account_id = _vobiz_account_id(payload, recording_url)
    for headers in _vobiz_media_auth_candidates(payload, task, account_id):
        if _vobiz_media_url_exists(recording_url, headers):
            if call_log_doc:
                backfill_payload = {
                    "event": "recording.backfilled",
                    "source": "vobiz_media_probe",
                    "account_id": account_id,
                    "call_uuid": _vobiz_recording_call_uuid(payload, task),
                    "recording_url": recording_url,
                    "reason": "recording.completed webhook was not received for this call",
                }
                call_log_doc.external_recording_url = recording_url
                call_log_doc.recording_url = recording_url
                call_log_doc.recording_payload_json = as_json(backfill_payload)
                call_log_doc.save(ignore_permissions=True)
            if task and not getattr(task, "recording_url", None):
                task.recording_url = recording_url
            if attempt and not getattr(attempt, "recording_url", None):
                attempt.recording_url = recording_url
            return recording_url

    return None


def _expected_vobiz_recording_url(payload: dict, task=None) -> str | None:
    if payload.get("recording_url") or payload.get("url") or payload.get("recording"):
        return payload.get("recording_url") or payload.get("url") or payload.get("recording")

    account_id = _vobiz_account_id(payload)
    call_uuid = _vobiz_recording_call_uuid(payload, task)
    if not account_id or not call_uuid:
        return None
    return f"https://media.vobiz.ai/v1/Account/{account_id}/Recording/{call_uuid}.wav"


def _vobiz_account_id(payload: dict, recording_url: str | None = None) -> str | None:
    account_id = payload.get("AccountId") or payload.get("account_id") or payload.get("account")
    if account_id:
        return str(account_id).strip()
    if recording_url:
        parts = [part for part in recording_url.split("/") if part]
        if "Account" in parts:
            idx = parts.index("Account")
            if len(parts) > idx + 1:
                return parts[idx + 1]
    return None


def _vobiz_recording_call_uuid(payload: dict, task=None) -> str | None:
    event_type = str(payload.get("event") or payload.get("event_type") or payload.get("Event") or "").lower()
    if event_type in {"hangup", "completed", "call_ended"}:
        value = payload.get("SIPCallID") or payload.get("sip_call_id")
    else:
        value = None
    value = (
        value
        or payload.get("call_uuid")
        or payload.get("CallUUID")
        or payload.get("recording_id")
        or payload.get("transcription_id")
        or payload.get("SIPCallID")
        or payload.get("sip_call_id")
        or getattr(task, "call_uuid", None)
    )
    return str(value).strip() if value else None


def _vobiz_media_auth_candidates(payload: dict, task=None, account_id: str | None = None) -> list[dict[str, str]]:
    channel_names = []

    for name in (
        getattr(task, "channel_account", None),
        _task_agent_channel_account(task),
    ):
        if name:
            channel_names.append(name)

    if account_id:
        channel_names.extend(
            frappe.get_all(
                "AI Channel Account",
                filters={"enabled": 1, "vobiz_auth_id": account_id},
                pluck="name",
            )
        )

    trunk_id = payload.get("TrunkID") or payload.get("trunk_id")
    if trunk_id:
        channel_names.extend(
            frappe.get_all(
                "AI Channel Account",
                filters={"enabled": 1, "trunk_id": trunk_id},
                pluck="name",
            )
        )

    candidates = []
    seen_channels = set()
    for channel_name in channel_names:
        if channel_name in seen_channels or not frappe.db.exists("AI Channel Account", channel_name):
            continue
        seen_channels.add(channel_name)
        try:
            channel = frappe.get_doc("AI Channel Account", channel_name)
        except Exception:
            continue

        auth_id = str(channel.get("vobiz_auth_id") or "").strip()
        auth_token = _get_password(channel, "vobiz_auth_token")
        if auth_id and auth_token:
            candidates.append({"X-Auth-ID": auth_id, "X-Auth-Token": auth_token})
        if account_id and auth_token and account_id != auth_id:
            candidates.append({"X-Auth-ID": account_id, "X-Auth-Token": auth_token})

        api_key = _get_password(channel, "api_key")
        api_secret = _get_password(channel, "api_secret")
        if api_key and api_secret and api_key.startswith("MA_"):
            candidates.append({"X-Auth-ID": api_key, "X-Auth-Token": api_secret})
        if account_id and api_secret:
            candidates.append({"X-Auth-ID": account_id, "X-Auth-Token": api_secret})

    unique = []
    seen = set()
    for headers in candidates:
        key = (headers.get("X-Auth-ID"), headers.get("X-Auth-Token"))
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            unique.append(headers)
    return unique


def _task_agent_channel_account(task) -> str | None:
    agent_name = getattr(task, "assigned_agent", None) or getattr(task, "target_agent", None)
    if not agent_name or not frappe.db.exists("AI Agent", agent_name):
        return None
    return frappe.db.get_value("AI Agent", agent_name, "allowed_channel_account")


def _get_password(doc, fieldname: str) -> str:
    try:
        return str(doc.get_password(fieldname, raise_exception=False) or "").strip()
    except TypeError:
        return str(doc.get_password(fieldname) or "").strip()
    except Exception:
        return ""


def _vobiz_media_url_exists(recording_url: str, headers: dict[str, str]) -> bool:
    import requests

    try:
        response = requests.get(
            recording_url,
            headers={**headers, "Range": "bytes=0-0"},
            stream=True,
            timeout=8,
        )
        response.close()
        return 200 <= response.status_code < 300
    except Exception:
        return False


def handle_callback(payload: dict) -> dict:
    from confluence_ai.services.inbound_sales import handle_vobiz_inbound_call

    # Inbound call starts must create their own task keyed by CallUUID before
    # generic phone/trunk matching runs. Otherwise a fresh inbound call from the
    # same caller can attach to an older still-running task.
    inbound_result = handle_vobiz_inbound_call(payload)
    if inbound_result.get("status") in {"routed", "duplicate"} and inbound_result.get("task"):
        task = frappe.get_doc("AI Task", inbound_result["task"])
        attempts = frappe.get_all(
            "AI Task Attempt",
            filters={"task": task.name},
            order_by="creation desc",
            limit=1,
            pluck="name",
        )
        attempt = frappe.get_doc("AI Task Attempt", attempts[0]) if attempts else None
        call_log = upsert_call_log(payload, task=task, attempt=attempt)
        frappe.db.commit()
        _enqueue_call_disposition_if_ready(call_log, payload.get("event") or payload.get("event_type") or payload.get("Event") or "status_update")
        inbound_result["call_log"] = call_log
        return inbound_result

    # 1. Match the webhook payload to a task and/or attempt
    task_name, attempt_name = find_task_and_attempt(payload)

    if not task_name:
        call_log = upsert_call_log(payload)
        frappe.db.commit()
        frappe.log_error(
            title="Vobiz callback match failed",
            message=f"Could not find matching AI Task or AI Task Attempt for payload: {json.dumps(payload, default=str)}",
        )
        return {"status": "error", "message": "No matching task or attempt found", "call_log": call_log}

    # 2. Get the documents
    task = frappe.get_doc("AI Task", task_name)
    attempt = frappe.get_doc("AI Task Attempt", attempt_name) if attempt_name else None
    if not attempt:
        latest_attempts = frappe.get_all(
            "AI Task Attempt",
            filters={"task": task_name},
            order_by="creation desc",
            limit=1,
        )
        if latest_attempts:
            attempt = frappe.get_doc("AI Task Attempt", latest_attempts[0].name)

    # 3. Determine the type of event and process accordingly
    event_type = payload.get("event") or payload.get("event_type") or payload.get("Event") or "status_update"
    event_type_lower = event_type.lower()

    # Load/initialize JSON payload trackers
    task_result = json.loads(task.result_json) if task.result_json else {}
    attempt_response = json.loads(attempt.response_json) if (attempt and attempt.response_json) else {}

    if not isinstance(task_result, dict):
        task_result = {"raw_result": task_result}
    if not isinstance(attempt_response, dict):
        attempt_response = {"raw_response": attempt_response}

    # Save the raw payload details
    task_result["last_vobiz_payload"] = payload
    if attempt:
        attempt_response["last_vobiz_payload"] = payload

    if event_type_lower in {"initiated", "dial", "ringing", "callinitiated"}:
        task.vobiz_initiated_payload = as_json(payload)
        if attempt:
            attempt.vobiz_initiated_payload = as_json(payload)
        task.status = "Running"
        if attempt:
            attempt.status = "Started"
            call_uuid = payload.get("CallUUID") or payload.get("call_uuid")
            sip_call_id = payload.get("SIPCallID") or payload.get("sip_call_id")
            if call_uuid:
                attempt.external_id = call_uuid
                attempt_response["vobiz_call_uuid"] = call_uuid
            elif sip_call_id:
                attempt.external_id = sip_call_id
            attempt_response["initiated_at"] = now()

    elif event_type_lower in {"status", "hangup", "answer", "completed", "failed", "busy", "no_answer", "timeout", "cancel"}:
        task.vobiz_hangup_payload = as_json(payload)
        if attempt:
            attempt.vobiz_hangup_payload = as_json(payload)
        status = payload.get("CallStatus") or payload.get("Status") or payload.get("status") or event_type
        status_lower = status.lower()

        if status_lower in {"completed", "hangup"}:
            task.status = "Completed"
            if attempt:
                attempt.status = "Succeeded"
                attempt.ended_at = now()
        elif status_lower in {"failed", "busy", "no_answer", "timeout", "cancel"}:
            task.status = "Failed"
            task.last_error = payload.get("Reason") or payload.get("hangup_cause") or status
            if attempt:
                attempt.status = "Failed"
                attempt.error_message = task.last_error
                attempt.ended_at = now()
        elif status_lower in {"ringing", "dialing", "in_progress", "answer"}:
            task.status = "Running"
            if attempt:
                attempt.status = "Started"

        # Update duration if available (duration is in seconds from Vobiz, store as MS)
        duration = payload.get("Duration") or payload.get("duration")
        if duration is not None:
            try:
                duration_ms = int(float(duration) * 1000)
                duration_sec = int(float(duration))
                if attempt:
                    attempt.duration_ms = duration_ms
                    attempt.duration = duration_sec
                task_result["duration_ms"] = duration_ms
                task.duration = duration_sec
            except (ValueError, TypeError):
                pass

        call_uuid = payload.get("CallUUID") or payload.get("call_uuid")
        if call_uuid:
            task_result["vobiz_call_uuid"] = call_uuid
            task.call_uuid = call_uuid
            if attempt:
                attempt_response["vobiz_call_uuid"] = call_uuid
                attempt.call_uuid = call_uuid
                if not attempt.external_id:
                    attempt.external_id = call_uuid

    elif event_type_lower in VOBIZ_TRANSCRIPT_EVENTS:
        task.vobiz_transcript_payload = as_json(payload)
        if attempt:
            attempt.vobiz_transcript_payload = as_json(payload)
        transcript = normalize_vobiz_ai_transcript_labels(
            payload.get("transcript") or payload.get("text") or payload.get("transcript_text") or payload.get("transcription_text")
        )
        if transcript:
            task_result["transcript"] = transcript
            task.transcript = transcript
            if attempt:
                attempt_response["transcript"] = transcript
                attempt.transcript = transcript

    elif event_type_lower in VOBIZ_RECORDING_EVENTS:
        task.vobiz_recording_payload = as_json(payload)
        if attempt:
            attempt.vobiz_recording_payload = as_json(payload)
        recording_url = payload.get("recording_url") or payload.get("url") or payload.get("recording")
        if recording_url:
            local_url = download_vobiz_recording(recording_url, task)
            final_url = local_url or recording_url
            task_result["recording_url"] = final_url
            task.recording_url = final_url
            if attempt:
                attempt_response["recording_url"] = final_url
                attempt.recording_url = recording_url


    # Always copy status/uuids to fields if present in payload
    telephony_status = payload.get("CallStatus") or payload.get("Status") or payload.get("status") or event_type
    if telephony_status:
        task.telephony_status = telephony_status
        if attempt:
            attempt.telephony_status = telephony_status

    call_uuid = payload.get("CallUUID") or payload.get("call_uuid")
    if call_uuid:
        task.call_uuid = call_uuid
        if attempt:
            attempt.call_uuid = call_uuid

    call_log = upsert_call_log(payload, task=task, attempt=attempt)
    if event_type_lower in VOBIZ_TRANSCRIPT_EVENTS:
        recovered_recording_url = backfill_vobiz_recording_from_media(payload, task=task, attempt=attempt, call_log=call_log)
        if recovered_recording_url:
            task_result["recording_url"] = recovered_recording_url
            task_result["recording_backfilled"] = True
            task.recording_url = recovered_recording_url
            if attempt:
                attempt_response["recording_url"] = recovered_recording_url
                attempt_response["recording_backfilled"] = True
                attempt.recording_url = recovered_recording_url

    # 4. Save updates
    task.result_json = as_json(task_result)
    task.save(ignore_permissions=True)

    if attempt:
        attempt.response_json = as_json(attempt_response)
        attempt.save(ignore_permissions=True)

    frappe.db.commit()
    disposition_result = _enqueue_call_disposition_if_ready(call_log, event_type_lower)
    order_confirmation_result = _handle_order_confirmation_callback(task, payload, event_type_lower)
    repeat_followup_result = _handle_repeat_followup_callback(task, payload, event_type_lower)
    fresh_followup_result = _handle_fresh_followup_callback(task, payload, event_type_lower)

    return {
        "status": "success",
        "task": task.name,
        "attempt": attempt.name if attempt else None,
        "call_log": call_log,
        "processed_event": event_type,
        "ai_disposition": disposition_result,
        "order_confirmation": order_confirmation_result,
        "repeat_followup": repeat_followup_result,
        "fresh_followup": fresh_followup_result,
    }


def _enqueue_call_disposition_if_ready(call_log: str | None, event_type: str) -> dict | None:
    event_type_lower = str(event_type or "").lower()
    if event_type_lower not in {
        "status",
        "hangup",
        "completed",
        "failed",
        "busy",
        "no_answer",
        "timeout",
        "cancel",
        "transcript",
        "call_transcript",
        "transcript_ready",
        "transcription.completed",
    }:
        return None
    try:
        from confluence_ai.services.call_disposition import enqueue_call_disposition

        return enqueue_call_disposition(call_log)
    except Exception as exc:
        from confluence_ai.services.utils import create_error

        create_error("AI Call Disposition Queue", str(exc), source="vobiz", payload={"call_log": call_log}, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _handle_order_confirmation_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice" or task.external_record_type != "Order Confirmation Workflow" or not task.external_record_id:
        return None
    if event_type_lower not in {"status", "hangup", "completed", "failed", "busy", "no_answer", "timeout", "cancel", "transcript", "call_transcript", "transcript_ready", "transcription.completed"}:
        return None
    try:
        from confluence_ai.services import order_confirmation

        if not frappe.db.exists("Order Confirmation Workflow", task.external_record_id):
            return {"status": "ignored", "reason": "missing_workflow"}
        workflow = frappe.get_doc("Order Confirmation Workflow", task.external_record_id)
        if workflow.status in order_confirmation.FINAL_STATES:
            return {"status": "ignored", "reason": "final_state", "workflow": workflow.name}

        status = str(payload.get("CallStatus") or payload.get("Status") or payload.get("status") or "").lower()
        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcription_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or payload.get("transcription_text")
            or ""
        )
        outcome = payload.get("order_confirmation_outcome") or payload.get("outcome")
        if status in {"failed", "busy", "no_answer", "timeout", "cancel", "cancelled", "canceled"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("Reason") or payload.get("hangup_cause") or "Voice call did not complete before confirmation."
        elif not notes and event_type_lower in {"status", "hangup", "completed"}:
            return order_confirmation.wait_for_voice_transcript(
                workflow.name,
                "Voice call ended; waiting for Vobiz transcript before deciding confirmation.",
            )

        return order_confirmation.handle_voice_result(
            workflow=workflow.name,
            task=task.name,
            outcome=outcome,
            notes=notes,
        )
    except Exception as exc:
        from confluence_ai.services.utils import create_error

        create_error("Order Confirmation Voice Callback", str(exc), source="vobiz", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _handle_repeat_followup_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice" or task.external_record_type != "AI Repeat Follow Up Workflow" or not task.external_record_id:
        return None
    if event_type_lower not in {"status", "hangup", "completed", "failed", "busy", "no_answer", "timeout", "cancel", "transcript", "call_transcript", "transcript_ready", "transcription.completed"}:
        return None
    try:
        from confluence_ai.services import repeat_followup

        if not frappe.db.exists("AI Repeat Follow Up Workflow", task.external_record_id):
            return {"status": "ignored", "reason": "missing_workflow"}

        status = str(payload.get("CallStatus") or payload.get("Status") or payload.get("status") or "").lower()
        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcription_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or payload.get("transcription_text")
            or ""
        )
        outcome = payload.get("repeat_followup_outcome") or payload.get("outcome")
        if status in {"failed", "busy", "no_answer", "timeout", "cancel", "cancelled", "canceled"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("Reason") or payload.get("hangup_cause") or "Repeat follow-up voice call did not complete."
        elif not notes and event_type_lower in {"status", "hangup", "completed"} and status in {"completed", "hangup"}:
            return repeat_followup.wait_for_voice_transcript(
                task.external_record_id,
                "Voice call ended; waiting for Vobiz transcript before deciding repeat follow-up outcome.",
            )

        return repeat_followup.handle_voice_result(
            workflow=task.external_record_id,
            task=task.name,
            outcome=outcome,
            notes=notes,
        )
    except Exception as exc:
        from confluence_ai.services.utils import create_error

        create_error("Repeat Follow Up Voice Callback", str(exc), source="vobiz", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _handle_fresh_followup_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice":
        return None
    if event_type_lower not in {"status", "hangup", "completed", "failed", "busy", "no_answer", "timeout", "cancel", "transcript", "call_transcript", "transcript_ready", "transcription.completed"}:
        return None
    workflow_name = _fresh_followup_workflow_for_task(task)
    if not workflow_name:
        return None
    try:
        from confluence_ai.services import fresh_followup

        status = str(payload.get("CallStatus") or payload.get("Status") or payload.get("status") or "").lower()
        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcription_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or payload.get("transcription_text")
            or ""
        )
        outcome = payload.get("fresh_followup_outcome") or payload.get("outcome")
        if status in {"failed", "busy", "no_answer", "timeout", "cancel", "cancelled", "canceled"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("Reason") or payload.get("hangup_cause") or "Fresh follow-up voice call did not complete."
        elif not notes and event_type_lower in {"status", "hangup", "completed"} and status in {"completed", "hangup"}:
            return fresh_followup.wait_for_voice_transcript(
                workflow_name,
                "Voice call ended; waiting for Vobiz transcript before scheduling the next fresh follow-up agent.",
            )

        return fresh_followup.handle_voice_result(
            workflow=workflow_name,
            task=task.name,
            outcome=outcome,
            notes=notes,
            result=payload,
        )
    except Exception as exc:
        from confluence_ai.services.utils import create_error

        create_error("Fresh Follow Up Voice Callback", str(exc), source="vobiz", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _fresh_followup_workflow_for_task(task) -> str | None:
    if task.external_record_type == "AI Fresh Follow Up Workflow" and task.external_record_id:
        return task.external_record_id
    return frappe.db.get_value("AI Fresh Follow Up Workflow Agent", {"task": task.name}, "parent")


def _payload_call_ids(payload: dict) -> list[str]:
    values = [
        payload.get("CallUUID"),
        payload.get("call_uuid"),
        payload.get("RequestID"),
        payload.get("request_id"),
        payload.get("SIPCallID"),
        payload.get("sip_call_id"),
        payload.get("recording_id"),
        payload.get("transcription_id"),
    ]
    result = []
    for value in values:
        if value:
            value = str(value).strip()
            if value and value not in result:
                result.append(value)
    return result


def _find_existing_call_log(payload: dict) -> str | None:
    ids = _payload_call_ids(payload)
    for value in ids:
        existing = frappe.db.exists("AI Call Log", {"call_uuid": value})
        if existing:
            return existing
        existing = frappe.db.exists("AI Call Log", {"sip_call_id": value})
        if existing:
            return existing

    phone = payload.get("To") or payload.get("to") or payload.get("to_number")
    from_number = payload.get("From") or payload.get("from") or payload.get("from_number")
    if phone and from_number:
        recent = frappe.get_all(
            "AI Call Log",
            filters={
                "customer_phone": phone,
                "from_number": from_number,
                "status": ["in", ["Initiated", "Ringing", "In Progress", "Unknown"]],
            },
            order_by="creation desc",
            limit=1,
            pluck="name",
        )
        if recent:
            return recent[0]

    nearby = _find_existing_call_log_by_phone_window(payload)
    if nearby:
        return nearby

    return None


def _find_existing_call_log_by_phone_window(payload: dict) -> str | None:
    customer_phone = _customer_phone_from_payload(payload)
    suffix = _phone_suffix(customer_phone)
    if not suffix:
        return None

    event_time = _parse_vobiz_datetime(payload.get("started_at") or payload.get("ended_at") or payload.get("add_time"))
    if not event_time:
        return None

    start = frappe.utils.add_to_date(event_time, minutes=-15)
    end = frappe.utils.add_to_date(event_time, minutes=15)
    company = payload.get("company")
    conditions = [
        "`creation` between %(start)s and %(end)s",
        "(`recording_url` is null or `recording_url` = '')",
        "(`external_recording_url` is null or `external_recording_url` = '')",
        "(`customer_phone` like %(suffix_like)s or `from_number` like %(suffix_like)s or `to_number` like %(suffix_like)s)",
    ]
    params = {
        "start": start,
        "end": end,
        "suffix_like": f"%{suffix}",
    }
    if company:
        conditions.append("(`company` = %(company)s or `company` is null or `company` = '')")
        params["company"] = company

    rows = frappe.db.sql(
        f"""
        select name
        from `tabAI Call Log`
        where {" and ".join(conditions)}
        order by creation desc
        limit 1
        """,
        params,
        as_dict=True,
    )
    return rows[0].name if rows else None


def _parse_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_livekit_trunk_ids(payload: dict) -> list[str]:
    """Return LiveKit ST_* trunk IDs that can correspond to a Vobiz callback."""
    raw_trunk_id = (payload.get("TrunkID") or payload.get("trunk_id") or "").strip()
    domain = (payload.get("Domain") or payload.get("domain") or "").strip()
    from_number = (payload.get("From") or payload.get("from") or payload.get("from_number") or "").strip()

    candidates: list[str] = []
    if raw_trunk_id:
        candidates.append(raw_trunk_id)

    if frappe.db.exists("DocType", "AI Sales Disease Route"):
        route_filters = []
        if raw_trunk_id:
            route_filters.append({"inbound_vobiz_trunk_id": raw_trunk_id})
        if domain:
            route_filters.append({"inbound_domain": domain})

        for flt in route_filters:
            for route in frappe.get_all("AI Sales Disease Route", filters=flt, fields=["channel_account"]):
                if not route.channel_account:
                    continue
                channel_trunk_id = frappe.db.get_value("AI Channel Account", route.channel_account, "trunk_id")
                if channel_trunk_id:
                    candidates.append(channel_trunk_id)

    for row in frappe.get_all(
        "AI Channel Account",
        filters={"enabled": 1},
        fields=["trunk_id", "endpoint_paths_json"],
    ):
        endpoints = _parse_json_object(row.endpoint_paths_json)
        matches = False
        if domain and endpoints.get("sip_uri") == domain:
            matches = True
        if domain and endpoints.get("inbound_domain") == domain:
            matches = True
        if from_number and endpoints.get("outbound_phone_number") == from_number:
            matches = True
        if raw_trunk_id and endpoints.get("vobiz_trunk_id") == raw_trunk_id:
            matches = True
        if raw_trunk_id and endpoints.get("inbound_vobiz_trunk_id") == raw_trunk_id:
            matches = True

        if matches and row.trunk_id:
            candidates.append(row.trunk_id)

    result = []
    for value in candidates:
        if value and value not in result:
            result.append(value)
    return result


def _candidate_channel_accounts(payload: dict) -> list[str]:
    candidates: list[str] = []
    explicit = payload.get("channel_account") or payload.get("ChannelAccount")
    if explicit:
        candidates.append(str(explicit).strip())

    account_id = _vobiz_account_id(payload, payload.get("recording_url") or payload.get("url"))
    if account_id:
        candidates.extend(
            frappe.get_all(
                "AI Channel Account",
                filters={"enabled": 1, "vobiz_auth_id": account_id},
                pluck="name",
            )
        )

    trunk_ids = _candidate_livekit_trunk_ids(payload)
    for trunk_id in trunk_ids:
        candidates.extend(
            frappe.get_all(
                "AI Channel Account",
                filters={"enabled": 1, "trunk_id": trunk_id},
                pluck="name",
            )
        )

    domain = (payload.get("Domain") or payload.get("domain") or "").strip()
    from_number = payload.get("From") or payload.get("from") or payload.get("from_number")
    to_number = payload.get("To") or payload.get("to") or payload.get("to_number")
    for row in frappe.get_all(
        "AI Channel Account",
        filters={"enabled": 1},
        fields=["name", "default_from", "endpoint_paths_json"],
        limit_page_length=500,
    ):
        endpoints = _parse_json_object(row.endpoint_paths_json)
        if domain and domain in {endpoints.get("sip_uri"), endpoints.get("inbound_domain")}:
            candidates.append(row.name)
        line_values = [
            row.default_from,
            endpoints.get("outbound_phone_number"),
            endpoints.get("inbound_phone_number"),
            endpoints.get("phone_number"),
            endpoints.get("vobiz_phone_number"),
        ]
        if any(_same_phone(from_number, line) or _same_phone(to_number, line) for line in line_values if line):
            candidates.append(row.name)

    result = []
    for value in candidates:
        if value and value not in result and frappe.db.exists("AI Channel Account", value):
            result.append(value)
    return result


def _default_agent_for_channel(channel_name: str | None) -> str | None:
    if not channel_name:
        return None
    return frappe.db.get_value("AI Agent", {"enabled": 1, "allowed_channel_account": channel_name}, "name")


def _customer_phone_from_payload(payload: dict, fallback: str | None = None) -> str | None:
    explicit = payload.get("customer_phone") or payload.get("phone") or payload.get("caller_number")
    if explicit:
        return explicit

    from_number = payload.get("From") or payload.get("from") or payload.get("from_number")
    to_number = payload.get("To") or payload.get("to") or payload.get("to_number")
    direction = str(payload.get("Direction") or payload.get("direction") or "").lower()

    channel_name = (payload.get("channel_account") or payload.get("ChannelAccount") or "").strip()
    channel = frappe.get_doc("AI Channel Account", channel_name) if channel_name and frappe.db.exists("AI Channel Account", channel_name) else None
    endpoints = _parse_json_object(channel.get("endpoint_paths_json")) if channel else {}
    line_values = []
    if channel:
        line_values.append(channel.get("default_from"))
    line_values.extend(
        [
            endpoints.get("outbound_phone_number"),
            endpoints.get("inbound_phone_number"),
            endpoints.get("phone_number"),
            endpoints.get("vobiz_phone_number"),
        ]
    )

    if any(_same_phone(from_number, line) for line in line_values if line):
        return to_number or fallback
    if any(_same_phone(to_number, line) for line in line_values if line):
        return from_number or fallback
    if direction.startswith("in"):
        return from_number or fallback
    if direction.startswith("out"):
        return to_number or fallback
    return fallback or to_number or from_number


def _same_phone(left: Any, right: Any) -> bool:
    left_digits = re.sub(r"\D", "", str(left or ""))
    right_digits = re.sub(r"\D", "", str(right or ""))
    if not left_digits or not right_digits:
        return False
    if len(left_digits) >= 10 and len(right_digits) >= 10:
        return left_digits[-10:] == right_digits[-10:]
    return left_digits == right_digits


def _phone_suffix(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _find_repeat_followup_task_by_phone_and_trunk(candidate_trunk_ids: list[str], suffix: str | None) -> tuple[str | None, str | None]:
    if not suffix or not candidate_trunk_ids or not frappe.db.exists("DocType", "AI Repeat Follow Up Workflow"):
        return None, None

    channel_accounts: list[str] = []
    for row in frappe.get_all(
        "AI Channel Account",
        filters={"enabled": 1},
        fields=["name", "trunk_id", "endpoint_paths_json"],
    ):
        endpoints = _parse_json_object(row.endpoint_paths_json)
        ids = {
            row.trunk_id,
            endpoints.get("sip_trunk_id"),
            endpoints.get("outbound_sip_trunk_id"),
            endpoints.get("trunk_id"),
        }
        if any(value and value in candidate_trunk_ids for value in ids):
            channel_accounts.append(row.name)

    if not channel_accounts:
        return None, None

    rows = frappe.get_all(
        "AI Repeat Follow Up Workflow",
        filters={"status": ["in", ["Call Queued", "Call Running"]]},
        fields=[
            "name",
            "patient_mobile",
            "voice_task",
            "voice_channel_account",
            "livekit_channel_account_fallback",
        ],
        order_by="modified desc",
        limit=50,
    )
    for workflow in rows:
        digits = "".join(c for c in str(workflow.patient_mobile or "") if c.isdigit())
        if not digits.endswith(suffix):
            continue
        workflow_channels = {
            workflow.voice_channel_account,
            workflow.livekit_channel_account_fallback,
        }
        if not any(channel and channel in channel_accounts for channel in workflow_channels):
            continue
        if not workflow.voice_task or not frappe.db.exists("AI Task", workflow.voice_task):
            continue
        latest_attempts = frappe.get_all(
            "AI Task Attempt",
            filters={"task": workflow.voice_task},
            order_by="creation desc",
            limit=1,
            pluck="name",
        )
        return workflow.voice_task, (latest_attempts[0] if latest_attempts else None)

    return None, None


def upsert_call_log(payload: dict, task=None, attempt=None) -> str | None:
    """Create/update a human-friendly call log row from Vobiz callback payloads."""
    if not frappe.db.exists("DocType", "AI Call Log"):
        return None

    call_uuid = (
        payload.get("CallUUID")
        or payload.get("call_uuid")
        or payload.get("RequestID")
        or payload.get("recording_id")
        or payload.get("transcription_id")
    )
    sip_call_id = payload.get("SIPCallID") or payload.get("sip_call_id")

    existing = _find_existing_call_log(payload)

    doc = frappe.get_doc("AI Call Log", existing) if existing else frappe.new_doc("AI Call Log")
    event_type = payload.get("event") or payload.get("event_type") or payload.get("Event") or "status_update"
    event_type_lower = str(event_type).lower()
    channel_accounts = _candidate_channel_accounts(payload)
    channel_account = channel_accounts[0] if channel_accounts else None

    doc.provider = "Vobiz"
    doc.event_type = event_type
    doc.direction = payload.get("Direction") or payload.get("direction")
    doc.from_number = payload.get("From") or payload.get("from") or payload.get("from_number")
    doc.to_number = payload.get("To") or payload.get("to") or payload.get("to_number")
    doc.customer_phone = _customer_phone_from_payload(payload, doc.customer_phone)
    doc.call_uuid = doc.call_uuid or call_uuid
    doc.sip_call_id = doc.sip_call_id or sip_call_id
    if not doc.sip_call_id and event_type_lower in {"initiated", "dial", "ringing", "callinitiated"} and call_uuid:
        doc.sip_call_id = call_uuid
    doc.trunk_id = payload.get("TrunkID") or payload.get("trunk_id") or doc.trunk_id
    doc.domain = payload.get("Domain") or payload.get("domain") or doc.domain
    doc.reason = payload.get("Reason") or payload.get("reason") or payload.get("hangup_cause") or doc.reason
    doc.last_payload_json = as_json(payload)
    doc.company = doc.company or payload.get("company")
    if channel_account:
        doc.company = doc.company or frappe.db.get_value("AI Channel Account", channel_account, "company")
        doc.agent = doc.agent or _default_agent_for_channel(channel_account)

    if task:
        doc.task = task.name
        doc.agent = task.assigned_agent or task.target_agent
        doc.company = task.company or doc.company
        if not doc.company and doc.agent:
            doc.company = frappe.db.get_value("AI Agent", doc.agent, "company") or doc.company
        try:
            context = json.loads(task.context_json or "{}")
            if isinstance(context, dict):
                doc.customer_name = context.get("customer_name") or context.get("patient_name") or context.get("order_patient_name") or doc.customer_name
                doc.customer_phone = context.get("customer_phone") or context.get("phone") or context.get("phone_number") or doc.customer_phone
        except Exception:
            pass
    if attempt:
        doc.attempt = attempt.name
        doc.company = doc.company or attempt.company

    if not doc.company and doc.agent:
        doc.company = frappe.db.get_value("AI Agent", doc.agent, "company") or doc.company

    status = payload.get("CallStatus") or payload.get("Status") or payload.get("status") or event_type
    status_lower = str(status or "").lower()
    if status_lower == "rejected":
        doc.status = "Rejected"
    elif status_lower in {"failed", "failure"}:
        doc.status = "Failed"
    elif status_lower == "busy":
        doc.status = "Busy"
    elif status_lower in {"no_answer", "no answer"}:
        doc.status = "No Answer"
    elif status_lower in {"cancel", "cancelled", "canceled"}:
        doc.status = "Cancelled"
    elif status_lower in {"completed", "hangup"}:
        doc.status = "Completed"
    elif status_lower in {"ringing"}:
        doc.status = "Ringing"
    elif status_lower in {"answer", "in_progress"}:
        doc.status = "In Progress"
    elif event_type_lower in {"initiated", "dial", "callinitiated"}:
        doc.status = "Initiated"
    elif not doc.status:
        doc.status = "Unknown"

    if event_type_lower in {"initiated", "dial", "ringing", "callinitiated"}:
        doc.initiated_payload_json = as_json(payload)
        if not doc.started_at:
            doc.started_at = now()
    elif event_type_lower in {"status", "hangup", "answer", "completed", "failed", "busy", "no_answer", "timeout", "cancel"}:
        doc.status_payload_json = as_json(payload)
        if doc.status in {"Completed", "Failed", "Rejected", "No Answer", "Busy", "Cancelled"}:
            doc.ended_at = now()
    elif event_type_lower in VOBIZ_RECORDING_EVENTS:
        event_time = _parse_vobiz_datetime(payload.get("started_at") or payload.get("add_time"))
        if event_time and not doc.started_at:
            doc.started_at = event_time
        if event_time and not doc.ended_at:
            doc.ended_at = event_time

    duration = payload.get("Duration") or payload.get("duration") or payload.get("recording_duration_sec")
    if duration is not None:
        try:
            doc.duration_sec = int(float(duration))
        except (TypeError, ValueError):
            pass

    transcript = normalize_vobiz_ai_transcript_labels(
        payload.get("transcript") or payload.get("text") or payload.get("transcript_text") or payload.get("transcription_text")
    )
    if event_type_lower in {"transcript", "call_transcript", "transcript_ready", "transcription.completed"}:
        doc.transcript_payload_json = as_json(payload)
        if transcript:
            doc.transcript = transcript
            doc.transcript_summary = payload.get("summary") or payload.get("transcription_summary") or transcript[:1000]
        doc.sentiment = payload.get("sentiment") or doc.sentiment

    recording_url = payload.get("recording_url") or payload.get("url") or payload.get("recording")
    if event_type_lower in {"recording", "call_recording", "recording_ready", "recording.completed"}:
        doc.recording_payload_json = as_json(payload)
        if recording_url:
            doc.external_recording_url = recording_url
            doc.recording_url = recording_url

    doc.save(ignore_permissions=True)
    return doc.name


def normalize_vobiz_ai_transcript_labels(transcript: object) -> str:
    text = str(transcript or "")
    if "[AGENT]:" not in text or "[CUSTOMER]:" not in text:
        return text
    # In Vobiz bridged AI calls, the PSTN caller can arrive as AGENT and the
    # AI audio as CUSTOMER. Normalize logs to Confluence meaning.
    text = re.sub(r"\[AGENT\]\s*:", "[CALLER_TMP]:", text)
    text = re.sub(r"\[CUSTOMER\]\s*:", "[AGENT]:", text)
    text = re.sub(r"\[CALLER_TMP\]\s*:", "[CUSTOMER]:", text)
    return text


def find_task_and_attempt(payload: dict) -> tuple[str | None, str | None]:
    payload_trunk_id = (payload.get("TrunkID") or payload.get("trunk_id") or "").strip()
    candidate_trunk_ids = _candidate_livekit_trunk_ids(payload)

    # 1. Match by task ID or room name (mainly for LiveKit events or direct mappings)
    task_name = payload.get("task") or payload.get("task_name") or payload.get("task_id")
    if task_name:
        if candidate_trunk_ids:
            if frappe.db.exists("AI Task", {"name": task_name, "trunk_id": ["in", candidate_trunk_ids]}):
                return task_name, None
        elif frappe.db.exists("AI Task", task_name):
            return task_name, None

    room_name = payload.get("room_name") or payload.get("room")
    if room_name and room_name.startswith("agent-army-"):
        t_name = room_name[len("agent-army-") :]
        if candidate_trunk_ids:
            if frappe.db.exists("AI Task", {"name": t_name, "trunk_id": ["in", candidate_trunk_ids]}):
                return t_name, None
        elif frappe.db.exists("AI Task", t_name):
            return t_name, None

    # 2. Extract Phone Suffix (last 10 digits)
    phone = (
        payload.get("To")
        or payload.get("to")
        or payload.get("to_number")
        or payload.get("phone")
        or payload.get("From")
        or payload.get("from")
    )
    suffix = None
    if phone:
        digits = "".join(c for c in str(phone) if c.isdigit())
        if len(digits) >= 10:
            suffix = digits[-10:]

    # 3. Extract UUID
    uuid = (
        payload.get("CallUUID")
        or payload.get("call_uuid")
        or payload.get("SIPCallID")
        or payload.get("sip_call_id")
        or payload.get("transcription_id")
        or payload.get("recording_id")
    )

    # 4. Strict match for Vobiz payloads (requiring trunk identity, UUID/SIPCallID, and Phone Suffix)
    if candidate_trunk_ids and suffix:
        if uuid:
            # Check attempts by external_id or call_uuid matching the trunk
            attempts = frappe.get_all(
                "AI Task Attempt",
                filters={"trunk_id": ["in", candidate_trunk_ids]},
                or_filters={"external_id": uuid, "call_uuid": uuid},
                fields=["name", "task"],
                order_by="creation desc",
            )
            for att in attempts:
                task = frappe.get_doc("AI Task", att.task)
                context = task.context_json or ""
                if suffix in context:
                    return task.name, att.name

            # Check tasks directly by call_uuid matching the trunk
            tasks = frappe.get_all(
                "AI Task",
                filters={"call_uuid": uuid, "trunk_id": ["in", candidate_trunk_ids]},
                fields=["name"],
                order_by="modified desc",
            )
            for t in tasks:
                task = frappe.get_doc("AI Task", t.name)
                context = task.context_json or ""
                if suffix in context:
                    latest_attempts = frappe.get_all(
                        "AI Task Attempt",
                        filters={"task": task.name},
                        order_by="creation desc",
                        limit=1,
                        pluck="name"
                    )
                    attempt_name = latest_attempts[0] if latest_attempts else None
                    return task.name, attempt_name

        # Fallback: Match by Trunk ID + Phone Suffix (e.g. for initial CallInitiated where UUID isn't in DB yet)
        tasks = frappe.get_all(
            "AI Task",
            filters={
                "status": ["in", ["Queued", "Running", "Waiting"]],
                "trunk_id": ["in", candidate_trunk_ids],
            },
            fields=["name"],
            order_by="modified desc",
        )
        for t in tasks:
            task = frappe.get_doc("AI Task", t.name)
            context = task.context_json or ""
            if suffix in context:
                latest_attempts = frappe.get_all(
                    "AI Task Attempt",
                    filters={"task": task.name},
                    order_by="creation desc",
                    limit=1,
                    pluck="name"
                )
                attempt_name = latest_attempts[0] if latest_attempts else None
                return task.name, attempt_name

        repeat_task, repeat_attempt = _find_repeat_followup_task_by_phone_and_trunk(candidate_trunk_ids, suffix)
        if repeat_task:
            return repeat_task, repeat_attempt

    # 5. Fallback for non-Trunk (LiveKit only) callbacks by session ID
    if uuid and not payload_trunk_id:
        filters = {"external_id": uuid}
        attempts = frappe.get_all(
            "AI Task Attempt",
            filters=filters,
            fields=["name", "task"],
            order_by="creation desc",
            limit=1,
        )
        if attempts:
            return attempts[0].task, attempts[0].name

        attempts_json = frappe.db.sql(
            """
            select name, task from `tabAI Task Attempt`
            where response_json like %s or request_json like %s
            order by creation desc limit 1
            """,
            (f"%{uuid}%", f"%{uuid}%"),
            as_dict=True,
        )
        if attempts_json:
            return attempts_json[0].task, attempts_json[0].name

    return None, None


def test_vobiz_callback():
    print("=== STARTING VOBIZ WEBHOOK VERIFICATION ===")

    # 1. Create a dummy channel account
    channel_acct = frappe.new_doc("AI Channel Account")
    channel_acct.account_name = "Test Voice Channel 999"
    channel_acct.channel_type = "LiveKit"
    channel_acct.trunk_id = "test-trunk-999"
    channel_acct.insert(ignore_permissions=True)
    channel_acct_name = channel_acct.name

    # 2. Create a dummy agent linked to the channel account
    agent = frappe.new_doc("AI Agent")
    agent.agent_name = "Test Voice Agent 999"
    agent.allowed_channel_account = channel_acct_name
    agent.system_prompt = "You are a helpful assistant."
    agent.insert(ignore_permissions=True)
    agent_name = agent.name

    # 3. Get or create dummy batch and template
    templates = frappe.get_all("AI Task Template", limit=1)
    if templates:
        template_name = templates[0].name
    else:
        tmpl = frappe.new_doc("AI Task Template")
        tmpl.template_name = "Test Template"
        tmpl.insert(ignore_permissions=True)
        template_name = tmpl.name

    batches = frappe.get_all("AI Task Batch", limit=1)
    if batches:
        batch_name = batches[0].name
    else:
        batch = frappe.new_doc("AI Task Batch")
        batch.batch_name = "Test Batch"
        batch.insert(ignore_permissions=True)
        batch_name = batch.name

    # 4. Create a dummy task
    task = frappe.new_doc("AI Task")
    task.target_agent = agent_name
    task.task_template = template_name
    task.task_batch = batch_name
    task.channel = "Voice"
    task.status = "Queued"
    task.trunk_id = "test-trunk-999"
    task.context_json = json.dumps({"phone": "+919999999999", "patient_name": "John Doe"})
    task.insert(ignore_permissions=True)
    task_name = task.name
    print(f"Created dummy AI Task: {task_name}")

    # 5. Create a dummy task attempt
    attempt = frappe.new_doc("AI Task Attempt")
    attempt.task = task_name
    attempt.status = "Started"
    attempt.trunk_id = "test-trunk-999"
    attempt.insert(ignore_permissions=True)
    attempt_name = attempt.name
    print(f"Created dummy AI Task Attempt: {attempt_name}")

    # Test Trunk ID mismatch case (Negative Match)
    payload_mismatch = {
        "Event": "CallInitiated",
        "CallUUID": "vobiz-uuid-12345",
        "TrunkID": "different-trunk-abc",
        "task": task_name
    }
    print("Testing initiated callback with mismatched TrunkID...")
    mismatch_res = handle_callback(payload_mismatch)
    print(f"Mismatch result: {mismatch_res}")
    assert mismatch_res.get("status") == "error", "Webhook should not match when TrunkID is different"

    # Test 1: Call Initiated Webhook (CallInitiated)
    payload_initiated = {
        "Event": "CallInitiated",
        "CallUUID": "vobiz-uuid-12345",
        "task": task_name,
        "TrunkID": "test-trunk-999",
        "Status": "initiated"
    }
    print("Sending initiated callback...")
    res = handle_callback(payload_initiated)
    print(f"Callback result: {res}")

    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task = frappe.get_doc("AI Task", task_name)
    assert attempt.status == "Started", f"Expected Started, got {attempt.status}"
    assert attempt.external_id == "vobiz-uuid-12345", f"Expected vobiz-uuid-12345, got {attempt.external_id}"
    assert json.loads(task.vobiz_initiated_payload).get("Event") == "CallInitiated", "Task initiated payload mismatch"
    assert json.loads(attempt.vobiz_initiated_payload).get("Event") == "CallInitiated", "Attempt initiated payload mismatch"
    print("✅ Initiated Callback Verified!")

    # Test 2: Call Status Webhook (Hangup / Completed)
    payload_status = {
        "Event": "Hangup",
        "Status": "completed",
        "CallUUID": "vobiz-uuid-12345",
        "TrunkID": "test-trunk-999",
        "Duration": 25.5
    }
    print("Sending status callback (completed)...")
    res = handle_callback(payload_status)
    print(f"Callback result: {res}")

    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task = frappe.get_doc("AI Task", task_name)
    assert attempt.status == "Succeeded", f"Expected Succeeded, got {attempt.status}"
    assert attempt.duration_ms == 25500, f"Expected 25500 ms, got {attempt.duration_ms}"
    assert task.status == "Completed", f"Expected Completed task, got {task.status}"
    assert json.loads(task.vobiz_hangup_payload).get("Event") == "Hangup", "Task hangup payload mismatch"
    assert json.loads(attempt.vobiz_hangup_payload).get("Event") == "Hangup", "Attempt hangup payload mismatch"
    print("✅ Status Callback Verified!")

    # Test 3: Call Transcript Webhook (transcription.completed)
    payload_transcript = {
        "event": "transcription.completed",
        "call_uuid": "vobiz-uuid-12345",
        "trunk_id": "test-trunk-999",
        "transcription_text": "Hello, how are you? I am fine, thank you."
    }
    print("Sending transcript callback...")
    res = handle_callback(payload_transcript)
    print(f"Callback result: {res}")

    task = frappe.get_doc("AI Task", task_name)
    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task_res = json.loads(task.result_json) if task.result_json else {}
    assert task_res.get("transcript") == "Hello, how are you? I am fine, thank you.", "Transcript mismatch in result_json"
    assert task.transcript == "Hello, how are you? I am fine, thank you.", "Transcript field mismatch"
    assert json.loads(task.vobiz_transcript_payload).get("event") == "transcription.completed", "Task transcript payload mismatch"
    assert json.loads(attempt.vobiz_transcript_payload).get("event") == "transcription.completed", "Attempt transcript payload mismatch"
    print("✅ Transcript Callback Verified!")

    # Test 4: Call Recording Webhook (recording.completed)
    payload_recording = {
        "event": "recording.completed",
        "call_uuid": "vobiz-uuid-12345",
        "trunk_id": "test-trunk-999",
        "recording_url": "https://storage.vobiz.ai/recordings/call-12345.mp3"
    }
    print("Sending recording callback...")
    res = handle_callback(payload_recording)
    print(f"Callback result: {res}")

    task = frappe.get_doc("AI Task", task_name)
    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task_res = json.loads(task.result_json) if task.result_json else {}
    assert task_res.get("recording_url") == "https://storage.vobiz.ai/recordings/call-12345.mp3", "Recording URL mismatch in result_json"
    assert task.recording_url == "https://storage.vobiz.ai/recordings/call-12345.mp3", "Recording URL field mismatch"
    assert json.loads(task.vobiz_recording_payload).get("event") == "recording.completed", "Task recording payload mismatch"
    assert json.loads(attempt.vobiz_recording_payload).get("event") == "recording.completed", "Attempt recording payload mismatch"
    print("✅ Recording Callback Verified!")

    # Clean up
    frappe.delete_doc("AI Task Attempt", attempt_name, force=True)
    frappe.delete_doc("AI Task", task_name, force=True)
    frappe.delete_doc("AI Agent", agent_name, force=True)
    frappe.delete_doc("AI Channel Account", channel_acct_name, force=True)
    print("Cleaned up test documents.")
    print("=== ALL TESTS PASSED SUCCESSFULLY ===")
