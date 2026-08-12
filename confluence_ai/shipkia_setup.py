from __future__ import annotations

import json
import os
import secrets
import zipfile
from pathlib import Path

import frappe

from confluence_ai.services.shipkia_voice import SHIPKIA_AGENT, SHIPKIA_COMPANY
from confluence_ai.prompts.shipkia_voice import APPROVED_SALES_BENEFITS, PROMPT_REGISTRY


SHIPKIA_CHANNEL = "channel-446"
SHIPKIA_AGENT_NAME = "shipkia-voice-sales"
SHIPKIA_TOKEN_KEY = "shipkia-voice-local"
DEFAULT_CREDENTIAL_ZIP = Path("/mnt/c/Users/Harsh/Downloads/Shipkia Livkit AGent (2).zip")


TOOL_SPECS = {
    "lookup_shipkia_crm_lead": {
        "description": "Find the canonical ShipKia CRM Lead by normalized customer phone before asking questions.",
        "condition": "Call once near the start of the call when the customer phone is available.",
        "parameters": [
            ("phone", "string", True, "Customer phone number including country code when available."),
        ],
    },
    "create_or_update_shipkia_lead": {
        "description": "Create or update one canonical CRM Lead and save only confirmed ShipKia onboarding details.",
        "condition": "Call after the customer clearly provides or confirms one or more onboarding details.",
        "parameters": [
            ("phone", "string", True, "Customer phone number used for normalized duplicate matching."),
            ("customer_name", "string", False, "Customer name when clearly provided."),
            ("organization", "string", False, "Business or organisation name when clearly provided."),
            ("email", "string", False, "Customer email when clearly provided."),
            ("shipkia_business_type", "string", False, "How the customer sells: directly to customers, to businesses, through marketplaces, or another model."),
            ("shipkia_business_platform", "string", False, "Confirmed order platform such as Shopify, WooCommerce, marketplace, own website or offline."),
            ("shipkia_monthly_shipments", "number", False, "Approximate monthly shipment count."),
            ("shipkia_delivery_zones", "string", False, "Customer delivery regions or zones."),
            ("shipkia_cod_required", "boolean", False, "Whether COD shipping is required."),
            ("shipkia_current_provider_type", "string", False, "Direct Courier, Shipping Aggregator, Own Arrangement, Other or Not Shared."),
            ("shipkia_current_courier_partner", "string", False, "Current courier or aggregator."),
            ("shipkia_current_shipping_rate", "number", False, "Current shipping rate explicitly stated by customer."),
            ("shipkia_current_rate_basis", "string", False, "Confirmed comparable weight, payment type, inclusions and route or zone."),
            ("shipkia_main_pain_point", "string", False, "High Rates, Pickup Issue, RTO Issue, Tracking Issue, COD Remittance, Support Issue, Integration Issue or Other."),
            ("shipkia_proposed_solution", "string", False, "Verified ShipKia solution actually explained for the customer's confirmed problem."),
            ("shipkia_interested_services", "string", False, "Confirmed services the customer is interested in."),
            ("shipkia_chatbot_status", "string", False, "Not Started, In Progress, Qualified, Not Qualified, Converted or Human Required."),
            ("shipkia_chat_summary", "string", False, "Concise confirmed conversation summary."),
            ("shipkia_customer_intent_score", "number", False, "Intent score from 0 to 100."),
            ("shipkia_objections", "string", False, "Customer objections stated during the call."),
            ("shipkia_required_follow_up", "boolean", False, "Whether a follow-up is required."),
            ("shipkia_next_follow_up_date", "string", False, "Confirmed follow-up datetime."),
            ("shipkia_lead_stage", "string", False, "New, Contacted, Qualified, Demo Scheduled, Negotiation, Won or Lost."),
        ],
    },
    "record_shipkia_call_progress": {
        "description": "Incrementally save confirmed ShipKia call information without erasing existing CRM Lead data.",
        "condition": "Call during the conversation after meaningful details are confirmed; do not call with guessed or blank data.",
        "parameters": [
            ("phone", "string", True, "Customer phone number used for normalized duplicate matching."),
            ("customer_name", "string", False, "Customer name when clearly provided."),
            ("organization", "string", False, "Confirmed business or organisation name."),
            ("shipkia_business_type", "string", False, "Confirmed business type."),
            ("shipkia_business_platform", "string", False, "Confirmed order platform or sales channel."),
            ("shipkia_monthly_shipments", "number", False, "Confirmed approximate monthly shipment count."),
            ("shipkia_delivery_zones", "string", False, "Confirmed delivery regions."),
            ("shipkia_cod_required", "boolean", False, "Confirmed COD requirement."),
            ("shipkia_current_provider_type", "string", False, "Confirmed provider arrangement type."),
            ("shipkia_current_courier_partner", "string", False, "Confirmed current courier."),
            ("shipkia_current_shipping_rate", "number", False, "Confirmed current rate."),
            ("shipkia_current_rate_basis", "string", False, "Confirmed comparable rate basis."),
            ("shipkia_main_pain_point", "string", False, "Confirmed main shipping problem."),
            ("shipkia_proposed_solution", "string", False, "Verified ShipKia solution actually explained for that problem."),
            ("shipkia_interested_services", "string", False, "Confirmed services of interest."),
            ("shipkia_chat_summary", "string", False, "Short progress summary."),
            ("shipkia_objections", "string", False, "Customer objections stated during the call."),
        ],
    },
    "create_shipkia_followup": {
        "description": "Create a ShipKia sales callback and mark the canonical CRM Lead for follow-up.",
        "condition": "Call only after the customer requests or agrees to a callback and the reason is known.",
        "parameters": [
            ("phone", "string", True, "Customer phone number."),
            ("customer_name", "string", False, "Customer name when known."),
            ("followup_reason", "string", True, "Confirmed reason for the callback."),
            ("preferred_time", "string", False, "Customer's preferred callback date/time in their own words."),
            ("notes", "string", False, "Useful callback notes."),
        ],
    },
    "finalize_shipkia_call_outcome": {
        "description": "Record the final ShipKia call outcome and update the canonical CRM Lead summary and stage.",
        "condition": "Call once before ending a completed conversation.",
        "parameters": [
            ("phone", "string", True, "Customer phone number."),
            ("customer_name", "string", False, "Customer name when known."),
            ("outcome", "string", True, "Contacted, Qualified, Human Required, Not Qualified or Lost."),
            ("summary", "string", True, "Accurate concise call summary."),
            ("next_action", "string", False, "Confirmed next action."),
        ],
    },
    "lookup_shipkia_route_rate": {
        "description": (
            "Resolve a ShipKia route zone and return that zone's verified starting rate from the "
            "active rate card using a customer-confirmed city/location pair or a Pan-India enquiry."
        ),
        "condition": (
            "Call once per customer-requested route after both endpoints are collected, or for a "
            "Pan-India/All-India starting-rate enquiry."
        ),
        "parameters": [
            ("pickup_location", "string", False, "Customer-confirmed pickup city or locality."),
            ("delivery_location", "string", False, "Customer-confirmed delivery city or locality."),
            (
                "pan_india",
                "boolean",
                False,
                "True only when the customer asks for Pan-India or All-India starting rates.",
            ),
        ],
    },
    "get_shipkia_starting_rate": {
        "description": (
            "Return the current rate-card starting rate for one approved Zone A-F, optionally "
            "filtered to a customer-requested courier. It never returns a generic or fallback "
            "amount."
        ),
        "condition": (
            "Call once for an explicit approved Zone A-F, or for a named-courier follow-up after "
            "the customer's route has resolved to a trusted zone. Route and Pan-India requests "
            "use lookup_shipkia_route_rate first."
        ),
        "parameters": [
            (
                "zone",
                "string",
                True,
                "Customer-supplied or trusted approved zone A, B, C, D, E or F.",
            ),
            (
                "courier_partner",
                "string",
                False,
                "Exact courier requested by the customer, normalized against the active rate card.",
            ),
        ],
    },
    "get_shipkia_flat_rates": {
        "description": (
            "Return the three verified GST-inclusive E-Kart Surface complete flat-rate slabs and "
            "the separately labelled Shadowfax Surface 5 KG flat additional-weight condition "
            "from active Rate Card 10. The Shadowfax condition is not a complete shipment rate."
        ),
        "condition": (
            "Call immediately for a ShipKia Voice V5 explicit flat-rate request and return all "
            "verified Prepaid slabs. Older guarded flows may use weight/payment-specific scopes."
        ),
        "parameters": [
            ("dead_weight", "number", False, "Actual shipment weight in kilograms for a matching slab."),
            ("weight_unit", "string", False, "kg by default; use g when dead_weight is in grams."),
            ("length", "number", False, "Package length in centimetres, when available."),
            ("width", "number", False, "Package width in centimetres, when available."),
            ("height", "number", False, "Package height in centimetres, when available."),
            ("payment_type", "string", False, "Prepaid by default, or COD when explicitly requested."),
            ("order_value", "number", False, "Required positive order value for a COD flat-rate request."),
            ("response_scope", "string", False, "Starting, Matching or All."),
        ],
    },
    "get_shipkia_flat_zonal_rates": {
        "description": (
            "Return the verified GST-inclusive E-Kart Express Flat-Zonal catalog from active "
            "Rate Card 10: one base group for Zones A-B, one for Zones C-F, plus the verified "
            "additional 500-gram condition."
        ),
        "condition": (
            "Call immediately only after an explicit Flat-Zonal, flat zonal, zonal-flat, or "
            "zone-wise flat-rate request. Never use it for generic Flat or Zonal rates."
        ),
        "parameters": [
            ("payment_type", "string", False, "Prepaid by default, or COD when explicitly requested."),
            ("order_value", "number", False, "Required positive order value for a COD Flat-Zonal request."),
        ],
    },
    "calculate_shipkia_rate": {
        "description": (
            "Calculate deterministic ShipKia courier rates from active Rate Card 10, including "
            "volumetric weight, COD, 18% GST and Zone A-F pricing. Never estimate rates."
        ),
        "condition": (
            "Call only when worker-gated pricing_mode is exact: positive weight, payment and every "
            "required shipment input are confirmed, with no starting-rate escape active."
        ),
        "parameters": [
            ("dead_weight", "number", True, "Actual package weight in kilograms."),
            ("weight_unit", "string", False, "kg by default; use g only when dead_weight is supplied in grams."),
            ("length", "number", False, "Package length in centimetres."),
            ("width", "number", False, "Package width in centimetres."),
            ("height", "number", False, "Package height in centimetres."),
            ("payment_type", "string", True, "Prepaid or COD."),
            ("order_value", "number", False, "COD order value when applicable."),
            ("zone", "string", False, "Approved shipping zone A, B, C, D, E or F. Omit when unknown."),
            ("movement_type", "string", False, "Forward, RTO or DTO."),
            (
                "mode",
                "string",
                False,
                (
                    "Surface, Air, Express or Fast. Express returns only services whose active "
                    "rate-card name contains Express. Fast returns only Air/Express-labelled "
                    "services and does not imply a delivery-time SLA."
                ),
            ),
            ("courier", "string", False, "Preferred courier when provided."),
            (
                "service",
                "string",
                False,
                (
                    "Exact service name from the active rate card. Never invent a service name "
                    "or put a generic express/fast request here."
                ),
            ),
        ],
    },
}


