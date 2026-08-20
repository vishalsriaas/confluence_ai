from __future__ import annotations

import asyncio
import json
import os
import re
import frappe
from google.protobuf import duration_pb2
from livekit import api
from livekit.protocol import room as proto_room
from livekit.protocol import agent_dispatch as proto_dispatch
from livekit.protocol import sip as proto_sip

from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event


import string

class SafeFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "{" + key + "}")
        return super().get_value(key, args, kwargs)


def _livekit_diagnostics_enabled() -> bool:
    try:
        return bool(frappe.db.get_single_value("Confluence AI Settings", "livekit_diagnostics_enabled"))
    except Exception:
        return False


def _livekit_diagnostics_max_events() -> int:
    try:
        value = int(frappe.db.get_single_value("Confluence AI Settings", "livekit_diagnostics_max_events") or 200)
    except Exception:
        value = 200
    return max(20, min(value, 1000))


def _compact_livekit_value(value, max_chars: int = 300):
    if value in (None, ""):
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}..."
    return text


def _compact_livekit_diagnostic_payload(payload: dict) -> dict:
    compact = {"received_at": frappe.utils.now_datetime().isoformat()}
    for key in (
        "event",
        "event_type",
        "status",
        "reason",
        "task",
        "room_name",
        "room",
        "diag_seq",
        "monotonic_ms",
        "metric_type",
        "metric_name",
        "tool_name",
        "trigger",
        "stage",
        "current_stage",
        "duration_ms",
        "exception_type",
        "error",
    ):
        value = _compact_livekit_value(payload.get(key))
        if value is not None:
            compact[key] = value

    transcript = payload.get("transcript") or payload.get("text") or payload.get("transcript_text")
    if transcript:
        transcript_text = str(transcript)
        compact["transcript_chars"] = len(transcript_text)
        compact["transcript_preview"] = _compact_livekit_value(transcript_text, 240)

    if "is_final" in payload:
        compact["is_final"] = bool(payload.get("is_final"))
    if "turn_count" in payload:
        compact["turn_count"] = _compact_livekit_value(payload.get("turn_count"))
    if "details" in payload:
        compact["details"] = _compact_livekit_value(payload.get("details"), 600)

    return compact


def _append_livekit_diagnostic_timeline(current, payload: dict) -> list[dict]:
    if isinstance(current, list):
        timeline = current
    elif isinstance(current, str) and current.strip():
        try:
            parsed = json.loads(current)
        except Exception:
            parsed = parse_json_object(current)
        if isinstance(parsed, list):
            timeline = parsed
        elif isinstance(parsed, dict):
            timeline = [parsed]
        else:
            timeline = []
    elif isinstance(current, dict):
        timeline = [current]
    else:
        timeline = []

    timeline.append(_compact_livekit_diagnostic_payload(payload))
    return timeline[-_livekit_diagnostics_max_events():]


def _voice_metadata_context(payload: dict) -> dict:
    """Return only the context the realtime voice model needs at call start.

    Full task context can be large and may contain backend-only rules or long
    patient notes. Keep LiveKit metadata compact so the agent can greet quickly.
    """
    event_name = str(payload.get("event") or payload.get("event_type") or "").lower()
    is_sales_flow = (
        "sales" in event_name
        or bool(payload.get("sales_brief"))
        or bool(payload.get("selected_sales_route"))
        or payload.get("build_sales_context") in (1, "1", True, "true", "True")
    )
    is_repeat_followup = (
        event_name == "repeat_followup"
        or bool(payload.get("repeat_followup_compacted"))
        or bool(payload.get("full_encounter_available_via_tool"))
    )
    if is_repeat_followup:
        allowed_keys = [
            "event",
            "simple_followup_mode",
            "workflow",
            "scenario_key",
            "company",
            "customer_name",
            "patient_name",
            "customer_phone",
            "phone",
            "patient_encounter",
            "encounter_id",
            "awb_number",
            "order_id",
            "tracking_summary",
            "required_order_script",
            "medicine_summary",
            "required_medicine_script",
            "required_diet_script",
            "simple_followup_script",
            "radha_runtime_version",
            "active_stage_id",
            "active_stage_name",
            "stage_sequence",
            "next_stage_after_order",
            "next_stage_after_medicine",
            "next_stage_after_diet",
            "stage_prompt_loading_required",
            "voice_channel_account",
            "livekit_channel_account_fallback",
            "full_encounter_available_via_tool",
        ]
        compact = {key: payload.get(key) for key in allowed_keys if payload.get(key) not in (None, "", [], {})}
        compact["repeat_followup_compacted"] = 1
        return compact
    if not is_sales_flow:
        return payload

    allowed_keys = [
        "event",
        "customer_name",
        "patient_name",
        "customer_phone",
        "phone",
        "disease_or_concern",
        "product_interest",
        "campaign",
        "customer_type",
        "repeat_customer_details",
        "profile_key",
        "outbound_phone_number",
    ]
    compact = {key: payload.get(key) for key in allowed_keys if payload.get(key) not in (None, "", [], {})}

    sales_brief = str(payload.get("sales_brief") or "")
    if sales_brief:
        # Enough to keep old/new awareness, without overloading the live prompt.
        compact["sales_brief"] = sales_brief[:550]

    patient_summary = str(payload.get("patient_summary") or "")
    if patient_summary:
        compact["patient_summary"] = patient_summary[:180]

    repeat_details = str(payload.get("repeat_customer_details") or "")
    if repeat_details:
        compact["repeat_customer_details"] = repeat_details[:380]

    compact["voice_context_compacted"] = 1
    return compact


