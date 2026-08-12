from __future__ import annotations

import json
import re
from typing import Any

import frappe

from confluence_ai.services.utils import record_provider_event


SHIPKIA_AGENT = "agent-445"
SHIPKIA_COMPANY = "shipkia"

SHIPKIA_CRM_FIELDS = {
    "shipkia_business_type",
    "shipkia_business_platform",
    "shipkia_monthly_shipments",
    "shipkia_pickup_pincode",
    "shipkia_delivery_zones",
    "shipkia_cod_required",
    "shipkia_current_provider_type",
    "shipkia_current_courier_partner",
    "shipkia_current_shipping_rate",
    "shipkia_current_rate_basis",
    "shipkia_main_pain_point",
    "shipkia_proposed_solution",
    "shipkia_interested_services",
    "shipkia_chatbot_status",
    "shipkia_chat_summary",
    "shipkia_customer_intent_score",
    "shipkia_objections",
    "shipkia_required_follow_up",
    "shipkia_next_follow_up_date",
    "shipkia_lead_stage",
}

_INTEGER_FIELDS = {"shipkia_monthly_shipments", "shipkia_customer_intent_score"}
_NUMBER_FIELDS = {"shipkia_current_shipping_rate"}
_BOOLEAN_FIELDS = {"shipkia_cod_required", "shipkia_required_follow_up"}
_LIST_TEXT_FIELDS = {"shipkia_delivery_zones", "shipkia_interested_services", "shipkia_objections"}