def configure_shipkia_voice(
    credential_zip: str | None = None,
    confluence_base_url: str = "http://127.0.0.1:8000",
) -> dict:
    """Idempotently configure the local ShipKia voice agent without touching WhatsApp."""
    if not frappe.conf.developer_mode:
        frappe.throw("ShipKia local voice configuration is allowed only in developer mode.")

    _validate_core_records()
    _disable_obsolete_route_tools()
    tool_names = _ensure_tools()
    token_value = _ensure_access_token()
    _configure_agent(tool_names)
    secret_paths = _write_local_secrets(
        token_value=token_value,
        credential_zip=Path(credential_zip) if credential_zip else DEFAULT_CREDENTIAL_ZIP,
        confluence_base_url=confluence_base_url,
    )
    frappe.db.commit()
    return {
        "status": "configured",
        "agent": SHIPKIA_AGENT,
        "channel": SHIPKIA_CHANNEL,
        "livekit_agent_name": SHIPKIA_AGENT_NAME,
        "primary_provider": "Gemini",
        "voice": "Puck",
        "model": "gemini-live-2.5-flash-native-audio",
        "tools": tool_names,
        "secret_directory": secret_paths["directory"],
        "env_file": secret_paths["env_file"],
        "google_credentials": secret_paths["google_credentials"],
    }


