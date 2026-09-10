from __future__ import annotations

import frappe
import hashlib
import json

from confluence_ai.services import whatsapp_bridge, vobiz
from confluence_ai.services import event_router
from confluence_ai.services import order_confirmation
from confluence_ai.services.auth import require_access
from confluence_ai.services.utils import as_json, get_request_json


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_vobiz() -> dict:
    payload = get_request_json()
    return _process_telephony_receipt("vobiz", payload, vobiz.handle_callback)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_whatsapp() -> dict:
    require_access("webhook")
    payload = get_request_json()
    event = _record_inbound("whatsapp", payload)
    result = whatsapp_bridge.handle_callback(payload)
    frappe.db.set_value("AI Webhook Event", event, {"status": "Processed", "response_json": as_json(result)})
    return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_livekit() -> dict:
    return {"status": "disabled", "reason": "voice_flow_is_vobiz_only"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_event(source_system: str | None = None) -> dict:
    """
    Universal inbound webhook endpoint for external systems (ERP, CRM, etc.).

    Routing is driven entirely by AI Event Route records in the UI — no code
    changes needed to support new event types.

    Optional query param:
        ?source_system=ERPNext   — used to disambiguate same event from multiple sources
    """
    require_access("webhook")
    payload = get_request_json()

    if _is_vobiz_payload(payload):
        return _process_telephony_receipt("vobiz", payload, vobiz.handle_callback)

    webhook_event = _record_inbound(source_system or "external", payload)

    if _is_order_confirmation_payload(payload):
        try:
            result = order_confirmation.start_from_event(payload)
            frappe.db.set_value(
                "AI Webhook Event",
                webhook_event,
                {"status": "Processed", "response_json": as_json(result)},
            )
            return result
        except Exception as exc:
            frappe.db.set_value(
                "AI Webhook Event",
                webhook_event,
                {"status": "Failed", "response_json": as_json({"error": str(exc)})},
            )
            raise

    # Find matching route
    route = event_router.find_matching_route(payload, source_system=source_system)

    if not route:
        event_key = payload.get("event") or payload.get("event_type") or "unknown"
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": "no_matching_route", "event": event_key})},
        )
        frappe.log_error(
            title="AI Event Route: no matching route",
            message=f"No enabled AI Event Route found for payload: {payload}",
        )
        return {"status": "no_route", "event": event_key, "message": "No matching AI Event Route configured for this event."}

    # Per-route optional auth: validate X-Webhook-Secret header if secret is configured
    if not event_router.validate_route_auth(route, frappe.request.headers):
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": "invalid_webhook_secret"})},
        )
        frappe.throw("Invalid or missing X-Webhook-Secret header", frappe.AuthenticationError)

    try:
        result = event_router.dispatch_from_route(route, payload)
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Processed", "response_json": as_json(result)},
        )
        return result
    except Exception as exc:
        frappe.db.set_value(
            "AI Webhook Event",
            webhook_event,
            {"status": "Failed", "response_json": as_json({"error": str(exc)})},
        )
        raise


def _is_vobiz_payload(payload: dict) -> bool:
    event_type = str(payload.get("event") or payload.get("Event") or payload.get("event_type") or "").lower()
    if event_type in {"hangup", "callinitiated", "recording.completed", "transcription.completed"}:
        return True
    return bool(
        payload.get("CallUUID")
        or payload.get("SIPCallID")
        or payload.get("recording_id")
        or payload.get("transcription_id")
        or payload.get("account_id")
        or payload.get("AccountId")
    )


def _is_order_confirmation_payload(payload: dict) -> bool:
    event_type = str(payload.get("event") or payload.get("event_type") or "").strip().lower()
    return event_type.endswith("order-confirmation") or event_type == "order_confirmation"


