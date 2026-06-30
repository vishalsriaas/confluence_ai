"""
event_router.py
---------------
Core logic for the AI Event Route system.

Handles:
  - Matching an inbound webhook payload to a configured AI Event Route
  - Building the task context JSON from the n8n-style field mapping table
  - Auth validation via optional per-route webhook secret
  - Dispatching tasks:
      Immediate — 1 task, High priority, dispatched right now
      Batch     — N tasks from payload array, Normal priority, dispatched as batch
"""
from __future__ import annotations

import frappe
from typing import Any

from confluence_ai.services.dispatcher import enqueue_task_execution, refresh_batch_counts
from confluence_ai.services.sales_disease_router import apply_sales_route_context, resolve_sales_disease_route
from confluence_ai.services.sales_context import enrich_sales_context
from confluence_ai.services.utils import as_json, create_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_matching_route(payload: dict, source_system: str | None = None) -> "frappe.Document | None":
    """
    Find the first enabled AI Event Route that matches the incoming payload.

    Matching logic:
      1. Read the field named by `event_key_field` from the payload.
      2. Compare it to `event_value` on each active route (ordered by creation).
      3. If `source_system` is provided AND the route has `source_system` set,
         both must match. Routes with an empty `source_system` are catch-all.
    """
    routes = frappe.get_all(
        "AI Event Route",
        filters={"enabled": 1},
        fields=["name", "event_key_field", "event_value", "source_system"],
        order_by="source_system desc, creation asc",
    )

    for r in routes:
        event_key = r.get("event_key_field") or "event"
        if payload.get(event_key) != r.get("event_value"):
            continue

        route_source = (r.get("source_system") or "").strip()
        if route_source:
            if not source_system or route_source != source_system:
                continue

        return frappe.get_doc("AI Event Route", r.name)

    return None


def validate_route_auth(route: "frappe.Document", request_headers: dict) -> bool:
    """
    Validate the optional per-route webhook secret.

    Rules:
    - If route has NO webhook_secret set → always passes (open route)
    - If route HAS webhook_secret set → X-Webhook-Secret header must match

    Returns True if valid, False if rejected.
    """
    secret = route.get_password("webhook_secret") if route.webhook_secret else None
    if not secret:
        return True  # No auth configured — allow all

    incoming = (request_headers.get("X-Webhook-Secret") or "").strip()
    return incoming == secret


def _get_nested_value(d: dict, path: str, default: str = "") -> Any:
    if not path:
        return default
    curr: Any = d
    for part in path.split("."):
        if isinstance(curr, dict):
            curr = curr.get(part)
        else:
            return default
    return curr if curr is not None else default


def build_context(route: "frappe.Document", payload: dict) -> dict:
    """
    Build the task context JSON using the n8n-style field mapping table.

    For each row in field_mappings:
      - value_type = "From Payload"  → read payload[source_field]
      - value_type = "Static Value"  → use static_value directly
      Then apply optional transformation (Uppercase / Lowercase / Trim / Trim + Lowercase)

    If no field_mappings are defined → pass full raw payload as context (safe default).
    """
    if not route.field_mappings:
        return dict(payload)

    context = {}
    for row in route.field_mappings:
        target = (row.target_field or "").strip()
        if not target:
            continue

        value_type = row.value_type or "From Payload"

        if value_type == "Static Value":
            value = row.static_value or ""
        else:
            # From Payload
            src = (row.source_field or "").strip()
            value = _get_nested_value(payload, src, "")

        # Apply transformation
        transform = row.transformation or "None"
        if isinstance(value, str):
            if transform == "Uppercase":
                value = value.upper()
            elif transform == "Lowercase":
                value = value.lower()
            elif transform == "Trim":
                value = value.strip()
            elif transform == "Trim + Lowercase":
                value = value.strip().lower()

        context[target] = value

    return context