def _disable_obsolete_route_tools() -> None:
    """Keep retired customer-input tools unavailable after an in-place upgrade."""
    obsolete_name = "lookup_" + "pincode_serviceability"
    for docname in frappe.get_all(
        "AI MCP Tool",
        filters={"tool_name": obsolete_name},
        pluck="name",
    ):
        frappe.db.set_value("AI MCP Tool", docname, "enabled", 0, update_modified=False)


def _validate_core_records() -> None:
    for doctype, name in (
        ("AI Agent", SHIPKIA_AGENT),
        ("AI Channel Account", SHIPKIA_CHANNEL),
        ("AI Company", SHIPKIA_COMPANY),
    ):
        if not frappe.db.exists(doctype, name):
            frappe.throw(f"Required {doctype} {name} does not exist.")
    if not frappe.db.exists("DocType", "CRM Lead"):
        frappe.throw("CRM Lead is required for the ShipKia calling agent.")


def _ensure_tools() -> list[str]:
    tool_docnames = []
    for tool_name, spec in TOOL_SPECS.items():
        docname = frappe.db.get_value("AI MCP Tool", {"tool_name": tool_name}, "name")
        tool = frappe.get_doc("AI MCP Tool", docname) if docname else frappe.new_doc("AI MCP Tool")
        tool.enabled = 1
        tool.company = SHIPKIA_COMPANY
        tool.tool_name = tool_name
        tool.description = spec["description"]
        tool.server = None
        tool.client_doctype = None
        tool.endpoint_url = None
        tool.http_method = "POST"
        tool.set("input_parameters", [])
        for parameter_name, parameter_type, required, description in spec["parameters"]:
            tool.append(
                "input_parameters",
                {
                    "parameter_name": parameter_name,
                    "type": parameter_type,
                    "required": 1 if required else 0,
                    "description": description,
                },
            )
        tool.save(ignore_permissions=True)
        tool_docnames.append(tool.name)
    return tool_docnames