def _process_telephony_receipt(source: str, payload: dict, handler) -> dict:
    event_key = hashlib.sha256(json.dumps([source, payload], sort_keys=True, default=str).encode()).hexdigest()
    with frappe.cache.lock("call-receipt:" + event_key, timeout=180, blocking_timeout=10):
        event = frappe.db.get_value("AI Webhook Event", {"event_key": event_key}, "name")
        if not event:
            event = _record_inbound(source, payload)
            frappe.db.set_value("AI Webhook Event", event, "event_key", event_key)
            frappe.db.commit()
        elif frappe.db.get_value("AI Webhook Event", event, "status") == "Processed":
            return {"status": "duplicate", "webhook_event": event}
        try:
            from confluence_ai.services.call_registry import aliases, company_for, identity_key
            task_name = payload.get("task") or payload.get("task_name")
            task = frappe.get_doc("AI Task", task_name) if task_name and frappe.db.exists("AI Task", task_name) else None
            company = company_for(payload, task)
            keys = [identity_key(company, kind, value) for kind, value in aliases(payload)] if company else []
            if frappe.db.get_value("AI Webhook Event", event, "status") == "Pending Matching":
                from confluence_ai.services.call_registry import find_call
                if not find_call(payload, company):
                    # Keep one durable receipt until an exact identity becomes available.
                    # Repeated recovery scans must not rerun the unmatched handler.
                    return {"status": "pending_matching", "webhook_event": event,
                            "call_log": None, "reason": "exact_call_identity_required"}
            frappe.db.set_value("AI Webhook Event", event, {
                "event_key": event_key, "company": company, "identity_keys_json": as_json(keys),
            })
            frappe.db.commit()
            received = frappe.db.get_value("AI Webhook Event", event, "creation")
            result = handler({**payload, "_received_at": str(received)})
            if result.get("status") == "pending_matching":
                from confluence_ai.services.call_registry import record_receipt_state
                record_receipt_state(payload, company, "Pending Matching")
            _mark_webhook_processed(event, result)
            frappe.db.commit()
        except Exception as exc:
            frappe.db.rollback()
            frappe.db.set_value("AI Webhook Event", event, {
                "status": "Failed", "error_message": str(exc)[:500],
                "response_json": as_json({"error": str(exc)}),
            })
            try:
                from confluence_ai.services.call_registry import record_receipt_state
                record_receipt_state(payload, frappe.db.get_value("AI Webhook Event", event, "company"), "Failed")
            except Exception:
                frappe.db.rollback()
                frappe.db.set_value("AI Webhook Event", event, {
                    "status": "Failed", "error_message": str(exc)[:500],
                    "response_json": as_json({"error": str(exc)}),
                })
            from confluence_ai.services.utils import create_error
            create_error("Call Webhook Processing", str(exc), source=source,
                task=payload.get("task"), payload={"webhook_event": event}, exc=exc)
            frappe.db.commit()
            raise
    if result.get("call_log") and not getattr(frappe.flags, "replaying_call_receipts", False):
        replay_pending_receipts(result["call_log"])
    elif result.get("status") == "pending_matching" and not getattr(frappe.flags, "retrying_call_identity", False):
        # The identity transaction may have committed while this receipt was waiting.
        from confluence_ai.services.call_registry import find_call
        matched = find_call(payload, company)
        if matched and frappe.db.get_value("AI Call Log", matched, "attempt"):
            frappe.flags.retrying_call_identity = True
            try:
                return _process_telephony_receipt(source, payload, handler)
            finally:
                frappe.flags.retrying_call_identity = False
    return result


def replay_pending_receipts(call_log):
    """Triggered by an identity-bearing event; no additional polling job."""
    keys = set(frappe.get_all("AI Call Identity", filters={"call_log": call_log}, pluck="name"))
    company = frappe.db.get_value("AI Call Log", call_log, "company")
    if not keys or not company:
        return
    rows = frappe.get_all("AI Webhook Event", filters={"company": company, "status": "Pending Matching"},
        fields=["name", "source", "payload_json", "identity_keys_json"], order_by="creation asc")
    frappe.flags.replaying_call_receipts = True
    try:
        from confluence_ai.services.call_registry import aliases, identity_key
        remaining = list(rows)
        while remaining:
            deferred = []
            for row in remaining:
                payload = json.loads(row.payload_json)
                receipt_keys = {identity_key(company, kind, value) for kind, value in aliases(payload)}
                if not keys.intersection(receipt_keys):
                    deferred.append(row)
                    continue
                if row.source != "vobiz":
                    continue
                handler = vobiz.handle_callback
                try:
                    _process_telephony_receipt(row.source, payload, handler)
                except Exception:
                    # The failed receipt is durable; do not lose other pending events.
                    frappe.log_error(title="Call receipt replay failed", message=frappe.get_traceback())
            updated = set(frappe.get_all("AI Call Identity", filters={"call_log": call_log}, pluck="name"))
            if updated == keys:
                break
            # A bridge event can unlock earlier customer-leg receipts in this same batch.
            keys, remaining = updated, deferred
    finally:
        frappe.flags.replaying_call_receipts = False


def _record_inbound(source: str, payload: dict) -> str:
    task = payload.get("task") or payload.get("task_name")
    batch = payload.get("batch") or payload.get("task_batch")
    doc = frappe.new_doc("AI Webhook Event")
    doc.update(
        {
            "status": "Queued",
            "direction": "Inbound",
            "event_type": payload.get("event") or payload.get("event_type") or payload.get("Event") or payload.get("status") or "unknown",
            "source": source,
            "task": task if task and frappe.db.exists("AI Task", task) else None,
            "task_batch": batch if batch and frappe.db.exists("AI Task Batch", batch) else None,
            "signature_valid": 1,
            "payload_json": as_json(payload),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _mark_webhook_processed(webhook_event: str, result: dict) -> None:
    values = {"status": "Processed", "response_json": as_json(result)}
    if result.get("status") == "pending_matching":
        values["status"] = "Pending Matching"
    elif result.get("status") in {"error", "failed"}:
        values["status"] = "Failed"
    if isinstance(result, dict):
        if result.get("task") and frappe.db.exists("AI Task", result.get("task")):
            values["task"] = result.get("task")
        if result.get("batch") and frappe.db.exists("AI Task Batch", result.get("batch")):
            values["task_batch"] = result.get("batch")
    frappe.db.set_value("AI Webhook Event", webhook_event, values)
