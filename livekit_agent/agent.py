from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatMessage, function_tool
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import google, silero

from session_runtime import VoiceSessionRuntime


load_dotenv(os.getenv("SHIPKIA_ENV_FILE", ".env.local"), override=False)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("shipkia-livekit-agent")

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "shipkia-voice-sales")
MCP_GATEWAY = os.getenv("MCP_SERVER_URL", "").strip()
CONFLUENCE_CALLBACK = os.getenv("CONFLUENCE_LIVEKIT_WEBHOOK_URL", "").strip()
CONSOLE_MCP_SCOPE = "livekit-console-sandbox"

ALLOWED_TOOLS = {
    "lookup_shipkia_crm_lead",
    "create_or_update_shipkia_lead",
    "record_shipkia_call_progress",
    "create_shipkia_followup",
    "finalize_shipkia_call_outcome",
    "lookup_pincode_serviceability",
    "calculate_shipkia_rate",
}

_TOOL_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_SAME_CALL_MEMORY_MAX_TURNS = 40
_SAME_CALL_MEMORY_MAX_CHARS = 12000

_RATE_QUALIFICATION_PROPERTIES = {
    "business_name": {
        "type": "string",
        "description": (
            "Exact business or brand name stated by the customer, or Unknown/Not Applicable. "
            "Never invent or normalize it."
        ),
    },
    "business_type": {
        "type": "string",
        "description": (
            "Business type stated by the customer, or Unknown/Not Applicable. Never invent it."
        ),
    },
    "current_shipping_arrangement": {
        "type": "string",
        "enum": [
            "Direct Courier",
            "Shipping Aggregator",
            "Own Arrangement",
            "Other",
            "Unknown",
            "Not Applicable",
        ],
        "description": "Customer's stated current shipping arrangement.",
    },
    "current_provider_name": {
        "type": "string",
        "description": (
            "Exact courier or aggregator name when the arrangement is Direct Courier or Shipping "
            "Aggregator, or Unknown/Not Applicable. Never invent or normalize it."
        ),
    },
    "current_rate_status": {
        "type": "string",
        "enum": ["Shared", "Unknown", "Not Applicable", "Not Shared"],
        "description": (
            "Shared when the customer gave a comparable current rate; otherwise use the exact "
            "applicable status. Not Shared means the customer explicitly refused."
        ),
    },
    "current_shipping_rate": {
        "type": "number",
        "description": "Numeric current shipping rate, required only when current_rate_status is Shared.",
    },
    "current_rate_basis": {
        "type": "string",
        "description": "Any customer-stated weight, route, payment, or GST basis for the current rate.",
    },
    "current_problem": {
        "type": "string",
        "description": (
            "Customer's stated shipping problem, No Problem, Unknown, or Not Applicable. "
            "Use Not Shared only for an explicit refusal."
        ),
    },
    "qualification_refused_field": {
        "type": "string",
        "enum": [
            "business_name",
            "business_type",
            "current_shipping_arrangement",
            "current_provider_name",
            "current_shipping_rate",
            "current_problem",
        ],
        "description": (
            "Set only when the customer explicitly refuses this optional qualification question. "
            "That refusal ends the remaining optional qualification sequence."
        ),
    },
}
_RATE_CONTROL_PROPERTIES = {
    "rate_request_type": {
        "type": "string",
        "enum": ["Normal", "Flat"],
        "description": (
            "Use Flat when the customer asks for flat rates. Use Normal for ordinary courier or "
            "non-flat rates. This controls the response view and is not a courier service name."
        ),
    },
    "normal_rates_explicitly_requested": {
        "type": "boolean",
        "description": (
            "Set true only when the customer explicitly asks to leave a flat-rate discussion and "
            "hear normal courier rates. Never set it merely because a flat service is selected."
        ),
    },
    "flat_response_scope": {
        "type": "string",
        "enum": ["Best", "More Options", "Selected Service"],
        "description": (
            "Use Best for the first generic flat-rate request, More Options only when the customer "
            "asks for alternatives, and Selected Service when they name or choose one service. "
            "This controls only the voice response and is never forwarded to pricing."
        ),
    },
    "pickup_location_changed": {
        "type": "boolean",
        "description": (
            "Set true when the customer changes the pickup city or location but has not supplied "
            "its new 6-digit pincode. The voice worker will stop reuse of the old pickup pincode."
        ),
    },
    "delivery_location_changed": {
        "type": "boolean",
        "description": (
            "Set true when the customer changes the delivery city or location but has not supplied "
            "its new 6-digit pincode. The voice worker will stop reuse of the old delivery pincode."
        ),
    },
    "monthly_shipment_volume": {
        "type": "number",
        "description": (
            "Use only when the customer volunteers this value. Never ask for it. It is ignored by "
            "rate calculation and never forwarded to the pricing backend."
        ),
    },
    "order_value_status": {
        "type": "string",
        "enum": ["Shared", "Not Shared"],
        "description": (
            "Use Shared with a numeric order_value. Use Not Shared only when a customer who selected "
            "COD explicitly refuses to share order value; the voice worker will return a clearly "
            "labelled Prepaid-basis fallback."
        ),
    },
}
_RATE_LOCAL_PROPERTIES = {
    **_RATE_QUALIFICATION_PROPERTIES,
    **_RATE_CONTROL_PROPERTIES,
}
_RATE_LOCAL_ARGUMENTS = frozenset(_RATE_LOCAL_PROPERTIES)
_RATE_REQUEST_ONLY_ARGUMENTS = frozenset(
    {
        "courier",
        "service",
        "mode",
        "rate_request_type",
        "normal_rates_explicitly_requested",
        "flat_response_scope",
        "pickup_location_changed",
        "delivery_location_changed",
        "monthly_shipment_volume",
    }
)
_FLAT_RATE_SERVICE_ALIASES = {
    "flat",
    "flat rate",
    "flat rates",
    "flat rate options",
    "flat_rate_options",
    "flat additional rate options",
    "flat_additional_rate_options",
}
_PROVIDER_ARRANGEMENTS = {"direct courier", "shipping aggregator"}
_REFUSAL_VALUES = {
    "not shared",
    "refused",
    "prefer not to share",
    "do not want to share",
    "don't want to share",
}
_PAYMENT_REFUSAL_VALUES = _REFUSAL_VALUES | {"unknown", "not applicable"}
_BOTH_PAYMENT_VALUES = {
    "both",
    "dono",
    "prepaid and cod",
    "prepaid aur cod",
    "cod and prepaid",
    "cod aur prepaid",
}
_RATE_FIELD_LABELS = {
    "business_name": "business or brand name",
    "business_type": "business type",
    "current_shipping_arrangement": "current shipping arrangement",
    "current_provider_name": "current courier or aggregator name",
    "current_shipping_rate": "current comparable shipping rate",
    "current_problem": "main problem with the current shipping arrangement",
    "pickup_pincode": "6-digit pickup pincode",
    "delivery_pincode": "6-digit delivery pincode",
    "dead_weight": "shipment weight",
    "payment_type": "Prepaid or COD payment mode",
    "order_value": "COD order value",
}
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
_HINGLISH_MARKERS = {
    "aap",
    "abhi",
    "achha",
    "achhe",
    "agar",
    "aur",
    "bata",
    "batao",
    "bataye",
    "batayen",
    "bhai",
    "chahiye",
    "chahenge",
    "hai",
    "hain",
    "ho",
    "hoga",
    "ka",
    "kaha",
    "kar",
    "karo",
    "ke",
    "kya",
    "liye",
    "main",
    "mein",
    "mera",
    "mujhe",
    "nahi",
    "nh",
    "par",
    "pe",
    "phir",
    "raha",
    "rahe",
    "se",
    "sirf",
    "thik",
    "wala",
    "ye",
    "ya",
}
_LANGUAGE_NEUTRAL_TOKENS = {
    "yes",
    "yeah",
    "yep",
    "no",
    "okay",
    "ok",
    "fine",
    "sure",
    "correct",
    "right",
    "thanks",
    "thank",
    "you",
    "prepaid",
    "cod",
    "kg",
    "g",
}