def _configure_agent(tool_docnames: list[str]) -> None:
    agent = frappe.get_doc("AI Agent", SHIPKIA_AGENT)
    if agent.allowed_channel_account != SHIPKIA_CHANNEL:
        frappe.throw(
            f"{SHIPKIA_AGENT} is attached to {agent.allowed_channel_account}, not the required {SHIPKIA_CHANNEL}."
        )

    agent.enabled = 1
    agent.primary_provider = "Gemini"
    agent.audio_name = "Puck"
    agent.enable_sales_context = 0
    # Versioned prompts are registered in code for direct Console evaluation.
    # Do not overwrite the active production prompt during setup.
    agent.set("allowed_mcp_tools", [])
    by_name = {frappe.db.get_value("AI MCP Tool", name, "tool_name"): name for name in tool_docnames}
    for tool_name in TOOL_SPECS:
        agent.append(
            "allowed_mcp_tools",
            {
                "tool": by_name[tool_name],
                "calling_condition": TOOL_SPECS[tool_name]["condition"],
            },
        )
    agent.save(ignore_permissions=True)


def get_registered_shipkia_prompts() -> dict[str, str]:
    """Expose immutable candidate prompts without mutating the production agent."""
    return dict(PROMPT_REGISTRY)


def get_approved_shipkia_sales_benefits() -> tuple[str, ...]:
    return APPROVED_SALES_BENEFITS


