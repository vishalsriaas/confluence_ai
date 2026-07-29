from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from importlib.resources import files

import frappe
from livekit import api

from confluence_ai.prompts.shipkia_voice import (
    SHIPKIA_VOICE_PROMPT_VERSION,
    get_shipkia_voice_prompt,
)
from confluence_ai.services import livekit
from confluence_ai.services.shipkia_voice import normalize_phone
from confluence_ai.services.utils import as_json, get_request_json
from confluence_ai.shipkia_setup import (
    SHIPKIA_AGENT,
    SHIPKIA_CHANNEL,
    SHIPKIA_COMPANY,
    SHIPKIA_TEMPLATE_KEY,
)


ALLOWED_VERDICTS = {"Pass", "Fail", "Needs Work"}
_SECRET_PATTERN = re.compile(
    r"(?i)\b(otp|password|passcode|cvv|card pin|payment pin|api[_ -]?key|access[_ -]?token)"
    r"(\s*(?:is|:|=|-)\s*)\S+"
)


@frappe.whitelist(methods=["POST"])
def create_local_test_session(
    customer_phone: str | None = None,
    customer_name: str | None = None,
    test_case_id: str | None = None,
    prompt_version: str = SHIPKIA_VOICE_PROMPT_VERSION,
    sandbox: bool | str | int = True,
    confirm_integration_writes: bool | str | int = False,
) -> dict:
    """Create a manager-only browser Voice Lab room and short-lived memory token."""
    _require_voice_lab_manager()
    payload = get_request_json()
    return _create_voice_test_session(
        customer_phone=customer_phone or payload.get("customer_phone"),
        customer_name=customer_name or payload.get("customer_name"),
        test_case_id=test_case_id or payload.get("test_case_id"),
        prompt_version=payload.get("prompt_version") or prompt_version,
        sandbox=payload.get("sandbox", sandbox),
        confirm_integration_writes=payload.get(
            "confirm_integration_writes",
            confirm_integration_writes,
        ),
    )


def _create_voice_test_session(
    *,
    customer_phone: str | None,
    customer_name: str | None,
    test_case_id: str | None,
    prompt_version: str,
    sandbox: bool | str | int,
    confirm_integration_writes: bool | str | int,
) -> dict:
    normalized_phone = normalize_phone(customer_phone)
    if not normalized_phone:
        frappe.throw("customer_phone is required.")
    sandbox_enabled = _as_bool(sandbox)
    integration_confirmed = _as_bool(confirm_integration_writes)
    if not sandbox_enabled and not integration_confirmed:
        frappe.throw("Integration mode requires explicit Manager confirmation before CRM writes.")

    # Validate the version without writing or touching the active AI Agent prompt.
    get_shipkia_voice_prompt(prompt_version)
    selected_case = _get_test_case(test_case_id) if test_case_id else None

    template = frappe.db.get_value(
        "AI Task Template",
        {"template_key": SHIPKIA_TEMPLATE_KEY, "enabled": 1},
        "name",
    )
    if not template:
        frappe.throw("ShipKia voice configuration is missing. Run configure_shipkia_voice first.")

    session_key = secrets.token_hex(12)
    context = {
        "event": "shipkia.voice.voice_lab",
        "customer_phone": normalized_phone,
        "phone": normalized_phone,
        "customer_name": customer_name or "",
        "company": SHIPKIA_COMPANY,
        "local_browser_test": 1,
        "voice_lab_session": 1,
        "voice_lab_sandbox": 1 if sandbox_enabled else 0,
        "voice_lab_manager_confirmed": 1 if integration_confirmed else 0,
        "test_case_id": (selected_case or {}).get("id") or "",
        "prompt_version": prompt_version,
    }

    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "company": SHIPKIA_COMPANY,
            "status": "Running",
            "source_system": "shipkia-voice-lab",
            "batch_label": f"ShipKia Voice Lab {session_key}",
            "idempotency_key": f"shipkia-voice-lab-batch-{session_key}",
            "task_template": template,
            "target_agent": SHIPKIA_AGENT,
            "priority": "Normal",
            "record_count": 1,
            "running_count": 1,
            "source_payload_json": as_json(context),
        }
    )
    batch.insert(ignore_permissions=True)

    task = frappe.new_doc("AI Task")
    task.update(
        {
            "company": SHIPKIA_COMPANY,
            "status": "Running",
            "task_batch": batch.name,
            "task_template": template,
            "target_agent": SHIPKIA_AGENT,
            "assigned_agent": SHIPKIA_AGENT,
            "channel": "Voice",
            "priority": "Normal",
            "external_record_id": normalized_phone,
            "external_record_type": "CRM Lead Phone",
            "idempotency_key": f"shipkia-voice-lab-task-{session_key}",
            "context_json": as_json(context),
        }
    )
    task.insert(ignore_permissions=True)

    run = frappe.new_doc("AI Voice Test Run")
    run.update(
        {
            "company": SHIPKIA_COMPANY,
            "status": "Created",
            "task": task.name,
            "test_case_id": context["test_case_id"],
            "prompt_version": prompt_version,
            "sandbox": 1 if sandbox_enabled else 0,
            "customer_name": customer_name or "",
            "customer_phone": normalized_phone,
            "verdict": "Pending",
        }
    )
    run.insert(ignore_permissions=True)
    context["voice_test_run"] = run.name
    task.context_json = as_json(context)
    task.save(ignore_permissions=True)

    # An empty execution payload creates a browser room instead of an outbound SIP call.
    # build_voice_metadata() reloads the Voice Lab context from the saved AI Task.
    dispatch = livekit.start_voice_task(task.name, {})
    room_name = dispatch.get("room_name")
    dispatch_succeeded = bool(
        room_name and (dispatch.get("dispatch_id") or dispatch.get("status") == "dispatched")
    )
    if not dispatch_succeeded:
        task.status = "Failed"
        task.last_error = json.dumps(dispatch, default=str)[:1000]
        task.result_json = as_json(dispatch)
        task.save(ignore_permissions=True)
        batch.status = "Failed"
        batch.failed_count = 1
        batch.running_count = 0
        batch.save(ignore_permissions=True)
        run.status = "Failed"
        run.failure_code = "dispatch_failed"
        run.close_reason = task.last_error
        run.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.throw(f"LiveKit dispatch failed: {dispatch}")

    account = frappe.get_doc("AI Channel Account", SHIPKIA_CHANNEL)
    participant_identity = f"shipkia-browser-{session_key}"
    participant_token = (
        api.AccessToken(account.get_password("api_key"), account.get_password("api_secret"))
        .with_identity(participant_identity)
        .with_ttl(timedelta(minutes=15))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )

    task.result_json = as_json(dispatch)
    task.save(ignore_permissions=True)
    run.status = "Ready"
    run.room_name = room_name
    run.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "status": "ready",
        "run_id": run.name,
        "task": task.name,
        "batch": batch.name,
        "test_case_id": context["test_case_id"],
        "prompt_version": prompt_version,
        "sandbox": sandbox_enabled,
        "room_name": room_name,
        "server_url": account.base_url,
        "participant_identity": participant_identity,
        "participant_token": participant_token,
        "expires_in_seconds": 900,
        "agent_name": dispatch.get("livekit_agent_name"),
    }