def normalize_phone(value: object) -> str:
    """Normalize Indian customer numbers while preserving explicit country codes."""
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[-10:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if text.startswith("+"):
        return f"+{digits}"
    return f"+{digits}"


def lookup_shipkia_crm_lead(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    phone = normalize_phone(_first(arguments, "phone", "customer_phone", "mobile", "mobile_no"))
    if not phone:
        return {"status": "validation_error", "message": "A customer phone number is required."}

    matches = _matching_leads(phone)
    if not matches:
        result = {
            "status": "success",
            "found": False,
            "phone": phone,
            "message": "No existing ShipKia CRM Lead was found for this phone number.",
        }
    else:
        selected = matches[0]
        result = {
            "status": "success",
            "found": True,
            "phone": phone,
            "lead": selected["name"],
            "customer": _safe_lead_context(selected["name"]),
            "ambiguous_matches": max(0, len(matches) - 1),
        }

    _record("lookup_shipkia_crm_lead", arguments, result, task_id=task_id, agent=agent)
    return result


def create_or_update_shipkia_lead(
    arguments: dict,
    *,
    task_id: str | None = None,
    agent: str | None = None,
    operation: str = "create_or_update_shipkia_lead",
) -> dict:
    phone = normalize_phone(_first(arguments, "phone", "customer_phone", "mobile", "mobile_no"))
    if not phone:
        return {"status": "validation_error", "message": "A customer phone number is required before saving."}

    matches = _matching_leads(phone)
    lead = frappe.get_doc("CRM Lead", matches[0]["name"]) if matches else frappe.new_doc("CRM Lead")
    action = "updated" if matches else "created"

    if not matches:
        first_name, last_name = _split_name(_first(arguments, "customer_name", "name") or "ShipKia Customer")
        lead.first_name = first_name
        if last_name:
            lead.last_name = last_name
        lead.mobile_no = phone
        lead.status = _default_crm_status()

    customer_name = str(_first(arguments, "customer_name", "name") or "").strip()
    if customer_name:
        first_name, last_name = _split_name(customer_name)
        lead.first_name = first_name
        lead.last_name = last_name

    organization = str(_first(arguments, "organization", "business_name", "company_name") or "").strip()
    if organization:
        lead.organization = organization

    email = str(_first(arguments, "email", "email_id") or "").strip()
    if email:
        lead.email = email

    if not lead.mobile_no:
        lead.mobile_no = phone

    updates = _shipkia_updates(arguments)
    for fieldname, value in updates.items():
        lead.set(fieldname, value)

    lead.flags.ignore_permissions = True
    lead.save()

    result = {
        "status": "success",
        "action": action,
        "lead": lead.name,
        "phone": phone,
        "updated_fields": sorted(updates),
        "ambiguous_matches": max(0, len(matches) - 1),
        "customer": _safe_lead_context(lead.name),
    }
    _record(operation, arguments, result, task_id=task_id, agent=agent)
    return result


def record_shipkia_call_progress(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    return create_or_update_shipkia_lead(
        arguments,
        task_id=task_id,
        agent=agent,
        operation="record_shipkia_call_progress",
    )


def create_shipkia_followup(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    phone = normalize_phone(_first(arguments, "phone", "customer_phone", "mobile", "mobile_no"))
    reason = str(_first(arguments, "followup_reason", "reason") or "").strip()
    if not phone or not reason:
        return {
            "status": "validation_error",
            "message": "Phone and follow-up reason are required.",
        }

    lead_args = dict(arguments)
    lead_args["phone"] = phone
    lead_args["shipkia_required_follow_up"] = True
    preferred_time = _first(arguments, "preferred_time", "callback_time", "shipkia_next_follow_up_date")
    parsed_time = _parse_datetime(preferred_time)
    if parsed_time:
        lead_args["shipkia_next_follow_up_date"] = parsed_time
    lead_result = create_or_update_shipkia_lead(
        lead_args,
        task_id=task_id,
        agent=agent,
        operation="create_shipkia_followup_lead_update",
    )
    if lead_result.get("status") != "success":
        return lead_result

    followup = frappe.new_doc("AI Sales Follow Up")
    followup.update(
        {
            "company": SHIPKIA_COMPANY,
            "customer_name": _first(arguments, "customer_name", "name"),
            "phone": phone,
            "status": "Open",
            "followup_reason": reason,
            "preferred_time": str(preferred_time or ""),
            "notes": str(_first(arguments, "notes", "summary") or ""),
            "source_task": task_id,
            "source_agent": agent or SHIPKIA_AGENT,
        }
    )
    followup.insert(ignore_permissions=True)

    result = {
        "status": "success",
        "followup": followup.name,
        "lead": lead_result.get("lead"),
        "phone": phone,
    }
    _record("create_shipkia_followup", arguments, result, task_id=task_id, agent=agent)
    return result


def finalize_shipkia_call_outcome(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    phone = normalize_phone(_first(arguments, "phone", "customer_phone", "mobile", "mobile_no"))
    outcome = str(arguments.get("outcome") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if not phone or not outcome or not summary:
        return {
            "status": "validation_error",
            "message": "Phone, outcome and summary are required.",
        }

    mapped = {
        "contacted": ("In Progress", "Contacted"),
        "qualified": ("Qualified", "Qualified"),
        "human required": ("Human Required", "Contacted"),
        "not qualified": ("Not Qualified", "Lost"),
        "lost": ("Not Qualified", "Lost"),
    }
    chatbot_status, lead_stage = mapped.get(outcome.lower(), ("In Progress", "Contacted"))
    lead_args = dict(arguments)
    lead_args.update(
        {
            "phone": phone,
            "shipkia_chat_summary": summary,
            "shipkia_chatbot_status": chatbot_status,
            "shipkia_lead_stage": lead_stage,
        }
    )
    lead_result = create_or_update_shipkia_lead(
        lead_args,
        task_id=task_id,
        agent=agent,
        operation="finalize_shipkia_call_lead_update",
    )
    if lead_result.get("status") != "success":
        return lead_result

    outcome_doc = frappe.new_doc("AI Sales Call Outcome")
    outcome_doc.update(
        {
            "company": SHIPKIA_COMPANY,
            "phone": phone,
            "outcome": outcome,
            "summary": summary,
            "next_action": str(arguments.get("next_action") or ""),
            "source_task": task_id,
            "source_agent": agent or SHIPKIA_AGENT,
        }
    )
    outcome_doc.insert(ignore_permissions=True)

    if task_id and frappe.db.exists("AI Task", task_id):
        frappe.db.set_value(
            "AI Task",
            task_id,
            {
                "status": "Completed",
                "transcript": summary,
            },
            update_modified=True,
        )

    result = {
        "status": "success",
        "outcome_record": outcome_doc.name,
        "lead": lead_result.get("lead"),
        "phone": phone,
    }
    _record("finalize_shipkia_call_outcome", arguments, result, task_id=task_id, agent=agent)
    return result


def lookup_pincode_serviceability(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    from confluence_ai.services.shipkia_rates import get_starting_rate
    from confluence_ai.services.shipkia_zones import resolve_shipkia_zone

    result = resolve_shipkia_zone(arguments)
    if result.get("status") == "success" and result.get("zone_verified"):
        starting_rate = get_starting_rate({"zone": result.get("zone")})
        result["starting_rate"] = starting_rate
        if starting_rate.get("status") != "success":
            result.update(
                {
                    "status": "configuration_required",
                    "zone": None,
                    "zone_verified": False,
                    "message": "The route resolved, but its verified starting rate is unavailable.",
                }
            )
    _record("lookup_pincode_serviceability", arguments, result, task_id=task_id, agent=agent)
    return result


def calculate_shipkia_rate(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    from confluence_ai.services.shipkia_rates import calculate_rate

    try:
        result = calculate_rate(arguments)
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "status": "configuration_required",
            "eligible_rates": [],
            "message": f"{exc} Do not estimate or fabricate a shipping rate.",
        }
    _record("calculate_shipkia_rate", arguments, result, task_id=task_id, agent=agent)
    return result


def get_shipkia_starting_rate(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    from confluence_ai.services.shipkia_rates import get_starting_rate

    try:
        result = get_starting_rate(arguments)
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "status": "configuration_required",
            "response_type": "zone_starting",
            "zone": None,
            "amount": None,
            "message": str(exc),
        }
    _record("get_shipkia_starting_rate", arguments, result, task_id=task_id, agent=agent)
    return result


def get_shipkia_flat_rates(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    from confluence_ai.services.shipkia_rates import get_flat_rates

    try:
        result = get_flat_rates(arguments)
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "status": "configuration_required",
            "response_type": "flat_unavailable",
            "flat_rate_options": [],
            "message": f"{exc} Do not estimate or fabricate a flat rate.",
        }
    _record("get_shipkia_flat_rates", arguments, result, task_id=task_id, agent=agent)
    return result


def get_shipkia_flat_zonal_rates(
    arguments: dict, *, task_id: str | None = None, agent: str | None = None
) -> dict:
    from confluence_ai.services.shipkia_rates import get_flat_zonal_rates

    try:
        result = get_flat_zonal_rates(arguments)
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "status": "configuration_required",
            "response_type": "flat_zonal_unavailable",
            "zone_groups": [],
            "message": f"{exc} Do not estimate or fabricate a Flat-Zonal rate.",
        }
    _record("get_shipkia_flat_zonal_rates", arguments, result, task_id=task_id, agent=agent)
    return result


def execute_shipkia_tool(
    tool_name: str,
    arguments: dict,
    *,
    task_id: str | None = None,
    agent: str | None = None,
) -> dict | None:
    handlers = {
        "lookup_shipkia_crm_lead": lookup_shipkia_crm_lead,
        "create_or_update_shipkia_lead": create_or_update_shipkia_lead,
        "record_shipkia_call_progress": record_shipkia_call_progress,
        "create_shipkia_followup": create_shipkia_followup,
        "finalize_shipkia_call_outcome": finalize_shipkia_call_outcome,
        "lookup_pincode_serviceability": lookup_pincode_serviceability,
        "get_shipkia_starting_rate": get_shipkia_starting_rate,
        "get_shipkia_flat_rates": get_shipkia_flat_rates,
        "get_shipkia_flat_zonal_rates": get_shipkia_flat_zonal_rates,
        "calculate_shipkia_rate": calculate_shipkia_rate,
    }
    handler = handlers.get(tool_name)
    if not handler:
        return None
    return handler(arguments or {}, task_id=task_id, agent=agent or SHIPKIA_AGENT)


def _matching_leads(phone: str) -> list[dict]:
    variants = _phone_variants(phone)
    rows = frappe.get_all(
        "CRM Lead",
        or_filters=[
            ["CRM Lead", "mobile_no", "in", variants],
            ["CRM Lead", "phone", "in", variants],
        ],
        fields=["name", "mobile_no", "phone", "modified"],
        order_by="modified desc",
        limit=20,
    )
    exact = [row for row in rows if phone in {normalize_phone(row.mobile_no), normalize_phone(row.phone)}]
    return [dict(row) for row in exact]


def _phone_variants(phone: str) -> list[str]:
    digits = re.sub(r"\D", "", phone)
    variants = {phone, digits, f"+{digits}"}
    if len(digits) >= 10:
        last_ten = digits[-10:]
        variants.update({last_ten, f"0{last_ten}", f"+91{last_ten}", f"91{last_ten}"})
    return sorted(value for value in variants if value)


def _safe_lead_context(lead_name: str) -> dict:
    meta = frappe.get_meta("CRM Lead")
    fields = [
        "first_name",
        "last_name",
        "organization",
        "mobile_no",
        "phone",
        "email",
        "status",
        *sorted(field for field in SHIPKIA_CRM_FIELDS if meta.has_field(field)),
    ]
    values = frappe.db.get_value("CRM Lead", lead_name, fields, as_dict=True) or {}
    values["customer_name"] = " ".join(
        part for part in (values.pop("first_name", ""), values.pop("last_name", "")) if part
    ).strip()
    return {key: value for key, value in dict(values).items() if value not in (None, "")}


def _shipkia_updates(arguments: dict) -> dict:
    nested = arguments.get("fields") or arguments.get("updates") or {}
    supplied = {**nested, **arguments} if isinstance(nested, dict) else dict(arguments)
    meta = frappe.get_meta("CRM Lead")
    updates: dict[str, Any] = {}
    for fieldname in SHIPKIA_CRM_FIELDS:
        if fieldname not in supplied or not meta.has_field(fieldname):
            continue
        value = supplied.get(fieldname)
        if value in (None, ""):
            continue
        if fieldname in _INTEGER_FIELDS:
            value = int(float(value))
            if fieldname == "shipkia_customer_intent_score":
                value = max(0, min(100, value))
        elif fieldname in _NUMBER_FIELDS:
            value = float(value)
        elif fieldname in _BOOLEAN_FIELDS:
            value = 1 if _truthy(value) else 0
        elif fieldname in _LIST_TEXT_FIELDS and isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item).strip() for item in value if str(item).strip())
        elif fieldname == "shipkia_next_follow_up_date":
            value = _parse_datetime(value)
            if not value:
                continue
        else:
            value = str(value).strip()

        field = meta.get_field(fieldname)
        if field and field.fieldtype == "Select" and value:
            options = {option.strip() for option in str(field.options or "").splitlines() if option.strip()}
            if options and str(value) not in options:
                raise frappe.ValidationError(
                    f"{field.label or fieldname} must be one of: {', '.join(sorted(options))}"
                )
        updates[fieldname] = value
    return updates


def _parse_datetime(value: object):
    if not value:
        return None
    try:
        return frappe.utils.get_datetime(value)
    except Exception:
        return None


def _default_crm_status() -> str:
    for status in ("New", "New Lead", "Contacted"):
        if frappe.db.exists("CRM Lead Status", status):
            return status
    return frappe.db.get_value("CRM Lead Status", {}, "name", order_by="position asc") or "New"


def _split_name(value: object) -> tuple[str, str]:
    parts = str(value or "").strip().split()
    if not parts:
        return "ShipKia Customer", ""
    return parts[0], " ".join(parts[1:])


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "haan", "required", "cod"}


def _first(arguments: dict, *keys: str):
    for key in keys:
        value = arguments.get(key)
        if value not in (None, ""):
            return value
    return None


def _record(
    operation: str,
    request: dict,
    response: dict,
    *,
    task_id: str | None,
    agent: str | None,
) -> None:
    record_provider_event(
        provider="ShipKia Voice",
        operation=operation,
        status=(
            "Succeeded"
            if response.get("status")
            in {
                "success",
                "simulated",
                "configuration_required",
                "no_eligible_rate",
                "requested_service_unavailable",
            }
            else "Failed"
        ),
        company=SHIPKIA_COMPANY,
        agent=agent or SHIPKIA_AGENT,
        task=task_id,
        request=request,
        response=response,
        error=response.get("message") if response.get("status") == "validation_error" else None,
    )
