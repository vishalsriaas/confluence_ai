from __future__ import annotations

import json
import secrets
from datetime import timedelta

import frappe
from livekit import api

from confluence_ai.services import livekit
from confluence_ai.services.shipkia_voice import normalize_phone
from confluence_ai.services.utils import as_json, get_request_json
from confluence_ai.shipkia_setup import (
    SHIPKIA_AGENT,
    SHIPKIA_CHANNEL,
    SHIPKIA_COMPANY,
    SHIPKIA_TEMPLATE_KEY,
)


@frappe.whitelist(methods=["POST"])
def create_local_test_session(
    customer_phone: str | None = None,
    customer_name: str | None = None,
) -> dict:
    """Create an authenticated browser test room for the local ShipKia worker."""
    if not frappe.conf.developer_mode:
        frappe.throw("Local ShipKia voice tests are disabled outside developer mode.")
    frappe.only_for(("System Manager", "Confluence AI Manager"))

    payload = get_request_json()
    customer_phone = customer_phone or payload.get("customer_phone")
    customer_name = customer_name or payload.get("customer_name")
    normalized_phone = normalize_phone(customer_phone)
    if not normalized_phone:
        frappe.throw("customer_phone is required.")

    template = frappe.db.get_value(
        "AI Task Template",
        {"template_key": SHIPKIA_TEMPLATE_KEY, "enabled": 1},
        "name",
    )
    if not template:
        frappe.throw("ShipKia voice configuration is missing. Run configure_shipkia_voice first.")

    session_key = secrets.token_hex(12)
    context = {
        "event": "shipkia.voice.local_test",
        "customer_phone": normalized_phone,
        "phone": normalized_phone,
        "customer_name": customer_name or "",
        "company": SHIPKIA_COMPANY,
        "local_browser_test": 1,
    }

    batch = frappe.new_doc("AI Task Batch")
    batch.update(
        {
            "company": SHIPKIA_COMPANY,
            "status": "Running",
            "source_system": "shipkia-livekit-local",
            "batch_label": f"ShipKia Local Voice {session_key}",
            "idempotency_key": f"shipkia-local-batch-{session_key}",
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
            "idempotency_key": f"shipkia-local-task-{session_key}",
            "context_json": as_json(context),
        }
    )
    task.insert(ignore_permissions=True)

    # Pass an empty execution payload so LiveKit creates a browser room rather
    # than treating the context's customer phone as an outbound SIP request.
    # build_voice_metadata() loads the full context from the saved AI Task.
    dispatch = livekit.start_voice_task(task.name, {})
    room_name = dispatch.get("room_name")
    dispatch_succeeded = bool(
        room_name
        and (
            dispatch.get("dispatch_id")
            or dispatch.get("status") == "dispatched"
        )
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
    frappe.db.commit()
    return {
        "status": "ready",
        "task": task.name,
        "batch": batch.name,
        "room_name": room_name,
        "server_url": account.base_url,
        "participant_identity": participant_identity,
        "participant_token": participant_token,
        "expires_in_seconds": 900,
        "agent_name": dispatch.get("livekit_agent_name"),
        "instructions": "Open LiveKit Meet or Agent Console, choose Custom, and enter server_url and participant_token.",
    }