@frappe.whitelist(methods=["GET"])
def list_voice_test_cases() -> dict:
    _require_voice_lab_manager()
    return {"prompt_version": SHIPKIA_VOICE_PROMPT_VERSION, "cases": _load_test_cases()}


@frappe.whitelist(methods=["GET"])
def get_voice_test_run(run_id: str) -> dict:
    _require_voice_lab_manager()
    run = frappe.get_doc("AI Voice Test Run", run_id)
    data = run.as_dict(no_nulls=True)
    for fieldname in ("metrics_json", "scores_json"):
        raw = data.get(fieldname)
        if raw:
            try:
                data[fieldname.removesuffix("_json")] = json.loads(raw)
            except (TypeError, ValueError):
                data[fieldname.removesuffix("_json")] = {}
    return {"run": data}


@frappe.whitelist(methods=["POST"])
def submit_voice_test_feedback(
    run_id: str | None = None,
    verdict: str | None = None,
    scores=None,
    issue_tags=None,
    notes: str | None = None,
) -> dict:
    _require_voice_lab_manager()
    payload = get_request_json()
    run_id = run_id or payload.get("run_id")
    verdict = verdict or payload.get("verdict")
    if not run_id or verdict not in ALLOWED_VERDICTS:
        frappe.throw("run_id and a verdict of Pass, Fail, or Needs Work are required.")

    parsed_scores = _json_value(scores if scores is not None else payload.get("scores"), {})
    parsed_tags = _json_value(issue_tags if issue_tags is not None else payload.get("issue_tags"), [])
    if not isinstance(parsed_scores, dict) or not isinstance(parsed_tags, list):
        frappe.throw("scores must be an object and issue_tags must be a list.")

    run = frappe.get_doc("AI Voice Test Run", run_id)
    run.verdict = verdict
    run.scores_json = as_json(parsed_scores)
    run.issue_tags = ", ".join(sorted({str(tag).strip() for tag in parsed_tags if str(tag).strip()}))
    run.feedback_notes = _redact(notes if notes is not None else payload.get("notes"))
    run.status = "Passed" if verdict == "Pass" else "Needs Review"
    run.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "saved", "run_id": run.name, "verdict": verdict}


@frappe.whitelist(methods=["POST"])
def restart_voice_test_session(run_id: str | None = None) -> dict:
    _require_voice_lab_manager()
    payload = get_request_json()
    run_id = run_id or payload.get("run_id")
    if not run_id:
        frappe.throw("run_id is required.")
    previous = frappe.get_doc("AI Voice Test Run", run_id)
    previous.status = "Restarted"
    previous.save(ignore_permissions=True)
    return _create_voice_test_session(
        customer_phone=previous.customer_phone,
        customer_name=previous.customer_name,
        test_case_id=previous.test_case_id,
        prompt_version=previous.prompt_version,
        sandbox=bool(previous.sandbox),
        confirm_integration_writes=not bool(previous.sandbox),
    )


def _load_test_cases() -> list[dict]:
    path = files("confluence_ai").joinpath("evals", "shipkia_voice_cases.json")
    if not path.is_file():
        return []
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        frappe.throw("ShipKia voice evaluation data must be a list.")
    return cases


def _get_test_case(test_case_id: str) -> dict:
    for test_case in _load_test_cases():
        if test_case.get("id") == test_case_id:
            return test_case
    frappe.throw(f"Unknown Voice Lab test case: {test_case_id}")


def _require_voice_lab_manager() -> None:
    if not frappe.conf.developer_mode:
        frappe.throw("ShipKia Voice Lab is disabled outside developer mode.")
    frappe.only_for(("System Manager", "Confluence AI Manager"))


def _json_value(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            frappe.throw("Invalid JSON value.")
    return value


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _redact(value) -> str:
    text = " ".join(str(value or "").split())
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
