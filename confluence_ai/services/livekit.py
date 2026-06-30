from __future__ import annotations

import asyncio
import json
import os
import frappe
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


def start_voice_task(task_name: str, payload: dict) -> dict:
    return asyncio.run(_start_voice_task_async(task_name, payload))


def build_voice_metadata(task_name: str, payload: dict | None = None) -> dict:
    """Build the metadata consumed by the universal LiveKit worker."""
    task = frappe.get_doc("AI Task", task_name)
    payload = payload or parse_json_object(task.context_json, "Task Context JSON") or {}
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None

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

    return {
        "task": task.name,
        "agent": agent_name,
        "system_prompt": system_prompt,
        "personality": personality,
        "context": _voice_metadata_context(payload),
    }


async def _start_voice_task_async(task_name: str, payload: dict) -> dict:
    task = frappe.get_doc("AI Task", task_name)
    agent_name = task.assigned_agent or task.target_agent
    agent = frappe.get_doc("AI Agent", agent_name) if agent_name else None
    account_name = agent.allowed_channel_account if agent else None
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

            sip_req = proto_sip.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=phone,
                room_name=room_name,
                participant_identity=f"sip_{phone.replace('+', '')}",
                participant_metadata=metadata_str
            )
            sip_info = await lkapi.sip.create_sip_participant(sip_req)
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
        return result_payload

    except Exception as exc:
        create_error("LiveKit", str(exc), source="livekit", task=task.name, agent=agent_name, exc=exc)
        raise
    finally:
        await lkapi.aclose()



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
    task_result["last_livekit_payload"] = payload
    if attempt:
        attempt_response["last_livekit_payload"] = payload

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

    elif event_type_lower in {"room_finished", "call_ended", "recording_ready", "transcript_ready", "completed", "failed", "room_failed", "call_failed"}:
        if event_type_lower in {"room_finished", "call_ended", "recording_ready", "transcript_ready", "completed"}:
            task.status = "Completed"
            if attempt:
                attempt.status = "Succeeded"
                from confluence_ai.services.utils import now
                attempt.ended_at = now()
            if event_type_lower in {"room_finished", "call_ended", "completed"}:
                from confluence_ai.services.sales_context import ensure_final_sales_mcp
                ensure_final_sales_mcp(task.name, trigger=f"livekit:{event_type_lower}")
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

    return {
        "status": "success",
        "task": task.name,
        "attempt": attempt.name if attempt else None,
        "processed_event": event_type,
    }


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