def load_local_console_prompt() -> tuple[str, str]:
    """Load the current versioned prompt for direct LiveKit Console sessions."""
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "confluence_ai"
        / "prompts"
        / "shipkia_voice.py"
    )
    spec = importlib.util.spec_from_file_location("shipkia_voice_console_prompt", prompt_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load the local ShipKia prompt from {prompt_path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompt_version = str(module.SHIPKIA_VOICE_PROMPT_VERSION)
    return str(module.get_shipkia_voice_prompt(prompt_version)), prompt_version


def build_headers(task_id: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if token:
        headers[os.getenv("MCP_AUTH_HEADER", "X-MCP-Token")] = token
    if task_id:
        headers["X-Confluence-Task-ID"] = task_id
    return headers


def unwrap_frappe_response(payload: dict) -> dict:
    message = payload.get("message")
    return message if isinstance(message, dict) else payload


def parse_dispatch_metadata(ctx: JobContext) -> dict[str, Any]:
    candidates = (
        getattr(ctx.room, "metadata", None),
        getattr(getattr(ctx.job, "room", None), "metadata", None),
        getattr(ctx.job, "metadata", None),
    )
    for raw in candidates:
        if isinstance(raw, dict):
            return raw
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def compact_json(value: Any, max_chars: int = 3500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "...TRUNCATED"


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _response_language_for_turn(text: object, current_language: str) -> str:
    clean = _normalized_text(text)
    if not clean:
        return current_language

    if clean == "english" or re.search(
        r"\b(?:speak|reply|talk|continue|answer)\s+(?:to me\s+)?in\s+english\b"
        r"|\benglish\s+(?:please|only|me|mein)\b",
        clean,
    ):
        return "English"
    if clean in {"hindi", "hinglish"} or re.search(
        r"\b(?:hindi|hinglish)\s+(?:mein|me|please|only)\b"
        r"|\b(?:speak|reply|talk|continue|answer)\s+(?:to me\s+)?in\s+(?:hindi|hinglish)\b",
        clean,
    ):
        return "Hinglish"
    if _DEVANAGARI_RE.search(str(text or "")):
        return "Hinglish"

    tokens = _WORD_RE.findall(clean)
    if not tokens or set(tokens).issubset(_LANGUAGE_NEUTRAL_TOKENS):
        return current_language
    if any(token in _HINGLISH_MARKERS for token in tokens):
        return "Hinglish"
    if len(tokens) >= 2:
        return "English"
    return current_language


def _normal_rates_declined(text: object, previous_agent_text: object = "") -> bool:
    clean = _normalized_text(text)
    if not clean:
        return False
    negative_phrases = (
        "no",
        "no thanks",
        "not interested",
        "do not want",
        "don't want",
        "nahi",
        "nahin",
        "nhi",
        "mat batao",
        "rehne do",
    )
    mentions_normal_rates = "normal" in clean and "rate" in clean
    has_negative = any(
        re.search(rf"\b{re.escape(phrase)}\b", clean)
        for phrase in negative_phrases
    )
    if mentions_normal_rates and has_negative:
        return True
    previous = _normalized_text(previous_agent_text)
    return (
        clean in negative_phrases
        and "normal" in previous
        and "rate" in previous
    )


def _normal_rates_explicitly_requested(text: object) -> bool:
    clean = _normalized_text(text)
    if "normal" not in clean or "rate" not in clean:
        return False
    return not _normal_rates_declined(clean)


def _multiple_service_selection(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw or "," not in raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _has_value(arguments: dict[str, object], field: str) -> bool:
    value = arguments.get(field)
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _merge_remembered_rate_arguments(
    remembered: dict[str, object],
    current: dict[str, object],
    backend_argument_names: frozenset[str],
) -> dict[str, object]:
    rememberable = (
        backend_argument_names | _RATE_LOCAL_ARGUMENTS
    ) - _RATE_REQUEST_ONLY_ARGUMENTS

    for changed_field, pincode_field in (
        ("pickup_location_changed", "pickup_pincode"),
        ("delivery_location_changed", "delivery_pincode"),
    ):
        if current.get(changed_field) is True and not _has_value(current, pincode_field):
            remembered.pop(pincode_field, None)
            remembered.pop("zone", None)

    route_changed = any(
        field in current
        and _has_value(current, field)
        and remembered.get(field) != current[field]
        for field in ("pickup_pincode", "delivery_pincode")
    )
    if route_changed and "zone" not in current:
        remembered.pop("zone", None)

    payment_changed = (
        _has_value(current, "payment_type")
        and _has_value(remembered, "payment_type")
        and _normalized_text(current["payment_type"])
        != _normalized_text(remembered["payment_type"])
    )
    if payment_changed:
        remembered.pop("order_value", None)
        remembered.pop("order_value_status", None)
    if _has_value(current, "order_value"):
        remembered.pop("order_value_status", None)
    elif _normalized_text(current.get("order_value_status")) == "not shared":
        remembered.pop("order_value", None)

    supplied = {
        key: value
        for key, value in current.items()
        if _has_value(current, key)
    }
    for key, value in supplied.items():
        if key in rememberable:
            remembered[key] = copy.deepcopy(value)

    merged = copy.deepcopy(remembered)
    merged.update(supplied)
    return merged


def _normalize_rate_request_arguments(
    arguments: dict[str, object],
) -> tuple[dict[str, object], str]:
    normalized = dict(arguments)
    requested_type = _normalized_text(normalized.get("rate_request_type"))
    service_alias = _normalized_text(normalized.get("service"))
    is_flat = requested_type == "flat" or service_alias in _FLAT_RATE_SERVICE_ALIASES
    rate_request_type = "Flat" if is_flat else "Normal"
    normalized["rate_request_type"] = rate_request_type
    if service_alias in _FLAT_RATE_SERVICE_ALIASES:
        normalized.pop("service", None)
    return normalized, rate_request_type


def _is_refusal_value(value: object) -> bool:
    return _normalized_text(value) in _REFUSAL_VALUES


def _rate_gate_response(status: str, field: str, message: str) -> dict[str, object]:
    return {
        "status": status,
        "next_missing_field": field,
        "next_question": _RATE_FIELD_LABELS[field],
        "message": message,
        "missing_field_recovery": (
            "First inspect the same-call transcript and confirmed call context. If the customer "
            "already answered this field, do not ask it again: silently call "
            "calculate_shipkia_rate with that answer so it is checkpointed. Ask the customer once "
            "only when the field was genuinely never answered or remains unclear."
        ),
        "repeat_question_prohibited_if_answered": True,
    }


def _qualification_sequence(arguments: dict[str, object]) -> list[str]:
    sequence = ["business_name", "business_type", "current_shipping_arrangement"]
    if _normalized_text(arguments.get("current_shipping_arrangement")) in _PROVIDER_ARRANGEMENTS:
        sequence.append("current_provider_name")
    sequence.extend(["current_shipping_rate", "current_problem"])
    return sequence


def _prepare_rate_arguments(
    raw_arguments: dict[str, object],
    backend_argument_names: frozenset[str],
) -> tuple[dict[str, object] | None, dict[str, object], dict[str, object] | None]:
    arguments = dict(raw_arguments or {})
    unknown = sorted(set(arguments) - backend_argument_names - _RATE_LOCAL_ARGUMENTS)
    if unknown:
        return (
            None,
            {},
            {
                "status": "invalid_arguments",
                "invalid_fields": unknown,
                "message": (
                    "Remove the unsupported fields and call calculate_shipkia_rate once with only "
                    "the registered arguments. Do not ask the customer to repeat known details."
                ),
            },
        )

    refused_field = str(arguments.get("qualification_refused_field") or "").strip()
    refusal_reached = False
    for field in _qualification_sequence(arguments):
        if refused_field == field:
            refusal_reached = True
            break

        if field == "current_shipping_rate":
            rate_status = _normalized_text(arguments.get("current_rate_status"))
            if not rate_status:
                return (
                    None,
                    {},
                    _rate_gate_response(
                        "qualification_required",
                        field,
                        "The current comparable shipping-rate status is not present in local call state.",
                    ),
                )
            if rate_status == "shared" and not _has_value(arguments, "current_shipping_rate"):
                return (
                    None,
                    {},
                    _rate_gate_response(
                        "qualification_required",
                        field,
                        "The customer said the rate is shared, but its numeric amount is missing.",
                    ),
                )
            if rate_status == "not shared":
                refusal_reached = True
                break
            continue

        if not _has_value(arguments, field):
            return (
                None,
                {},
                _rate_gate_response(
                    "qualification_required",
                    field,
                    f"The customer's {_RATE_FIELD_LABELS[field]} is not present in local call state.",
                ),
            )
        if _is_refusal_value(arguments[field]):
            refusal_reached = True
            break

    if refused_field and not refusal_reached:
        return (
            None,
            {},
            {
                "status": "invalid_qualification_state",
                "message": (
                    "qualification_refused_field does not match the next applicable qualification "
                    "question. Preserve known answers and continue from the actual missing field."
                ),
            },
        )

    for field in ("pickup_pincode", "delivery_pincode", "dead_weight"):
        if not _has_value(arguments, field):
            return (
                None,
                {},
                _rate_gate_response(
                    "shipment_details_required",
                    field,
                    f"The customer's {_RATE_FIELD_LABELS[field]} is not present in local call state.",
                ),
            )

    for field in ("pickup_pincode", "delivery_pincode"):
        if not re.fullmatch(r"\d{6}", str(arguments[field]).strip()):
            return (
                None,
                {},
                _rate_gate_response(
                    "shipment_details_required",
                    field,
                    f"A valid {_RATE_FIELD_LABELS[field]} is required.",
                ),
            )

    try:
        if float(arguments["dead_weight"]) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return (
            None,
            {},
            _rate_gate_response(
                "shipment_details_required",
                "dead_weight",
                "A valid shipment weight greater than zero is required.",
            ),
        )

    payment_type = _normalized_text(arguments.get("payment_type"))
    if not payment_type:
        return (
            None,
            {},
            _rate_gate_response(
                "shipment_details_required",
                "payment_type",
                "Ask once whether the shipment is Prepaid or COD.",
            ),
        )

    payment_defaulted = payment_type in _PAYMENT_REFUSAL_VALUES
    payment_selected_both = payment_type in _BOTH_PAYMENT_VALUES
    payment_is_cod = payment_type in {"cod", "cash on delivery"}
    if (
        payment_type not in {"prepaid", "pre-paid", "paid", "cod", "cash on delivery"}
        and not payment_defaulted
        and not payment_selected_both
    ):
        return (
            None,
            {},
            _rate_gate_response(
                "shipment_details_required",
                "payment_type",
                "The payment mode must be Prepaid, COD, Both, or an explicit refusal.",
            ),
        )

    order_value_status = _normalized_text(arguments.get("order_value_status"))
    if order_value_status and order_value_status not in {"shared", "not shared"}:
        return (
            None,
            {},
            {
                "status": "invalid_arguments",
                "invalid_fields": ["order_value_status"],
                "message": "order_value_status must be Shared or Not Shared.",
            },
        )

    if _has_value(arguments, "order_value"):
        try:
            if float(arguments["order_value"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            return (
                None,
                {},
                _rate_gate_response(
                    "shipment_details_required",
                    "order_value",
                    "A valid COD order value of zero or more is required.",
                ),
            )

    cod_order_value_refused = (
        payment_is_cod
        and order_value_status == "not shared"
        and not _has_value(arguments, "order_value")
    )
    if (
        payment_is_cod
        and not _has_value(arguments, "order_value")
        and not cod_order_value_refused
    ):
        return (
            None,
            {},
            _rate_gate_response(
                "shipment_details_required",
                "order_value",
                (
                    "COD is selected, but order value is not present in local call state. Ask only "
                    "for the COD order value. Do not quote a rate, say the route is unavailable, or "
                    "offer other rates before this is handled."
                ),
            ),
        )

    forwarded = {
        key: value
        for key, value in arguments.items()
        if key in backend_argument_names
    }
    payment_basis_reason = ""
    if payment_defaulted:
        payment_basis_reason = "payment_type_refused"
        forwarded["payment_type"] = "Prepaid"
        forwarded.pop("order_value", None)
    elif payment_selected_both:
        payment_basis_reason = "both_selected"
        forwarded["payment_type"] = "Prepaid"
        forwarded.pop("order_value", None)
    elif cod_order_value_refused:
        payment_basis_reason = "cod_order_value_refused"
        forwarded["payment_type"] = "Prepaid"
        forwarded.pop("order_value", None)
    elif payment_is_cod:
        forwarded["payment_type"] = "COD"
    else:
        forwarded["payment_type"] = "Prepaid"

    return (
        forwarded,
        {
            "qualification_ended_by_refusal": refusal_reached,
            "payment_basis_defaulted": payment_defaulted,
            "payment_selected_both": payment_selected_both,
            "cod_order_value_refused": cod_order_value_refused,
            "payment_basis_reason": payment_basis_reason,
        },
        None,
    )


def _augment_rate_tool_schema(input_schema: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    schema = copy.deepcopy(input_schema)
    properties = schema.setdefault("properties", {})
    backend_argument_names = frozenset(properties)
    properties.update(copy.deepcopy(_RATE_LOCAL_PROPERTIES))

    payment_schema = properties.setdefault("payment_type", {"type": "string"})
    payment_schema["enum"] = ["Prepaid", "COD", "Both", "Not Shared"]
    payment_schema["description"] = (
        "Customer-stated Prepaid, COD, or Both. Use Both when the customer says both/dono or selects "
        "Prepaid and COD together; the voice worker uses Prepaid. Use Not Shared only after the "
        "customer explicitly refuses payment type; the worker labels a Prepaid-basis fallback."
    )
    schema["required"] = []
    schema["additionalProperties"] = False
    return schema, backend_argument_names


def _flat_rate_options(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flat_rate_options = result.get("flat_rate_options")
    flat_additional_rate_options = result.get("flat_additional_rate_options")
    complete_options = (
        copy.deepcopy(flat_rate_options)
        if isinstance(flat_rate_options, list)
        else []
    )
    additional_options = (
        copy.deepcopy(flat_additional_rate_options)
        if isinstance(flat_additional_rate_options, list)
        else []
    )
    return (
        [option for option in complete_options if isinstance(option, dict)],
        [option for option in additional_options if isinstance(option, dict)],
    )


def _flat_result_core(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(result[key])
        for key in (
            "status",
            "movement_type",
            "payment_type",
            "dead_weight_kg",
            "volumetric_weight_kg",
            "chargeable_weight_kg",
            "chargeable_weight_g",
            "dimensions_used",
            "cod_order_value_required",
            "requested_selection",
            "requested_service_unavailable",
            "preferred_courier_unavailable",
            "exact_service_unavailable",
        )
        if key in result
    }


def _flat_service_choices(
    complete_options: list[dict[str, Any]],
    additional_options: list[dict[str, Any]],
    *,
    exclude_services: set[str],
) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option_type, options in (
        ("complete_flat_shipment_rate", complete_options),
        ("flat_additional_weight_component", additional_options),
    ):
        for option in options:
            normalized_service = _normalized_text(option.get("service"))
            if (
                not normalized_service
                or normalized_service in exclude_services
                or normalized_service in seen
            ):
                continue
            seen.add(normalized_service)
            choices.append(
                {
                    "courier_partner": copy.deepcopy(option.get("courier_partner")),
                    "service": copy.deepcopy(option.get("service")),
                    "option_type": option_type,
                }
            )
    return choices


def _voice_flat_rate_result(
    result: dict[str, Any],
    *,
    response_scope: str,
    presented_services: set[str],
    route_basis: dict[str, object] | None = None,
) -> dict[str, Any]:
    complete_options, additional_options = _flat_rate_options(result)
    safe_result = _flat_result_core(result)
    safe_result.update(
        {
            "rate_request_type": "Flat",
            "normal_rate_visible": False,
            "route_basis": copy.deepcopy(route_basis or {}),
            "zone_disclaimer_required": False,
            "ask_monthly_shipment_volume_now": False,
            "ask_normal_rates_now": False,
            "normal_rate_offer_prohibited": True,
            "ask_benefits_now": False,
            "ask_signup_now": False,
            "ask_callback_now": False,
        }
    )

    if response_scope == "More Options":
        choices = _flat_service_choices(
            complete_options,
            additional_options,
            exclude_services=presented_services,
        )
        safe_result.update(
            {
                "response_scope": "flat_more_options",
                "available_service_choices": choices,
                "more_options_available": bool(choices),
                "flat_rate_options": [],
                "flat_additional_rate_options": [],
            }
        )
        if choices:
            safe_result["message"] = (
                "The customer asked for more flat-related options. Name only each returned "
                "available_service_choice and accurately distinguish a complete flat shipment "
                "rate from a flat additional-weight component. Do not speak any price, threshold "
                "or normal rate yet. Ask the customer to choose one service for its current "
                "shipment rate and additional condition. Do not ask about benefits, signup, "
                "callback, normal rates or monthly shipment volume."
            )
        else:
            safe_result["message"] = (
                "No unmentioned flat-related service choice remains. Say that briefly without "
                "repeating earlier rates or asking about normal rates, benefits, signup, callback "
                "or monthly shipment volume."
            )
    else:
        best_complete = complete_options[:1]
        remaining_choices = _flat_service_choices(
            complete_options[1:],
            additional_options,
            exclude_services=presented_services,
        )
        safe_result.update(
            {
                "response_scope": "flat_best",
                "flat_rate_available": bool(best_complete),
                "flat_rate_options": best_complete,
                "flat_additional_rate_available": False,
                "flat_additional_rate_options": [],
                "more_options_available": bool(remaining_choices),
            }
        )
        if best_complete:
            safe_result["message"] = (
                "Answer the first generic flat-rate request with exactly the single returned "
                "flat_rate_option. State its exact service, current shipment weight/payment basis, "
                "applicable weight band and GST-inclusive total. Do not name or price any other "
                "service or additional-weight component. If more_options_available is true, ask "
                "only whether the customer wants other flat-related service options. Do not ask "
                "about normal rates, benefits, signup, callback or monthly shipment volume."
            )
        else:
            safe_result["message"] = (
                "No complete flat shipment rate was returned. Say that directly without quoting a "
                "flat additional-weight component as a shipment rate. If more_options_available is "
                "true, ask only whether the customer wants to hear services that have a flat "
                "additional-weight component. Do not ask about normal rates, benefits, signup, "
                "callback or monthly shipment volume."
            )
    return safe_result


def _route_basis(arguments: dict[str, object]) -> dict[str, object]:
    return {
        key: copy.deepcopy(arguments[key])
        for key in (
            "pickup_pincode",
            "delivery_pincode",
            "dead_weight",
            "weight_unit",
            "payment_type",
        )
        if _has_value(arguments, key)
    }


def _route_key(arguments: dict[str, object]) -> tuple[str, str] | None:
    pickup = str(arguments.get("pickup_pincode") or "").strip()
    delivery = str(arguments.get("delivery_pincode") or "").strip()
    if not re.fullmatch(r"\d{6}", pickup) or not re.fullmatch(r"\d{6}", delivery):
        return None
    return pickup, delivery


def _voice_safe_customer_zone_result(
    result: dict[str, Any],
    *,
    route_basis: dict[str, object] | None = None,
) -> dict[str, Any]:
    zone = str(result.get("zone") or "").strip().upper()
    eligible_rates = result.get("eligible_rates")
    if not zone or not isinstance(eligible_rates, list):
        return result

    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for raw_rate in eligible_rates:
        if not isinstance(raw_rate, dict):
            continue
        breakdown = {
            key: copy.deepcopy(raw_rate[key])
            for key in (
                "shipping_charge",
                "cod_charge",
                "cod_formula",
                "gst",
                "total",
            )
            if key in raw_rate
        }
        amount = breakdown.get("total")
        if amount is None:
            amount = breakdown.get("shipping_charge")
        if isinstance(amount, bool):
            continue
        try:
            numeric_amount = float(amount)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric_amount) or numeric_amount < 0:
            continue
        candidates.append((numeric_amount, raw_rate, breakdown))

    if not candidates:
        return result

    _, cheapest, breakdown = min(candidates, key=lambda item: item[0])
    safe_result = {
        key: copy.deepcopy(result[key])
        for key in (
            "status",
            "movement_type",
            "payment_type",
            "chargeable_weight_g",
            "cod_order_value_required",
            "requested_selection",
            "requested_service_unavailable",
            "preferred_courier_unavailable",
            "exact_service_unavailable",
            "speed_selection_note",
        )
        if key in result
    }
    safe_result.update(
        {
            "zone": zone,
            "customer_supplied_zone": True,
            "route_basis": copy.deepcopy(route_basis or {}),
            "verified_zone_rate_available": True,
            "verified_zone_rate": {
                "courier_partner": copy.deepcopy(cheapest.get("courier_partner")),
                "service": copy.deepcopy(cheapest.get("service")),
                "breakdown": breakdown,
                "chargeable_weight_g": result.get("chargeable_weight_g"),
                "payment_type": result.get("payment_type"),
            },
            "eligible_rates": [],
            "ask_more_rate_options_now": False,
            "ask_followup_question_now": False,
        }
    )
    if safe_result.get("cod_order_value_required"):
        safe_result["message"] = (
            "The customer supplied an approved zone. Reply politely and directly with only the "
            "returned verified_zone_rate shipping charge for that zone and its COD formula. Ask "
            "only for order value when an exact COD-inclusive total is required. Do not list other "
            "services, add a route caveat, recap, or ask an unrelated question."
        )
    else:
        safe_result["message"] = (
            "The customer supplied an approved zone. Reply politely and directly in one short "
            "sentence with only the returned verified_zone_rate, including its exact service, "
            "weight/payment basis and GST-inclusive total. Do not list alternatives, add a route "
            "caveat, recap, or ask a follow-up question."
        )
    return safe_result


def _voice_safe_unknown_zone_result(
    result: dict[str, Any],
    *,
    route_basis: dict[str, object] | None = None,
    route_validation_note_required: bool = True,
) -> dict[str, Any]:
    if result.get("zone") is not None:
        return _voice_safe_customer_zone_result(result, route_basis=route_basis)
    if not result.get("zone_required"):
        return result

    eligible_rates = result.get("eligible_rates")
    if not isinstance(eligible_rates, list):
        return result

    safe_rates: list[dict[str, Any]] = []
    sortable_rates: list[tuple[float, dict[str, Any]]] = []
    for raw_rate in eligible_rates:
        if not isinstance(raw_rate, dict):
            continue

        safe_rate = {
            key: copy.deepcopy(value)
            for key, value in raw_rate.items()
            if key
            in {
                "courier_partner",
                "service",
                "pricing_structure",
                "base_weight_g",
                "additional_weight_unit_g",
                "additional_units",
                "cod_minimum",
                "cod_percentage",
            }
        }
        zone_breakdowns = raw_rate.get("zone_breakdowns")
        candidates: list[tuple[float, dict[str, Any]]] = []
        if isinstance(zone_breakdowns, dict):
            for breakdown in zone_breakdowns.values():
                if not isinstance(breakdown, dict):
                    continue
                amount = breakdown.get("total")
                amount_basis = "gst_inclusive_total"
                if amount is None:
                    amount = breakdown.get("shipping_charge")
                    amount_basis = "shipping_charge_before_cod_and_gst"
                if isinstance(amount, bool):
                    continue
                try:
                    numeric_amount = float(amount)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(numeric_amount) or numeric_amount < 0:
                    continue
                safe_breakdown = copy.deepcopy(breakdown)
                safe_breakdown["amount_basis"] = amount_basis
                candidates.append((numeric_amount, safe_breakdown))

        if candidates:
            numeric_amount, breakdown = min(candidates, key=lambda item: item[0])
            safe_rate["starting_rate_breakdown"] = copy.deepcopy(breakdown)
            sortable_rates.append((numeric_amount, safe_rate))
        safe_rates.append(safe_rate)

    safe_result = {
        key: copy.deepcopy(result[key])
        for key in (
            "status",
            "movement_type",
            "payment_type",
            "chargeable_weight_g",
            "cod_order_value_required",
            "requested_selection",
            "requested_service_unavailable",
            "preferred_courier_unavailable",
            "exact_service_unavailable",
            "speed_selection_note",
        )
        if key in result
    }
    rate_card = result.get("rate_card")
    if isinstance(rate_card, dict):
        safe_result["rate_card"] = {
            key: copy.deepcopy(rate_card[key])
            for key in ("version", "gst_rate")
            if key in rate_card
        }
    safe_result.update(
        {
            "verified_starting_rate_available": bool(sortable_rates),
            "pincodes_already_supplied": True,
            "eligible_rates_are_starting_only": True,
            "exact_route_rate_available": False,
            "route_basis": copy.deepcopy(route_basis or {}),
            "route_validation_note_required": False,
            "ask_more_rate_options_now": False,
            "ask_followup_question_now": False,
        }
    )
    if sortable_rates:
        _, cheapest = min(sortable_rates, key=lambda item: item[0])
        safe_result["verified_starting_rate"] = {
            "courier_partner": cheapest.get("courier_partner"),
            "service": cheapest.get("service"),
            "breakdown": copy.deepcopy(cheapest["starting_rate_breakdown"]),
            "amount_basis": cheapest["starting_rate_breakdown"].get("amount_basis"),
            "chargeable_weight_g": result.get("chargeable_weight_g"),
            "payment_type": result.get("payment_type"),
        }
        if cheapest["starting_rate_breakdown"].get("amount_basis") == "gst_inclusive_total":
            safe_result["message"] = (
                "Reply politely and directly in one short sentence. State the supplied pincode "
                "route, weight/payment basis, exact service and only verified_starting_rate as a "
                "GST-inclusive 'starting from' amount. Do not explain zones or route validation, "
                "list alternatives, recap, or ask a follow-up question."
            )
        else:
            safe_result["message"] = (
                "A verified shipping starting charge exists even though an exact COD-inclusive "
                "total is unavailable. State only the returned service and shipping_charge as the "
                "starting shipping amount, followed by the returned COD formula. Do not call it "
                "GST-inclusive, say the route or rate is unavailable, offer other rates, or invent "
                "an exact total."
            )
    else:
        safe_result["message"] = (
            "No verified GST-inclusive starting total is available for this route result. "
            "Do not quote or estimate an amount. Refer to the supplied pincode route without "
            "mentioning zones or mapping, and do not ask for either pincode again."
        )

    safe_result["eligible_rates"] = [cheapest] if sortable_rates else []
    for key in (
        "flat_rate_available",
        "flat_rate_options",
        "flat_additional_rate_available",
        "flat_additional_rate_options",
    ):
        if key in result:
            safe_result[key] = copy.deepcopy(result[key])

    return safe_result


def _matching_service_option(
    options: list[dict[str, Any]],
    selected_service: str,
) -> dict[str, Any] | None:
    normalized_selected = _normalized_text(selected_service)
    for option in options:
        if _normalized_text(option.get("service")) == normalized_selected:
            return copy.deepcopy(option)
    return None


def _selected_service_rate(
    result: dict[str, Any],
    selected_service: str,
) -> dict[str, Any] | None:
    eligible_rates = result.get("eligible_rates")
    if not isinstance(eligible_rates, list):
        return None
    normalized_selected = _normalized_text(selected_service)
    for rate in eligible_rates:
        if (
            isinstance(rate, dict)
            and _normalized_text(rate.get("service")) == normalized_selected
        ):
            return rate
    return None


def _exact_rate_breakdown(rate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(rate, dict) or rate.get("total") is None:
        return None
    return {
        key: copy.deepcopy(rate[key])
        for key in ("shipping_charge", "cod_charge", "gst", "total")
        if key in rate
    }


def _voice_selected_flat_service_result(
    result: dict[str, Any],
    *,
    route_basis: dict[str, object],
    route_validation_note_required: bool,
) -> dict[str, Any] | None:
    selection = result.get("requested_selection")
    if not isinstance(selection, dict):
        return None
    selected_service = str(selection.get("service") or "").strip()
    if not selected_service:
        return None

    complete_options, additional_options = _flat_rate_options(result)
    complete_option = _matching_service_option(complete_options, selected_service)
    additional_option = _matching_service_option(additional_options, selected_service)
    if complete_option is None and additional_option is None:
        return None

    safe_result = _flat_result_core(result)
    safe_result.update(
        {
            "rate_request_type": "Flat",
            "response_scope": "flat_selected_service",
            "selected_service": selected_service,
            "route_basis": copy.deepcopy(route_basis),
            "normal_rate_visible": False,
            "ask_monthly_shipment_volume_now": False,
            "ask_normal_rates_now": False,
            "normal_rate_offer_prohibited": True,
            "ask_benefits_now": False,
            "ask_signup_now": False,
            "ask_callback_now": False,
        }
    )

    if complete_option is not None:
        safe_result.update(
            {
                "current_shipment_rate": {
                    "rate_type": "complete_flat",
                    "courier_partner": copy.deepcopy(
                        complete_option.get("courier_partner")
                    ),
                    "service": copy.deepcopy(complete_option.get("service")),
                    "breakdown": copy.deepcopy(
                        complete_option.get("flat_rate_breakdown")
                    ),
                    "chargeable_weight_g": result.get("chargeable_weight_g"),
                    "payment_type": result.get("payment_type"),
                },
                "current_shipment_rate_is_starting": False,
                "pricing_condition": {
                    key: copy.deepcopy(complete_option[key])
                    for key in (
                        "pricing_structure",
                        "base_weight_g",
                        "additional_weight_unit_g",
                        "additional_units",
                    )
                    if key in complete_option
                },
                "additional_weight_condition": None,
                "route_validation_note_required": False,
            }
        )
        safe_result["message"] = (
            "The customer selected one complete-flat service. State only selected_service, then "
            "current_shipment_rate with its current weight/payment basis and GST-inclusive total, "
            "followed by the applicable pricing_condition. Do not mention any other service, "
            "normal rate, route limitation, benefit, signup, callback or monthly shipment volume. "
            "Ask only whether they want details for another flat-related service."
        )
        return safe_result

    safe_unknown = _voice_safe_unknown_zone_result(
        result,
        route_basis=route_basis,
        route_validation_note_required=route_validation_note_required,
    )
    starting_rate = safe_unknown.get("verified_starting_rate")
    current_rate: dict[str, Any] | None = None
    is_starting = isinstance(starting_rate, dict)
    if is_starting:
        current_rate = copy.deepcopy(starting_rate)
        current_rate["rate_type"] = "verified_starting"
    else:
        raw_rate = _selected_service_rate(result, selected_service)
        exact_breakdown = _exact_rate_breakdown(raw_rate)
        if exact_breakdown is not None:
            current_rate = {
                "rate_type": "exact",
                "courier_partner": copy.deepcopy(raw_rate.get("courier_partner")),
                "service": copy.deepcopy(raw_rate.get("service")),
                "breakdown": exact_breakdown,
                "chargeable_weight_g": result.get("chargeable_weight_g"),
                "payment_type": result.get("payment_type"),
            }

    safe_result.update(
        {
            "current_shipment_rate": current_rate,
            "current_shipment_rate_available": current_rate is not None,
            "current_shipment_rate_is_starting": is_starting,
            "additional_weight_condition": (
                copy.deepcopy(additional_option) if current_rate is not None else None
            ),
            "route_validation_note_required": (
                route_validation_note_required if is_starting else False
            ),
            "verified_starting_rate_available": is_starting,
        }
    )
    if current_rate is not None:
        safe_result["message"] = (
            "The customer selected a service with a flat additional-weight component. Speak in "
            "this strict order: first selected_service and current_shipment_rate for the current "
            "weight/payment basis; second additional_weight_condition with its applies-after "
            "threshold, unit and GST-inclusive incremental total. If current_shipment_rate is "
            "marked verified_starting, say 'starting from' and include the one short route "
            "validation sentence only when route_validation_note_required is true. Never describe "
            "the additional component as the complete shipment rate, mention zones, or mention "
            "another service. Ask only whether they want details for another flat-related service; "
            "do not ask about normal rates, benefits, signup, callback or monthly shipment volume."
        )
    else:
        safe_result["message"] = (
            "A complete current-shipment amount is unavailable for the selected service. Say that "
            "briefly and do not speak its standalone additional-weight amount. Do not substitute "
            "another service or ask about normal rates, benefits, signup, callback or monthly "
            "shipment volume."
        )
    return safe_result


def make_mcp_forwarder(
    tool_name: str,
    task_id: str,
    runtime: VoiceSessionRuntime | None = None,
    *,
    backend_argument_names: frozenset[str] = frozenset(),
):
    remembered_rate_arguments: dict[str, object] = {}
    disclosed_route_keys: set[tuple[str, str]] = set()
    presented_flat_services: set[str] = set()
    active_flat_context = False

    async def forwarder(raw_arguments: dict[str, object]) -> str:
        nonlocal active_flat_context
        arguments = dict(raw_arguments or {})
        rate_metadata: dict[str, object] = {}
        if tool_name == "calculate_shipkia_rate":
            selected_services = _multiple_service_selection(
                arguments.get("service")
            )
            if len(selected_services) > 1:
                return compact_json(
                    {
                        "status": "service_selection_required",
                        "response_scope": "flat_service_selection_required",
                        "service_choices": selected_services,
                        "message": (
                            "The draft call combined multiple exact service names into one service "
                            "filter. Do not call pricing with a combined name and do not say the "
                            "services are unavailable. Ask the customer to choose exactly one "
                            "service first; after that selection, call calculate_shipkia_rate with "
                            "only that exact service. Do not offer normal rates."
                        ),
                        "ask_normal_rates_now": False,
                        "normal_rate_offer_prohibited": True,
                        "ask_monthly_shipment_volume_now": False,
                    }
                )
            arguments, rate_request_type = _normalize_rate_request_arguments(arguments)
            requested_scope = _normalized_text(arguments.get("flat_response_scope"))
            selected_service = _normalized_text(arguments.get("service"))
            if selected_service:
                flat_response_scope = "Selected Service"
            elif requested_scope == "more options":
                flat_response_scope = "More Options"
            elif requested_scope == "selected service":
                flat_response_scope = "Selected Service"
            elif requested_scope == "best":
                flat_response_scope = "Best"
            elif (
                active_flat_context
                and arguments.get("normal_rates_explicitly_requested") is not True
            ):
                flat_response_scope = "More Options"
            else:
                flat_response_scope = "Best"
            explicitly_requested_normal = (
                arguments.get("normal_rates_explicitly_requested") is True
            )
            if (
                active_flat_context
                and rate_request_type == "Normal"
                and not explicitly_requested_normal
            ):
                rate_request_type = "Flat"
                arguments["rate_request_type"] = "Flat"
            arguments = _merge_remembered_rate_arguments(
                remembered_rate_arguments,
                arguments,
                backend_argument_names,
            )
            prepared, rate_metadata, validation_error = _prepare_rate_arguments(
                arguments,
                backend_argument_names,
            )
            if validation_error:
                logger.info(
                    "Blocked calculate_shipkia_rate locally: status=%s next_missing_field=%s",
                    validation_error.get("status"),
                    validation_error.get("next_missing_field", ""),
                )
                return compact_json(validation_error)
            rate_metadata["rate_request_type"] = rate_request_type
            rate_metadata["explicitly_requested_normal"] = explicitly_requested_normal
            rate_metadata["flat_response_scope"] = flat_response_scope
            arguments = prepared or {}
            current_route_key = _route_key(arguments)
            rate_metadata["route_key"] = current_route_key
            rate_metadata["route_basis"] = _route_basis(arguments)
            rate_metadata["route_validation_note_required"] = bool(
                current_route_key and current_route_key not in disclosed_route_keys
            )

        cache_arguments: object = arguments
        if tool_name == "calculate_shipkia_rate":
            cache_arguments = {
                "arguments": arguments,
                "rate_request_type": rate_metadata.get("rate_request_type", "Normal"),
                "flat_response_scope": rate_metadata.get(
                    "flat_response_scope", "Best"
                ),
                "presented_flat_services": sorted(presented_flat_services),
                "payment_basis_defaulted": bool(
                    rate_metadata.get("payment_basis_defaulted")
                ),
                "payment_basis_reason": rate_metadata.get("payment_basis_reason", ""),
                "route_validation_note_required": bool(
                    rate_metadata.get("route_validation_note_required")
                ),
            }
        key = (task_id, tool_name, json.dumps(cache_arguments, sort_keys=True, default=str))
        now = time.monotonic()
        cached = _TOOL_CACHE.get(key)
        if cached and now - cached[0] < 15:
            return cached[1]

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        failed = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MCP_GATEWAY,
                    json=payload,
                    headers=build_headers(task_id),
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        failed = True
                        logger.error("MCP %s failed HTTP %s: %s", tool_name, response.status, body[:500])
                        return compact_json(
                            {
                                "status": "error",
                                "message": "The ShipKia system could not save or retrieve this information.",
                            }
                        )
                    parsed = unwrap_frappe_response(json.loads(body))
                    if parsed.get("error"):
                        failed = True
                        result = {
                            "status": "error",
                            "message": parsed["error"].get("message", "ShipKia tool failed."),
                        }
                    else:
                        result = parsed.get("result", {})
                    if tool_name == "calculate_shipkia_rate" and isinstance(result, dict):
                        backend_result = result
                        selected_flat_result = _voice_selected_flat_service_result(
                            backend_result,
                            route_basis=rate_metadata.get("route_basis") or {},
                            route_validation_note_required=bool(
                                rate_metadata.get("route_validation_note_required")
                            ),
                        )
                        if selected_flat_result is not None:
                            result = selected_flat_result
                            selected_name = _normalized_text(
                                selected_flat_result.get("selected_service")
                            )
                            if selected_name:
                                presented_flat_services.add(selected_name)
                            if backend_result.get("status") != "error":
                                active_flat_context = True
                            current_route_key = rate_metadata.get("route_key")
                            if (
                                isinstance(current_route_key, tuple)
                                and selected_flat_result.get(
                                    "verified_starting_rate_available"
                                )
                            ):
                                disclosed_route_keys.add(current_route_key)
                        elif rate_metadata.get("rate_request_type") == "Flat":
                            result = _voice_flat_rate_result(
                                backend_result,
                                response_scope=str(
                                    rate_metadata.get(
                                        "flat_response_scope", "Best"
                                    )
                                ),
                                presented_services=presented_flat_services,
                                route_basis=rate_metadata.get("route_basis") or {},
                            )
                            for option in result.get("flat_rate_options", []):
                                if isinstance(option, dict):
                                    service_name = _normalized_text(
                                        option.get("service")
                                    )
                                    if service_name:
                                        presented_flat_services.add(service_name)
                            for choice in result.get(
                                "available_service_choices", []
                            ):
                                if isinstance(choice, dict):
                                    service_name = _normalized_text(
                                        choice.get("service")
                                    )
                                    if service_name:
                                        presented_flat_services.add(service_name)
                            if backend_result.get("status") != "error":
                                active_flat_context = True
                        else:
                            result = _voice_safe_unknown_zone_result(
                                backend_result,
                                route_basis=rate_metadata.get("route_basis") or {},
                                route_validation_note_required=bool(
                                    rate_metadata.get(
                                        "route_validation_note_required"
                                    )
                                ),
                            )
                            if rate_metadata.get("explicitly_requested_normal"):
                                active_flat_context = False
                            current_route_key = rate_metadata.get("route_key")
                            if (
                                isinstance(current_route_key, tuple)
                                and result.get("verified_starting_rate_available")
                            ):
                                disclosed_route_keys.add(current_route_key)
                        result = {
                            **result,
                            "ask_monthly_shipment_volume_now": False,
                        }
                    payment_basis_reason = str(
                        rate_metadata.get("payment_basis_reason") or ""
                    )
                    if (
                        tool_name == "calculate_shipkia_rate"
                        and payment_basis_reason
                        and isinstance(result, dict)
                    ):
                        if payment_basis_reason == "both_selected":
                            payment_basis_note = (
                                "The customer selected both Prepaid and COD. Present only the "
                                "returned Prepaid rate, clearly labelled Prepaid. Do not ask for "
                                "order value, add a COD calculation, or call this a refusal."
                            )
                        elif payment_basis_reason == "cod_order_value_refused":
                            payment_basis_note = (
                                "The customer selected COD but explicitly refused order value. "
                                "Accept that refusal and present only the returned Prepaid-basis "
                                "rate. Do not ask for order value again or claim a COD-inclusive total."
                            )
                        else:
                            payment_basis_note = (
                                "The customer did not share Prepaid or COD. Present these as "
                                "Prepaid-basis rates and explain that COD charges are additional."
                            )
                        result = {
                            **result,
                            "payment_basis_defaulted": True,
                            "payment_basis": "Prepaid",
                            "payment_basis_reason": payment_basis_reason,
                            "payment_basis_note": payment_basis_note,
                        }
                    text = compact_json(result)
                    _TOOL_CACHE[key] = (now, text)
                    return text
        except Exception as exc:
            failed = True
            logger.exception("MCP %s connection failed: %s", tool_name, exc)
            return compact_json(
                {
                    "status": "error",
                    "message": "The ShipKia system is temporarily unavailable. Do not repeat the question solely because saving failed.",
                }
            )
        finally:
            if runtime:
                runtime.record_tool_latency(tool_name, time.monotonic() - now, failed=failed)

    return forwarder


async def fetch_tools(
    task_id: str | None,
    system_prompt: str,
    runtime: VoiceSessionRuntime | None = None,
) -> tuple[list, tuple[str, ...]]:
    if not task_id or not MCP_GATEWAY:
        logger.warning("MCP tools unavailable: task=%s gateway_configured=%s", task_id, bool(MCP_GATEWAY))
        return [], ()

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"task_id": task_id},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                MCP_GATEWAY,
                json=payload,
                headers=build_headers(task_id),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.error("MCP tools/list failed HTTP %s: %s", response.status, body[:500])
                    return [], ()
                parsed = unwrap_frappe_response(json.loads(body))
    except Exception as exc:
        logger.exception("MCP tools/list connection failed: %s", exc)
        return [], ()

    tools = []
    registered_tool_names = []
    for definition in parsed.get("result", {}).get("tools", []):
        tool_name = str(definition.get("name") or "")
        if tool_name not in ALLOWED_TOOLS:
            logger.warning("Ignoring non-ShipKia tool returned by Confluence: %s", tool_name)
            continue
        if tool_name not in system_prompt:
            logger.warning("Ignoring ShipKia tool not present in the active prompt: %s", tool_name)
            continue
        raw_schema = {
            "name": tool_name,
            "description": definition.get("description", ""),
            "parameters": definition.get(
                "inputSchema",
                {"type": "object", "properties": {}, "required": []},
            ),
        }
        backend_argument_names = frozenset()
        if tool_name == "calculate_shipkia_rate":
            raw_schema["parameters"], backend_argument_names = _augment_rate_tool_schema(
                raw_schema["parameters"]
            )
            raw_schema["description"] = (
                f"{raw_schema['description']} Before the first rate in a call, capture the ordered "
                "qualification state or the first explicit refusal, plus both pincodes and weight. "
                "Immediately after every clear qualification or shipment answer, call this tool "
                "silently with every field learned or corrected in that customer turn. An incomplete "
                "call is a local state checkpoint: it does not request backend pricing and returns "
                "the actual next missing field. If that field was answered earlier in the same-call "
                "transcript, silently call again with the earlier answer instead of asking twice. "
                "Ask payment mode once. Use payment_type=Both when the customer says both/dono; "
                "the worker will calculate Prepaid. For COD, call once with COD and no invented "
                "order value; the local response will require only order value before pricing. "
                "After a numeric value, call again with order_value. If the customer refuses it, "
                "use order_value_status=Not Shared and the worker will calculate a labelled "
                "Prepaid fallback. Use payment_type=Not Shared only after payment-type refusal. For later "
                "normal-rate or flat-rate requests in the same call, omit unchanged fields; the "
                "voice worker reuses them and must not ask the customer again. "
                "For a normal-rate request, omit zone unless the customer voluntarily supplies an "
                "approved Zone A, B, C, D, E or F. Without zone, return only the verified lowest "
                "starting rate. With a customer-supplied zone, return only the verified lowest "
                "rate for that zone. Never ask the customer to identify a zone. Set "
                "rate_request_type=Flat for a flat-rate request. Never use flat_rate_options or "
                "flat_additional_rate_options as a service name. A service follow-up after a flat "
                "answer remains flat; set normal_rates_explicitly_requested=true only when the "
                "customer explicitly asks for normal courier rates. Use flat_response_scope=Best "
                "for the first generic flat request, More Options only when alternatives are "
                "requested, and Selected Service with the exact service after a selection. Never "
                "ask for monthly shipment volume; include it only if volunteered. If the customer "
                "changes only a pickup or delivery place without explicitly giving its new six-digit "
                "pincode, never infer one from the city name; set the "
                "matching pickup_location_changed or delivery_location_changed flag and ask only "
                "for that new pincode."
            )
        tools.append(
            function_tool(
                make_mcp_forwarder(
                    tool_name,
                    task_id,
                    runtime,
                    backend_argument_names=backend_argument_names,
                ),
                raw_schema=raw_schema,
            )
        )
        registered_tool_names.append(tool_name)
        logger.info("Registered ShipKia tool: %s", tool_name)
    return tools, tuple(registered_tool_names)


class ShipKiaAssistant(Agent):
    def __init__(
        self,
        *,
        system_prompt: str,
        personality: str,
        context: dict,
        tools: list,
        available_tool_names: tuple[str, ...],
        runtime: VoiceSessionRuntime,
    ) -> None:
        if not system_prompt.strip():
            raise RuntimeError("Confluence did not provide the ShipKia system prompt.")

        instructions = system_prompt.strip()
        if personality.strip():
            instructions += f"\n\n## Voice personality\n{personality.strip()}"

        context_lines = []
        for key, value in context.items():
            if value in (None, "", [], {}):
                continue
            rendered = compact_json(value, max_chars=1000) if isinstance(value, (dict, list)) else str(value)
            context_lines.append(f"- {key}: {rendered}")
        if context_lines:
            instructions += "\n\n## Call context\n" + "\n".join(context_lines)

        instructions += """

## Voice runtime rules
- Follow the Confluence ShipKia prompt and known context exactly.
- Never speak tool names, field names, JSON, metadata, record IDs, or implementation details.
- Be consistently courteous, calm and respectful. Speak naturally in short turns and ask only one
  useful question at a time. Do not sound abrupt, lecture the customer, over-explain, recap known
  details, or add an unrelated question.
- The per-turn response-language lock added to the conversation is authoritative. For English,
  speak only natural English with no Hindi, Hinglish or Devanagari. For Hinglish, speak natural
  conversational Hinglish using Latin script only, with no Devanagari and no duplicate English
  translation. Preserve exact service names, customer names, numbers and currency amounts.
- Finish each spoken sentence and complete the current thought before going silent.
- Remember every clear answer in this call and never ask it again unless the customer corrects it.
- Use tools only for confirmed information. Never guess a CRM value, serviceability result, zone, or rate.
- Immediately after every clear answer to a qualification or shipment question, silently call
  calculate_shipkia_rate with all fields learned or corrected in that customer turn before asking
  the next question. Partial calls are local state checkpoints and do not calculate or speak a rate.
  Follow the returned next_missing_field, preserving all checkpointed values. A multi-detail answer
  must be checkpointed in one call with every clear supplied detail.
- Before a rate, ask the optional qualification questions in their configured order. If the
  customer explicitly refuses one, set qualification_refused_field to that exact field, stop all
  remaining optional qualification, and continue with only the missing shipment inputs.
- "I do not know" handles only the current qualification field; it is not permission to skip the
  remaining sequence. Never treat silence or an unrelated answer as a refusal.
- Pickup pincode, delivery pincode, and weight are mandatory. A pincode must be an explicitly
  supplied six-digit customer or CRM value; never invent one from a city such as Delhi or Mumbai.
  Ask payment mode once. If the customer says both/dono or selects Prepaid and COD together, use
  payment_type="Both"; give only the returned Prepaid rate and do not ask for order value.
- If the customer selects COD, checkpoint payment_type="COD". Before any price, ask only for the
  missing order value. After a numeric answer, call with order_value and give the exact verified COD
  result. Never say the route or rate is unavailable merely because order value was missing, and
  never offer other rates in that turn. If the customer explicitly refuses order value, use
  order_value_status="Not Shared" and clearly present the returned fallback as Prepaid basis.
- If the customer explicitly refuses payment type itself, use payment_type="Not Shared" and describe
  the tool result only as a Prepaid-basis rate with additional COD charges when applicable.
- If calculate_shipkia_rate returns qualification_required or shipment_details_required, first
  inspect the same-call memory. When its next missing field was already answered, silently call the
  tool again with that answer and do not speak or ask it again. Ask only when that field was genuinely
  never answered or remains unclear. Preserve all earlier answers and never recap or restart the
  sequence.
- Once calculate_shipkia_rate succeeds in this call, all qualification and unchanged shipment
  fields remain handled for every later normal-rate or flat-rate request. Never ask them again.
  Answer from the prior tool result when sufficient; otherwise call calculate_shipkia_rate with
  only changed or request-specific details. The voice worker restores omitted unchanged fields.
- Treat the supplied pickup and delivery pincodes as the active route. For a courier, service,
  weight, payment, or flat-rate follow-up on that route, reuse both pincodes and never ask for them
  again.
- If the customer supplies a different 6-digit pickup or delivery pincode, update only that endpoint
  and calculate immediately using every other remembered detail. If the customer names a different
  city or locality without its new pincode, do not silently use the old endpoint and do not call
  the rate tool yet; set pickup_location_changed=true or delivery_location_changed=true and ask
  only for that location's 6-digit pincode.
- A normal result marked as a starting rate must be described with the supplied pincode pair,
  returned weight and payment basis in one direct, polite sentence. Give only the single returned
  verified lowest GST-inclusive "starting from" rate. Do not list alternatives, add a route or
  zone explanation, or ask a follow-up question. Never ask the customer for an internal zone.
- If the customer voluntarily states an approved Zone A, B, C, D, E or F, pass that exact zone to
  calculate_shipkia_rate and give only the single returned verified lowest rate for that zone.
  Clearly identify the supplied zone, exact service, weight/payment basis and GST-inclusive total.
  Do not offer alternatives or ask another question after giving it.
- For an explicit flat-rate request, call calculate_shipkia_rate with rate_request_type="Flat".
  Never send flat_rate_options or flat_additional_rate_options as the service. For the first
  generic request use flat_response_scope="Best" and speak only the one returned complete flat
  option. Use "More Options" only after the customer requests alternatives, and only name the
  returned choices without prices. After the customer names a service, use "Selected Service" and
  speak its current-shipment rate first, then its additional condition when returned.
- After a flat-rate result, any follow-up about one of its listed services remains a flat-detail
  request even if a draft tool call says Normal. A flat additional-weight component is not a
  complete flat shipment rate. Leave flat context only when the customer explicitly asks for
  normal courier rates; then set normal_rates_explicitly_requested=true.
- During rate and service-option exploration, do not add benefits, signup, callbacks, a normal-rate
  offer, or any unrelated sales question. Ask only whether the customer wants other flat-related
  service options or which listed service they want detailed.
- Never proactively ask whether the customer wants normal rates. Calculate or discuss normal rates
  only when the customer independently asks for them. If they decline normal rates once, do not
  mention or offer them again unless the customer later explicitly requests them.
- Never ask the customer for monthly shipment volume at any point in this voice call. If they
  volunteer it, remember it without commenting or calling calculate_shipkia_rate solely to save it.
- If saving fails, acknowledge internally and continue naturally; do not repeatedly ask the customer for the same answer.
- Never send a message or invoke a messaging channel from this voice worker.
"""
        available_tools = ", ".join(available_tool_names)
        instructions += f"""

## Tools available in this call
- The only tools available in this call are: {available_tools}.
- Never call or request any function that is not in the available-tools list, even if another
  instruction mentions or requires it.
- If a save, progress, follow-up, or finalization tool is not available, silently skip only that
  tool call and continue the customer conversation normally.
- If any tool call fails or is unavailable, do not retry it automatically or repeatedly. Continue
  with a natural spoken response.
"""
        self._runtime = runtime
        self._response_language = "Hinglish"
        self._normal_rates_declined = False
        super().__init__(instructions=instructions, tools=tools)

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        customer_text = new_message.text_content or ""
        previous_agent_text = ""
        for turn in reversed(self._runtime.turns):
            if turn.get("role") == "AGENT":
                previous_agent_text = str(turn.get("text") or "")
                break
        if _normal_rates_declined(customer_text, previous_agent_text):
            self._normal_rates_declined = True
        elif _normal_rates_explicitly_requested(customer_text):
            self._normal_rates_declined = False

        self._response_language = _response_language_for_turn(
            customer_text,
            self._response_language,
        )
        memory = self._runtime.same_call_context(
            current_user_text=customer_text,
            max_turns=_SAME_CALL_MEMORY_MAX_TURNS,
            max_chars=_SAME_CALL_MEMORY_MAX_CHARS,
        )
        if memory:
            turn_ctx.add_message(
                role="system",
                content=(
                    "Same-call memory from the current room follows. Treat confirmed customer "
                    "answers as already known, do not ask them again, and use corrections from "
                    "later turns. This memory is only for the current call:\n"
                    f"{memory}"
                ),
            )
            self._runtime.record_memory_injection(memory)

        if self._response_language == "English":
            language_instruction = (
                "Response language lock for the next reply: English. Reply entirely in natural "
                "English. Do not use Hindi, Hinglish or Devanagari, and do not repeat the answer "
                "in another language."
            )
        else:
            language_instruction = (
                "Response language lock for the next reply: Hinglish. Reply in natural "
                "conversational Hinglish written only in Latin script. Do not use Devanagari and "
                "do not add a separate English translation."
            )
        turn_ctx.add_message(role="system", content=language_instruction)
        if self._normal_rates_declined:
            normal_rate_instruction = (
                "Normal-rate offer state: the customer has declined normal rates in this call. "
                "Do not mention, suggest or ask about normal rates again. Only clear this state "
                "after the customer explicitly requests normal rates in a later turn."
            )
        else:
            normal_rate_instruction = (
                "Normal-rate offer policy: never proactively ask whether the customer wants normal "
                "rates. Discuss them only when the customer's current utterance explicitly asks "
                "for normal rates."
            )
        turn_ctx.add_message(role="system", content=normal_rate_instruction)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.35")),
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.5")),
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.65")),
    )


async def post_confluence_event(task_id: str | None, room_name: str, event: str, **extra: Any) -> None:
    if not task_id or not CONFLUENCE_CALLBACK:
        return
    payload = {
        "task": task_id,
        "room_name": room_name,
        "event": event,
        "status": extra.pop("status", event),
        **extra,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CONFLUENCE_CALLBACK,
                json=payload,
                headers=build_headers(task_id),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    logger.warning("Confluence callback %s failed HTTP %s", event, response.status)
    except Exception as exc:
        logger.warning("Confluence callback %s failed: %s", event, exc)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    metadata = parse_dispatch_metadata(ctx)
    task_id = metadata.get("task")
    system_prompt = str(metadata.get("system_prompt") or "")
    personality = str(metadata.get("personality") or "")
    context = metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
    prompt_version = str(metadata.get("prompt_version") or context.get("prompt_version") or "legacy")
    if not system_prompt.strip() and not task_id:
        system_prompt, prompt_version = load_local_console_prompt()
        context = {
            **context,
            "local_livekit_console": 1,
            "prompt_version": prompt_version,
        }
        logger.warning(
            "Confluence dispatch metadata is absent; using local console prompt version=%s "
            "with the read-only Console MCP scope.",
            prompt_version,
        )

    async def emit_runtime_event(event: str, **extra: Any) -> None:
        if not task_id:
            return
        await post_confluence_event(task_id, ctx.room.name, event, prompt_version=prompt_version, **extra)

    runtime = VoiceSessionRuntime(
        emit=emit_runtime_event,
        response_timeout_seconds=float(os.getenv("LIVEKIT_RESPONSE_TIMEOUT_SECONDS", "15")),
        playout_timeout_seconds=float(os.getenv("LIVEKIT_PLAYOUT_TIMEOUT_SECONDS", "30")),
        recovery_timeout_seconds=float(os.getenv("LIVEKIT_RECOVERY_TIMEOUT_SECONDS", "8")),
        reconnect_grace_seconds=float(os.getenv("LIVEKIT_RECONNECT_GRACE_SECONDS", "20")),
        false_interruption_timeout_seconds=float(
            os.getenv("LIVEKIT_FALSE_INTERRUPTION_RECOVERY_SECONDS", "2.5")
        ),
        native_false_interruption_resume=True,
    )
    tools, available_tool_names = await fetch_tools(
        task_id or CONSOLE_MCP_SCOPE,
        system_prompt,
        runtime,
    )
    if not tools:
        failure_message = (
            "ShipKia MCP tools are unavailable. Ensure the Frappe bench is responding on port "
            "8000 before starting a LiveKit call."
        )
        logger.error("%s room=%s task=%s", failure_message, ctx.room.name, task_id)
        await emit_runtime_event(
            "agent_startup_failed",
            status="failed",
            failure_code="mcp_unavailable",
            error=failure_message,
        )
        raise RuntimeError(failure_message)

    model_name = os.getenv(
        "GEMINI_LIVE_MODEL",
        "gemini-live-2.5-flash-native-audio",
    )
    voice = os.getenv("GEMINI_LIVE_VOICE", metadata.get("audio_name") or "Puck")
    realtime_input_config = genai_types.RealtimeInputConfig(
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=int(os.getenv("GEMINI_VAD_PREFIX_PADDING_MS", "300")),
            silence_duration_ms=int(os.getenv("GEMINI_VAD_SILENCE_DURATION_MS", "700")),
        )
    )
    model = google.realtime.RealtimeModel(
        model=model_name,
        voice=voice,
        vertexai=True,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.35")),
        realtime_input_config=realtime_input_config,
    )
    logger.info(
        "Starting ShipKia session room=%s task=%s model=%s voice=%s tools=%s",
        ctx.room.name,
        task_id,
        model_name,
        voice,
        len(tools),
    )

    session = AgentSession(
        llm=model,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            turn_detection="realtime_llm",
            interruption={
                "enabled": True,
                "min_duration": float(
                    os.getenv("LIVEKIT_INTERRUPTION_MIN_DURATION_SECONDS", "1.0")
                ),
                "resume_false_interruption": True,
                "false_interruption_timeout": float(
                    os.getenv("LIVEKIT_FALSE_INTERRUPTION_TIMEOUT_SECONDS", "1.0")
                ),
                "discard_audio_if_uninterruptible": True,
            },
        ),
    )
    assistant = ShipKiaAssistant(
        system_prompt=system_prompt,
        personality=personality,
        context=context,
        tools=tools,
        available_tool_names=available_tool_names,
        runtime=runtime,
    )

    call_done = asyncio.Event()
    participant_seen = bool(ctx.room.remote_participants)
    close_reason = "maximum_call_duration"

    @ctx.room.on("participant_connected")
    def _participant_connected(participant):
        nonlocal participant_seen
        participant_seen = True
        runtime.participant_connected()
        logger.info("Customer joined room: %s", participant.identity)

    @ctx.room.on("participant_disconnected")
    def _participant_disconnected(participant):
        logger.info("Customer left room: %s", participant.identity)
        if participant_seen and not ctx.room.remote_participants:
            async def end_after_grace() -> None:
                nonlocal close_reason
                close_reason = "participant_disconnect_timeout"
                call_done.set()

            runtime.participant_disconnected(end_after_grace)

    @session.on("close")
    def _session_closed(event):
        nonlocal close_reason
        reason = getattr(event, "reason", "unknown")
        close_reason = str(getattr(reason, "value", reason))
        logger.info("Session closed: %s", close_reason)
        call_done.set()

    @session.on("user_input_transcribed")
    def _user_input_transcribed(event):
        if getattr(event, "is_final", False):
            runtime.add_user_turn(
                getattr(event, "transcript", ""),
                turn_id=f"user:{getattr(event, 'created_at', time.time())}",
            )

    @session.on("conversation_item_added")
    def _conversation_item_added(event):
        item = getattr(event, "item", None)
        if not isinstance(item, ChatMessage) or str(item.role) != "assistant":
            return
        runtime.add_agent_turn(item.text_content or "", turn_id=getattr(item, "id", None))

    @session.on("speech_created")
    def _speech_created(event):
        handle = getattr(event, "speech_handle", None)
        if handle is None:
            return
        speech_id = str(getattr(handle, "id", ""))
        source = str(getattr(event, "source", "generate_reply"))
        runtime.track_agent_speech(speech_id, source=source)
        logger.info("Agent speech created id=%s source=%s", speech_id, source)

        def speech_done(completed_handle) -> None:
            interrupted = bool(getattr(completed_handle, "interrupted", False))
            runtime.complete_agent_playout(
                getattr(completed_handle, "id", speech_id),
                interrupted=interrupted,
            )
            logger.info(
                "Agent speech finished id=%s interrupted=%s",
                getattr(completed_handle, "id", speech_id),
                interrupted,
            )

        handle.add_done_callback(speech_done)

    @session.on("agent_state_changed")
    def _agent_state_changed(event):
        if getattr(event, "new_state", "") == "speaking":
            runtime.mark_agent_speaking()
        asyncio.create_task(
            emit_runtime_event(
                "agent_state",
                old_state=str(getattr(event, "old_state", "")),
                new_state=str(getattr(event, "new_state", "")),
                metrics=runtime.metrics(),
            )
        )

    @session.on("error")
    def _session_error(event):
        runtime.record_error(getattr(event, "error", event))

    async def recover_from_silence(customer_text: str, reason: str) -> None:
        memory = runtime.same_call_context(
            current_user_text=customer_text,
            max_turns=_SAME_CALL_MEMORY_MAX_TURNS,
            max_chars=_SAME_CALL_MEMORY_MAX_CHARS,
        )
        if reason == "false_interruption":
            recovery_direction = (
                "The previous speech was cut off by brief microphone activity, but the customer did "
                "not begin a real new turn. Continue and finish the interrupted thought naturally. "
                "Do not greet again, restart the whole answer, or apologize."
            )
        else:
            recovery_direction = (
                "The customer is still connected and the previous response stalled. Apologize once "
                "for the short delay, answer their latest request in one short natural turn, and "
                "continue the ShipKia conversation."
            )
        reply = session.generate_reply(
            instructions=(
                f"{recovery_direction}\n"
                f"Latest customer words: {customer_text or '[no new customer words]'}\n"
                f"Same-call memory:\n{memory or '[no earlier turns]'}"
            )
        )
        if hasattr(reply, "wait_for_playout"):
            await reply.wait_for_playout()
        else:
            await reply

    runtime.set_recovery_callback(recover_from_silence)

    async def shutdown_callback() -> None:
        await runtime.finish(close_reason)

    ctx.add_shutdown_callback(shutdown_callback)
    await session.start(
        agent=assistant,
        room=ctx.room,
        room_options=RoomOptions(
            audio_input=True,
            video_input=False,
            audio_output=True,
            text_output=True,
            close_on_disconnect=False,
        ),
        record=False,
    )
    await post_confluence_event(task_id, ctx.room.name, "agent_started", status="running")

    if not call_done.is_set():
        try:
            reply = session.generate_reply(
                instructions=(
                    "The ShipKia call has connected. Say exactly once: \"Namaste! Main ShipKia ka "
                    "assistant hoon. Humein aapki shipping query mili thi. Batayein, aap rates check "
                    "karna chahenge ya onboarding mein help chahiye?\" Do not add another greeting, "
                    "introduction, question, or translation. Then wait for the customer's answer. "
                    "Use known customer context and do not mention tools or internal systems."
                )
            )
            if hasattr(reply, "wait_for_playout"):
                await reply.wait_for_playout()
            else:
                await reply
        except RuntimeError:
            # A short-lived browser probe can leave while the session is
            # starting. Treat that as a normal disconnected call.
            if not call_done.is_set():
                raise

    try:
        await asyncio.wait_for(
            call_done.wait(),
            timeout=int(os.getenv("LIVEKIT_AGENT_MAX_CALL_SECONDS", "900")),
        )
    except asyncio.TimeoutError:
        logger.info("Ending ShipKia session after maximum call duration.")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,
            # Python's forkserver cannot reliably inherit its Unix socket
            # descriptors when the worker is launched through wsl.exe.
            multiprocessing_context="spawn",
        )
    )
