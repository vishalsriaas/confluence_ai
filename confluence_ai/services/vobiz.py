from __future__ import annotations

import json
import frappe
from confluence_ai.services.utils import as_json, now


def download_vobiz_recording(recording_url: str, task) -> str | None:
    if not recording_url:
        return None
    if "storage.vobiz.ai" in recording_url or "test" in recording_url:
        return None

    import requests
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


def handle_callback(payload: dict) -> dict:
    # 1. Match the webhook payload to a task and/or attempt
    task_name, attempt_name = find_task_and_attempt(payload)

    if not task_name:
        frappe.log_error(
            title="Vobiz callback match failed",
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

    elif event_type_lower in {"transcript", "call_transcript", "transcript_ready", "transcription.completed"}:
        task.vobiz_transcript_payload = as_json(payload)
        if attempt:
            attempt.vobiz_transcript_payload = as_json(payload)
        transcript = payload.get("transcript") or payload.get("text") or payload.get("transcript_text") or payload.get("transcription_text")
        if transcript:
            task_result["transcript"] = transcript
            task.transcript = transcript
            if attempt:
                attempt_response["transcript"] = transcript
                attempt.transcript = transcript

    elif event_type_lower in {"recording", "call_recording", "recording_ready", "recording.completed"}:
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

    # 4. Save updates
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


def find_task_and_attempt(payload: dict) -> tuple[str | None, str | None]:
    payload_trunk_id = (payload.get("TrunkID") or payload.get("trunk_id") or "").strip()

    # 1. Match by task ID or room name (mainly for LiveKit events or direct mappings)
    task_name = payload.get("task") or payload.get("task_name") or payload.get("task_id")
    if task_name:
        if payload_trunk_id:
            if frappe.db.exists("AI Task", {"name": task_name, "trunk_id": payload_trunk_id}):
                return task_name, None
        elif frappe.db.exists("AI Task", task_name):
            return task_name, None

    room_name = payload.get("room_name") or payload.get("room")
    if room_name and room_name.startswith("agent-army-"):
        t_name = room_name[len("agent-army-") :]
        if payload_trunk_id:
            if frappe.db.exists("AI Task", {"name": t_name, "trunk_id": payload_trunk_id}):
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

    # 4. Strict match for Vobiz payloads (requiring Trunk ID, UUID/SIPCallID, and Phone Suffix)
    if payload_trunk_id and suffix:
        if uuid:
            # Check attempts by external_id or call_uuid matching the trunk
            attempts = frappe.db.sql(
                """
                select name, task from `tabAI Task Attempt`
                where (external_id = %s or call_uuid = %s) and trunk_id = %s
                order by creation desc
                """,
                (uuid, uuid, payload_trunk_id),
                as_dict=True
            )
            for att in attempts:
                task = frappe.get_doc("AI Task", att.task)
                context = task.context_json or ""
                if suffix in context:
                    return task.name, att.name

            # Check tasks directly by call_uuid matching the trunk
            tasks = frappe.db.sql(
                """
                select name from `tabAI Task`
                where call_uuid = %s and trunk_id = %s
                order by modified desc
                """,
                (uuid, payload_trunk_id),
                as_dict=True
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
        tasks = frappe.db.sql(
            """
            select name from `tabAI Task`
            where status in ('Queued', 'Running', 'Waiting') and trunk_id = %s
            order by modified desc
            """,
            (payload_trunk_id,),
            as_dict=True
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
