from __future__ import annotations

import frappe


def after_install() -> None:
    ensure_roles()
    ensure_settings()
    ensure_sales_defaults()


def after_migrate() -> None:
    ensure_roles()
    ensure_settings()
    ensure_sales_defaults()


def ensure_roles() -> None:
    for role_name in ("Confluence AI Manager", "Confluence AI Operator"):
        if not frappe.db.exists("Role", role_name):
            role = frappe.new_doc("Role")
            role.role_name = role_name
            role.desk_access = 1
            role.insert(ignore_permissions=True)


def ensure_settings() -> None:
    if frappe.db.exists("Confluence AI Settings", "Confluence AI Settings"):
        return

    settings = frappe.new_doc("Confluence AI Settings")
    settings.update(
        {
            "doctype": "Confluence AI Settings",
            "ingest_queue": "agent_ingest",
            "dispatch_queue": "agent_dispatch",
            "whatsapp_queue": "agent_whatsapp",
            "voice_queue": "agent_voice",
            "llm_queue": "agent_llm",
            "callbacks_queue": "agent_callbacks",
            "deadline_queue": "agent_deadline",
            "default_timezone": "Asia/Kolkata",
            "default_task_timeout_seconds": 900,
            "dispatch_batch_size": 500,
            "embedding_provider": "OpenAI",
            "embedding_model": "text-embedding-3-small",
            "embedding_base_url": "https://api.openai.com/v1",
            "embedding_path": "/embeddings",
            "embedding_timeout_seconds": 30,
        }
    )
    settings.insert(ignore_permissions=True)


def ensure_sales_defaults() -> None:
    ensure_embedding_settings()
    ensure_knowledge_categories()
    ensure_sales_mcp_tools()


def ensure_embedding_settings() -> None:
    defaults = {
        "embedding_provider": "OpenAI",
        "embedding_model": "text-embedding-3-small",
        "embedding_base_url": "https://api.openai.com/v1",
        "embedding_path": "/embeddings",
        "embedding_timeout_seconds": 30,
    }
    for fieldname, value in defaults.items():
        current = frappe.db.get_single_value("Confluence AI Settings", fieldname)
        if current in (None, ""):
            frappe.db.set_single_value("Confluence AI Settings", fieldname, value)


def ensure_knowledge_categories() -> None:
    for category_name, description in {
        "Company": "Company overview, clinic/process information, and brand positioning.",
        "Treatments": "Treatment and medicine information approved for sales conversations.",
        "Diet": "Diet and lifestyle guidance approved for sales conversations.",
        "Pricing": "Pricing, discount, offer, and payment information.",
        "Objections": "Common customer objections, FAQs, and safe response guidance.",
        "Policies": "Escalation, compliance, SOP, and safety rules.",
    }.items():
        if frappe.db.exists("AI Knowledge Category", category_name):
            continue
        doc = frappe.new_doc("AI Knowledge Category")
        doc.category_name = category_name
        doc.description = description
        doc.insert(ignore_permissions=True)


def ensure_sales_mcp_tools() -> None:
    tools = {
        "search_sales_knowledge": {
            "description": "Search approved sales knowledge base snippets when the customer asks a detailed product, treatment, pricing, diet, objection, or policy question.",
            "parameters": [
                ("query", "string", 1, "Customer question or topic to search in the sales knowledge base."),
                ("limit", "number", 0, "Maximum number of snippets to return."),
            ],
        },
        "lookup_patient_sales_context": {
            "description": "Fetch repeat-customer/patient context before or during a sales call using phone, encounter, or name.",
            "parameters": [
                ("phone", "string", 0, "Customer phone number."),
                ("customer_phone", "string", 0, "Customer phone number if provided under customer_phone."),
                ("patient_encounter", "string", 0, "Patient Encounter ID if available."),
                ("patient_name", "string", 0, "Patient/customer name if available."),
            ],
        },
        "create_or_update_lead": {
            "description": "Create or update a sales lead after customer confirms interest or next action.",
            "parameters": [
                ("customer_name", "string", 1, "Customer name."),
                ("phone", "string", 1, "Customer phone number."),
                ("interest", "string", 0, "Treatment/product/campaign interest."),
                ("summary", "string", 0, "Short call summary and customer intent."),
                ("next_action", "string", 0, "Next sales action."),
            ],
        },
        "create_sales_followup": {
            "description": "Create a sales follow-up or callback task with reason and preferred time.",
            "parameters": [
                ("customer_name", "string", 0, "Customer name."),
                ("phone", "string", 1, "Customer phone number."),
                ("followup_reason", "string", 1, "Reason for follow-up."),
                ("preferred_time", "string", 0, "Preferred callback time."),
                ("notes", "string", 0, "Additional details from the call."),
            ],
        },
        "log_sales_call_outcome": {
            "description": "Log final sales call outcome, disposition, and summary.",
            "parameters": [
                ("phone", "string", 1, "Customer phone number."),
                ("outcome", "string", 1, "Final call outcome/disposition."),
                ("summary", "string", 1, "Short summary of what happened in the call."),
                ("next_action", "string", 0, "Next action if any."),
            ],
        },
        "create_draft_patient_encounter_from_sales": {
            "description": "Create a draft Patient Encounter from a sales call after the customer agrees to proceed, and store the collected sales details.",
            "parameters": [
                ("customer_name", "string", 1, "Customer/patient name."),
                ("phone", "string", 1, "Customer phone number."),
                ("age", "string", 0, "Customer age if shared."),
                ("location", "string", 0, "Customer city/location."),
                ("disease_or_concern", "string", 1, "Disease or main concern discussed."),
                ("duration", "string", 0, "How long the customer has had the issue."),
                ("symptoms", "string", 0, "Symptoms shared by customer."),
                ("has_report", "string", 0, "Whether reports/photos are available."),
                ("previous_treatment", "string", 0, "Existing or previous treatment details."),
                ("summary", "string", 1, "Structured call summary and sales notes."),
                ("next_action", "string", 0, "Next action for prescription/order team."),
            ],
        },
    }

    for tool_name, config in tools.items():
        existing = frappe.db.get_value("AI MCP Tool", {"tool_name": tool_name}, "name")
        tool = frappe.get_doc("AI MCP Tool", existing) if existing else frappe.new_doc("AI MCP Tool")
        tool.enabled = 1
        tool.tool_name = tool_name
        tool.description = config["description"]
        tool.operation_type = tool.operation_type or "Read"

        existing_params = {row.parameter_name for row in (tool.get("input_parameters") or [])}
        for parameter_name, param_type, required, description in config["parameters"]:
            if parameter_name in existing_params:
                continue
            tool.append(
                "input_parameters",
                {
                    "parameter_name": parameter_name,
                    "type": param_type,
                    "required": required,
                    "description": description,
                },
            )

        tool.save(ignore_permissions=True) if existing else tool.insert(ignore_permissions=True)