def _ensure_access_token() -> str:
    docname = frappe.db.get_value("AI Access Token", {"token_key": SHIPKIA_TOKEN_KEY}, "name")
    token = frappe.get_doc("AI Access Token", docname) if docname else frappe.new_doc("AI Access Token")
    token.enabled = 1
    token.company = SHIPKIA_COMPANY
    token.token_key = SHIPKIA_TOKEN_KEY
    token.token_name = "ShipKia Local Voice Worker"
    token.scope = "mcp,webhook"
    token.source_system = "shipkia-livekit-local"
    if not docname:
        token.token_secret = secrets.token_urlsafe(36)
    token.save(ignore_permissions=True)
    return f"{token.token_key}:{token.get_password('token_secret')}"


def _write_local_secrets(
    *,
    token_value: str,
    credential_zip: Path,
    confluence_base_url: str,
) -> dict:
    if not credential_zip.exists():
        frappe.throw(f"Credential ZIP was not found: {credential_zip}")

    private_dir = Path(frappe.get_site_path("private", "shipkia_livekit")).resolve()
    private_dir.mkdir(parents=True, exist_ok=True)
    credentials_path = private_dir / "google-credentials.json"
    env_path = private_dir / ".env.local"

    with zipfile.ZipFile(credential_zip) as archive:
        credential_entries = [
            name for name in archive.namelist() if name.replace("\\", "/").endswith("/creds.json")
        ]
        if len(credential_entries) != 1:
            frappe.throw("The ShipKia ZIP must contain exactly one creds.json file.")
        credential_bytes = archive.read(credential_entries[0])

    credential_data = json.loads(credential_bytes.decode("utf-8"))
    if credential_data.get("type") != "service_account" or not credential_data.get("project_id"):
        frappe.throw("The ZIP's creds.json is not a valid Google service-account credential.")
    credentials_path.write_bytes(credential_bytes)

    channel = frappe.get_doc("AI Channel Account", SHIPKIA_CHANNEL)
    if "shipkia-voice-sales" not in (channel.endpoint_paths_json or ""):
        frappe.throw("channel-446 does not dispatch to shipkia-voice-sales.")

    base_url = confluence_base_url.rstrip("/")
    env_values = {
        "LIVEKIT_URL": channel.base_url,
        "LIVEKIT_API_KEY": channel.get_password("api_key"),
        "LIVEKIT_API_SECRET": channel.get_password("api_secret"),
        "LIVEKIT_AGENT_NAME": SHIPKIA_AGENT_NAME,
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path),
        "GOOGLE_CLOUD_PROJECT": credential_data["project_id"],
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "GEMINI_LIVE_MODEL": "gemini-live-2.5-flash-native-audio",
        "GEMINI_LIVE_VOICE": "Puck",
        "GEMINI_TEMPERATURE": "0.35",
        "GEMINI_VAD_PREFIX_PADDING_MS": "300",
        "GEMINI_VAD_SILENCE_DURATION_MS": "700",
        "LIVEKIT_INTERRUPTION_MIN_DURATION_SECONDS": "0.7",
        "LIVEKIT_FALSE_INTERRUPTION_TIMEOUT_SECONDS": "2.0",
        "LIVEKIT_FALSE_INTERRUPTION_RECOVERY_SECONDS": "2.5",
        "CONFLUENCE_BASE_URL": base_url,
        "MCP_SERVER_URL": f"{base_url}/api/method/confluence_ai.api.mcp.gateway",
        "MCP_BEARER_TOKEN": token_value,
        "MCP_AUTH_HEADER": "X-MCP-Token",
        "CONFLUENCE_LIVEKIT_WEBHOOK_URL": (
            f"{base_url}/api/method/confluence_ai.api.webhook.receive_livekit"
        ),
        "LIVEKIT_AGENT_MAX_CALL_SECONDS": "900",
        "LOG_LEVEL": "INFO",
    }
    env_path.write_text(
        "\n".join(f"{key}={_shell_quote(value)}" for key, value in env_values.items()) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(credentials_path, 0o600)
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    return {
        "directory": str(private_dir),
        "env_file": str(env_path),
        "google_credentials": str(credentials_path),
    }


def _shell_quote(value: object) -> str:
    text = str(value or "")
    return "'" + text.replace("'", "'\"'\"'") + "'"
