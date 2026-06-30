from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import frappe
import requests
from frappe.utils.password import get_decrypted_password

from confluence_ai.services.knowledge_base import format_knowledge_snippets, retrieve_knowledge
from confluence_ai.services.utils import create_error, parse_json_object, record_provider_event


SALES_EVENT_VALUES = {"sales-call-required", "sales_call_required", "sales-lead-call", "sales"}
DEFAULT_HANDOFF_RULES = [
    "Do not diagnose, prescribe, guarantee cure, or claim medical certainty.",
    "Explain sriyaas/company process, treatment categories, diet guidance, pricing ranges, and approved offers only from the brief.",
    "For uncertain medical questions, serious symptoms, or treatment suitability, offer doctor callback.",
    "If customer is interested, create or update lead/follow-up with clear next action.",
]


def should_build_sales_context(route: "frappe.Document | None", agent: "frappe.Document | None", payload: dict) -> bool:
    if payload.get("build_sales_context") in (1, "1", True, "true", "True"):
        return True
    if agent and getattr(agent, "enable_sales_context", 0):
        return True
    if route and (route.event_value or "").strip() in SALES_EVENT_VALUES:
        return True
    event = (payload.get("event") or "").strip()
    return event in SALES_EVENT_VALUES


def enrich_sales_context(
    context: dict,
    *,
    route: "frappe.Document | None" = None,
    agent: "frappe.Document | None" = None,
) -> dict:
    if not should_build_sales_context(route, agent, context):
        return context

    enriched = dict(context)
    phone = _first_value(
        enriched,
        "customer_phone",
        "phone",
        "phone_number",
        "mobile",
        "payload_json.phone_number",
        "payload_json.customer_phone",
    )
    customer_name = _first_value(enriched, "customer_name", "patient_name", "name", "payload_json.patient_name")
    patient_encounter = _first_value(enriched, "patient_encounter", "payload_json.patient_encounter")

    patient_context = lookup_patient_sales_context(
        {
            "phone": phone,
            "customer_phone": phone,
            "patient_name": customer_name,
            "customer_name": customer_name,
            "patient_encounter": patient_encounter,
        },
        agent=agent.name if agent else None,
    )
    customer_type = "repeat" if patient_context.get("found") else "new"

    query = _build_kb_query(enriched, patient_context)
    snippets = retrieve_knowledge(
        query,
        agent=agent.name if agent else None,
        limit=int(getattr(agent, "sales_kb_limit", 6) or 6) if agent else 6,
    )

    brief = _compose_sales_brief(
        customer_name=customer_name,
        phone=phone,
        customer_type=customer_type,
        payload=enriched,
        patient_context=patient_context,
        knowledge_snippets=snippets,
    )

    enriched.update(
        {
            "customer_type": customer_type,
            "sales_brief": brief,
            "patient_summary": patient_context.get("summary", ""),
            "knowledge_snippets": [
                {
                    "title": item.get("title"),
                    "document": item.get("document"),
                    "category": item.get("category"),
                    "score": item.get("score"),
                }
                for item in snippets
            ],
            "recommended_talking_points": _talking_points(enriched, customer_type),
            "allowed_offers": _extract_allowed_offers(snippets),
            "handoff_rules": DEFAULT_HANDOFF_RULES,
        }
    )

    record_provider_event(
        provider="Confluence AI",
        operation="build_sales_context",
        status="Succeeded",
        agent=agent.name if agent else None,
        request={"phone": phone, "customer_name": customer_name, "query": query},
        response={
            "customer_type": customer_type,
            "patient_found": patient_context.get("found"),
            "knowledge_chunks": len(snippets),
        },
    )
    return enriched


def lookup_patient_sales_context(arguments: dict, *, agent: str | None = None) -> dict:
    phone = _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile"))
    encounter = arguments.get("patient_encounter")
    patient_name = arguments.get("patient_name") or arguments.get("customer_name")

    result = _lookup_with_configured_tool(arguments, agent=agent)
    if result.get("found"):
        return result

    result = _lookup_remote_patient_encounters(phone=phone, encounter=encounter, patient_name=patient_name)
    if result.get("found"):
        return result

    result = _lookup_local_patient(phone=phone, encounter=encounter, patient_name=patient_name)
    if result.get("found"):
        return result

    return {
        "found": False,
        "lookup_key": phone or encounter or patient_name or "",
        "summary": "No existing patient/customer record was found before the call. Treat this as a new sales lead.",
        "records": [],
    }