def _livekit_dispatch_name(agent, endpoints: dict, payload: dict) -> str:
    """Resolve the LiveKit worker dispatch name for voice calls."""
    return (
        os.getenv("LIVEKIT_AGENT_NAME")
        or endpoints.get("livekit_agent_name")
        or endpoints.get("agent_name")
        or endpoints.get("dispatch_agent_name")
        or payload.get("livekit_agent_name")
        or payload.get("agent_name")
    )


def _voice_environment_metadata(agent) -> dict:
    if not agent or not agent.get("applied_ambient_sound_enabled"):
        return {"ambient_sound_enabled": 0}
    sound = str(agent.get("applied_ambient_sound") or "Quiet office").strip() or "Quiet office"
    try:
        volume = float(agent.get("applied_ambient_sound_volume") or 5)
    except Exception:
        volume = 5.0
    return {
        "ambient_sound_enabled": 1,
        "ambient_sound": sound,
        "ambient_sound_volume": max(0.0, min(volume, 100.0)),
    }


def start_voice_task(task_name: str, payload: dict) -> dict:
    return asyncio.run(_start_voice_task_async(task_name, payload))


def _normalize_phone(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        ten_digit = digits[-10:]
        if len(digits) == 10 or (digits.startswith("0") and len(digits) == 11):
            return f"+91{ten_digit}"
        if digits.startswith("91") and len(digits) == 12:
            return f"+{digits}"
        if text.startswith("+"):
            return f"+{digits}"
        return f"+{digits}"
    return text


def _livekit_transfer_uri(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith(("tel:", "sip:", "sips:")):
        return text
    phone = _normalize_phone(text)
    if not phone:
        return None
    if phone.startswith("+"):
        return f"tel:{phone}"
    return phone


def _sip_call_to_from_transfer_uri(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("tel:"):
        return text[4:]
    return text


def _safe_participant_suffix(value: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "", str(value or ""))
    return suffix[-32:] or "human"


def _livekit_account_for_agent(agent) -> tuple["frappe.Document", str, str, str]:
    account_name = agent.allowed_channel_account if agent else None
    if not account_name:
        frappe.throw("Live transfer requires AI Agent Allowed Channel Account.")

    account = frappe.get_doc("AI Channel Account", account_name)
    url = account.base_url or ""
    if url.startswith("wss://"):
        url = url.replace("wss://", "https://")
    elif url.startswith("ws://"):
        url = url.replace("ws://", "http://")

    api_key = account.get_password("api_key")
    api_secret = account.get_password("api_secret")
    if not (url and api_key and api_secret):
        frappe.throw("LiveKit channel account URL/API credentials are required for live transfer.")
    return account, url, api_key, api_secret


def build_voice_metadata(task_name: str, payload: dict | None = None) -> dict:
    """Build the metadata consumed by the universal LiveKit worker."""
    task = frappe.get_doc("AI Task", task_name)
    payload = payload or parse_json_object(task.context_json, "Task Context JSON") or {}
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    audio_name = (
        (agent.get("audio_name") if agent else None)
        or payload.get("audio_name")
        or payload.get("voice_name")
        or "Puck"
    )

    try:
        system_prompt = agent.get_system_prompt(include_tool_catalog=False) if agent else ""
    except TypeError:
        system_prompt = agent.get_system_prompt() if agent else ""
    personality = agent.personality if agent else ""

    if system_prompt:
        if "{{" in system_prompt:
            try:
                system_prompt = frappe.render_template(system_prompt, payload)
            except Exception:
                pass
        if "{" in system_prompt:
            try:
                system_prompt = SafeFormatter().format(system_prompt, **payload)
            except Exception:
                pass

    if personality:
        if "{{" in personality:
            try:
                personality = frappe.render_template(personality, payload)
            except Exception:
                pass
        if "{" in personality:
            try:
                personality = SafeFormatter().format(personality, **payload)
            except Exception:
                pass

    stage_prompts = []
    if agent and agent.get("agent_type") == "Multi-Stage State Machine":
        for s in agent.get("stage_prompts") or []:
            prompt_text = s.system_prompt or ""
            if prompt_text:
                if "{{" in prompt_text:
                    try:
                        prompt_text = frappe.render_template(prompt_text, payload)
                    except Exception:
                        pass
                if "{" in prompt_text:
                    try:
                        prompt_text = SafeFormatter().format(prompt_text, **payload)
                    except Exception:
                        pass
            stage_prompts.append({
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "system_prompt": prompt_text,
                "is_orchestrator": bool(s.is_orchestrator)
            })

    voice_context = _voice_metadata_context(payload)
    voice_context["livekit_diagnostics_enabled"] = 1 if _livekit_diagnostics_enabled() else 0
    voice_context["livekit_diagnostics_max_events"] = _livekit_diagnostics_max_events()
    voice_context["voice_environment"] = _voice_environment_metadata(agent)

    metadata = {
        "task": task.name,
        "agent": agent_name,
        "audio_name": audio_name,
        "voice_environment": voice_context["voice_environment"],
        "system_prompt": system_prompt,
        "personality": personality,
        "context": voice_context,
    }
    if stage_prompts:
        metadata["stage_prompts"] = stage_prompts
    return metadata


async def _start_voice_task_async(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    account_name = payload.get("voice_channel_account") or (agent.allowed_channel_account if agent else None)
    if not account_name:
        account_name = payload.get("livekit_channel_account_fallback")
    if not account_name:
        return {"status": "skipped", "reason": "no_livekit_account"}

    account = frappe.get_doc("AI Channel Account", account_name)
    endpoints = parse_json_object(account.endpoint_paths_json, "Endpoint Paths JSON") or {}
    operation = "outbound_call" if payload.get("phone") or payload.get("to") else "create_room"

    url = account.base_url or ""
    # Ensure HTTP/HTTPS schemes for REST calls inside LiveKitAPI
    if url.startswith("wss://"):
        url = url.replace("wss://", "https://")
    elif url.startswith("ws://"):
        url = url.replace("ws://", "http://")

    api_key = account.get_password("api_key")
    api_secret = account.get_password("api_secret")

    room_name = f"agent-army-{task.name}"

    metadata = build_voice_metadata(task.name, payload)
    metadata_str = json.dumps(metadata)
    livekit_agent_name = _livekit_dispatch_name(agent, endpoints, payload)

    lkapi = api.LiveKitAPI(url, api_key, api_secret)
    try:
        # 1. Create Room (Always create room first)
        room_req = proto_room.CreateRoomRequest(
            name=room_name,
            metadata=metadata_str,
            empty_timeout=300,
            max_participants=20
        )
        room_info = await lkapi.room.create_room(room_req)

        result_payload = {
            "room_sid": room_info.sid,
            "room_name": room_info.name,
            "metadata": room_info.metadata,
        }

        # 2. If it's a SIP call, place the participant before dispatching the agent.
        if operation == "outbound_call":
            phone = payload.get("phone") or payload.get("to")
            sip_trunk_id = account.trunk_id or endpoints.get("sip_trunk_id")
            if not sip_trunk_id:
                raise ValueError("Missing SIP trunk ID. Configure AI Channel Account.trunk_id or endpoint_paths_json.sip_trunk_id.")
            ringing_timeout_seconds = int(endpoints.get("ringing_timeout_seconds") or 45)
            sip_api_timeout_seconds = int(endpoints.get("sip_api_timeout_seconds") or 60)

            record_provider_event(
                provider=account.provider_type or "LiveKit",
                operation="outbound_call_requested",
                status="Succeeded",
                agent=agent_name,
                task=task.name,
                request=payload,
                response={
                    "room_name": room_name,
                    "sip_trunk_id": sip_trunk_id,
                    "livekit_agent_name": livekit_agent_name,
                    "timeout_seconds": sip_api_timeout_seconds,
                },
            )

            sip_req = proto_sip.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=phone,
                room_name=room_name,
                participant_identity=f"sip_{phone.replace('+', '')}",
                participant_metadata=metadata_str,
                wait_until_answered=False,
                ringing_timeout=duration_pb2.Duration(seconds=ringing_timeout_seconds),
            )
            sip_info = await asyncio.wait_for(
                lkapi.sip.create_sip_participant(sip_req),
                timeout=sip_api_timeout_seconds,
            )
            result_payload["sip_call_sid"] = sip_info.sip_call_id
            result_payload["participant_identity"] = sip_info.participant_identity

        # 3. Dispatch the LiveKit voice agent for both outbound SIP and room-only calls.
        dispatch_req = proto_dispatch.CreateAgentDispatchRequest(
            agent_name=livekit_agent_name,
            room=room_name,
            metadata=metadata_str
        )
        dispatch_info = await lkapi.agent_dispatch.create_dispatch(dispatch_req)
        result_payload["dispatch_id"] = dispatch_info.id
        result_payload["livekit_agent_name"] = livekit_agent_name

        record_provider_event(
            provider=account.provider_type or "LiveKit",
            operation=operation,
            status="Succeeded",
            agent=agent_name,
            task=task.name,
            request=payload,
            response=result_payload,
        )
        if task.channel == "Voice" and task.external_record_type == "AI Repeat Follow Up Workflow":
            try:
                from confluence_ai.services import repeat_followup

                repeat_followup.mark_voice_started(task.name, result_payload)
            except Exception as exc:
                create_error("Repeat Follow Up Voice Start", str(exc), source="livekit", task=task.name, agent=agent_name, exc=exc)
        return result_payload

    except Exception as exc:
        create_error("LiveKit", str(exc), source="livekit", task=task.name, agent=agent_name, exc=exc)
        raise
    finally:
        await lkapi.aclose()


def transfer_live_call(arguments: dict, *, task_id: str | None, agent: str | None = None) -> dict:
    """Bridge the configured human number into the active LiveKit room."""
    if not task_id or not frappe.db.exists("AI Task", task_id):
        frappe.throw("Active AI Task is required for live transfer.")

    consent = arguments.get("consent_confirmed")
    if str(consent).strip().lower() not in {"1", "true", "yes", "y"}:
        frappe.throw("Customer consent is required before live transfer.")

    task = frappe.get_doc("AI Task", task_id)
    agent_name = agent or task.assigned_agent or task.target_agent
    if not agent_name or not frappe.db.exists("AI Agent", agent_name):
        frappe.throw("AI Agent is required for live transfer.")

    agent_doc = frappe.get_doc("AI Agent", agent_name)
    if not agent_doc.get("enable_live_transfer"):
        frappe.throw("Live transfer is not enabled for this AI Agent.")

    transfer_to = _livekit_transfer_uri(agent_doc.get("human_agent_number"))
    if not transfer_to:
        frappe.throw("Human Agent Number is required for live transfer.")

    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        frappe.throw("Transfer reason is required.")

    return asyncio.run(_transfer_live_call_async(task, agent_doc, transfer_to, reason))


async def _transfer_live_call_async(task, agent_doc, transfer_to: str, reason: str) -> dict:
    account, url, api_key, api_secret = _livekit_account_for_agent(agent_doc)
    context = parse_json_object(task.context_json, "Task Context JSON") or {}
    result_json = parse_json_object(task.result_json, "Task Result JSON") or {}
    last_livekit_payload = result_json.get("last_livekit_payload") if isinstance(result_json.get("last_livekit_payload"), dict) else {}
    room_name = (
        last_livekit_payload.get("room_name")
        or last_livekit_payload.get("room")
        or context.get("room_name")
        or context.get("room")
    )
    if not room_name:
        frappe.throw("Active LiveKit room was not found for this task.")

    endpoints = parse_json_object(account.endpoint_paths_json, "Endpoint Paths JSON") or {}
    sip_trunk_id = (
        endpoints.get("outbound_sip_trunk_id")
        or endpoints.get("sip_trunk_id")
        or account.trunk_id
    )
    if not sip_trunk_id:
        frappe.throw("Missing outbound SIP trunk ID for live human merge.")

    lkapi = api.LiveKitAPI(url, api_key, api_secret)
    try:
        timeout_seconds = int(agent_doc.get("transfer_ring_timeout") or 20)
        sip_call_to = _sip_call_to_from_transfer_uri(transfer_to)
        human_identity = f"human_{_safe_participant_suffix(sip_call_to)}"
        request = proto_sip.CreateSIPParticipantRequest(
            sip_trunk_id=sip_trunk_id,
            sip_call_to=sip_call_to,
            room_name=room_name,
            participant_identity=human_identity,
            participant_name="Human Agent",
            participant_metadata=as_json(
                {
                    "role": "human_agent",
                    "reason": reason,
                    "source": "transfer_live_call",
                }
            ),
            play_ringtone=True,
            wait_until_answered=True,
        )
        if timeout_seconds > 0:
            request.ringing_timeout.FromSeconds(timeout_seconds)

        info = await lkapi.sip.create_sip_participant(request)
        response = {
            "status": "success",
            "mode": "merged_human_into_room",
            "room_name": room_name,
            "human_participant_identity": human_identity,
            "transfer_to": transfer_to,
            "sip_call_to": sip_call_to,
            "sip_trunk_id": sip_trunk_id,
            "reason": reason,
            "sip_call_id": getattr(info, "sip_call_id", None),
            "instruction": "Human agent is connected in the same room. Voice agent should stay silent unless directly asked.",
        }
        record_provider_event(
            provider="LiveKit",
            operation="transfer_live_call",
            status="Succeeded",
            agent=agent_doc.name,
            task=task.name,
            request={"reason": reason, "room_name": room_name, "human_participant_identity": human_identity},
            response=response,
        )
        return response
    except Exception as exc:
        create_error("LiveKit Transfer", str(exc), source="livekit", task=task.name, agent=agent_doc.name, exc=exc)
        raise
    finally:
        await lkapi.aclose()


async def _find_sip_participant_identity(lkapi: api.LiveKitAPI, room_name: str) -> str | None:
    response = await lkapi.room.list_participants(proto_room.ListParticipantsRequest(room=room_name))
    for participant in response.participants:
        identity = getattr(participant, "identity", "") or ""
        if identity.startswith("sip_"):
            return identity
    for participant in response.participants:
        identity = getattr(participant, "identity", "") or ""
        if identity:
            return identity
    return None



def handle_callback(payload: dict) -> dict:
    # 1. Match the webhook payload to a task and/or attempt
    from confluence_ai.services.vobiz import find_task_and_attempt
    task_name, attempt_name = find_task_and_attempt(payload)

    if not task_name:
        frappe.log_error(
            title="LiveKit callback match failed",
            message=f"Could not find matching AI Task or AI Task Attempt for payload: {json.dumps(payload, default=str)}",
        )
        return {"status": "error", "message": "No matching task or attempt found"}

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
    event_type = payload.get("event") or payload.get("event_type") or "status_update"
    event_type_lower = event_type.lower()

    # Load/initialize JSON payload trackers
    task_result = json.loads(task.result_json) if task.result_json else {}
    attempt_response = json.loads(attempt.response_json) if (attempt and attempt.response_json) else {}

    if not isinstance(task_result, dict):
        task_result = {"raw_result": task_result}
    if not isinstance(attempt_response, dict):
        attempt_response = {"raw_response": attempt_response}

    # Save the raw payload details
    diagnostics_enabled = _livekit_diagnostics_enabled()
    task_result["last_livekit_payload"] = payload
    if attempt:
        attempt_response["last_livekit_payload"] = payload

    if diagnostics_enabled:
        task_result["livekit_diagnostic_timeline"] = _append_livekit_diagnostic_timeline(
            task_result.get("livekit_diagnostic_timeline"),
            payload,
        )
        if attempt:
            attempt_response["livekit_diagnostic_timeline"] = _append_livekit_diagnostic_timeline(
                attempt_response.get("livekit_diagnostic_timeline"),
                payload,
            )

    _upsert_livekit_call_log(payload, task, attempt, diagnostics_enabled=diagnostics_enabled)

    # Update statuses
    if event_type_lower in {"room_started", "participant_joined", "initiated"}:
        task.status = "Running"
        if attempt:
            attempt.status = "Started"
            call_uuid = payload.get("call_uuid") or payload.get("call_sid") or payload.get("room_sid") or payload.get("sip_call_sid") or payload.get("CallUUID")
            if call_uuid:
                attempt.external_id = call_uuid
                attempt.call_uuid = call_uuid
                task.call_uuid = call_uuid
            from confluence_ai.services.utils import now
            attempt_response["initiated_at"] = now()

    elif event_type_lower in {"room_finished", "call_ended", "participant_left", "recording_ready", "transcript_ready", "completed", "failed", "room_failed", "call_failed"}:
        if event_type_lower in {"room_finished", "call_ended", "participant_left", "recording_ready", "transcript_ready", "completed"}:
            task.status = "Completed"
            if attempt:
                attempt.status = "Succeeded"
                from confluence_ai.services.utils import now
                attempt.ended_at = now()
        else:
            task.status = "Failed"
            task.last_error = payload.get("error") or payload.get("error_message") or event_type
            if attempt:
                attempt.status = "Failed"
                attempt.error_message = task.last_error
                from confluence_ai.services.utils import now
                attempt.ended_at = now()

        # Update duration if available
        duration = payload.get("duration") or payload.get("duration_ms") or payload.get("Duration")
        if duration is not None:
            try:
                val = float(duration)
                if "ms" in str(duration).lower() or val > 5000:
                    duration_ms = int(val)
                    duration_sec = int(val / 1000)
                else:
                    duration_sec = int(val)
                    duration_ms = int(val * 1000)
                
                if attempt:
                    attempt.duration_ms = duration_ms
                    attempt.duration = duration_sec
                task_result["duration_ms"] = duration_ms
                task.duration = duration_sec
            except (ValueError, TypeError):
                pass

        # Update transcript if available
        transcript = payload.get("transcript") or payload.get("text") or payload.get("transcript_text")
        if transcript:
            task_result["transcript"] = transcript
            task.transcript = transcript
            if attempt:
                attempt_response["transcript"] = transcript
                attempt.transcript = transcript

        # Update recording_url if available
        recording_url = payload.get("recording_url") or payload.get("url") or payload.get("recording")
        if recording_url:
            task_result["recording_url"] = recording_url
            task.recording_url = recording_url
            if attempt:
                attempt_response["recording_url"] = recording_url
                attempt.recording_url = recording_url

    # Always copy telephony status and Call UUID if present
    telephony_status = payload.get("status") or payload.get("telephony_status") or event_type
    if telephony_status:
        task.telephony_status = telephony_status
        if attempt:
            attempt.telephony_status = telephony_status

    call_uuid = payload.get("call_uuid") or payload.get("call_sid") or payload.get("room_sid") or payload.get("sip_call_sid") or payload.get("CallUUID")
    if call_uuid:
        task.call_uuid = call_uuid
        if attempt:
            attempt.call_uuid = call_uuid

    # Save updates
    task.result_json = as_json(task_result)
    task.save(ignore_permissions=True)

    if attempt:
        attempt.response_json = as_json(attempt_response)
        attempt.save(ignore_permissions=True)

    frappe.db.commit()
    order_confirmation_result = _handle_order_confirmation_callback(task, payload, event_type_lower)
    repeat_followup_result = _handle_repeat_followup_callback(task, payload, event_type_lower)
    fresh_followup_result = _handle_fresh_followup_callback(task, payload, event_type_lower)

    return {
        "status": "success",
        "task": task.name,
        "attempt": attempt.name if attempt else None,
        "processed_event": event_type,
        "order_confirmation": order_confirmation_result,
        "repeat_followup": repeat_followup_result,
        "fresh_followup": fresh_followup_result,
    }


def _handle_order_confirmation_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice" or task.external_record_type != "Order Confirmation Workflow" or not task.external_record_id:
        return None
    if event_type_lower not in {"room_finished", "call_ended", "participant_left", "transcript_ready", "completed", "failed", "room_failed", "call_failed"}:
        return None
    try:
        from confluence_ai.services import order_confirmation

        if not frappe.db.exists("Order Confirmation Workflow", task.external_record_id):
            return {"status": "ignored", "reason": "missing_workflow"}
        workflow = frappe.get_doc("Order Confirmation Workflow", task.external_record_id)
        if workflow.status in order_confirmation.FINAL_STATES:
            return {"status": "ignored", "reason": "final_state", "workflow": workflow.name}

        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcript_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or ""
        )
        outcome = payload.get("order_confirmation_outcome") or payload.get("outcome")
        if event_type_lower in {"failed", "room_failed", "call_failed"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("error") or payload.get("error_message") or "Voice call failed before confirmation."
        elif not notes and event_type_lower in {"room_finished", "call_ended", "participant_left", "completed"}:
            return order_confirmation.wait_for_voice_transcript(
                workflow.name,
                "Voice call ended; waiting for transcript before deciding confirmation.",
            )

        return order_confirmation.handle_voice_result(
            workflow=workflow.name,
            task=task.name,
            outcome=outcome,
            notes=notes,
        )
    except Exception as exc:
        create_error("Order Confirmation Voice Callback", str(exc), source="livekit", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _handle_repeat_followup_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice" or task.external_record_type != "AI Repeat Follow Up Workflow" or not task.external_record_id:
        return None
    if event_type_lower not in {"room_finished", "call_ended", "participant_left", "transcript_ready", "completed", "failed", "room_failed", "call_failed"}:
        return None
    try:
        from confluence_ai.services import repeat_followup

        if not frappe.db.exists("AI Repeat Follow Up Workflow", task.external_record_id):
            return {"status": "ignored", "reason": "missing_workflow"}

        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcript_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or ""
        )
        outcome = payload.get("repeat_followup_outcome") or payload.get("outcome")
        if event_type_lower in {"failed", "room_failed", "call_failed"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("error") or payload.get("error_message") or "Repeat follow-up voice call failed."

        return repeat_followup.handle_voice_result(
            workflow=task.external_record_id,
            task=task.name,
            outcome=outcome,
            notes=notes,
        )
    except Exception as exc:
        create_error("Repeat Follow Up Voice Callback", str(exc), source="livekit", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _handle_fresh_followup_callback(task, payload: dict, event_type_lower: str) -> dict | None:
    if task.channel != "Voice":
        return None
    if event_type_lower not in {"room_finished", "call_ended", "participant_left", "transcript_ready", "completed", "failed", "room_failed", "call_failed"}:
        return None
    workflow_name = _fresh_followup_workflow_for_task(task)
    if not workflow_name:
        return None
    try:
        from confluence_ai.services import fresh_followup

        notes = (
            payload.get("outcome")
            or payload.get("notes")
            or payload.get("summary")
            or payload.get("transcript_summary")
            or payload.get("transcript")
            or payload.get("text")
            or payload.get("transcript_text")
            or ""
        )
        outcome = payload.get("fresh_followup_outcome") or payload.get("outcome")
        if event_type_lower in {"failed", "room_failed", "call_failed"}:
            outcome = outcome or "missed"
            notes = notes or payload.get("error") or payload.get("error_message") or "Fresh follow-up voice call failed."
        elif not notes and event_type_lower in {"room_finished", "call_ended", "participant_left", "completed"}:
            return fresh_followup.wait_for_voice_transcript(
                workflow_name,
                "Voice call ended; waiting for transcript before scheduling the next fresh follow-up agent.",
            )

        return fresh_followup.handle_voice_result(
            workflow=workflow_name,
            task=task.name,
            outcome=outcome,
            notes=notes,
            result=payload,
        )
    except Exception as exc:
        create_error("Fresh Follow Up Voice Callback", str(exc), source="livekit", task=task.name, exc=exc)
        return {"status": "failed", "error": str(exc)}


def _fresh_followup_workflow_for_task(task) -> str | None:
    if task.external_record_type == "AI Fresh Follow Up Workflow" and task.external_record_id:
        return task.external_record_id
    return frappe.db.get_value("AI Fresh Follow Up Workflow Agent", {"task": task.name}, "parent")


def _upsert_livekit_call_log(payload: dict, task, attempt=None, diagnostics_enabled: bool = False) -> None:
    """Create/update the human-facing call log from LiveKit callbacks."""
    if not frappe.db.exists("DocType", "AI Call Log"):
        return

    try:
        from confluence_ai.services.utils import now

        context = parse_json_object(task.context_json)
        livekit_event = payload.get("event") or payload.get("event_type") or payload.get("status") or "status_update"
        event_type_lower = str(livekit_event or "").lower()
        call_uuid = (
            payload.get("call_uuid")
            or payload.get("CallUUID")
            or task.call_uuid
            or task.external_record_id
            or payload.get("room_name")
            or payload.get("room")
        )

        existing = None
        if task.name:
            existing = frappe.db.exists("AI Call Log", {"task": task.name})
        for fieldname, value in (
            ("call_uuid", call_uuid),
            ("sip_call_id", payload.get("sip_call_id") or payload.get("room_name") or payload.get("room")),
        ):
            if not existing and value:
                existing = frappe.db.exists("AI Call Log", {fieldname: value})

        doc = frappe.get_doc("AI Call Log", existing) if existing else frappe.new_doc("AI Call Log")
        doc.provider = "LiveKit"
        doc.event_type = livekit_event
        doc.direction = context.get("direction") or "Inbound"
        doc.agent = task.assigned_agent or task.target_agent or doc.agent
        doc.task = task.name
        doc.company = task.company or doc.company
        if not doc.company and doc.agent:
            doc.company = frappe.db.get_value("AI Agent", doc.agent, "company") or doc.company
        if attempt:
            doc.attempt = attempt.name
            doc.company = doc.company or attempt.company
        doc.customer_name = context.get("customer_name") or context.get("patient_name") or doc.customer_name
        doc.customer_phone = (
            payload.get("caller_phone")
            or payload.get("from")
            or context.get("customer_phone")
            or context.get("phone")
            or doc.customer_phone
        )
        doc.from_number = payload.get("from") or payload.get("caller_phone") or context.get("customer_phone") or context.get("phone") or doc.from_number
        doc.to_number = (
            payload.get("to")
            or payload.get("called_number")
            or context.get("called_number")
            or context.get("inbound_phone_number")
            or context.get("outbound_phone_number")
            or doc.to_number
        )
        doc.call_uuid = doc.call_uuid or call_uuid
        doc.sip_call_id = payload.get("sip_call_id") or payload.get("room_name") or payload.get("room") or doc.sip_call_id
        doc.trunk_id = payload.get("trunk_id") or context.get("trunk_id") or task.trunk_id or doc.trunk_id
        doc.domain = payload.get("domain") or context.get("vobiz_domain") or doc.domain
        doc.reason = payload.get("reason") or doc.reason
        doc.last_payload_json = as_json(payload)
        if diagnostics_enabled and doc.meta.has_field("livekit_diagnostic_timeline_json"):
            doc.livekit_diagnostic_timeline_json = as_json(
                _append_livekit_diagnostic_timeline(
                    doc.livekit_diagnostic_timeline_json,
                    payload,
                )
            )

        status = str(payload.get("status") or livekit_event or "").lower()
        if status in {"completed", "call_ended", "participant_left", "room_finished", "recording_ready", "transcript_ready"}:
            doc.status = "Completed"
        elif status in {"failed", "room_failed", "call_failed"}:
            doc.status = "Failed"
        elif status in {"running", "room_started", "participant_joined", "initiated"}:
            doc.status = "In Progress"
        elif not doc.status:
            doc.status = "Unknown"

        if event_type_lower in {"room_started", "participant_joined", "initiated"}:
            doc.initiated_payload_json = as_json(payload)
            doc.started_at = payload.get("started_at") or doc.started_at or now()
        elif event_type_lower in {"room_finished", "call_ended", "participant_left", "completed", "failed", "room_failed", "call_failed"}:
            doc.status_payload_json = as_json(payload)
            doc.started_at = payload.get("started_at") or doc.started_at
            doc.ended_at = payload.get("ended_at") or doc.ended_at or now()

        duration = payload.get("duration_sec") or payload.get("duration") or payload.get("duration_ms")
        if duration is not None:
            try:
                value = float(duration)
                doc.duration_sec = int(value / 1000) if value > 5000 else int(value)
            except (TypeError, ValueError):
                pass

        transcript = payload.get("transcript") or payload.get("text") or payload.get("transcript_text")
        if transcript:
            doc.transcript = transcript
            doc.transcript_summary = payload.get("summary") or payload.get("transcript_summary") or str(transcript)[:1000]
            doc.transcript_payload_json = as_json(payload)

        recording_url = payload.get("recording_url") or payload.get("url") or payload.get("recording")
        if recording_url:
            doc.recording_url = recording_url
            doc.external_recording_url = recording_url
            doc.recording_payload_json = as_json(payload)

        doc.save(ignore_permissions=True)
    except Exception as exc:
        create_error("LiveKit Call Log", str(exc), source="livekit", task=task.name, exc=exc)


def test_livekit_callback():
    print("=== STARTING LIVEKIT WEBHOOK VERIFICATION ===")

    # 1. Get or create a dummy agent
    agents = frappe.get_all("AI Agent", limit=1)
    if agents:
        agent_name = agents[0].name
    else:
        agent = frappe.new_doc("AI Agent")
        agent.agent_name = "Test Agent"
        agent.channel_type = "Voice"
        agent.system_prompt = "You are a helpful assistant."
        agent.insert(ignore_permissions=True)
        agent_name = agent.name

    # 2. Get or create dummy batch and template
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

    # 3. Create a dummy task
    task = frappe.new_doc("AI Task")
    task.target_agent = agent_name
    task.task_template = template_name
    task.task_batch = batch_name
    task.channel = "Voice"
    task.status = "Queued"
    task.context_json = json.dumps({"phone": "+919999999999", "patient_name": "John Doe"})
    task.insert(ignore_permissions=True)
    task_name = task.name
    print(f"Created dummy AI Task: {task_name}")

    # 3. Create a dummy task attempt
    attempt = frappe.new_doc("AI Task Attempt")
    attempt.task = task_name
    attempt.status = "Started"
    attempt.insert(ignore_permissions=True)
    attempt_name = attempt.name
    print(f"Created dummy AI Task Attempt: {attempt_name}")

    # Test 1: Call Initiated Webhook
    payload_initiated = {
        "event": "initiated",
        "call_sid": "livekit-sid-12345",
        "task": task_name
    }
    print("Sending initiated callback...")
    res = handle_callback(payload_initiated)
    print(f"Callback result: {res}")

    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task = frappe.get_doc("AI Task", task_name)
    assert attempt.status == "Started", f"Expected Started, got {attempt.status}"
    assert attempt.external_id == "livekit-sid-12345", f"Expected livekit-sid-12345, got {attempt.external_id}"
    print("✅ Initiated Callback Verified!")

    # Test 2: Call Status Webhook (Completed)
    payload_status = {
        "event": "call_ended",
        "status": "completed",
        "call_sid": "livekit-sid-12345",
        "duration": 42.0
    }
    print("Sending status callback (completed)...")
    res = handle_callback(payload_status)
    print(f"Callback result: {res}")

    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task = frappe.get_doc("AI Task", task_name)
    assert attempt.status == "Succeeded", f"Expected Succeeded, got {attempt.status}"
    assert attempt.duration_ms == 42000, f"Expected 42000 ms, got {attempt.duration_ms}"
    assert task.status == "Completed", f"Expected Completed task, got {task.status}"
    print("✅ Status Callback Verified!")

    # Test 3: Call Transcript Webhook
    payload_transcript = {
        "event": "transcript_ready",
        "call_sid": "livekit-sid-12345",
        "transcript": "Hello, how are you? I am livekit agent."
    }
    print("Sending transcript callback...")
    res = handle_callback(payload_transcript)
    print(f"Callback result: {res}")

    task = frappe.get_doc("AI Task", task_name)
    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task_res = json.loads(task.result_json) if task.result_json else {}
    assert task_res.get("transcript") == "Hello, how are you? I am livekit agent.", "Transcript mismatch"
    print("✅ Transcript Callback Verified!")

    # Test 4: Call Recording Webhook
    payload_recording = {
        "event": "recording_ready",
        "call_sid": "livekit-sid-12345",
        "recording_url": "https://storage.livekit.ai/recordings/call-12345.mp4"
    }
    print("Sending recording callback...")
    res = handle_callback(payload_recording)
    print(f"Callback result: {res}")

    task = frappe.get_doc("AI Task", task_name)
    attempt = frappe.get_doc("AI Task Attempt", attempt_name)
    task_res = json.loads(task.result_json) if task.result_json else {}
    assert task_res.get("recording_url") == "https://storage.livekit.ai/recordings/call-12345.mp4", "Recording URL mismatch"
    print("✅ Recording Callback Verified!")

    # Clean up
    frappe.delete_doc("AI Task Attempt", attempt_name, force=True)
    frappe.delete_doc("AI Task", task_name, force=True)
    print("Cleaned up test documents.")
    print("=== ALL TESTS PASSED SUCCESSFULLY ===")