def dispatch_from_route(route: "frappe.Document", payload: dict) -> dict:
    """
    Create AI Task(s) and dispatch based on route dispatch_mode.

    Immediate → 1 task, High priority by default, dispatched right now
    Batch     → N tasks from payload records array, dispatched as a batch
    """
    try:
        if route.dispatch_mode == "Batch":
            return _dispatch_batch(route, payload)
        return _dispatch_immediate(route, payload)
    except Exception as exc:
        create_error("Event Route", str(exc), source="event_router", exc=exc)
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dispatch_immediate(route: "frappe.Document", payload: dict) -> dict:
    """
    Single task dispatch — used for real-time events (order_confirmed, delivery_completed, etc.).
    Priority: defaults to High so immediate tasks are processed before batch tasks.
    """
    context = build_context(route, payload)
    sales_selection = resolve_sales_disease_route(context, payload, route=route)
    context = apply_sales_route_context(context, sales_selection)
    target_agent = sales_selection.get("target_agent") or route.target_agent or None
    agent = frappe.get_doc("AI Agent", target_agent) if target_agent else None
    context = enrich_sales_context(context, route=route, agent=agent)

    # Idempotency: block duplicate tasks on same key
    idem_key = _build_idempotency_key(route, payload)
    if idem_key:
        existing = frappe.db.exists("AI Task", {"idempotency_key": idem_key})
        if existing:
            return {"status": "duplicate", "task": existing, "idempotency_key": idem_key}

    # Immediate tasks are always High priority to beat batch tasks in queue
    effective_priority = route.priority or "High"

    batch = _create_batch(route, source_payload=payload, batch_label=None)

    task = frappe.new_doc("AI Task")
    task.update({
        "status": "Queued",
        "task_batch": batch.name,
        "task_template": route.task_template,
        "target_agent": target_agent,
        "target_group": route.target_group or None,
        "channel": route.channel or "Voice",
        "priority": effective_priority,
        "idempotency_key": idem_key or None,
        "context_json": as_json(context),
    })
    task.insert(ignore_permissions=True)

    refresh_batch_counts(batch.name)
    enqueue_task_execution(task.name, task.channel)

    return {
        "status": "queued",
        "dispatch_mode": "Immediate",
        "batch": batch.name,
        "task": task.name,
        "priority": effective_priority,
    }


def _dispatch_batch(route: "frappe.Document", payload: dict) -> dict:
    """
    Bulk task dispatch — used for batch events (followup_batch with 1500 records, etc.).
    Batch tasks default to Normal priority to yield to real-time Immediate tasks.
    """
    records_field = (route.batch_records_field or "records").strip()
    records = payload.get(records_field)

    if not records or not isinstance(records, list):
        frappe.throw(
            f"AI Event Route '{route.name}' (Batch mode) expects payload field "
            f"'{records_field}' to contain a list of records."
        )

    batch_label = (route.batch_label or "").strip() or None
    batch = _create_batch(route, source_payload=payload, batch_label=batch_label)

    # Batch tasks default to Normal priority
    effective_priority = route.priority or "Normal"

    created = 0
    for index, record in enumerate(records, start=1):
        context = build_context(route, record)
        sales_selection = resolve_sales_disease_route(context, record, route=route)
        context = apply_sales_route_context(context, sales_selection)
        target_agent = sales_selection.get("target_agent") or route.target_agent or None
        agent = frappe.get_doc("AI Agent", target_agent) if target_agent else None
        context = enrich_sales_context(context, route=route, agent=agent)
        idem_key = _build_idempotency_key(route, record, batch_prefix=batch.name)
        record_id = record.get("external_record_id") or record.get("id") or str(index)

        task = frappe.new_doc("AI Task")
        task.update({
            "status": "Queued",
            "task_batch": batch.name,
            "task_template": route.task_template,
            "target_agent": target_agent,
            "target_group": route.target_group or None,
            "channel": route.channel or "Voice",
            "priority": effective_priority,
            "external_record_id": record_id,
            "idempotency_key": idem_key or None,
            "context_json": as_json(context),
        })
        task.insert(ignore_permissions=True)
        created += 1

    refresh_batch_counts(batch.name)

    # Kick off dispatch (scheduler also picks it up every minute as backup)
    from confluence_ai.services.utils import get_queue_name
    queue = get_queue_name("dispatch_queue", "agent_dispatch")
    frappe.enqueue(
        "confluence_ai.services.dispatcher.dispatch_batch",
        queue=queue,
        batch_name=batch.name,
        enqueue_after_commit=True,
    )

    return {
        "status": "queued",
        "dispatch_mode": "Batch",
        "batch": batch.name,
        "batch_label": batch_label,
        "records": created,
        "priority": effective_priority,
    }


def _create_batch(
    route: "frappe.Document",
    source_payload: dict,
    batch_label: str | None,
) -> "frappe.Document":
    """Create and return a new AI Task Batch linked to this route."""
    batch = frappe.new_doc("AI Task Batch")
    batch.update({
        "status": "Queued",
        "source_system": route.source_system or route.route_name,
        "batch_label": batch_label or None,
        "task_template": route.task_template,
        "target_agent": route.target_agent or None,
        "target_group": route.target_group or None,
        "priority": route.priority or "Normal",
        "callback_url": route.callback_url or None,
        "source_payload_json": as_json(source_payload),
    })
    batch.insert(ignore_permissions=True)
    return batch


def _build_idempotency_key(
    route: "frappe.Document",
    payload: dict,
    batch_prefix: str | None = None,
) -> str | None:
    """
    Build an idempotency key from the configured payload field.
    Returns None if idempotency_key_field is not configured on the route.
    """
    field = (route.idempotency_key_field or "").strip()
    if not field:
        return None
    value = payload.get(field)
    if not value:
        return None
    prefix = batch_prefix or route.name
    return f"{prefix}:{value}"