def create_or_update_lead(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    if _doctype_exists("AI Sales Lead"):
        return _upsert_local_sales_lead(arguments, task_id=task_id, agent=agent)
    return _execute_named_sales_tool("create_or_update_lead", arguments, task_id=task_id, agent=agent)


def create_sales_followup(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    if _doctype_exists("AI Sales Follow Up"):
        return _create_local_sales_followup(arguments, task_id=task_id, agent=agent)
    return _execute_named_sales_tool("create_sales_followup", arguments, task_id=task_id, agent=agent)


def log_sales_call_outcome(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    if _doctype_exists("AI Sales Call Outcome"):
        return _create_local_sales_call_outcome(arguments, task_id=task_id, agent=agent)

    response = _execute_named_sales_tool("log_sales_call_outcome", arguments, task_id=task_id, agent=agent, allow_log=True)
    if response.get("status") != "not_configured":
        return response

    record_provider_event(
        provider="Sales",
        operation="log_sales_call_outcome",
        status="Succeeded",
        agent=agent,
        task=task_id,
        request=arguments,
        response={"message": "Stored as provider event because no external sales outcome tool is configured."},
    )
    return {"status": "success", "logged": True}


def create_draft_patient_encounter_from_sales(
    arguments: dict,
    *,
    task_id: str | None = None,
    agent: str | None = None,
) -> dict:
    """Create a draft Patient Encounter in the configured ERP and store local sales context."""
    phone = _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile"))
    customer_name = arguments.get("customer_name") or arguments.get("patient_name") or arguments.get("name") or "Unknown"
    concern = _stringify(arguments.get("disease_or_concern") or arguments.get("concern") or arguments.get("interest") or "")
    notes = _compose_sales_patient_encounter_notes(arguments, task_id=task_id, agent=agent)

    lead_result = None
    if _doctype_exists("AI Sales Lead"):
        lead_result = _upsert_local_sales_lead(
            {
                "customer_name": customer_name,
                "phone": phone,
                "interest": concern,
                "summary": notes,
                "next_action": "Prescription/detail call required from sales/doctor team",
                "status": "Converted",
            },
            task_id=task_id,
            agent=agent,
        )

    server_name = _default_remote_frappe_server()
    if not server_name:
        result = {
            "status": "partial_success",
            "lead": lead_result,
            "message": "Local sales lead stored, but no enabled MCP server is configured for Patient Encounter creation.",
        }
        record_provider_event(
            provider="Sales",
            operation="create_draft_patient_encounter_from_sales",
            status="Failed",
            agent=agent,
            task=task_id,
            request=arguments,
            response=result,
            error=result["message"],
        )
        return result

    patient_result = _remote_find_or_create_patient(
        server_name,
        customer_name=customer_name,
        phone=phone,
        concern=concern,
    )
    if not patient_result.get("ok"):
        result = {
            "status": "failed",
            "lead": lead_result,
            "patient_encounter": None,
            "erp_response": patient_result,
            "created_by": "Confluence AI sales voice agent",
        }
        record_provider_event(
            provider="Sales",
            operation="create_draft_patient_encounter_from_sales",
            status="Failed",
            agent=agent,
            task=task_id,
            request=arguments,
            response=result,
            error=patient_result.get("error"),
        )
        return result

    encounter_payload = _build_patient_encounter_payload(
        server_name,
        customer_name=customer_name,
        phone=phone,
        concern=concern,
        notes=notes,
        arguments=arguments,
        patient=patient_result.get("patient"),
    )
    create_result = _remote_frappe_create(server_name, "Patient Encounter", encounter_payload)
    status = "Succeeded" if create_result.get("ok") else "Failed"
    result = {
        "status": "success" if create_result.get("ok") else "failed",
        "lead": lead_result,
        "patient_encounter": create_result.get("data", {}).get("name") if isinstance(create_result.get("data"), dict) else None,
        "erp_response": create_result,
        "created_by": "Confluence AI sales voice agent",
    }
    record_provider_event(
        provider="Sales",
        operation="create_draft_patient_encounter_from_sales",
        status=status,
        agent=agent,
        task=task_id,
        request=arguments,
        response=result,
        error=create_result.get("error") if not create_result.get("ok") else None,
    )
    return result


def _upsert_local_sales_lead(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    phone = _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile"))
    customer_name = arguments.get("customer_name") or arguments.get("patient_name") or arguments.get("name") or "Unknown"
    filters = {"phone": phone} if phone else {"customer_name": customer_name}
    existing = frappe.db.get_value("AI Sales Lead", filters, "name") if filters else None

    status = _valid_sales_lead_status(arguments.get("status") or "Interested")
    values = {
        "customer_name": customer_name,
        "phone": phone,
        "interest": _stringify(arguments.get("interest") or arguments.get("product_interest") or arguments.get("disease_or_concern") or ""),
        "summary": _stringify(arguments.get("summary") or arguments.get("notes") or ""),
        "next_action": _stringify(arguments.get("next_action") or ""),
        "status": status,
        "source_task": task_id,
        "source_agent": agent,
        "last_contact_at": frappe.utils.now(),
    }
    values = {key: value for key, value in values.items() if value not in (None, "")}

    if existing:
        frappe.db.set_value("AI Sales Lead", existing, values, update_modified=True)
        name = existing
        action = "updated"
    else:
        doc = frappe.new_doc("AI Sales Lead")
        doc.update(values)
        doc.insert(ignore_permissions=True)
        name = doc.name
        action = "created"

    frappe.db.commit()
    result = {"status": "success", "action": action, "lead": name}
    record_provider_event(
        provider="Sales",
        operation="create_or_update_lead",
        status="Succeeded",
        agent=agent,
        task=task_id,
        request=arguments,
        response=result,
    )
    return result


def _valid_sales_lead_status(status: str | None) -> str:
    allowed = {"New", "Interested", "Callback", "Not Interested", "Converted", "Closed"}
    normalized = (status or "").strip()
    if normalized in allowed:
        return normalized
    status_map = {
        "Prescription Requested": "Converted",
        "Order Confirmed": "Converted",
        "Sale": "Converted",
        "Follow Up": "Callback",
        "Follow-up": "Callback",
    }
    return status_map.get(normalized, "Interested")


def _create_local_sales_followup(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    doc = frappe.new_doc("AI Sales Follow Up")
    doc.update(
        {
            "customer_name": arguments.get("customer_name") or arguments.get("patient_name") or arguments.get("name"),
            "phone": _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile")),
            "followup_reason": arguments.get("followup_reason") or arguments.get("reason") or "Sales follow-up",
            "preferred_time": arguments.get("preferred_time") or arguments.get("callback_time"),
            "notes": _stringify(arguments.get("notes") or arguments.get("summary") or ""),
            "status": arguments.get("status") or "Open",
            "source_task": task_id,
            "source_agent": agent,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    result = {"status": "success", "followup": doc.name}
    record_provider_event(
        provider="Sales",
        operation="create_sales_followup",
        status="Succeeded",
        agent=agent,
        task=task_id,
        request=arguments,
        response=result,
    )
    return result


def _create_local_sales_call_outcome(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> dict:
    doc = frappe.new_doc("AI Sales Call Outcome")
    doc.update(
        {
            "phone": _clean_phone(arguments.get("phone") or arguments.get("customer_phone") or arguments.get("mobile")),
            "outcome": arguments.get("outcome") or "Completed",
            "summary": _stringify(arguments.get("summary") or ""),
            "next_action": _stringify(arguments.get("next_action") or ""),
            "source_task": task_id,
            "source_agent": agent,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    result = {"status": "success", "outcome": doc.name}
    record_provider_event(
        provider="Sales",
        operation="log_sales_call_outcome",
        status="Succeeded",
        agent=agent,
        task=task_id,
        request=arguments,
        response=result,
    )
    return result


def _execute_named_sales_tool(
    tool_name: str,
    arguments: dict,
    *,
    task_id: str | None = None,
    agent: str | None = None,
    allow_log: bool = False,
) -> dict:
    tool_docname = frappe.db.get_value("AI MCP Tool", {"tool_name": tool_name, "enabled": 1}, "name")
    if not tool_docname:
        if allow_log:
            return {"status": "not_configured"}
        frappe.throw(f"Sales MCP tool is not configured: {tool_name}")

    tool = frappe.get_doc("AI MCP Tool", tool_docname)
    if not tool.client_doctype and not tool.endpoint_url:
        if allow_log:
            return {"status": "not_configured"}
        frappe.throw(f"Sales MCP tool has no target mapping configured: {tool_name}")

    from confluence_ai.api.mcp import execute_mcp_tool

    result = execute_mcp_tool(tool, arguments, task_id)
    record_provider_event(
        provider="Sales",
        operation=tool_name,
        status="Succeeded",
        agent=agent,
        task=task_id,
        request=arguments,
        response=result,
    )
    return result


def _lookup_with_configured_tool(arguments: dict, *, agent: str | None = None) -> dict:
    tool_name = None
    if agent and frappe.db.exists("AI Agent", agent):
        tool_name = frappe.db.get_value("AI Agent", agent, "sales_patient_lookup_tool")

    tool_docname = tool_name or frappe.db.get_value(
        "AI MCP Tool",
        {"tool_name": "lookup_patient_sales_context", "enabled": 1},
        "name",
    )
    if not tool_docname:
        return {"found": False}

    tool = frappe.get_doc("AI MCP Tool", tool_docname)
    if not tool.client_doctype and not tool.endpoint_url:
        return {"found": False}

    try:
        from confluence_ai.api.mcp import execute_mcp_tool

        result = execute_mcp_tool(tool, arguments, None)
        records = _extract_records(result)
        if not records:
            return {"found": False}
        return {
            "found": True,
            "source": tool.tool_name,
            "summary": _summarize_records(records),
            "records": records[:5],
        }
    except Exception as exc:
        create_error(
            "Sales Patient Lookup",
            str(exc),
            source="sales_context",
            agent=agent,
            payload={"arguments": arguments},
            exc=exc,
        )
        return {"found": False, "error": str(exc)}


def _lookup_local_patient(phone: str | None, encounter: str | None, patient_name: str | None) -> dict:
    records: list[dict[str, Any]] = []

    if encounter and _doctype_exists("Patient Encounter"):
        rows = frappe.get_all(
            "Patient Encounter",
            filters={"name": encounter},
            fields=["name", "patient", "patient_name", "encounter_date", "sr_notes"],
            limit=1,
        )
        records.extend([dict(row) for row in rows])

    if phone and _doctype_exists("Patient"):
        fields = _existing_fields("Patient", ["name", "patient_name", "mobile", "phone", "email", "sex", "dob"])
        filters = []
        for field in ("mobile", "phone"):
            if field in fields:
                filters.append({field: ["in", [phone, phone.replace("+", "")]]})
        for patient_filter in filters:
            rows = frappe.get_all("Patient", filters=patient_filter, fields=fields, limit=3)
            records.extend([dict(row) for row in rows])

    if not records and patient_name and _doctype_exists("Patient"):
        fields = _existing_fields("Patient", ["name", "patient_name", "mobile", "phone", "email", "sex", "dob"])
        if "patient_name" in fields:
            rows = frappe.get_all(
                "Patient",
                filters={"patient_name": ["like", f"%{patient_name}%"]},
                fields=fields,
                limit=3,
            )
            records.extend([dict(row) for row in rows])

    if not records:
        return {"found": False}

    return {
        "found": True,
        "source": "local_frappe",
        "summary": _summarize_records(records),
        "records": records[:5],
    }


def _lookup_remote_patient_encounters(phone: str | None, encounter: str | None, patient_name: str | None) -> dict:
    server_name = _default_remote_frappe_server()
    if not server_name:
        return {"found": False}

    records: list[dict[str, Any]] = []
    errors: list[str] = []

    if encounter:
        result = _remote_frappe_list(
            server_name,
            "Patient Encounter",
            filters=[["name", "=", encounter]],
            fields=_patient_encounter_fields(),
            limit=3,
        )
        if result.get("ok"):
            records.extend(result.get("data") or [])
        elif result.get("error"):
            errors.append(result["error"])

    if phone:
        for candidate in _phone_candidates(phone):
            result = _remote_frappe_list(
                server_name,
                "Patient Encounter",
                filters=[["sr_pe_mobile", "=", candidate]],
                fields=_patient_encounter_fields(),
                limit=5,
                order_by="creation desc",
            )
            if result.get("ok") and result.get("data"):
                records.extend(result["data"])
                break
            if result.get("error"):
                errors.append(result["error"])

    if not records and patient_name:
        result = _remote_frappe_list(
            server_name,
            "Patient Encounter",
            filters=[["patient_name", "like", f"%{patient_name}%"]],
            fields=_patient_encounter_fields(),
            limit=3,
            order_by="creation desc",
        )
        if result.get("ok"):
            records.extend(result.get("data") or [])
        elif result.get("error"):
            errors.append(result["error"])

    if not records:
        if errors:
            create_error(
                "Sales Patient Lookup",
                "; ".join(errors[:3]),
                source="sales_context_remote",
                payload={"phone": phone, "encounter": encounter, "patient_name": patient_name},
            )
        return {"found": False}

    unique: list[dict[str, Any]] = []
    seen = set()
    for record in records:
        key = record.get("name") or json.dumps(record, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)

    return {
        "found": True,
        "source": "remote_patient_encounter",
        "summary": _summarize_records(unique),
        "records": unique[:5],
    }


def _default_remote_frappe_server() -> str | None:
    preferred = frappe.db.get_value("AI MCP Server", {"server_name": "Remote Dev SR Frappe", "enabled": 1}, "name")
    if preferred:
        return preferred
    return frappe.db.get_value("AI MCP Server", {"enabled": 1}, "name")


def _mcp_server_connection(server_name: str) -> tuple[str, dict]:
    values = frappe.db.get_value("AI MCP Server", server_name, ["server_url", "api_key"], as_dict=True)
    if not values:
        return "", {"Content-Type": "application/json"}

    headers = {"Content-Type": "application/json"}
    api_key = values.get("api_key")
    api_secret = get_decrypted_password("AI MCP Server", server_name, "api_secret", raise_exception=False)
    if api_key and api_secret:
        headers["Authorization"] = f"token {api_key}:{api_secret}"
    return (values.get("server_url") or "").strip(), headers


def _remote_frappe_list(
    server_name: str,
    doctype: str,
    *,
    filters: list,
    fields: list[str],
    limit: int = 5,
    order_by: str | None = None,
) -> dict:
    try:
        server_url, headers = _mcp_server_connection(server_name)
        if not server_url:
            return {"ok": False, "error": f"MCP server {server_name} has no URL"}

        params = {
            "filters": json.dumps(filters),
            "fields": json.dumps(fields),
            "limit_page_length": limit,
        }
        if order_by:
            params["order_by"] = order_by

        response = requests.get(
            urljoin(server_url.rstrip("/") + "/", f"api/resource/{doctype}"),
            headers=headers,
            params=params,
            timeout=20,
        )
        if not response.ok:
            return {"ok": False, "error": f"{doctype} lookup HTTP {response.status_code}: {response.text[:300]}"}
        return {"ok": True, "data": response.json().get("data") or []}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _remote_frappe_create(server_name: str, doctype: str, payload: dict) -> dict:
    try:
        server_url, headers = _mcp_server_connection(server_name)
        if not server_url:
            return {"ok": False, "error": f"MCP server {server_name} has no URL"}

        response = requests.post(
            urljoin(server_url.rstrip("/") + "/", f"api/resource/{doctype}"),
            headers=headers,
            json=payload,
            timeout=30,
        )
        if not response.ok:
            return {"ok": False, "error": f"{doctype} create HTTP {response.status_code}: {response.text[:800]}"}
        return {"ok": True, "data": response.json().get("data") or {}}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _remote_find_or_create_patient(
    server_name: str,
    *,
    customer_name: str,
    phone: str | None,
    concern: str,
) -> dict:
    patient = _remote_find_patient(server_name, phone)
    if patient:
        return {"ok": True, "patient": patient, "action": "found"}

    patient_payload = _build_patient_payload(
        server_name,
        customer_name=customer_name,
        phone=phone,
        concern=concern,
    )
    created = _remote_frappe_create(server_name, "Patient", patient_payload)
    if not created.get("ok"):
        return {
            "ok": False,
            "error": f"Patient create failed before Patient Encounter: {created.get('error')}",
            "patient_payload": patient_payload,
            "patient_response": created,
        }
    data = created.get("data") if isinstance(created.get("data"), dict) else {}
    return {"ok": True, "patient": data.get("name"), "action": "created", "patient_response": created}


def _remote_find_patient(server_name: str, phone: str | None) -> str | None:
    variants = _phone_variants(phone)
    if not variants:
        return None
    for fieldname in ("mobile", "phone"):
        for value in variants:
            result = _remote_frappe_list(
                server_name,
                "Patient",
                filters=[[fieldname, "=", value]],
                fields=["name", "patient_name", "mobile", "phone"],
                limit=1,
            )
            if result.get("ok") and result.get("data"):
                return result["data"][0].get("name")
    return None


def _phone_variants(phone: str | None) -> list[str]:
    cleaned = _clean_phone(phone)
    digits = re.sub(r"\D", "", str(phone or cleaned or ""))
    if not cleaned and not digits:
        return []
    variants = []
    for value in (phone, cleaned, digits):
        if value and str(value).strip() not in variants:
            variants.append(str(value).strip())
    if digits.startswith("91") and len(digits) == 12:
        ten_digit = digits[-10:]
        for value in (ten_digit, f"+91{ten_digit}"):
            if value not in variants:
                variants.append(value)
    elif len(digits) == 10:
        for value in (f"91{digits}", f"+91{digits}"):
            if value not in variants:
                variants.append(value)
    return variants


def _build_patient_payload(
    server_name: str,
    *,
    customer_name: str,
    phone: str | None,
    concern: str,
) -> dict:
    digits = re.sub(r"\D", "", str(phone or ""))
    mobile = digits[-10:] if digits.startswith("91") and len(digits) == 12 else digits or _clean_phone(phone)
    first_name = (customer_name or "Unknown").strip() or "Unknown"
    field_types = _remote_doctype_field_types(server_name, "Patient")
    candidate = {
        "first_name": first_name,
        "patient_name": first_name,
        "mobile": mobile or phone,
        "phone": phone,
        "sr_medical_department": _department_for_concern(server_name, concern),
        "status": "Active",
    }
    if field_types:
        return {
            key: value
            for key, value in candidate.items()
            if key in field_types and value not in (None, "") and not _is_child_table_field(field_types.get(key))
        }
    return {key: value for key, value in candidate.items() if value not in (None, "")}


def _department_for_concern(server_name: str, concern: str) -> str:
    concern_l = (concern or "").lower()
    candidates = []
    if any(term in concern_l for term in ("psoriasis", "skin", "itch", "patch", "redness", "scaling")):
        candidates.append("Psoriasis")
    if "kidney" in concern_l or "creatinine" in concern_l:
        candidates.append("Kidney")
    if "liver" in concern_l or "fatty" in concern_l:
        candidates.append("Liver")
    if "cancer" in concern_l:
        candidates.append("Cancer")
    if "paralysis" in concern_l:
        candidates.append("Paralysis")
    if "male infertility" in concern_l:
        candidates.append("Male Infertility")
    candidates.append("All Department")

    for candidate in candidates:
        result = _remote_frappe_list(
            server_name,
            "Medical Department",
            filters=[["name", "=", candidate]],
            fields=["name"],
            limit=1,
        )
        if result.get("ok") and result.get("data"):
            return candidate
    return "All Department"


def _default_company(server_name: str) -> str | None:
    result = _remote_frappe_list(
        server_name,
        "Company",
        filters=[],
        fields=["name"],
        limit=1,
    )
    if result.get("ok") and result.get("data"):
        return result["data"][0].get("name")
    return None


def _remote_doctype_fields(server_name: str, doctype: str) -> set[str]:
    return set(_remote_doctype_field_types(server_name, doctype).keys())


def _remote_doctype_field_types(server_name: str, doctype: str) -> dict[str, str]:
    try:
        server_url, headers = _mcp_server_connection(server_name)
        if not server_url:
            return {}

        response = requests.get(
            urljoin(server_url.rstrip("/") + "/", "api/method/frappe.desk.form.load.getdoctype"),
            headers=headers,
            params={"doctype": doctype},
            timeout=20,
        )
        if not response.ok:
            return {}
        docs = response.json().get("docs") or []
        if not docs and isinstance(response.json().get("message"), dict):
            docs = response.json()["message"].get("docs") or []
        for doc in docs:
            if doc.get("name") == doctype or doc.get("doctype") == "DocType":
                fields = {
                    field.get("fieldname"): field.get("fieldtype")
                    for field in doc.get("fields", [])
                    if field.get("fieldname")
                }
                fields["name"] = "Data"
                return fields
    except Exception:
        return {}
    return {}


def _is_child_table_field(fieldtype: str | None) -> bool:
    return str(fieldtype or "").strip().lower() in {"table", "table multiselect"}


def _build_patient_encounter_payload(
    server_name: str,
    *,
    customer_name: str,
    phone: str | None,
    concern: str,
    notes: str,
    arguments: dict,
    patient: str | None = None,
) -> dict:
    candidate = {
        "patient": patient,
        "patient_name": customer_name,
        "sr_pe_mobile": phone,
        "mobile": phone,
        "phone": phone,
        "encounter_date": frappe.utils.today(),
        "encounter_time": frappe.utils.nowtime(),
        "company": _default_company(server_name),
        "sr_encounter_type": "Order",
        "sr_encounter_place": "Online",
        "sr_encounter_source": arguments.get("encounter_source") or "Vobiz Ai Call",
        "sr_lead_notes": notes[:1000],
        "sr_utm_source": arguments.get("source_system") or "Vobiz Inbound Sales",
        "status": "Open",
        "sr_encounter_status": "Draft",
        "sr_notes": notes,
        "encounter_comment": notes,
        "sr_pe_order_items": _build_sales_order_items(server_name, concern, arguments),
    }

    extra_map = {
        "age": arguments.get("age"),
        "location": arguments.get("location") or arguments.get("city"),
        "duration": arguments.get("duration") or arguments.get("kab_se_hai"),
        "has_report": arguments.get("has_report"),
        "previous_treatment": arguments.get("previous_treatment"),
        "address": arguments.get("address") or arguments.get("delivery_address"),
        "payment_mode": arguments.get("payment_mode"),
    }
    for key, value in extra_map.items():
        if value not in (None, "", [], {}):
            candidate[key] = value

    field_types = _remote_doctype_field_types(server_name, "Patient Encounter")
    if field_types:
        return {
            key: value
            for key, value in candidate.items()
            if key in field_types
            and value not in (None, "")
            and (key == "sr_pe_order_items" or not _is_child_table_field(field_types.get(key)))
        }
    return {key: value for key, value in candidate.items() if value not in (None, "")}


def _build_sales_order_items(server_name: str, concern: str, arguments: dict) -> list[dict]:
    raw_items = arguments.get("items")
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception:
            raw_items = None

    items = raw_items if isinstance(raw_items, list) and raw_items else [{}]
    rows = []
    for item in items[:5]:
        item = item if isinstance(item, dict) else {"name": str(item)}
        item_record = _resolve_sales_item(server_name, item, concern)
        if not item_record:
            continue
        qty = item.get("qty") or item.get("quantity") or 1
        rate = item.get("rate") or item.get("price") or item.get("amount") or 5500
        amount = item.get("amount") or (float(qty or 1) * float(rate or 0))
        rows.append(
            {
                "sr_item_code": item_record.get("item_code") or item_record.get("name"),
                "sr_item_name": item_record.get("item_name") or item_record.get("name"),
                "sr_item_uom": item_record.get("stock_uom") or "Nos",
                "sr_item_qty": qty,
                "sr_item_rate": rate,
                "sr_item_amount": amount,
            }
        )
    return rows


def _resolve_sales_item(server_name: str, item: dict, concern: str) -> dict | None:
    explicit = item.get("item_code") or item.get("name") or item.get("item_name")
    for term in [explicit, *_default_item_terms(concern)]:
        if not term:
            continue
        found = _find_remote_item(server_name, str(term))
        if found:
            return found
    return None


def _default_item_terms(concern: str) -> list[str]:
    concern_l = (concern or "").lower()
    if any(term in concern_l for term in ("psoriasis", "skin", "itch", "patch", "redness", "scaling")):
        return ["PSO", "Psoriasis Treatment 30 Days", "Skin Treatment 30Days", "AROGYA"]
    if "kidney" in concern_l:
        return ["AROGYA"]
    if "male infertility" in concern_l or "mi " in f"{concern_l} ":
        return ["MI Varicocele 30days", "AROGYA"]
    return ["AROGYA"]


def _find_remote_item(server_name: str, term: str) -> dict | None:
    fields = ["name", "item_code", "item_name", "stock_uom"]
    filters_to_try = [
        [["name", "=", term]],
        [["item_code", "=", term]],
        [["item_name", "=", term]],
        [["item_name", "like", f"%{term}%"]],
    ]
    for filters in filters_to_try:
        result = _remote_frappe_list(server_name, "Item", filters=filters, fields=fields, limit=1)
        if result.get("ok") and result.get("data"):
            return result["data"][0]
    return None


def _compose_sales_patient_encounter_notes(arguments: dict, *, task_id: str | None = None, agent: str | None = None) -> str:
    rows = [
        "Created by Confluence AI sales voice agent.",
        f"Task: {task_id or ''}",
        f"Agent: {agent or ''}",
        f"Customer Name: {arguments.get('customer_name') or arguments.get('patient_name') or arguments.get('name') or ''}",
        f"Phone: {arguments.get('phone') or arguments.get('customer_phone') or arguments.get('mobile') or ''}",
        f"Age: {arguments.get('age') or ''}",
        f"Location: {arguments.get('location') or arguments.get('city') or ''}",
        f"Concern/Disease: {arguments.get('disease_or_concern') or arguments.get('concern') or arguments.get('interest') or ''}",
        f"Duration / Kab se hai: {arguments.get('duration') or arguments.get('kab_se_hai') or ''}",
        f"Symptoms: {_stringify(arguments.get('symptoms') or '')}",
        f"Reports Available: {arguments.get('has_report') or arguments.get('reports_available') or ''}",
        f"Previous Treatment: {_stringify(arguments.get('previous_treatment') or '')}",
        f"Current Treatment/Medicine: {_stringify(arguments.get('current_treatment') or arguments.get('current_medicine') or '')}",
        f"Customer Interest/Readiness: {_stringify(arguments.get('readiness') or arguments.get('outcome') or '')}",
        f"Package/Price Discussed: {_stringify(arguments.get('price_discussed') or arguments.get('package_discussed') or '')}",
        f"Delivery Address: {_stringify(arguments.get('address') or arguments.get('delivery_address') or '')}",
        f"Address Confirmed By Customer: {_stringify(arguments.get('address_confirmed') or arguments.get('whatsapp_address_confirmed') or '')}",
        f"WhatsApp Sent: {_stringify(arguments.get('whatsapp_sent') or arguments.get('whatsapp_confirmation_sent') or '')}",
        f"Payment Mode: {_stringify(arguments.get('payment_mode') or '')}",
        f"Prepaid Discount Discussed: {_stringify(arguments.get('prepaid_discount') or '')}",
        f"COD Token/Prescription Charge Discussed: {_stringify(arguments.get('cod_token_amount') or arguments.get('token_amount') or '')}",
        f"Remaining Amount Instruction: {_stringify(arguments.get('remaining_amount_instruction') or '')}",
        f"Order Confirmation Details: {_stringify(arguments.get('order_details') or '')}",
        f"Next Action: {_stringify(arguments.get('next_action') or 'Prescription/detail call required')}",
        f"Call Summary: {_stringify(arguments.get('summary') or arguments.get('notes') or '')}",
    ]
    return "\n".join(row for row in rows if not row.endswith(": "))


def _patient_encounter_fields() -> list[str]:
    return [
        "name",
        "patient",
        "patient_name",
        "sr_pe_mobile",
        "encounter_date",
        "status",
        "sr_encounter_status",
        "sr_notes",
        "encounter_comment",
        "pe_shipkia_order_id",
        "pe_shipkia_awb_number",
        "pe_shipkia_status",
        "sr_delivery_type",
    ]


def _phone_candidates(phone: str) -> list[str]:
    cleaned = _clean_phone(phone) or phone
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    values = [cleaned]
    if digits:
        values.append(digits)
        if digits.startswith("91") and len(digits) > 10:
            values.append(digits[-10:])
        else:
            values.append(f"91{digits[-10:]}")
            values.append(f"+91{digits[-10:]}")
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _build_kb_query(payload: dict, patient_context: dict) -> str:
    values = [
        _first_value(payload, "product_interest", "item", "items", "payload_json.items"),
        _first_value(payload, "disease_or_concern", "concern", "treatment", "campaign"),
        _first_value(payload, "patient_name", "customer_name", "payload_json.patient_name"),
        patient_context.get("summary"),
    ]
    query = " ".join([_stringify(v) for v in values if v])
    return query or "company treatments medicines diet pricing discount sales objections FAQs"


def _compose_sales_brief(
    *,
    customer_name: str | None,
    phone: str | None,
    customer_type: str,
    payload: dict,
    patient_context: dict,
    knowledge_snippets: list[dict[str, Any]],
) -> str:
    lines = [
        "## Sales Brief",
        f"- Customer Name: {customer_name or 'Unknown'}",
        f"- Phone: {phone or 'Unknown'}",
        f"- Customer Type: {customer_type}",
    ]

    product_interest = _first_value(payload, "product_interest", "item", "payload_json.items")
    concern = _first_value(payload, "disease_or_concern", "concern", "campaign")
    if product_interest:
        lines.append(f"- Product/Item Interest: {_stringify(product_interest)}")
    if concern:
        lines.append(f"- Concern/Campaign: {_stringify(concern)}")

    if patient_context.get("summary"):
        lines.extend(["", "## Existing Patient/Customer Context", patient_context["summary"]])

    snippets_text = format_knowledge_snippets(knowledge_snippets)
    if snippets_text:
        lines.extend(["", "## Relevant Knowledge Base", snippets_text])

    lines.extend(
        [
            "",
            "## Conversation Rules",
            "- Start with a warm Hinglish/Roman Hindi greeting unless the customer uses another language.",
            "- Mention only facts available in this brief or confirmed by the customer.",
            "- Ask useful qualification questions before agreeing to discounts, callbacks, or changes.",
            "- Escalate medical suitability questions to a doctor callback.",
        ]
    )
    return "\n".join(lines)


def _talking_points(payload: dict, customer_type: str) -> list[str]:
    points = [
        "Introduce yourself and confirm you are calling from the care/sales team.",
        "Understand the customer's concern and current requirement.",
        "Explain relevant treatment/product benefits from the knowledge brief.",
        "Discuss pricing/offers only from approved KB information.",
        "Close with next action: doctor callback, follow-up, appointment, or lead update.",
    ]
    if customer_type == "repeat":
        points.insert(2, "Refer to previous treatment/purchase context respectfully and confirm what they need now.")
    return points


def _extract_allowed_offers(snippets: list[dict[str, Any]]) -> list[str]:
    offers: list[str] = []
    for item in snippets:
        content = item.get("content") or ""
        for line in content.splitlines():
            lower = line.lower()
            if any(word in lower for word in ("discount", "offer", "price", "pricing", "coupon")):
                clean = line.strip("- *\t ")
                if clean and clean not in offers:
                    offers.append(clean[:250])
            if len(offers) >= 5:
                return offers
    return offers


def _extract_records(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return []
    if isinstance(result, dict):
        data = result.get("data") or result.get("body") or result.get("records") or result.get("message")
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]
    if isinstance(result, list):
        return [dict(row) for row in result if isinstance(row, dict)]
    return []


def _summarize_records(records: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for idx, record in enumerate(records[:5], start=1):
        compact = {key: value for key, value in record.items() if value not in (None, "", [], {})}
        parts.append(f"{idx}. {json.dumps(compact, ensure_ascii=False, default=str)[:900]}")
    return "\n".join(parts)


def _first_value(data: dict, *paths: str):
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
                break
        if current not in (None, "", [], {}):
            return current
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _clean_phone(phone: Any) -> str | None:
    if not phone:
        return None
    text = str(phone).strip()
    if not text:
        return None
    if text.startswith("+"):
        return text
    if text.isdigit() and len(text) >= 10:
        return f"+{text}"
    return text


def _doctype_exists(doctype: str) -> bool:
    try:
        return bool(frappe.db.exists("DocType", doctype))
    except Exception:
        return False


def _existing_fields(doctype: str, fields: list[str]) -> list[str]:
    meta = frappe.get_meta(doctype)
    return [field for field in fields if field == "name" or meta.has_field(field)]


@frappe.whitelist()
def preview_sales_context(payload_json: str | dict, agent: str | None = None) -> dict:
    payload = parse_json_object(payload_json, "Sales Payload")
    agent_doc = frappe.get_doc("AI Agent", agent) if agent and frappe.db.exists("AI Agent", agent) else None
    return enrich_sales_context(payload, agent=agent_doc)
