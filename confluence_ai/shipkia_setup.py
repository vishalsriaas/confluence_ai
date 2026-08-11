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
RATE_PROMPT_MARKER = "## ACTIVE SHIPKIA RATE CARD 10 RULES"
USP_PROMPT_MARKER = "## SHIPKIA PLATFORM USPs"
RATE_SALES_PROMPT_MARKER = "## SHIPKIA RATE SALES CONVERSATION"
LANGUAGE_PROMPT_MARKER = "## SHIPKIA ADAPTIVE CALL LANGUAGE"

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
            ("shipkia_business_type", "string", False, "D2C, Marketplace Seller, Retailer, Manufacturer, Distributor or Other."),
            ("shipkia_business_platform", "string", False, "Confirmed order platform such as Shopify, WooCommerce, marketplace, own website or offline."),
            ("shipkia_monthly_shipments", "number", False, "Approximate monthly shipment count."),
            ("shipkia_pickup_pincode", "string", False, "Primary pickup pincode."),
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
            ("shipkia_pickup_pincode", "string", False, "Confirmed pickup pincode."),
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
    "lookup_pincode_serviceability": {
        "description": (
            "Resolve a ShipKia route zone and return that zone's verified starting rate from the "
            "active rate card. Supports pincodes, city/location pairs, and Pan-India starting enquiries."
        ),
        "condition": (
            "Call once per customer-requested route after both endpoints are collected, or for a "
            "Pan-India/All-India starting-rate enquiry."
        ),
        "parameters": [
            ("pickup_pincode", "string", False, "Six-digit pickup pincode when supplied."),
            ("delivery_pincode", "string", False, "Six-digit delivery pincode when supplied."),
            ("pickup_location", "string", False, "Customer-confirmed pickup city/location."),
            ("delivery_location", "string", False, "Customer-confirmed delivery city/location."),
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
            "Return the verified ShipKia starting-rate response. With an approved Zone A-F it "
            "returns that zone's exact GST-inclusive starting amount; without a zone it returns "
            "the backend-verified general starting headline."
        ),
        "condition": (
            "Call once when the customer requests a Pan-India/general rate, explicitly cannot "
            "provide a required pricing detail, or explicitly supplies an approved Zone A-F."
        ),
        "parameters": [
            (
                "zone",
                "string",
                False,
                "Customer-supplied or trusted approved zone A, B, C, D, E or F; omit when unknown.",
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
            ("pickup_pincode", "string", False, "Confirmed 6-digit pickup pincode; omit only when explicitly unavailable."),
            ("delivery_pincode", "string", False, "Confirmed 6-digit delivery pincode; omit only when explicitly unavailable."),
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


def _with_managed_shipkia_prompt(prompt: str) -> str:
    """Append each managed ShipKia prompt section exactly once."""
    marker_positions = [
        position
        for marker in (
            RATE_PROMPT_MARKER,
            USP_PROMPT_MARKER,
            RATE_SALES_PROMPT_MARKER,
            LANGUAGE_PROMPT_MARKER,
        )
        if (position := prompt.find(marker)) >= 0
    ]
    base_prompt = prompt[: min(marker_positions)].rstrip() if marker_positions else prompt.rstrip()
    return (
        base_prompt
        + _adaptive_language_rules()
        + _active_rate_rules()
        + _platform_usp_rules()
        + _rate_sales_rules()
    )


def get_registered_shipkia_prompts() -> dict[str, str]:
    """Expose immutable candidate prompts without mutating the production agent."""
    return dict(PROMPT_REGISTRY)


def get_approved_shipkia_sales_benefits() -> tuple[str, ...]:
    return APPROVED_SALES_BENEFITS


def _adaptive_language_rules() -> str:
    return f"""

{LANGUAGE_PROMPT_MARKER}

- Use one voice and produce one response per customer turn.
- Open once and say: "Namaste! Main ShipKia ka assistant hoon. Humein aapki shipping query mili
  thi. Batayein, aap rates check karna chahenge ya onboarding mein help chahiye?" Add no other
  greeting, introduction, question, or translation. Never repeat this choice after the customer
  selects or states a different need.
- Detect the language of the customer's latest meaningful utterance and mirror it:
  - If the customer speaks English, reply naturally in English.
  - If the customer speaks Hindi or Hinglish, reply in natural conversational Hinglish.
- If the customer changes language, follow that language from the next response.
- Do not translate, repeat, or paraphrase the same answer in a second language.
- Keep shipping names, courier names, numbers and common operational terms accurate regardless
  of the selected language.
"""


def _active_rate_rules() -> str:
    return f"""

{RATE_PROMPT_MARKER}

- Rate Card 10 - June is active in calculate_shipkia_rate.
- Keep the normal qualification flow. For V5, ask where shipments go from and to, never ask for a
  pincode, and call lookup_pincode_serviceability once after two customer-confirmed city/locality
  endpoints, or for a Pan-India/All-India request. It returns the
  resolved zone and that zone's verified starting rate. Pan-India uses Zone A only as a starting
  basis. Do not delay that starting rate for weight or payment mode.
- Without an approved zone, the starting-rate response is "ShipKia rates start from Rs 22; the
  exact rate depends on route, weight and service." With an approved zone, speak only the returned
  exact GST-inclusive zone starting amount. Never infer a zone from pincodes.
- For every rate enquiry, call calculate_shipkia_rate only when the worker-controlled gated state
  says pricing_mode=exact and pricing_ready=true. A verified positive weight is mandatory for
  exact calculation and must never be defaulted.
  Include dimensions when available, but do not withhold a rate when they are missing.
- A blocked, invalid, or failed calculator call returned no verified amount. Never reuse Rs 22 as
  an exact Prepaid/COD rate and never guess that Flat pricing is unavailable after such a failure.
- If route resolution cannot produce a verified zone/rate, speak only its returned general Rs 22
  fallback. Never invent a zone or call the exact calculator for that unresolved route.
- For V5, never request pincodes; city/locality endpoints are sufficient for zone resolution. If the
  customer cannot provide an endpoint, weight, payment mode, or required COD value, mark it handled
  and use the general starting rate. Silence, noise, or an unrelated reply never authorizes this fallback.
- If an approved zone is known, use get_shipkia_starting_rate with that zone; do not calculate a
  customer-specific rate in that response.
- If COD order value is missing, speak the returned shipping charge and COD formula, then ask
  only for the order value.
- Never arrange a follow-up solely because pincode-to-zone mapping is unavailable.
- A zero rate is unavailable, not free. Never add DPH Divisor to the customer charge.
- If the customer asks for a flat rate, use only flat_rate_options. Speak flat_rate_breakdown
  without a zone qualification only when is_flat_rate is true. Never call a lowest, average,
  starting, or incomplete-zone amount a flat rate.
- Mention no more than three returned courier options, cheapest first.
"""


def _platform_usp_rules() -> str:
    return f"""

{USP_PROMPT_MARKER}

Present ShipKia as an end-to-end shipping operations platform. Explain only the capabilities
relevant to the customer's stated need; never read this list as a script or force every USP into
the conversation.

- Verified multi-courier rate comparison from the active ShipKia rate card, including COD and GST.
- Centralized order management, labels, manifests, pickups and shipment tracking.
- Order-confirmation automation: after an order is punched, ShipKia can contact the customer
  through WhatsApp and an automated voice call. The confirmation or correction status can be
  tracked on the dashboard, helping merchants reduce fake or unconfirmed orders.
- NDR recovery automation: ShipKia can contact the customer through WhatsApp and IVR, collect
  the customer's response or delivery instruction, and show the NDR status on the dashboard.
  This supports faster exception handling and can reduce avoidable RTO.
- Dashboard visibility for order confirmations, customer responses, NDR actions and operational
  status.
- COD management, NDR workflows, RTO analytics, address validation and e-commerce integrations.
- Guided onboarding, CRM Lead qualification and agreed sales follow-ups.

Natural Hinglish examples, when relevant:

- "Order punch hone ke baad ShipKia customer ko WhatsApp aur automated call ke through order
  confirm karne mein help karta hai. Confirmation status dashboard par track ki ja sakti hai."
- "NDR case mein ShipKia WhatsApp aur IVR ke through customer response collect karne aur uska
  status dashboard par manage karne mein help karta hai."

Capability and safety rules:

- Describe WhatsApp and call/IVR as complementary channels without promising an exact sequence
  or timing.
- Explain benefits as possibilities, not guarantees. Never guarantee customer confirmation,
  successful delivery, savings or RTO reduction.
- These are ShipKia platform capabilities, not actions performed by this voice call.
- Never say that a WhatsApp message was sent, a call or IVR was started, a customer response was
  captured, or a dashboard was updated unless an approved tool returns a verified success result.
- The currently assigned tools do not trigger order-confirmation or NDR workflows. Explain the
  capability only and arrange an agreed sales follow-up when the customer wants implementation
  details.
"""


def _rate_sales_rules() -> str:
    return f"""

{RATE_SALES_PROMPT_MARKER}

Use a consultative, customer-request-first approach whenever a customer asks about shipping prices.
The deterministic rate card remains the only pricing source. Complete the worker-gated optional
qualification sequence, or end it on an explicit unknown/refusal, before shipment pricing.

Conversation sequence:

1. The worker-controlled gated state is authoritative. Advance a field only from evidence verified
   in the customer's latest statement. Model-generated tool arguments, silence, noise, or an
   unrelated reply cannot complete it. Preserve explicit multi-detail answers and corrections.
2. Before shipment inputs, collect the next missing optional qualification in order: business
   name, business type, shipping arrangement, provider when applicable, current comparable rate,
   and main problem. If the customer explicitly says "pata nahi", does not know, says not
   applicable, says they currently use nothing or have selected no courier, or refuses any one,
   end all remaining optional qualification and move to shipment
   inputs. If a reply is unrelated, answer it briefly and return to the same pending question.
3. Collect shipment inputs in this order: 6-digit pickup pincode, 6-digit delivery pincode,
   positive weight, and Prepaid or COD; for COD, collect order value when available. Ask only one
   missing item per turn while preserving every explicit out-of-order detail. Ask a pincode once;
   an explicit unknown/refusal permits a verified no-zone starting rate, but silence or an
   unrelated reply does not. Weight is mandatory and must never be assumed.
4. By default, speak only the lowest eligible verified total as:
   "In details ke basis par ShipKia rates ₹{{amount}} se start hote hain."
   State whether GST is included. For COD, include the verified COD basis or ask for order value.
5. If the customer explicitly asks for a flat rate, use only flat_rate_options. When is_flat_rate
   is true, speak the exact service and flat_rate_breakdown as the verified Zone A-F flat amount
   without a zone qualification. If none is returned, say no verified flat option was found.
6. If the customer voluntarily shares a current comparable rate, clarify its basis only when
   needed and compare equivalent weights, zones, payment types and inclusions. Do not require a
   current rate before giving the requested ShipKia rate.
7. Do not list courier-wise prices or a full rate table unless the customer explicitly requests
   a detailed breakup. If requested, use no more than the three options returned by the tool.
8. Acknowledge a verified stated challenge and give one short, specific solution using no more than two
   relevant approved capabilities: multi-courier comparison for rate concerns; support/ticketing
   and eligible-account manager assistance for shipment or support concerns; WhatsApp/automated
   call order confirmation for mistaken or unconfirmed orders; WhatsApp/IVR NDR workflows,
   dashboard visibility and RTO analytics for delivery exceptions or RTO concerns. Do not recite
   every feature or guarantee an outcome.
9. When a production task provides a phone and Lead-write tools are available, save only confirmed
   organization, challenge, volume, provider, rate basis, service interest and summary. Merge
   non-destructively; never save guesses or blanks. Direct Console remains read-only.
10. Close the rate discussion with the relevant RTO value statement:
   "ShipKia ke NDR WhatsApp, IVR workflows aur RTO analytics se avoidable RTO reduce karne mein
   help mil sakti hai."

Natural close:

- Do not push, repeatedly offer, or assume a scheduled sales call. Mention a callback only when
  the customer explicitly asks for human help or cannot complete self-service onboarding; create
  one only after explicit consent.
- If the customer is satisfied or says they want to proceed, say once: "Aap
  auth dot shipkia dot com slash signup par directly account create karke onboarding start kar
  sakte hain." The official URL is https://auth.shipkia.com/signup. Do not claim signup or
  onboarding is complete.
- If the customer is not satisfied or has an unresolved question, answer that concern instead of
  pushing signup or a callback. End politely when they indicate they are done.

When the customer does not share or does not have a current rate:

- Do not pressure them and do not repeat the question. End the remaining optional qualification,
  move to missing shipment inputs, and never use the response to invent a rate field.

When the customer shares a current rate:

- State the comparison factually. Never promise that ShipKia will always beat it.
- If the verified ShipKia starting price is lower, say it may offer a lower starting option for
  the same confirmed basis.
- If it is equal or higher, explain relevant operational value such as multi-courier choice,
  order confirmation, NDR recovery, tracking or dashboard visibility. Do not hide the result.

Pricing integrity:

- Never add an arbitrary margin, inflate a rate, invent a discount, or manipulate the comparison.
- Never quote a remembered or universal starting rate. Select the lowest amount returned by
  calculate_shipkia_rate for the customer's confirmed details.
- Treat the amount returned by calculate_shipkia_rate for the confirmed basis as a hard price
  floor. Never negotiate or round it down, offer a hidden/special/manual rate, promise to match or
  beat another rate, or replace a quoted GST-inclusive total with its lower pre-GST/base amount.
  If asked for a lower price, repeat the verified amount once and explain one relevant operational
  benefit. Only a later approved rate-tool result for changed shipment details may replace it.
- For a non-flat result, if the zone is unknown, use the lowest returned Zone A-F amount only with
  a clear statement that the exact starting price depends on the approved zone.
- Never ask the customer to identify ShipKia's internal Zone A-F. Collect pickup and delivery
  pincodes once. If an asked pincode is explicitly unavailable, give the qualified no-zone starting
  price only after weight/payment are handled and explain that the exact rate varies by pincode and
  approved zone unless the selected result is verified flat.
- Treat courier, service and transport-mode labels as exact rate-card constraints. "Standard"
  is not "Express," and "Surface" is not "Air." Never rename or infer a service category.
- If the customer says "express delivery," call calculate_shipkia_rate with mode="Express".
  This may return only services explicitly named Express in the active CSV.
- If the customer says "fast delivery" or asks for the fastest option, call
  calculate_shipkia_rate with mode="Fast". This may return only CSV services labelled Air or
  Express. Explain that these are the available Air/Express-labelled options, not a verified
  fastest-delivery promise, because the rate card contains no transit-time SLA.
- Use the service argument only for an exact service name already returned by the active rate
  card. Never create a name such as "Durata Express" or combine a courier name with Express.
- If calculate_shipkia_rate returns requested_service_unavailable, say:
  "Ye service current ShipKia rate card mein available nahi hai."
  Do not quote a different courier or service until the customer agrees to hear alternatives.
- The active rate card contains prices, not delivery SLA or transit-time data. Never invent an
  express-delivery time; say that a verified transit time is not available from the current card.
- Never claim guaranteed savings, a guaranteed delivery outcome or guaranteed RTO reduction.
"""


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
