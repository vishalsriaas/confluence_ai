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
    StopResponse,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatMessage, function_tool
from livekit.agents.voice.room_io import RoomOptions, TextInputOptions
from livekit.plugins import google, silero

try:
    from conversation_state import (
        GatedConversationState,
        OPTIONAL_QUALIFICATION_FIELDS,
        PINCODE_FIELDS,
        STATE_MANAGED_RATE_FIELDS,
        SemanticAnswerGuard,
        is_anything_else_no_answer,
    )
    from session_runtime import VoiceSessionRuntime
except ModuleNotFoundError:  # Package imports used by the unit-test runner.
    from .conversation_state import (
        GatedConversationState,
        OPTIONAL_QUALIFICATION_FIELDS,
        PINCODE_FIELDS,
        STATE_MANAGED_RATE_FIELDS,
        SemanticAnswerGuard,
        is_anything_else_no_answer,
    )
    from .session_runtime import VoiceSessionRuntime


load_dotenv(os.getenv("SHIPKIA_ENV_FILE", ".env.local"), override=False)

logger = logging.getLogger("shipkia-livekit-agent")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "shipkia-voice-sales")
STRICT_PROMPT_VERSIONS = frozenset(
    {"shipkia-voice-v4", "shipkia-voice-v5", "shipkia-voice-v6"}
)
EXACT_OPENING_PROMPT_VERSIONS = frozenset({*STRICT_PROMPT_VERSIONS, "shipkia-voice-v6"})
MCP_GATEWAY = os.getenv("MCP_SERVER_URL", "").strip()
CONFLUENCE_CALLBACK = os.getenv("CONFLUENCE_LIVEKIT_WEBHOOK_URL", "").strip()
CONSOLE_MCP_SCOPE = "livekit-console-sandbox"

_SHIPKIA_COURIER_PARTNERS = (
    "Amazon",
    "Bluedart",
    "Delhivery",
    "E-Kart",
    "Shadowfax",
    "Shree Maruti",
    "Xpressbees",
)

ALLOWED_TOOLS = {
    "lookup_shipkia_crm_lead",
    "create_or_update_shipkia_lead",
    "record_shipkia_call_progress",
    "create_shipkia_followup",
    "finalize_shipkia_call_outcome",
    "lookup_pincode_serviceability",
    "get_shipkia_starting_rate",
    "get_shipkia_flat_rates",
    "get_shipkia_flat_zonal_rates",
    "calculate_shipkia_rate",
}
PRICING_TOOLS = {
    "lookup_pincode_serviceability",
    "get_shipkia_starting_rate",
    "get_shipkia_flat_rates",
    "get_shipkia_flat_zonal_rates",
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
    "business_platform": {
        "type": "string",
        "description": (
            "How the customer operates or receives orders, stated in their own words, such as "
            "Shopify, WooCommerce, marketplace, custom website, offline, or another platform."
        ),
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
            "business_platform",
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
    "pickup_pincode_status": {
        "type": "string",
        "enum": ["Unavailable"],
        "description": (
            "Worker-controlled state used only after the customer was asked for the pickup pincode "
            "and explicitly said it is unavailable."
        ),
    },
    "delivery_pincode_status": {
        "type": "string",
        "enum": ["Unavailable"],
        "description": (
            "Worker-controlled state used only after the customer was asked for the delivery "
            "pincode and explicitly said it is unavailable."
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
            "COD explicitly refuses to share order value; authoritative state will route that turn "
            "to get_shipkia_starting_rate instead of this calculator."
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
_PAYMENT_MODE_SERVICE_ALIASES = {
    "prepaid",
    "pre-paid",
    "cod",
    "cash on delivery",
    "both",
    "dono",
    "donon",
    "dona",
    "दोनों",
    "दोनो",
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
    "conversation_consent": "permission to continue the conversation",
    "assistance_intent": "choice between checking rates and onboarding help",
    "business_name": "business or brand name",
    "business_type": "business type",
    "business_platform": "business operating platform",
    "current_shipping_arrangement": "current shipping arrangement",
    "current_provider_name": "current courier or aggregator name",
    "current_shipping_rate": "current comparable shipping rate",
    "current_problem": "main problem with the current shipping arrangement",
    "pickup_pincode": "6-digit pickup pincode",
    "delivery_pincode": "6-digit delivery pincode",
    "pickup_location": "pickup city or locality",
    "delivery_location": "delivery city or locality",
    "dead_weight": "shipment weight",
    "payment_type": "Prepaid or COD payment mode",
    "order_value": "COD order value",
    "monthly_shipments": "monthly shipment quantity",
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
_STRONG_ENGLISH_TOKENS = {
    "i",
    "my",
    "we",
    "our",
    "you",
    "your",
    "what",
    "which",
    "why",
    "how",
    "is",
    "are",
    "am",
    "want",
    "need",
    "please",
    "tell",
    "check",
    "give",
    "show",
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


_SPOKEN_CURRENCY_AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|rupees?|inr)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _shipkia_rate_claim_amounts(value: object) -> list[float]:
    """Return numeric ShipKia rate claims that require backend authorization.

    Customer-comparison amounts may legitimately be repeated by the assistant, so a
    currency amount is gated only when the response presents it as ShipKia pricing or
    as a starting rate.
    """
    text = str(value or "")
    clean = _normalized_text(text)
    if not clean:
        return []
    is_shipkia_claim = bool(
        "shipkia" in clean
        or re.search(r"\bflat(?:[\s-]*zonal)?\b.{0,25}\b(?:rate|rates|price|pricing)\b", clean)
        or re.search(r"\b(?:e[\s-]*kart|ekart)\b", clean)
        or re.search(r"\bstarting\s+rate\b", clean)
        or re.search(
            r"\b(?:rate|rates|price|pricing)\b.{0,35}\b(?:start|starts|starting|shuru)\b"
            r"|\b(?:start|starts|starting|shuru)\b.{0,35}\b(?:rate|rates|price|pricing)\b",
            clean,
        )
    )
    if not is_shipkia_claim:
        return []
    amounts: list[float] = []
    for match in _SPOKEN_CURRENCY_AMOUNT_RE.finditer(text):
        local_context = clean[max(0, match.start() - 35) : match.end() + 20]
        if re.search(r"\b(?:order|cod)\s*(?:value|amount)\b", local_context):
            # A customer-supplied COD order value is an input, not a ShipKia
            # price claim, even when the same sentence mentions Flat rates.
            continue
        try:
            amounts.append(float(match.group(1).replace(",", "")))
        except (TypeError, ValueError):
            continue
    for match in re.finditer(
        r"\b(?:starting\s+rate|rate)\b[^\d]{0,14}([\d,]+(?:\.\d+)?)"
        r"|\b([\d,]+(?:\.\d+)?)\s*(?:rupees?|rs\.?)?\s*(?:se\s+)?shuru\b",
        clean,
        re.IGNORECASE,
    ):
        raw = match.group(1) or match.group(2)
        try:
            amount = float(raw.replace(",", ""))
        except (TypeError, ValueError):
            continue
        if amount not in amounts:
            amounts.append(amount)
    return amounts


def _provider_rate_response_complete(
    agent_text: object,
    conversation_state: GatedConversationState,
) -> bool:
    """Require each verified option's exact amount beside its courier/service name."""
    clean = _normalized_text(agent_text)
    options = conversation_state.verified_starting_options
    if not clean or not options:
        return False

    aliases = {
        "bluedart": ("bluedart", "blue dart"),
        "e-kart": ("e-kart", "e kart", "ekart"),
        "xpressbees": ("xpressbees", "xpress bees"),
    }
    mentions: list[tuple[int, dict[str, Any]]] = []
    for option in options:
        courier = _normalized_text(option.get("courier"))
        service = _normalized_text(option.get("service"))
        variants = {courier, service, courier.replace("-", " "), service.replace("-", " ")}
        variants.update(aliases.get(courier, ()))
        positions = [clean.find(name) for name in variants if name and name in clean]
        if not positions:
            return False
        mentions.append((min(positions), option))

    mentions.sort(key=lambda item: item[0])
    for index, (position, option) in enumerate(mentions):
        end = mentions[index + 1][0] if index + 1 < len(mentions) else len(clean)
        segment = clean[position:end]
        amount = float(option.get("amount") or 0)
        exact_amount = f"{amount:.2f}"
        if exact_amount not in segment:
            return False
    return True


def _assistant_pincode_claims(value: object) -> list[str]:
    """Return six-digit route claims from an assistant transcript."""
    return list(dict.fromkeys(re.findall(r"\b\d{6}\b", str(value or ""))))


def _assistant_single_zone_claims(value: object) -> list[str]:
    """Return singular Zone A-F claims, excluding Flat-Zonal ranges like A-B."""
    return list(
        dict.fromkeys(
            match.group(1).upper()
            for match in re.finditer(
                r"\bzone\s*([a-f])\b(?!\s*[-–]\s*[a-f])",
                str(value or ""),
                re.IGNORECASE,
            )
        )
    )


_CUSTOMER_USP_QUERY_RE = re.compile(
    r"(?:\b(?:benefit|benefits|advantage|advantages|feature|features|facility|facilities|"
    r"procedure|process|how\s+(?:do|does)\s+(?:you|ship\s*kia)\s+work|kaise\s+kaam)\b|"
    r"\u092c\u0947\u0928\u093f\u092b\u093f\u091f|\u092b\u093e\u092f\u0926\u093e|\u092a\u094d\u0930\u094b\u0938\u0940\u091c\u0930|"
    r"\u092a\u094d\u0930\u094b\u0938\u0947\u0938|\u0915\u0948\u0938\u0947.{0,20}\u0915\u093e\u092e)",
    re.IGNORECASE,
)
_UNSUPPORTED_USP_CLAIM_RE = re.compile(
    r"\b(?:guaranteed?|guarantee|assured?)\s+(?:saving|savings|discount|delivery|rate)\b|"
    r"\b(?:fixed|minimum)\s+(?:saving|savings|discount)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:percent|%)\s+(?:saving|savings|discount)\b|"
    r"\bdelivery\s+(?:is\s+)?guaranteed\b",
    re.IGNORECASE,
)
_SPOKEN_ONBOARDING_URL_RE = re.compile(
    r"\bauth(?:\s+dot|\.)\s*shipkia(?:\s+dot|\.)\s*com(?:\s+slash|/)\s*signup\b",
    re.IGNORECASE,
)
_AGENT_MOVE_FORWARD_RE = re.compile(
    r"(?:ship\s*kia.{0,40}aage\s+(?:badhna|badna|badh)|"
    r"aage\s+(?:badhna|badna|badh).{0,40}ship\s*kia|"
    r"\bkya\s+aap\b.{0,25}\baage\s+(?:badhna|badna|badh)\w*\b)",
    re.IGNORECASE,
)
_AGENT_RESOLUTION_CLOSE_RE = re.compile(
    r"(?=.*\bbetter\s+plan\b)(?=.*\bteam\b)(?=.*\b(?:discuss|solution)\b)",
    re.IGNORECASE,
)
_AGENT_PLAN_OFFER_RE = re.compile(
    r"\b(?:better|alag|custom|dedicated|special)\s+plan\b.{0,55}"
    r"\b(?:discuss|offer|provide|de|bana)\b|"
    r"\b(?:discuss|offer|provide|de|bana)\b.{0,55}"
    r"\b(?:better|alag|custom|dedicated|special)\s+plan\b",
    re.IGNORECASE,
)
_CUSTOMER_OPTIONAL_REFUSAL_RE = re.compile(
    r"\b(?:nahi|nahin|nhi|cannot|can't|cant|won't|wont|do not|don't)\b"
    r".{0,35}\b(?:bata|share|disclose|provide)\b|"
    r"\b(?:bata|share|disclose|provide)\b.{0,25}"
    r"\b(?:nahi|nahin|nhi|cannot|can't|cant|won't|wont)\b",
    re.IGNORECASE,
)
_AGENT_ANYTHING_ELSE_RE = re.compile(
    r"(?:kya\s+aap\s+(?:kuch\s+aur|aur\s+kuch)\s+(?:jaan-?na|jaanna|janna)|"
    r"anything\s+else)",
    re.IGNORECASE,
)
_AGENT_RATE_PERMISSION_RE = re.compile(
    r"\bkya\s+aap\b.{0,45}\b(?:rate|rates)\b.{0,35}"
    r"\b(?:chah|chaah|jaan|janna|jaanna|check|dekh)\w*\b|"
    r"\bwould\s+you\s+like\b.{0,55}\b(?:check|know|see|hear)\b.{0,25}"
    r"\b(?:rate|rates|pricing)\b|"
    r"\bdo\s+you\s+want\b.{0,45}\b(?:rate|rates|pricing)\b",
    re.IGNORECASE,
)
_AGENT_COD_QUESTION_RE = re.compile(
    r"\bcod\b|\bcash\s+on\s+delivery\b",
    re.IGNORECASE,
)
_CUSTOMER_POLITE_END_RE = re.compile(
    r"^(?:no[,.! ]*(?:thank\s*you|thanks)|thank\s*you[,.! ]*(?:bye|goodbye)?|"
    r"thanks[,.! ]*(?:bye|goodbye)?|that's\s+all|that\s+is\s+all|"
    r"bas[,.! ]*(?:thank\s*you|thanks|dhanyavaad)|"
    r"\u0928\u0939\u0940\u0902[,.!\u0964 ]*(?:\u0927\u0928\u094d\u092f\u0935\u093e\u0926|\u0925\u0948\u0902\u0915\s*\u092f\u0942))"
    r"[.!?\u0964]*$",
    re.IGNORECASE,
)
_AGENT_ONBOARDING_LINK_RE = re.compile(
    r"\b(?:signup|sign[\s-]*up|onboarding)\b.{0,45}\b(?:link|whatsapp)\b|"
    r"\b(?:link|whatsapp)\b.{0,45}\b(?:signup|sign[\s-]*up|onboarding)\b",
    re.IGNORECASE,
)
_AGENT_POLITE_FAREWELL_RE = re.compile(
    r"\b(?:thank\s*you|thanks|shukriya|dhanyavaad)\b.{0,80}"
    r"\b(?:time|baat|day|calling|call)\b|\bhave\s+a\s+good\s+day\b",
    re.IGNORECASE,
)
_AGENT_ROUTE_RATE_AVAILABILITY_RE = re.compile(
    r"\b(?:rate|rates|pricing)\b.{0,30}\b(?:available|mil\s+(?:raha|rahi|rahe)|"
    r"start(?:s|ing)?\s+from)\b|"
    r"\b(?:available|mil\s+(?:raha|rahi|rahe))\b.{0,30}\b(?:rate|rates|pricing)\b",
    re.IGNORECASE,
)
_AGENT_EARLY_INFO_CONTINUATION_RE = re.compile(
    r"(?:kuch\s+aur|aur\s+kuch).{0,100}\brates?\b.{0,100}\bonboarding\b|"
    r"\brates?\b.{0,100}\bonboarding\b.{0,100}(?:kuch\s+aur|aur\s+kuch)",
    re.IGNORECASE,
)
_AGENT_FLAT_ZONAL_CLAIM_RE = re.compile(
    r"\bflat[\s-]*zonal\b.{0,120}\b(?:rates?\s+available|rs\s*\d|zones?\s+[a-f])\b|"
    r"\be[\s-]*kart\s+express\b.{0,80}\bzones?\s+[a-f]",
    re.IGNORECASE,
)
_QUALIFICATION_BRIDGE_RE = re.compile(
    r"\brates?\s+batane\s+se\s+pehle\b.{0,90}"
    r"\b(?:details?|cheezein|cheezen|baatein)\b.{0,70}\b(?:jaan|pooch)",
    re.IGNORECASE,
)
_CUSTOMER_CORRECTION_RE = re.compile(
    r"\b(?:change|changed|correct|correction|actually|instead|update|galat|sahi|theek\s+karo)\b|"
    r"\u092c\u0926\u0932|\u0917\u0932\u0924|\u0938\u0939\u0940\s+\u0915\u0930",
    re.IGNORECASE,
)
_NOISE_ONLY_GREETING_RE = re.compile(r"^(?:hello|hallo|halo|alo|hi|hey)[.!?]*$", re.IGNORECASE)
_OPENING_ACTIONABLE_SHORT_RE = re.compile(
    r"\b(?:hello|hallo|halo|helo|hi|hey|haan|han|yes|yeah|yep|ji|ok|okay|theek|bataiye|bataye|boliye|no|nahi|nahin|"
    r"rate|rates|onboarding|service|services|shipkia|courier|wait|hold|ruko|ruk|busy|"
    r"later|baad|minute|interested|wrong|number)\b|"
    r"(?:हां|हा|जी|बताइए|बोलिए|ठीक|नहीं|रेट|सर्विस|रुकिए|बाद|मिनट)",
    re.IGNORECASE,
)
_AGENT_OPENING_RE = re.compile(
    r"\bnamaste\b.{0,100}\bshipkia\b.{0,180}\b(?:shipping\s+query|do\s+minute\s+baat)\b|"
    r"\bhumein\s+aapki\s+shipping\s+query\s+mili\s+thi\b|"
    r"\bkya\s+abhi\s+hum\s+do\s+minute\s+baat\s+kar\s+sakte\s+hain\b",
    re.IGNORECASE,
)

_UNSUPPORTED_ASR_SCRIPT_RE = re.compile(
    # ShipKia currently supports English/Hinglish (Latin) and Hindi
    # (Devanagari). These blocks appeared in call-1838 when background noise
    # was hallucinated as Urdu, Korean and Japanese speech.
    r"[\u0600-\u06ff\u0750-\u077f\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\uac00-\ud7af]"
)
_ASR_NOISE_NEGATIVE_RE = re.compile(
    r"\b(?:no|nope|nah+i|nai|nhi|nahin)\b|\u0928\u0939\u0940\u0902",
    re.IGNORECASE,
)
_ASR_ACTIONABLE_RE = re.compile(
    r"\b(?:rate|rates|pricing|onboarding|courier|shipment|shipping|service|services|"
    r"business|company|brand|pickup|delivery|pincode|weight|cod|prepaid|flat|zone|"
    r"problem|issue|manager|support|yes|haan|han|ji|bata|batao|chahiye|want)\b|"
    r"\u0930\u0947\u091f|\u0915\u0942\u0930\u093f\u092f\u0930|\u0936\u093f\u092a\u092e\u0947\u0902\u091f|\u0939\u093e\u0901|\u0939\u093e\u0902|\u091a\u093e\u0939\u093f\u090f",
    re.IGNORECASE,
)


def _asr_noise_reason(
    customer_text: object,
    *,
    language: object = None,
    confidence: object = None,
) -> str:
    """Return why a final realtime transcript is unsafe to treat as speech."""
    clean = _normalized_text(customer_text).strip()
    if not clean:
        return "empty_transcript"

    language_code = str(language or "").strip().casefold().replace("_", "-")
    if language_code and not language_code.startswith(("en", "hi")):
        return f"unsupported_language:{language_code[:16]}"
    if _UNSUPPORTED_ASR_SCRIPT_RE.search(clean):
        return "unsupported_script"
    if "\u00bf" in clean or "\u00a1" in clean:
        return "unexpected_language_punctuation"
    if confidence is not None:
        try:
            if float(confidence) < 0.45:
                return "low_transcript_confidence"
        except (TypeError, ValueError):
            pass

    negatives = _ASR_NOISE_NEGATIVE_RE.findall(clean)
    if len(negatives) >= 2 and not _ASR_ACTIONABLE_RE.search(clean):
        return "repeated_negative_fragment"
    if re.fullmatch(
        r"(?:m+|h+m+|uh+|um+|huh+)[\s,.-]+(?:no|nahi|nai|nhi|nahin)[.!?]*",
        clean,
        re.IGNORECASE,
    ):
        return "filler_negative_fragment"
    return ""


def _is_opening_noise_turn(
    customer_text: object,
    conversation_state: GatedConversationState,
) -> bool:
    """Reject a short non-actionable ASR fragment while consent is pending."""
    if conversation_state.pending_field() != "conversation_consent":
        return False
    clean = _normalized_text(customer_text).strip(" .,!?।")
    if not clean or len(clean.split()) > 2:
        return False
    return not bool(_OPENING_ACTIONABLE_SHORT_RE.search(clean))
_HANDLED_QUESTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "conversation_consent",
        re.compile(r"\b(?:do\s+minute|convenient\s+time|abhi\s+baat|baat\s+kar\s+sakte)\b"),
    ),
    (
        "assistance_intent",
        re.compile(r"(?=.*\b(?:rate|rates)\b)(?=.*\bonboarding\b)"),
    ),
    (
        "business_name",
        re.compile(
            r"\b(?:company|business|brand)\b.{0,30}\b(?:name|naam)\b|"
            r"\b(?:name|naam)\b.{0,20}\b(?:company|business|brand)\b"
        ),
    ),
    (
        "business_type",
        re.compile(r"\b(?:b\s*(?:2|to)\s*c|d\s*(?:2|to)\s*c|business\s+type)\b"),
    ),
    (
        "business_platform",
        re.compile(
            r"\b(?:shopify|woo\s*commerce|website|marketplace|platform)\b|"
            r"\b(?:orders?|business)\b.{0,45}\b(?:receive|sell|operate|kahaan\s+se|kahan\s+se)\b|"
            r"\b(?:receive|sell|operate|kahaan\s+se|kahan\s+se)\b.{0,45}\b(?:orders?|business)\b"
        ),
    ),
    (
        "current_shipping_arrangement",
        re.compile(
            r"\b(?:courier|shipping\s+provider|shipping\s+arrangement|aggregator)\b"
            r".{0,45}\b(?:use|using|currently|abhi|arrangement)\b|"
            r"\b(?:use|using|currently|abhi)\b.{0,45}"
            r"\b(?:courier|provider|aggregator)\b"
        ),
    ),
    (
        "current_provider_name",
        re.compile(
            r"\b(?:which|what|kaun|kaunsa|konsa)\b.{0,35}"
            r"\b(?:courier|provider|aggregator)\b|"
            r"\b(?:courier|provider|aggregator)\b.{0,30}\b(?:name|naam)\b"
        ),
    ),
    (
        "current_shipping_rate",
        re.compile(
            r"(?=.*\b(?:current|abhi|comparable|provider|courier|mil)\b)"
            r"(?=.*\b(?:rate|price|charge)\b)|"
            r"\b(?:rate|price|charge)\b.{0,30}\b(?:chal|mil)\b"
        ),
    ),
    (
        "current_problem",
        re.compile(
            r"\b(?:problem|issue|challenge|difficulty|dikkat|pareshani)\b"
        ),
    ),
    (
        "pickup_pincode",
        re.compile(
            r"\b(?:pickup|pick\s*up|origin|kahaan\s+se|kahan\s+se|kaha\s+se|"
            r"shipping\s+kahaan\s+se|shipping\s+kahan\s+se)\b"
        ),
    ),
    (
        "delivery_pincode",
        re.compile(
            r"\b(?:delivery|drop|destination|kahaan\s+tak|kahan\s+tak|kaha\s+tak)\b"
        ),
    ),
    (
        "dead_weight",
        re.compile(r"\b(?:weight|wazan|vajan|kilo|kilogram|grams?)\b"),
    ),
    (
        "payment_type",
        re.compile(r"\b(?:prepaid|pre-paid|cod|cash\s+on\s+delivery|payment\s+mode)\b"),
    ),
    (
        "order_value",
        re.compile(r"\b(?:order\s+value|cod\s+value|order\s+amount)\b"),
    ),
    (
        "monthly_shipments",
        re.compile(
            r"\b(?:monthly\s+shipments?|shipments?\s+per\s+month|monthly\s+orders?|"
            r"shipment\s+quantity|shipment\s+volume)\b"
        ),
    ),
)


def _assistant_question_scopes(agent_text: object) -> tuple[str, ...]:
    """Return only clauses that are functioning as customer questions.

    Sales replies commonly acknowledge the answer before asking the next
    question. Scanning the whole reply makes words in that acknowledgement
    look like a repeated question (for example, acknowledging a support
    problem before asking monthly volume).
    """
    clean = _normalized_text(agent_text)
    if not clean:
        return ()
    clauses = tuple(
        clause.strip(" ,;:-")
        for clause in re.split(r"(?<=[?.!])\s+|[;\n]+", clean)
        if clause.strip(" ,;:-")
    )
    question_cue = re.compile(
        r"\b(?:kya|kaun|kaunsa|konsa|kitna|kitni|kahaan|kahan|which|what|where|"
        r"whether|bataye|do\s+you|would\s+you|how\s+many|how\s+much)\b"
    )
    questions = tuple(
        clause for clause in clauses if "?" in clause or question_cue.search(clause)
    )
    return questions


def _assistant_reasked_handled_field(
    agent_text: object,
    customer_text: object,
    conversation_state: GatedConversationState,
) -> str:
    """Identify a question that asks V5 for context it already owns."""
    agent_clean = _normalized_text(agent_text)
    if not agent_clean or _CUSTOMER_CORRECTION_RE.search(_normalized_text(customer_text)):
        return ""
    question_scopes = _assistant_question_scopes(agent_text)
    if not question_scopes:
        return ""
    # In "Ye prepaid rates hain. Kya aap kuch aur jaanna chahenge?", the
    # only question is the anything-else checkpoint; "prepaid" is an answer,
    # not a repeated payment-mode question. Scope field detection to that
    # question so the guard does not interrupt a valid answer and regenerate.
    pending = conversation_state.pending_field()

    def must_not_reask(field: str) -> bool:
        if field == pending:
            return False
        if field == "pickup_pincode":
            return conversation_state.route_endpoint_handled("pickup")
        if field == "delivery_pincode":
            return conversation_state.route_endpoint_handled("delivery")
        if field in {"business_name", "business_type"} and conversation_state.company_details_ended_by:
            return True
        if field in OPTIONAL_QUALIFICATION_FIELDS and conversation_state.optional_ended_by:
            sequence = conversation_state.optional_sequence()
            try:
                return sequence.index(field) >= sequence.index(conversation_state.optional_ended_by)
            except ValueError:
                return False
        return conversation_state.is_handled(field)

    for field, pattern in _HANDLED_QUESTION_PATTERNS:
        if not any(pattern.search(scope) for scope in question_scopes) or not must_not_reask(field):
            continue
        if field == "current_shipping_arrangement" and pending == "current_provider_name":
            # "Which courier/provider do you use?" can naturally contain the
            # arrangement words while asking the still-missing provider name.
            continue
        if (
            field in {"current_shipping_arrangement", "current_provider_name"}
            and conversation_state.provider_clarification_due
        ):
            continue
        return field
    return ""


def _assistant_question_fields(agent_text: object) -> list[str]:
    """Return authoritative fields the assistant draft is asking about."""
    question_scopes = _assistant_question_scopes(agent_text)
    return [
        field
        for field, pattern in _HANDLED_QUESTION_PATTERNS
        if any(pattern.search(scope) for scope in question_scopes)
    ]


_V6_BLOCKING_FLOW_VIOLATIONS = frozenset(
    {
        "spoken_onboarding_url",
        "unauthorized_onboarding_link",
        "unsupported_usp_claim",
        "unverified_flat_zonal_claim",
        "contradicted_verified_route",
        "unauthorized_better_plan",
        "unsolicited_cod_question",
    }
)


def _flow_violation_requires_correction(
    violation: str,
    *,
    model_led_flow: bool,
    preverified_rate_flow: bool = False,
) -> bool:
    """Keep V6 advisory checks single-owner, except required pre-rate gates."""
    return bool(
        violation
        and (
            not model_led_flow
            or violation in _V6_BLOCKING_FLOW_VIOLATIONS
            or (
                preverified_rate_flow
                and violation.startswith(
                    ("skipped_pending:", "reasked_handled:")
                )
            )
        )
    )


def _question_answers_pending(pending: str, asked_fields: list[str]) -> bool:
    """Treat city/locality and legacy pincode route labels as one endpoint."""
    equivalent = {
        "pickup_location": "pickup_pincode",
        "delivery_location": "delivery_pincode",
    }
    return pending in asked_fields or equivalent.get(pending, "") in asked_fields


def _flat_zonal_catalog_response_complete(
    agent_text: object,
    conversation_state: GatedConversationState,
) -> bool:
    """Require both verified zone groups and additional weight in one answer."""
    expected = {
        round(float(value), 2)
        for value in (
            conversation_state.flat_zonal_group_totals.get("A-B"),
            conversation_state.flat_zonal_group_totals.get("C-F"),
            conversation_state.flat_zonal_additional_total,
        )
        if isinstance(value, (int, float))
    }
    claimed = {round(float(value), 2) for value in _shipkia_rate_claim_amounts(agent_text)}
    clean = _normalized_text(agent_text)
    return bool(
        len(expected) == 3
        and expected.issubset(claimed)
        and re.search(r"\ba\s*[-–]\s*b\b", clean)
        and re.search(r"\bc\s*[-–]\s*f\b", clean)
        and re.search(r"\badditional\b|\bextra\b|\badd-on\b", clean)
    )


def _flat_catalog_response_complete(
    agent_text: object,
    conversation_state: GatedConversationState,
) -> bool:
    """Require E-Kart slabs and the verified Shadowfax condition together."""
    expected = {
        round(float(item.get("total") or 0), 2)
        for item in conversation_state.flat_catalog_options
        if isinstance(item, dict) and float(item.get("total") or 0) > 0
    }
    for item in conversation_state.flat_additional_options:
        if not isinstance(item, dict):
            continue
        breakdown = item.get("flat_additional_rate_breakdown")
        if isinstance(breakdown, dict) and float(breakdown.get("total") or 0) > 0:
            expected.add(round(float(breakdown["total"]), 2))
    claimed = {round(float(value), 2) for value in _shipkia_rate_claim_amounts(agent_text)}
    clean = _normalized_text(agent_text)
    return bool(
        len(expected) >= 4
        and expected.issubset(claimed)
        and re.search(r"\be[\s-]*kart\b", clean)
        and "shadowfax" in clean
        and re.search(r"\badditional\b|\bextra\b|\badd-on\b", clean)
    )


def _shipkia_flow_response_violation(
    *,
    agent_text: object,
    customer_text: object,
    previous_agent_text: object,
    conversation_state: GatedConversationState,
) -> str:
    """Return a V5 flow violation that must be corrected before playout continues."""
    if not conversation_state.v5_company_pair_flow:
        return ""
    agent_clean = _normalized_text(agent_text)
    customer_clean = _normalized_text(customer_text)
    previous_clean = _normalized_text(previous_agent_text)
    if not agent_clean:
        return ""

    if _SPOKEN_ONBOARDING_URL_RE.search(agent_clean):
        return "spoken_onboarding_url"
    if conversation_state.polite_close_due:
        return "" if _AGENT_POLITE_FAREWELL_RE.search(agent_clean) else "polite_close_omitted"
    if (
        _AGENT_ONBOARDING_LINK_RE.search(agent_clean)
        and not conversation_state.onboarding_link_due
    ):
        return "unauthorized_onboarding_link"
    if (
        conversation_state.is_handled("conversation_consent")
        and _AGENT_OPENING_RE.search(agent_clean)
    ):
        return "restarted_opening"
    if (
        conversation_state.model_led_flow
        and any(
            _AGENT_COD_QUESTION_RE.search(scope)
            for scope in _assistant_question_scopes(agent_text)
        )
        and conversation_state.pending_field() != "order_value"
    ):
        return "unsolicited_cod_question"
    if (
        _AGENT_FLAT_ZONAL_CLAIM_RE.search(agent_clean)
        and not conversation_state.flat_zonal_catalog_presented
    ):
        return "unverified_flat_zonal_claim"
    if (
        conversation_state.flat_catalog_delivery_due
        and not _flat_catalog_response_complete(agent_text, conversation_state)
    ):
        return "flat_catalog_omitted"
    if (
        conversation_state.flat_zonal_catalog_delivery_due
        and _AGENT_FLAT_ZONAL_CLAIM_RE.search(agent_clean)
        and not _flat_zonal_catalog_response_complete(agent_text, conversation_state)
    ):
        return "flat_zonal_catalog_omitted"
    if (
        conversation_state.anything_else_checkpoint_consumed
        and _AGENT_ANYTHING_ELSE_RE.search(agent_clean)
    ):
        return "repeated_anything_else_checkpoint"
    if (
        conversation_state.value("assistance_intent") == "Rates"
        and _AGENT_RATE_PERMISSION_RE.search(agent_clean)
    ):
        return "reopened_rate_intent"
    if (
        conversation_state.value("assistance_intent") == "Rates"
        and _AGENT_ROUTE_RATE_AVAILABILITY_RE.search(agent_clean)
        and not conversation_state.verified_rate_presented()
    ):
        return "unverified_route_rate_availability"
    if (
        conversation_state.rate_answer_owed
        and conversation_state.primary_rate_amount is not None
        and not any(
            abs(amount - conversation_state.primary_rate_amount) < 0.011
            for amount in _shipkia_rate_claim_amounts(agent_text)
        )
    ):
        return "owed_rate_omitted"

    previous_questions = _assistant_question_fields(previous_agent_text)
    draft_questions = _assistant_question_fields(agent_text)
    early_information_resume = bool(
        conversation_state.pending_field() == "assistance_intent"
        and (conversation_state.last_usp_query or conversation_state.last_provider_options_query)
        and _AGENT_EARLY_INFO_CONTINUATION_RE.search(agent_clean)
    )
    if _CUSTOMER_OPTIONAL_REFUSAL_RE.search(customer_clean):
        refused_optional = next(
            (field for field in previous_questions if field in OPTIONAL_QUALIFICATION_FIELDS),
            "",
        )
        if refused_optional:
            refused_index = OPTIONAL_QUALIFICATION_FIELDS.index(refused_optional)
            if any(
                field in OPTIONAL_QUALIFICATION_FIELDS
                and OPTIONAL_QUALIFICATION_FIELDS.index(field) > refused_index
                for field in draft_questions
            ):
                return "advanced_after_optional_refusal"

    if (
        conversation_state.qualification_bridge_due()
        and "business_name" in _assistant_question_fields(agent_text)
        and not _QUALIFICATION_BRIDGE_RE.search(agent_clean)
    ):
        return "qualification_bridge_omitted"

    if (
        conversation_state.better_plan_close_presented
        or conversation_state.unsatisfied_resolution_presented
    ) and _AGENT_RESOLUTION_CLOSE_RE.search(agent_clean):
        return "repeated_resolution_close"

    reasked_field = _assistant_reasked_handled_field(
        agent_text,
        customer_text,
        conversation_state,
    )
    if reasked_field:
        return f"reasked_handled:{reasked_field}"

    if (
        conversation_state.route_zone_lookup_status == "verified_starting"
        and conversation_state.is_confirmed("zone")
        and re.search(
            r"\b(?:zone|route)\b.{0,45}\b(?:not|nahi|nahin|nhi)\b.{0,30}"
            r"\b(?:verified|verify|available|resolve|confirm)\b|"
            r"\b(?:zone|route)\b.{0,30}\b(?:verify|resolve|confirm)\b.{0,25}"
            r"\b(?:nahi|nahin|nhi|couldn't|cannot|can't)\b",
            agent_clean,
        )
    ):
        return "contradicted_verified_route"

    if (
        conversation_state.monthly_quantity_due
        and conversation_state.primary_rate_amount is not None
        and _AGENT_ANYTHING_ELSE_RE.search(agent_clean)
    ):
        return "verified_rate_omitted"

    if (
        conversation_state.anything_else_detail_due
        and _AGENT_ANYTHING_ELSE_RE.search(agent_clean)
    ):
        return "anything_else_detail_not_requested"

    if (
        conversation_state.provider_rates_answer_due
        and conversation_state.verified_starting_options
    ):
        if not _provider_rate_response_complete(agent_text, conversation_state):
            return "provider_rates_incomplete"

    unauthorized_better_plan = bool(
        (
            "better plan" in agent_clean
            and re.search(r"\b(?:team|discuss|solution)\b", agent_clean)
            or _AGENT_PLAN_OFFER_RE.search(agent_clean)
        )
        and not conversation_state.better_plan_close_due
        and not conversation_state.unsatisfied_resolution_due
    )
    if unauthorized_better_plan:
        return "unauthorized_better_plan"

    if _CUSTOMER_USP_QUERY_RE.search(customer_clean) or conversation_state.last_usp_query:
        if _UNSUPPORTED_USP_CLAIM_RE.search(agent_clean):
            return "unsupported_usp_claim"
        support_specific = bool(re.search(r"\b(?:support|ticket|account manager)\b", customer_clean))
        order_specific = bool(re.search(r"\b(?:order|confirmation|whatsapp)\b", customer_clean))
        ndr_specific = bool(re.search(r"\b(?:ndr|rto|delivery exception|ivr)\b", customer_clean))
        if support_specific or order_specific or ndr_specific:
            answered_specific = bool(
                support_specific and "account manager" in agent_clean
                or order_specific and "whatsapp" in agent_clean and "confirmation" in agent_clean
                or ndr_specific and ("ivr" in agent_clean or "whatsapp" in agent_clean)
            )
            if not answered_specific:
                return "usp_ignored"
        else:
            verified_usp_count = sum(
                (
                    bool(
                        re.search(
                            r"\b(?:multiple|different|several)\s+courier(?:\s+partners?)?\b"
                            r"|\bshipment(?:s)?\s+manage(?:ment)?\b",
                            agent_clean,
                        )
                    ),
                    "account manager" in agent_clean,
                    "whatsapp" in agent_clean and "confirmation" in agent_clean,
                    "ndr" in agent_clean and ("ivr" in agent_clean or "whatsapp" in agent_clean),
                )
            )
            required_usp_count = 4 if conversation_state.last_detailed_usp_query else 2
            if verified_usp_count < required_usp_count:
                return "usp_ignored"

    if (
        _AGENT_ANYTHING_ELSE_RE.search(agent_clean)
        and not conversation_state.anything_else_question_due
        and not early_information_resume
    ):
        # The information checkpoint belongs to one worker-authorized point
        # after the first verified rate and monthly quantity. It must not be
        # improvised during discovery, service answers, or later catalog/rate
        # follow-ups. USP completeness is checked first so a correction still
        # answers every requested service instead of emitting only a closing.
        return "unexpected_anything_else_checkpoint"

    pending = conversation_state.pending_field()
    asked_fields = _assistant_question_fields(agent_text)
    if (
        pending
        and pending in asked_fields
        and pending in _assistant_question_fields(previous_agent_text)
        and not early_information_resume
    ):
        return f"repeated_pending:{pending}"
    if (
        pending
        and asked_fields
        and not _question_answers_pending(pending, asked_fields)
        and (
            not conversation_state.verified_rate_presented()
            or conversation_state.model_led_flow
        )
    ):
        allowed_provider_pair = bool(
            pending == "current_shipping_arrangement"
            and "current_provider_name" in asked_fields
        )
        if not allowed_provider_pair:
            return f"skipped_pending:{pending}"

    if not _AGENT_MOVE_FORWARD_RE.search(agent_clean):
        return ""
    contextual_anything_else_no = bool(
        _AGENT_ANYTHING_ELSE_RE.search(previous_clean)
        and is_anything_else_no_answer(customer_clean)
    )
    if (
        conversation_state.monthly_quantity_due
        or (
            not conversation_state.move_forward_question_due
            and not contextual_anything_else_no
        )
        or conversation_state.onboarding_link_due
        or conversation_state.better_plan_close_due
    ):
        return "premature_move_forward"
    if conversation_state.last_customer_dissatisfied:
        explained = bool(
            re.search(
                r"\b(?:starting|comparison|compare|exact|volume|quantity|dedicated\s+plan|"
                r"team|route|courier|service)\b",
                agent_clean,
            )
        )
        if not explained:
            return "pricing_objection_ignored"
    if _AGENT_MOVE_FORWARD_RE.search(previous_clean) and agent_clean == previous_clean:
        return "repeated_move_forward"
    return ""


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
    if clean == "hindi" or re.search(
        r"\bhindi\s+(?:mein|me|please|only)\b"
        r"|\b(?:speak|reply|talk|continue|answer)\s+(?:to me\s+)?in\s+hindi\b",
        clean,
    ):
        return "Hindi"
    if clean == "hinglish" or re.search(
        r"\bhinglish\s+(?:mein|me|please|only)\b"
        r"|\b(?:speak|reply|talk|continue|answer)\s+(?:to me\s+)?in\s+hinglish\b",
        clean,
    ):
        return "Hinglish"
    if _DEVANAGARI_RE.search(str(text or "")):
        return "Hindi"

    tokens = _WORD_RE.findall(clean)
    if not tokens or set(tokens).issubset(_LANGUAGE_NEUTRAL_TOKENS):
        return current_language
    if any(token in _HINGLISH_MARKERS for token in tokens):
        return "Hinglish"
    # A route/name/code answer such as "Delhi to Mumbai", "Shiprocket",
    # "B2C", or "Rs 50" does not express a language preference. Switch to
    # English only when the turn has enough English sentence structure.
    if sum(token in _STRONG_ENGLISH_TOKENS for token in tokens) >= 2:
        return "English"
    return current_language


def _detailed_services_reply_instruction(
    response_language: str,
    conversation_state: GatedConversationState | None = None,
) -> str:
    model_led = bool(conversation_state and conversation_state.model_led_flow)
    if response_language == "English":
        response = (
            "ShipKia has four main services. First, you can manage shipments across multiple "
            "courier partners from one platform. Second, a dedicated account manager helps with "
            "support queries and ticketing. Third, order confirmation is sent on WhatsApp first, "
            "with a call fallback when there is no response. Fourth, delivery NDR follow-up uses "
            "both WhatsApp and IVR calls."
        )
        if not model_led:
            response += (
                " Would you like to know anything else, or may I help you check rates or with "
                "onboarding?"
            )
    else:
        response = (
            "ShipKia ki chaar main services hain. Pehli, aap ek platform par multiple courier "
            "partners ke saath shipments manage kar sakte hain. Dusri, support queries aur ticketing "
            "ke liye dedicated account manager help karta hai. Teesri, order confirmation pehle "
            "WhatsApp se hota hai, aur response na aane par call fallback hota hai. Chauthi, delivery "
            "NDR follow-up mein WhatsApp aur IVR calls dono use hote hain."
        )
        if not model_led:
            response += (
                " Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya onboarding "
                "mein help karun?"
            )
    closing_rule = (
        "End after the useful answer without asking whether they want rates, onboarding, or "
        "anything else; the central V6 sales flow owns the next step."
        if model_led
        else "Do not repeat the closing question."
    )
    return (
        "This is a controlled detailed-services response in an active call. Say exactly once and "
        f"say nothing else: \"{response}\" Never greet, split it into another response, call a "
        f"tool or shorten or generalize any of the four explanations. {closing_rule} "
        "Every clause is required. In particular, explicitly say that WhatsApp confirmation comes "
        "first, a call is the fallback when there is no response, and NDR uses both WhatsApp and "
        "IVR calls."
    )


def _provider_options_reply_instruction(
    response_language: str,
    conversation_state: GatedConversationState,
) -> str:
    names = ", ".join(
        conversation_state.available_courier_partners
        or _SHIPKIA_COURIER_PARTNERS
    )
    pending = conversation_state.pending_field()
    if response_language == "English":
        answer = (
            f"ShipKia has courier partners including {names}. Applicable services and rates are "
            "verified from the relevant shipment details."
        )
        continuation = {
            "conversation_consent": " Is this a convenient time for a two-minute conversation?",
            "assistance_intent": (
                " What is your main shipping priority right now?"
                if conversation_state.model_led_flow
                else " Would you like to check shipping rates or get onboarding help?"
            ),
            "business_name": (
                " Before sharing rates, I would like to know a few necessary details. What is "
                "your business or brand name?"
            ),
            "business_type": " Is your business B2C or D2C?",
            "business_platform": (
                " How do you operate your business: through Shopify, WooCommerce, a marketplace, "
                "your own website, or something else?"
            ),
            "current_shipping_arrangement": (
                " Which courier or shipping arrangement do you currently use?"
            ),
            "current_provider_name": " Which courier or aggregator do you currently use?",
            "current_shipping_rate": " What is your current comparable shipping rate?",
            "current_problem": " What is your main current shipping challenge?",
        }.get(pending, " Would you like to know anything else?")
    else:
        answer = (
            f"ShipKia par {names} jaise courier partners available hain. Applicable services aur "
            "rates relevant shipment details ke basis par verify hote hain."
        )
        continuation = {
            "conversation_consent": " Kya abhi hum do minute baat kar sakte hain?",
            "assistance_intent": (
                " Abhi aapki shipping mein sabse badi priority kya hai?"
                if conversation_state.model_led_flow
                else " Aap shipping rates check karna chahenge ya onboarding mein help chahiye?"
            ),
            "business_name": (
                " Rates batane se pehle main aapse kuch zaroori details jaan lena chahunga. "
                "Aapke business ya brand ka naam kya hai?"
            ),
            "business_type": " Aapka business B2C hai ya D2C?",
            "business_platform": (
                " Aap apna business kaise operate karte hain—Shopify, WooCommerce, marketplace, "
                "apni website, ya kisi aur platform se?"
            ),
            "current_shipping_arrangement": (
                " Aap abhi kaunsa courier ya shipping arrangement use karte hain?"
            ),
            "current_provider_name": " Aap abhi kaunsa courier ya aggregator use karte hain?",
            "current_shipping_rate": " Aapka current comparable shipping rate kya hai?",
            "current_problem": " Aapki main current shipping problem kya hai?",
        }.get(pending, " Kya aap kuch aur jaanna chahenge?")
    response = answer + continuation
    return (
        "This is a controlled courier-partner response in an active call. Say exactly once and "
        f"say nothing else: \"{response}\" Never greet, split it into another response, quote a "
        "rate, call a tool, repeat any information, or ask any other question."
    )


def _high_volume_manager_reply_instruction(response_language: str, quantity: int) -> str:
    formatted_quantity = f"{quantity:,}"
    if response_language == "English":
        response = (
            f"Understood, your monthly shipment volume is {formatted_quantity}. For this volume, "
            "you will get a dedicated account manager who will help you with support and "
            "ticketing. Would you like to know anything else?"
        )
    else:
        response = (
            f"Theek hai, aapki monthly shipments {formatted_quantity} hain. Is volume ke liye "
            "aapko ek dedicated account manager milega jo support aur ticketing mein aapki help "
            "karega. Kya aap kuch aur jaanna chahenge?"
        )
    return (
        "This is a controlled high-volume response in an active call. Say exactly once and say "
        f"nothing else: \"{response}\" Never ask for monthly shipments again; "
        "never add another qualification question, quote a rate, call a tool, or repeat the "
        "anything-else question."
    )


def _high_volume_manager_delivery_complete(text: object) -> bool:
    """True only after the customer-facing manager benefit was spoken."""
    clean = _normalized_text(text)
    return bool(
        "dedicated account manager" in clean
        and ("support" in clean or "ticketing" in clean)
    )


def _gemini_start_sensitivity() -> genai_types.StartSensitivity:
    """Return the configured speech-start profile, defaulting fail-safe for noise."""
    configured = os.getenv("GEMINI_VAD_START_SENSITIVITY", "LOW").strip().upper()
    if configured == "HIGH":
        return genai_types.StartSensitivity.START_SENSITIVITY_HIGH
    if configured != "LOW":
        logger.warning(
            "Invalid GEMINI_VAD_START_SENSITIVITY=%s; using LOW",
            configured,
        )
    return genai_types.StartSensitivity.START_SENSITIVITY_LOW


def _gemini_end_sensitivity() -> genai_types.EndSensitivity:
    """Keep normal endpoint responsiveness unless explicitly tuned."""
    configured = os.getenv("GEMINI_VAD_END_SENSITIVITY", "HIGH").strip().upper()
    if configured == "LOW":
        return genai_types.EndSensitivity.END_SENSITIVITY_LOW
    if configured != "HIGH":
        logger.warning(
            "Invalid GEMINI_VAD_END_SENSITIVITY=%s; using HIGH",
            configured,
        )
    return genai_types.EndSensitivity.END_SENSITIVITY_HIGH


def _suppress_unsolicited_realtime_speech(
    *,
    controlled_flow: bool,
    user_turn_unsettled: bool = False,
    user_initiated: bool,
    expected_tool_reply: bool,
) -> bool:
    """Reject a premature Gemini server draft before its response turn is safe.

    LiveKit marks an explicit ``session.generate_reply`` speech as
    ``user_initiated=True``. A Gemini server-side/AAD generation is marked
    false. A server-started generation is still permitted when LiveKit is
    explicitly waiting for Gemini's automatic reply to a completed tool call.
    ``user_turn_unsettled`` covers the interval from speech-start until the
    final user transcript is published; replying inside that interval caused
    Gemini to talk over the customer and collide with the guarded response.
    """
    return bool(
        (controlled_flow or user_turn_unsettled)
        and not user_initiated
        and not expected_tool_reply
    )


def _authorized_controlled_reply_tools(
    conversation_state: GatedConversationState,
) -> list[str]:
    """Expose only the one tool authorized by the settled V5 state."""
    if not conversation_state.v5_company_pair_flow:
        return []
    if conversation_state.flat_catalog_due():
        return ["get_shipkia_flat_rates"]
    if conversation_state.flat_zonal_catalog_due():
        return ["get_shipkia_flat_zonal_rates"]
    if conversation_state.pricing_mode() == "route_starting_pending":
        return ["lookup_pincode_serviceability"]
    if (
        conversation_state.starting_rate_due()
        or conversation_state.shadowfax_surface_rate_due
    ):
        return ["get_shipkia_starting_rate"]
    if conversation_state.pricing_mode() == "exact" and conversation_state.pricing_ready():
        return ["calculate_shipkia_rate"]
    return []


def _worker_owns_realtime_turn(conversation_state: GatedConversationState) -> bool:
    """Prevent native V6 from speaking before a KB rate is verified.

    V5 remains fully worker-controlled. In V6, native Gemini owns ordinary
    information/onboarding and post-rate conversation. Once the customer
    selects Rates, the worker owns every pre-rate turn so a native model draft
    can never invent a ShipKia amount while collecting qualification details.
    The settled state exposes at most one authoritative pricing tool.
    """
    if not conversation_state.v5_company_pair_flow:
        return False
    if not conversation_state.model_led_flow:
        return True
    return bool(
        conversation_state.value("assistance_intent") == "Rates"
        and not conversation_state.verified_rate_presented()
    ) or bool(_authorized_controlled_reply_tools(conversation_state))


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


def _authoritative_rate_request_arguments(
    raw_arguments: dict[str, object],
    state_arguments: dict[str, object],
    backend_argument_names: frozenset[str],
) -> tuple[dict[str, object], list[str]]:
    """Build a rate request without letting model-only fields poison validated state."""
    allowed_model_fields = backend_argument_names | _RATE_LOCAL_ARGUMENTS
    ignored_fields = sorted(
        key
        for key in raw_arguments
        if key not in allowed_model_fields
        or (key in STATE_MANAGED_RATE_FIELDS and key not in state_arguments)
    )
    request_arguments = {
        key: value
        for key, value in raw_arguments.items()
        if key in allowed_model_fields and key not in STATE_MANAGED_RATE_FIELDS
    }
    trusted_state_arguments = {
        key: value
        for key, value in state_arguments.items()
        if key in allowed_model_fields
    }
    return {
        **request_arguments,
        **trusted_state_arguments,
    }, ignored_fields


def _normalize_rate_request_arguments(
    arguments: dict[str, object],
) -> tuple[dict[str, object], str]:
    normalized = dict(arguments)
    requested_type = _normalized_text(normalized.get("rate_request_type"))
    service_alias = _normalized_text(normalized.get("service"))
    is_flat = requested_type == "flat" or service_alias in _FLAT_RATE_SERVICE_ALIASES
    rate_request_type = "Flat" if is_flat else "Normal"
    normalized["rate_request_type"] = rate_request_type
    if (
        service_alias in _FLAT_RATE_SERVICE_ALIASES
        or service_alias in _PAYMENT_MODE_SERVICE_ALIASES
    ):
        normalized.pop("service", None)
    return normalized, rate_request_type


def _is_refusal_value(value: object) -> bool:
    return _normalized_text(value) in _REFUSAL_VALUES


def _rate_gate_response(status: str, field: str, message: str) -> dict[str, object]:
    field_label = _RATE_FIELD_LABELS.get(
        field,
        field.replace("_", " ") if field else "",
    )
    return {
        "status": status,
        "next_missing_field": field,
        "next_question": field_label,
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

    for field in ("pickup_pincode", "delivery_pincode"):
        unavailable = (
            _normalized_text(arguments.get(f"{field}_status")) == "unavailable"
        )
        if not _has_value(arguments, field) and not unavailable:
            return (
                None,
                {},
                _rate_gate_response(
                    "shipment_details_required",
                    field,
                    f"The customer's {_RATE_FIELD_LABELS[field]} is not present in local call state.",
                ),
            )

    if not _has_value(arguments, "dead_weight"):
        return (
            None,
            {},
            _rate_gate_response(
                "shipment_details_required",
                "dead_weight",
                "The customer's shipment weight is not present in validated call state.",
            ),
        )

    for field in ("pickup_pincode", "delivery_pincode"):
        if _has_value(arguments, field) and not re.fullmatch(
            r"\d{6}", str(arguments[field]).strip()
        ):
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
    for field in ("pickup_pincode", "delivery_pincode"):
        if _normalized_text(arguments.get(f"{field}_status")) == "unavailable":
            forwarded.pop(field, None)
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
    payment_schema["enum"] = ["Prepaid", "COD", "Both"]
    payment_schema["description"] = (
        "Customer-stated Prepaid, COD, or Both. Use Both when the customer says both/dono or selects "
        "Prepaid and COD together; the voice worker uses Prepaid. A payment refusal belongs to "
        "get_shipkia_starting_rate and must never call this calculator."
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
                "applicable weight band and GST-inclusive total, then stop. Do not name or price "
                "any other service or additional-weight component and do not ask any follow-up "
                "question."
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


def _voice_safe_pincode_serviceability_result(
    result: dict[str, Any],
    *,
    ask_monthly_shipment_quantity: bool = True,
    post_rate_pending_field: str = "",
) -> dict[str, Any]:
    """Expose a zone only when verified; otherwise return the backend's Rs 22 fallback."""
    if result.get("status") == "route_details_required":
        return {
            "status": "route_details_required",
            "response_type": "route_details_required",
            "zone": None,
            "zone_verified": False,
            "exact_route_rate_available": False,
            "missing_fields": list(result.get("missing_fields") or []),
            "spoken_response_instruction": (
                "The route is incomplete. Ask only for the missing pickup or delivery city/locality. "
                "Do not say rates are unavailable, being updated, or cannot be checked, and do not "
                "ask monthly shipment quantity yet."
            ),
        }
    zone = str(result.get("zone") or "").strip().upper().removeprefix("ZONE").strip()
    zone_verified = bool(result.get("zone_verified")) and zone in {"A", "B", "C", "D", "E", "F"}
    starting_rate = result.get("starting_rate")
    if (
        result.get("status") == "success"
        and zone_verified
        and isinstance(starting_rate, dict)
        and starting_rate.get("status") == "success"
        and starting_rate.get("amount") not in (None, "")
    ):
        amount = float(starting_rate["amount"])
        pan_india = result.get("resolution_basis") == "pan_india_zone_a_starting_policy"
        pickup_label = str(
            result.get("pickup_location") or result.get("pickup_pincode") or ""
        ).strip()
        delivery_label = str(
            result.get("delivery_location") or result.get("delivery_pincode") or ""
        ).strip()
        route_label = (
            "Pan-India Zone A starting basis"
            if pan_india
            else (
                f"{pickup_label} se {delivery_label}, Zone {zone}"
                if pickup_label and delivery_label
                else f"Zone {zone} route"
            )
        )
        if post_rate_pending_field:
            pending_label = _RATE_FIELD_LABELS.get(
                post_rate_pending_field,
                post_rate_pending_field.replace("_", " "),
            )
            quantity_instruction = (
                f" Then ask only for the customer's {pending_label}. Do not ask monthly shipment "
                "quantity until all consultative discovery fields are handled."
            )
        else:
            quantity_instruction = (
                " Then ask only for the customer's monthly shipment quantity."
                if ask_monthly_shipment_quantity
                else " The monthly shipment quantity is already handled; do not ask it again."
            )
        return {
            "status": "success",
            "response_type": "zone_starting",
            "serviceable": result.get("serviceable"),
            "zone": zone,
            "zone_verified": True,
            "amount": amount,
            "currency": starting_rate.get("currency", "INR"),
            "gst_inclusive": bool(starting_rate.get("gst_inclusive")),
            "basis": starting_rate.get("basis"),
            "available_courier_partners": list(
                starting_rate.get("available_courier_partners") or []
            ),
            "starting_rate_options": list(starting_rate.get("starting_rate_options") or []),
            "starting_rate_options_note": starting_rate.get("starting_rate_options_note"),
            "rate_card": starting_rate.get("rate_card"),
            "rate_scope": "starting_only",
            "resolution_basis": result.get("resolution_basis"),
            "pickup_pincode": result.get("pickup_pincode"),
            "delivery_pincode": result.get("delivery_pincode"),
            "pickup_location": result.get("pickup_location"),
            "delivery_location": result.get("delivery_location"),
            "spoken_response_instruction": (
                f"Say naturally and directly: {route_label} ke shipping rates Rs {amount:.2f} se "
                "start hote hain, GST included. Clearly call it a starting rate, not the exact "
                f"shipment charge.{quantity_instruction} "
                "Do not ask weight or payment mode before speaking this starting rate, do not call "
                "another pricing tool, and never replace the returned zone with a model inference."
            ),
        }

    fallback = result.get("fallback_starting_rate")
    if isinstance(fallback, dict) and fallback.get("status") == "success":
        amount = fallback.get("amount")
        try:
            numeric_amount = float(amount)
        except (TypeError, ValueError):
            numeric_amount = 0.0
        if numeric_amount > 0:
            return {
                "status": "success",
                "response_type": "general_starting",
                "zone": None,
                "zone_verified": False,
                "exact_route_rate_available": False,
                "amount": numeric_amount,
                "currency": fallback.get("currency", "INR"),
                "gst_inclusive": bool(fallback.get("gst_inclusive")),
                "rate_card": fallback.get("rate_card"),
                "spoken_response_instruction": (
                    f"Say exactly one short rate sentence: ShipKia ke shipping rates Rs {numeric_amount:.2f} "
                    "se start hote hain; exact rate route, weight aur service par depend karta hai. "
                    "Do not name or imply any Zone A-F, do not call calculate_shipkia_rate for this "
                    "route, and do not present this as an exact shipment rate."
                ),
            }

    return {
        "status": "configuration_required",
        "response_type": "zone_resolution_unavailable",
        "zone": None,
        "zone_verified": False,
        "exact_route_rate_available": False,
        "spoken_response_instruction": (
            "Say briefly that an exact route rate is temporarily unavailable. Do not name a zone "
            "and do not invent or estimate an amount."
        ),
    }


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
    route_complete = _route_key(route_basis or {}) is not None
    safe_result.update(
        {
            "verified_starting_rate_available": bool(sortable_rates),
            "pincodes_already_supplied": route_complete,
            "pincode_unavailable_fallback": not route_complete,
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
            if route_complete:
                safe_result["message"] = (
                    "Reply politely and directly in one short sentence. State the supplied pincode "
                    "route, weight/payment basis, exact service and only verified_starting_rate as a "
                    "GST-inclusive 'starting from' amount. Do not explain zones or route validation, "
                    "list alternatives, recap, or ask a follow-up question."
                )
            else:
                safe_result["message"] = (
                    "Reply politely and directly with only the single lowest verified GST-inclusive "
                    "starting rate for the confirmed weight/payment basis. Clearly say the exact "
                    "rate depends on the pickup/delivery pincode and approved zone. Do not imply a "
                    "pincode was supplied, list alternatives, or estimate another amount."
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
            "No verified GST-inclusive starting total is available. Do not quote or estimate an "
            "amount. Do not ask again for a pincode that the customer explicitly marked "
            "unavailable."
            if not route_complete
            else (
                "No verified GST-inclusive starting total is available for this route result. "
                "Do not quote or estimate an amount. Refer to the supplied pincode route without "
                "mentioning zones or mapping, and do not ask for either pincode again."
            )
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
            "followed by the applicable pricing_condition, then stop. Do not mention any other "
            "service, normal rate, route limitation, benefit, signup, callback or monthly shipment "
            "volume, and do not ask any follow-up question."
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
            "additional_weight_condition": None,
            "route_validation_note_required": (
                route_validation_note_required if is_starting else False
            ),
            "verified_starting_rate_available": is_starting,
        }
    )
    if current_rate is not None:
        safe_result["message"] = (
            "State only selected_service and current_shipment_rate for the current weight/payment "
            "basis, then stop. When current_shipment_rate_is_starting is true, say the exact "
            "GST-inclusive amount as 'starting from'. Do not say the selected service is "
            "unavailable, do not speak an additional-weight component, do not mention another "
            "service or normal rates, and do not ask any follow-up question."
        )
    else:
        safe_result["message"] = (
            "A complete current-shipment amount is unavailable for the selected service. Say that "
            "briefly and do not speak its standalone additional-weight amount. Do not substitute "
            "another service or ask about normal rates, benefits, signup, callback or monthly "
            "shipment volume."
        )
    return safe_result


def _catalog_continuation_instruction(
    conversation_state: GatedConversationState | None,
    *,
    next_catalog: str = "",
) -> str:
    """Continue from durable state instead of a generic direct-onboarding shortcut."""
    if conversation_state is None or not conversation_state.v5_company_pair_flow:
        return " Then ask once whether the customer would like to know anything else."
    if next_catalog == "flat_zonal" and conversation_state.flat_zonal_catalog_due():
        return (
            " Then call get_shipkia_flat_zonal_rates immediately and speak its verified "
            "Flat-Zonal zone groups too. Do not ask another question between the two catalogs."
        )
    if next_catalog == "flat" and conversation_state.flat_catalog_due():
        return (
            " Then call get_shipkia_flat_rates immediately and speak its complete verified Flat "
            "catalog. Do not ask another question between the two catalogs."
        )
    if (
        conversation_state.post_rate_followup_active
        or conversation_state.verified_rate_presented()
    ):
        # Tool conversion happens before the new catalog is marked verified.
        # An already verified earlier rate therefore proves this is a later
        # follow-up even if asynchronous state extraction has not set the
        # post-follow-up flag yet.
        return " Then stop without asking another question."
    if not conversation_state.is_handled("monthly_shipments"):
        return " Then ask only for the customer's approximate monthly shipment quantity."
    if not conversation_state.model_led_flow:
        if conversation_state.direct_onboarding_flow:
            return (
                " Then ask exactly: 'Kya aap ShipKia ke saath aage badhna chahte hain?' "
                "Do not call another tool and do not ask 'kuch aur'."
            )
        return (
            " Then stop without asking another question. Do not ask whether they want "
            "the rates and do not ask 'kuch aur' in this response."
        )
    if not conversation_state.anything_else_checkpoint_consumed:
        return " Then ask: 'Kya aap kuch aur jaanna chahenge?'"
    return " Then stop without asking another question."


def _voice_flat_catalog_result(
    result: dict[str, Any],
    *,
    conversation_state: GatedConversationState | None = None,
) -> dict[str, Any]:
    """Build the bounded V4 response for the verified three-slab flat catalog."""
    status = str(result.get("status") or "error")
    if status == "order_value_required":
        return {
            "status": status,
            "response_type": "flat_cod_order_value_required",
            "pricing_backend_called": True,
            "cod_order_value_required": True,
            "flat_rate_options": [],
            "spoken_response_instruction": (
                "Ask only for the positive COD order value. Do not quote a flat rate, default "
                "to Prepaid, or ask another question in this response."
            ),
        }
    if status != "success":
        return {
            "status": status,
            "response_type": result.get("response_type", "flat_unavailable"),
            "pricing_backend_called": True,
            "flat_rate_options": [],
            "message": result.get("message", "Verified flat rates are unavailable."),
            "spoken_response_instruction": (
                "Say briefly that the verified flat rate is temporarily unavailable. Do not "
                "invent an amount or substitute a normal, zone, or additional-weight rate."
            ),
        }

    options = [
        option
        for option in result.get("flat_rate_options", [])
        if isinstance(option, dict)
    ]
    additional_options = [
        option
        for option in result.get("flat_additional_rate_options", [])
        if isinstance(option, dict)
    ]
    starting = result.get("starting_flat_rate")
    starting = starting if isinstance(starting, dict) else {}
    response_type = str(result.get("response_type") or "flat_starting")
    payment_type = str(result.get("payment_type") or "Prepaid")
    both_selected = bool(
        conversation_state is not None
        and _normalized_text(conversation_state.value("payment_type")) == "both"
    )
    continuation = _catalog_continuation_instruction(
        conversation_state,
        next_catalog="flat_zonal",
    )

    if response_type == "flat_all":
        shadowfax = additional_options[0] if additional_options else {}
        shadowfax_breakdown = shadowfax.get("flat_additional_rate_breakdown")
        shadowfax_breakdown = (
            shadowfax_breakdown if isinstance(shadowfax_breakdown, dict) else {}
        )
        spoken_instruction = (
            "Present exactly two verified Flat-related options in one response. First, speak the "
            "three returned E-Kart SURFACE complete all-zone weight slabs and each GST-inclusive "
            "total, in ascending weight order. Second, say that Shadowfax Surface 5 KG has a "
            f"verified flat additional-weight condition after {int(shadowfax.get('applies_after_weight_g') or 0)} "
            f"grams: each additional {int(shadowfax.get('additional_weight_unit_g') or 0)} grams "
            f"is Rs {float(shadowfax_breakdown.get('total') or 0):.2f}, GST included. Explicitly "
            "state that Shadowfax's base shipment rate is zonal, so this additional amount is not "
            "a complete shipment rate. Do not omit either option and do not invent another one."
            + continuation
        )
    elif response_type == "flat_matching" and options:
        option = options[0]
        cod_note = (
            f" State that the verified additional COD charge is Rs "
            f"{float(option.get('cod_charge') or 0):.2f}."
            if payment_type == "COD"
            else ""
        )
        spoken_instruction = (
            f"Say that the GST-inclusive E-Kart SURFACE flat rate for the returned "
            f"{int(option.get('min_weight_g') or 0)}-{int(option.get('max_weight_g') or 0)} gram "
            f"slab is Rs {float(option.get('total') or 0):.2f}."
            + cod_note
            + continuation
        )
    elif response_type == "flat_starting_fallback":
        spoken_instruction = (
            f"Say only that ShipKia's GST-inclusive flat rate starts from Rs "
            f"{float(starting.get('total') or 0):.2f}. Do not imply that this amount applies to "
            "the supplied shipment weight."
            + continuation
        )
    else:
        spoken_instruction = (
            f"Say: ShipKia ka GST-inclusive flat rate Rs "
            f"{float(starting.get('total') or 0):.2f} se start hota hai."
            + continuation
        )
    if both_selected:
        spoken_instruction += (
            " Clearly label this as the Prepaid slab and add that the COD rate depends on "
            "order value. Do not ask for permission or order value now."
        )

    return {
        "status": "success",
        "response_type": response_type,
        "response_scope": result.get("response_scope"),
        "pricing_backend_called": True,
        "currency": result.get("currency", "INR"),
        "gst_inclusive": True,
        "movement_type": "Forward",
        "payment_type": payment_type,
        "order_value": result.get("order_value"),
        "courier_partner": result.get("courier_partner"),
        "service": result.get("service"),
        "chargeable_weight_g": result.get("chargeable_weight_g"),
        "exact_match_available": bool(result.get("exact_match_available")),
        "starting_flat_rate": starting,
        "flat_rate_options": options,
        "flat_additional_rate_options": additional_options,
        "flat_additional_rate_available": bool(additional_options),
        "verified_flat_rate_count": result.get("verified_flat_rate_count"),
        "excluded_additional_weight_components": False,
        "rate_card": result.get("rate_card"),
        "spoken_response_instruction": spoken_instruction,
    }


def _voice_flat_zonal_catalog_result(
    result: dict[str, Any],
    *,
    conversation_state: GatedConversationState | None = None,
) -> dict[str, Any]:
    """Bound the spoken response to verified Flat-Zonal groups only."""
    status = str(result.get("status") or "error")
    if status == "order_value_required":
        return {
            "status": status,
            "response_type": "flat_zonal_cod_order_value_required",
            "pricing_backend_called": True,
            "zone_groups": [],
            "spoken_response_instruction": (
                "Ask only for the positive COD order value. Do not quote a Flat-Zonal rate, "
                "default to Prepaid, or ask another question."
            ),
        }
    if status != "success":
        return {
            "status": status,
            "response_type": result.get("response_type", "flat_zonal_unavailable"),
            "pricing_backend_called": True,
            "zone_groups": [],
            "message": result.get("message", "Verified Flat-Zonal rates are unavailable."),
            "spoken_response_instruction": (
                "Say briefly that the verified Flat-Zonal rate is temporarily unavailable. "
                "Do not invent an amount or substitute a Flat or Zonal rate."
            ),
        }

    groups = [item for item in result.get("zone_groups", []) if isinstance(item, dict)]
    additional = result.get("additional_weight")
    additional = additional if isinstance(additional, dict) else {}
    if len(groups) != 2:
        return {
            "status": "configuration_required",
            "response_type": "flat_zonal_unavailable",
            "pricing_backend_called": True,
            "zone_groups": [],
            "spoken_response_instruction": (
                "Say briefly that the verified Flat-Zonal rate is temporarily unavailable. "
                "Do not invent or infer a zone group."
            ),
        }

    continuation = _catalog_continuation_instruction(
        conversation_state,
        next_catalog="flat",
    )
    spoken_instruction = (
        "Explain that Flat-Zonal means the base price is fixed within each returned zone group, "
        "but differs between groups. Speak only the two returned GST-inclusive E-Kart EXPRESS "
        f"500-gram totals: Zones {groups[0].get('zone_group')} Rs "
        f"{float(groups[0].get('total') or 0):.2f}, and Zones "
        f"{groups[1].get('zone_group')} Rs {float(groups[1].get('total') or 0):.2f}. "
        f"Then say the returned additional {int(additional.get('additional_weight_unit_g') or 0)}-gram "
        f"GST-inclusive amount is Rs {float(additional.get('total') or 0):.2f}. "
        "Say every Rs amount in rupees, never as paise. Do not call this all-zone Flat pricing "
        "and do not infer a route zone."
        + continuation
    )
    return {
        "status": "success",
        "response_type": "flat_zonal_all",
        "pricing_backend_called": True,
        "currency": result.get("currency", "INR"),
        "gst_inclusive": True,
        "movement_type": "Forward",
        "payment_type": result.get("payment_type", "Prepaid"),
        "courier_partner": result.get("courier_partner"),
        "service": result.get("service"),
        "zone_groups": groups,
        "additional_weight": additional,
        "rate_card": result.get("rate_card"),
        "spoken_response_instruction": spoken_instruction,
    }


class GuardedTurnProcessor:
    """Serialize and deduplicate state updates from every LiveKit turn path."""

    def __init__(
        self,
        *,
        conversation_state: GatedConversationState,
        answer_guard: SemanticAnswerGuard,
        runtime: VoiceSessionRuntime,
    ) -> None:
        self._conversation_state = conversation_state
        self._answer_guard = answer_guard
        self._runtime = runtime
        self._latest_task: asyncio.Task[None] | None = None
        self._latest_text = ""
        self._latest_started_monotonic = 0.0
        self._state_changed_callback: Any | None = None

    def set_state_changed_callback(self, callback: Any) -> None:
        self._state_changed_callback = callback

    async def _notify_state_changed(self) -> None:
        if self._state_changed_callback is None:
            return
        try:
            await self._state_changed_callback()
        except Exception as exc:
            logger.warning("Pricing tool availability sync failed: %s", str(exc)[:240])

    def schedule(self, customer_text: object, *, turn_id: object = None) -> asyncio.Task[None] | None:
        clean_text = " ".join(str(customer_text or "").split())
        if not clean_text:
            return None
        now = time.monotonic()
        if (
            self._latest_task is not None
            and clean_text.casefold() == self._latest_text.casefold()
            and now - self._latest_started_monotonic <= 3.0
        ):
            return self._latest_task

        previous_task = self._latest_task
        clean_turn_id = str(turn_id or f"user:{time.time()}")
        prepared_turn = None
        if previous_task is None or previous_task.done():
            # Gemini native audio can begin drafting as soon as its final
            # transcript event fires. Apply evidence-backed deterministic
            # answers before yielding back to the event loop so that draft is
            # based on the newly advanced authoritative state, not the prior
            # pending question. The slower semantic classifier still runs in
            # order below.
            prepared_turn = self._prepare_turn(clean_text, turn_id=clean_turn_id)

        async def run_in_order() -> None:
            if previous_task is not None and not previous_task.done():
                try:
                    await asyncio.shield(previous_task)
                except (asyncio.CancelledError, Exception):
                    pass
            await self._process(
                clean_text,
                turn_id=clean_turn_id,
                prepared_turn=prepared_turn,
            )

        task = asyncio.create_task(run_in_order())
        task.add_done_callback(VoiceSessionRuntime._log_task_exception)
        self._latest_task = task
        self._latest_text = clean_text
        self._latest_started_monotonic = now
        return task

    async def process(self, customer_text: object, *, turn_id: object = None) -> None:
        task = self.schedule(customer_text, turn_id=turn_id)
        if task is not None:
            await asyncio.shield(task)

    async def wait_latest(self) -> None:
        task = self._latest_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _process fails closed and normally consumes guard errors. A
            # callback/audit failure must not let model arguments bypass state.
            return

    def _prepare_turn(self, customer_text: str, *, turn_id: str) -> dict[str, Any]:
        previous_agent_text = ""
        for turn in reversed(self._runtime.turns):
            if turn.get("role") == "AGENT":
                previous_agent_text = str(turn.get("text") or "")
                break
        pending_field_at_turn_start = self._conversation_state.pending_field()
        state_snapshot_at_turn_start = self._conversation_state.snapshot()
        deterministic_transitions = self._conversation_state.apply_deterministic_answers(
            customer_text,
            turn_id=turn_id,
            previous_agent_text=previous_agent_text,
        )
        return {
            "previous_agent_text": previous_agent_text,
            "pending_field_at_turn_start": pending_field_at_turn_start,
            "state_snapshot_at_turn_start": state_snapshot_at_turn_start,
            "deterministic_transitions": deterministic_transitions,
        }

    async def _process(
        self,
        customer_text: str,
        *,
        turn_id: str,
        prepared_turn: dict[str, Any] | None = None,
    ) -> None:
        prepared = prepared_turn or self._prepare_turn(customer_text, turn_id=turn_id)
        previous_agent_text = str(prepared["previous_agent_text"])
        pending_field_at_turn_start = prepared["pending_field_at_turn_start"]
        state_snapshot_at_turn_start = prepared["state_snapshot_at_turn_start"]
        deterministic_transitions = prepared["deterministic_transitions"]
        if deterministic_transitions:
            # Native realtime can start drafting before the semantic classifier
            # returns. Publish deterministic facts immediately so the ongoing
            # session receives the new pending question and handled values.
            await self._notify_state_changed()
        deterministic_pending_answer = bool(
            pending_field_at_turn_start
            and any(
                transition.get("field") == pending_field_at_turn_start
                for transition in deterministic_transitions
            )
        )
        deterministic_pricing_request = any(
            transition.get("event") == "pricing_mode_updated"
            for transition in deterministic_transitions
        )
        try:
            if deterministic_pending_answer or deterministic_pricing_request:
                # Obvious structured answers and explicit pricing requests
                # should not pay for a second Gemini classifier round-trip.
                # The semantic guard remains the fallback when deterministic
                # parsing did not settle the current question.
                guard_result = {
                    "turn_disposition": "answered",
                    "decisions": [],
                    "source": "deterministic_turn_fast_path",
                }
                semantic_transitions = []
            else:
                guard_result = await self._answer_guard.classify(
                    customer_text=customer_text,
                    pending_field=pending_field_at_turn_start,
                    state_snapshot=state_snapshot_at_turn_start,
                    previous_agent_text=previous_agent_text,
                )
                semantic_transitions = self._conversation_state.apply_classifier_result(
                    guard_result,
                    customer_text=customer_text,
                    turn_id=turn_id,
                    pending_field_at_turn_start=pending_field_at_turn_start,
                )
            applied_transitions = [
                *deterministic_transitions,
                *semantic_transitions,
            ]
            if deterministic_transitions:
                self._conversation_state.last_turn_disposition = "answered"
            for transition in applied_transitions:
                logger.info("shipkia_state_transition %s", compact_json(transition))
        except Exception as exc:
            error_text = str(exc).strip() or type(exc).__name__
            self._conversation_state.record_guard_error(error_text, turn_id=turn_id)
            failure_transition = self._conversation_state.transitions[-1]
            for transition in [*deterministic_transitions, failure_transition]:
                logger.info("shipkia_state_transition %s", compact_json(transition))
            logger.warning(
                "Semantic answer guard failed closed pending_field=%s error=%s",
                self._conversation_state.pending_field(),
                error_text[:240],
            )
            try:
                await self._notify_state_changed()
                await self._runtime.emit(
                    "gated_state_checkpoint",
                    classifier_error=error_text[:240],
                    deterministic_transitions=deterministic_transitions,
                    applied_transitions=deterministic_transitions,
                    state_snapshot=self._conversation_state.snapshot(),
                    state_transitions=list(self._conversation_state.transitions),
                    transcript=self._runtime.transcript(),
                    metrics=self._runtime.metrics(),
                )
            except Exception as audit_exc:
                logger.warning("Gated-state audit callback failed: %s", str(audit_exc)[:240])
            return

        try:
            await self._notify_state_changed()
            await self._runtime.emit(
                "gated_state_checkpoint",
                classifier_decision=guard_result,
                deterministic_transitions=deterministic_transitions,
                applied_transitions=applied_transitions,
                state_snapshot=self._conversation_state.snapshot(),
                state_transitions=list(self._conversation_state.transitions),
                transcript=self._runtime.transcript(),
                metrics=self._runtime.metrics(),
            )
        except Exception as audit_exc:
            logger.warning("Gated-state audit callback failed: %s", str(audit_exc)[:240])


def make_mcp_forwarder(
    tool_name: str,
    task_id: str,
    runtime: VoiceSessionRuntime | None = None,
    conversation_state: GatedConversationState | None = None,
    turn_processor: GuardedTurnProcessor | None = None,
    *,
    backend_argument_names: frozenset[str] = frozenset(),
):
    remembered_rate_arguments: dict[str, object] = {}
    disclosed_route_keys: set[tuple[str, str]] = set()
    presented_flat_services: set[str] = set()
    active_flat_context = False
    blocked_rate_fingerprints: set[tuple[object, ...]] = set()
    blocked_lookup_fingerprints: set[tuple[object, ...]] = set()
    last_starting_rate_response = ""

    async def forwarder(raw_arguments: dict[str, object]) -> str:
        nonlocal active_flat_context, last_starting_rate_response
        arguments = dict(raw_arguments or {})
        rate_metadata: dict[str, object] = {}
        if (
            turn_processor is not None
            and tool_name in PRICING_TOOLS
        ):
            await turn_processor.wait_latest()
        if (
            conversation_state is not None
            and conversation_state.v5_company_pair_flow
            and tool_name in PRICING_TOOLS
            and conversation_state.pricing_close_locked()
        ):
            logger.info(
                "Pricing tool suppressed after close-stage lock tool=%s revision=%s",
                tool_name,
                conversation_state.revision,
            )
            return compact_json(
                {
                    "status": "close_stage_locked",
                    "worker_state_authoritative": True,
                    "pricing_backend_called": False,
                    "spoken_response_instruction": conversation_state.guidance(),
                }
            )

        if (
            tool_name == "lookup_pincode_serviceability"
            and conversation_state is not None
        ):
            pending = conversation_state.pending_field()
            if (
                conversation_state.v5_company_pair_flow
                and conversation_state.route_zone_lookup_status == "verified_starting"
                and conversation_state.verified_rate_presented()
                and conversation_state.unresolved_route_count() == 0
            ):
                return compact_json(
                    {
                        "status": "duplicate_suppressed",
                        "worker_state_authoritative": True,
                        "pricing_backend_called": False,
                        "zone": conversation_state.value("zone") or None,
                        "zone_verified": conversation_state.is_confirmed("zone"),
                        "spoken_response_instruction": (
                            "The route and starting rate are already verified. Do not call a "
                            "pricing tool, say the zone is unverified, or ask for either endpoint "
                            "again. Follow only this current action: "
                            + conversation_state.guidance()
                        ),
                    }
                )
            if (
                (
                    conversation_state.v5_company_pair_flow
                    or conversation_state.direct_onboarding_flow
                )
                and not conversation_state.pan_india_requested
                and pending
                in {
                    "conversation_consent",
                    "assistance_intent",
                    "monthly_shipments",
                    *OPTIONAL_QUALIFICATION_FIELDS,
                }
            ):
                provider_name = str(
                    conversation_state.value("current_provider_name")
                    or "current shipping provider"
                )
                pending_label = _RATE_FIELD_LABELS.get(pending, pending)
                if pending == "current_problem":
                    spoken_instruction = (
                        f"Do not quote a rate or ask for the route again. Ask only what problem "
                        f"the customer is facing with {provider_name}. After their answer, refusal, "
                        "or clear statement that there is no problem, use the retained route."
                    )
                else:
                    spoken_instruction = (
                        "Do not quote a rate or ask for the route again. Keep the supplied route in "
                        f"memory and ask only for the customer's {pending_label}. Continue the "
                        "authoritative discovery order before using the retained route."
                    )
                result = {
                    "status": "qualification_required",
                    "worker_state_authoritative": True,
                    "pricing_backend_called": False,
                    "route_retained": bool(
                        conversation_state.next_route_for_lookup()
                        or conversation_state.active_route()
                    ),
                    "required_next_question": pending_label,
                }
                if not conversation_state.model_led_flow:
                    result["spoken_response_instruction"] = spoken_instruction
                lookup_fingerprint = (conversation_state.revision, pending)
                if lookup_fingerprint in blocked_lookup_fingerprints:
                    duplicate = {**result, "status": "duplicate_suppressed"}
                    if not conversation_state.model_led_flow:
                        duplicate["spoken_response_instruction"] = (
                            "The route lookup is still blocked by the same unanswered "
                            f"{pending_label} question. Do not call this tool again until the "
                            "customer gives a new answer. " + spoken_instruction
                        )
                    return compact_json(duplicate)
                blocked_lookup_fingerprints.add(lookup_fingerprint)
                if runtime:
                    runtime.record_tool_outcome(
                        tool_name,
                        status="blocked",
                        summary=f"qualification_required pending_field={pending}",
                    )
                return compact_json(result)
            trusted_route = (
                conversation_state.next_route_for_lookup()
                or conversation_state.active_route()
            )
            if trusted_route:
                # A city/locality pair captured from the customer is the
                # authoritative active route for both controlled and model-led flows,
                # including later rate follow-ups after its first lookup was resolved.
                # This also prevents a later model tool call from dropping the
                # route or replacing it with guessed pincodes.
                arguments = dict(trusted_route)
                arguments.setdefault("pan_india", conversation_state.pan_india_requested)
            else:
                # Never forward model-provided route placeholders. Only the
                # customer's validated, retained route may reach the resolver.
                result = {
                    "status": "route_details_required",
                    "worker_state_authoritative": True,
                    "pricing_backend_called": False,
                    "zone": None,
                    "zone_verified": False,
                    "required_next_question": _RATE_FIELD_LABELS.get(pending, pending),
                }
                if not conversation_state.model_led_flow:
                    result["spoken_response_instruction"] = (
                        "Do not infer or name a zone. Ask only for the missing pickup or delivery "
                        "city/locality shown by required_next_question; do not ask for a pincode."
                    )
                return compact_json(result)

        if tool_name == "get_shipkia_flat_rates":
            if turn_processor is not None and not (
                conversation_state is not None and conversation_state.model_led_flow
            ):
                await turn_processor.wait_latest()
            if conversation_state is not None and conversation_state.model_led_flow:
                if not conversation_state.flat_catalog_due():
                    return compact_json(
                        {
                            "status": "flat_rate_not_requested",
                            "response_type": "flat_rate_not_requested",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "flat_rate_options": [],
                        }
                    )
                pending = conversation_state.pending_field()
                if pending:
                    return compact_json(
                        {
                            "status": "qualification_required",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "required_next_question": _RATE_FIELD_LABELS.get(pending, pending),
                        }
                    )
                arguments = {"response_scope": "All", "payment_type": "Prepaid"}
                if (
                    conversation_state.cod_rate_requested
                    and conversation_state.is_confirmed("order_value")
                ):
                    arguments.update(
                        {
                            "payment_type": "COD",
                            "order_value": conversation_state.value("order_value"),
                        }
                    )
                rate_metadata["direct_model_led_flat_catalog"] = True
            elif conversation_state is not None:
                direct_v5_catalog = bool(
                    conversation_state.v5_company_pair_flow
                    and conversation_state.flat_catalog_due()
                )
                if (
                    conversation_state.v4_strict_flow
                    and conversation_state.requested_rate_type != "Flat"
                ):
                    return compact_json(
                        {
                            "status": "flat_rate_not_requested",
                            "response_type": "flat_rate_not_requested",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "flat_rate_options": [],
                            "spoken_response_instruction": (
                                "The customer did not explicitly request a flat rate. Do not mention "
                                "any flat amount or flat availability. Follow the authoritative normal "
                                "rate or onboarding flow."
                            ),
                        }
                    )
                if (
                    conversation_state.v5_company_pair_flow
                    and conversation_state.flat_catalog_presented
                    and not conversation_state.flat_catalog_due()
                ):
                    return compact_json(
                        {
                            "status": "duplicate_suppressed",
                            "response_type": "flat_duplicate_suppressed",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "flat_rate_options": [],
                            "spoken_response_instruction": (
                                "The complete verified E-Kart Surface Flat catalog was already "
                                "spoken. Do not repeat any amount or call another tool. Say briefly "
                                "that those three slabs are the complete Flat catalog and there is "
                                "no additional verified Flat slab. Flat-Zonal is a separate rate "
                                "structure and should be given only when the customer asks for it."
                            ),
                        }
                    )
                pending = "" if direct_v5_catalog else conversation_state.pending_field()
                if pending:
                    pending_label = _RATE_FIELD_LABELS.get(
                        pending, pending.replace("_", " ")
                    )
                    result = _rate_gate_response(
                        "qualification_required",
                        pending,
                        (
                            "Flat-rate pricing is blocked until the existing V3 qualification "
                            "and shipment-requirement flow is complete."
                        ),
                    )
                    result.update(
                        {
                            "response_type": "flat_qualification_required",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "flat_rate_options": [],
                            "spoken_response_instruction": (
                                "Do not quote, mention, hint at, or call a flat rate yet. Briefly "
                                "acknowledge the rate request, then ask only for the customer's "
                                f"{pending_label}."
                            ),
                        }
                    )
                    if runtime:
                        runtime.record_tool_outcome(
                            tool_name,
                            status="blocked",
                            summary=f"qualification_required pending_field={pending}",
                        )
                    return compact_json(result)

                if direct_v5_catalog:
                    arguments = {
                        "response_scope": "All",
                        "payment_type": "Prepaid",
                    }
                    rate_metadata["direct_v5_flat_catalog"] = True
                else:
                    state_arguments = conversation_state.rate_arguments()
                    if _normalized_text(state_arguments.get("payment_type")) == "both":
                        state_arguments["payment_type"] = "Prepaid"
                        rate_metadata["payment_basis_reason"] = "both_selected"
                    arguments = {
                        key: value
                        for key, value in arguments.items()
                        if key in backend_argument_names
                        and key not in STATE_MANAGED_RATE_FIELDS
                    }
                    arguments.update(
                        {
                            key: value
                            for key, value in state_arguments.items()
                            if key in backend_argument_names
                        }
                    )

        if tool_name == "get_shipkia_flat_zonal_rates":
            if turn_processor is not None and not (
                conversation_state is not None and conversation_state.model_led_flow
            ):
                await turn_processor.wait_latest()
            if conversation_state is not None and conversation_state.model_led_flow:
                if not conversation_state.flat_zonal_catalog_due():
                    return compact_json(
                        {
                            "status": "flat_zonal_rate_not_requested",
                            "response_type": "flat_zonal_rate_not_requested",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "zone_groups": [],
                        }
                    )
                pending = conversation_state.pending_field()
                if pending:
                    return compact_json(
                        {
                            "status": "qualification_required",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "required_next_question": _RATE_FIELD_LABELS.get(pending, pending),
                        }
                    )
                arguments = {"payment_type": "Prepaid"}
                if (
                    conversation_state.cod_rate_requested
                    and conversation_state.is_confirmed("order_value")
                ):
                    arguments.update(
                        {
                            "payment_type": "COD",
                            "order_value": conversation_state.value("order_value"),
                        }
                    )
                rate_metadata["direct_model_led_flat_zonal_catalog"] = True
            elif conversation_state is not None:
                if (
                    conversation_state.flat_zonal_catalog_presented
                    and not conversation_state.flat_zonal_catalog_due()
                ):
                    return compact_json(
                        {
                            "status": "duplicate_suppressed",
                            "response_type": "flat_zonal_duplicate_suppressed",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "zone_groups": [],
                            "spoken_response_instruction": (
                                "The verified Flat-Zonal rates were already spoken. Do not repeat "
                                "them, do not ask which zone group they want, and do not ask for "
                                "permission to give the rate. Wait for a new customer request."
                            ),
                        }
                    )
                if (
                    not conversation_state.v5_company_pair_flow
                    or not conversation_state.flat_zonal_catalog_due()
                ):
                    return compact_json(
                        {
                            "status": "flat_zonal_rate_not_requested",
                            "response_type": "flat_zonal_rate_not_requested",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "zone_groups": [],
                            "spoken_response_instruction": (
                                "The customer did not explicitly request Flat-Zonal pricing. "
                                "Do not mention a Flat-Zonal amount; continue the active Zonal, "
                                "Flat, onboarding, or pending-question path."
                            ),
                        }
                    )
                arguments = {"payment_type": "Prepaid"}
                rate_metadata["direct_v5_flat_zonal_catalog"] = True

        if tool_name == "get_shipkia_starting_rate":
            if turn_processor is not None and not (
                conversation_state is not None and conversation_state.model_led_flow
            ):
                await turn_processor.wait_latest()
            if conversation_state is not None and conversation_state.model_led_flow:
                pricing_mode = conversation_state.pricing_mode()
                if pricing_mode not in {"general_starting", "zone_starting"}:
                    pending = conversation_state.pending_field()
                    trusted_route = (
                        conversation_state.next_route_for_lookup()
                        or conversation_state.active_route()
                    )
                    if pricing_mode == "route_starting_pending" and trusted_route:
                        return compact_json(
                            {
                                "status": "route_lookup_required",
                                "worker_state_authoritative": True,
                                "pricing_backend_called": False,
                                "route_retained": True,
                                "route": trusted_route,
                            }
                        )
                    return compact_json(
                        {
                            "status": "starting_rate_not_authorized",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "pricing_mode": pricing_mode,
                            "required_next_question": _RATE_FIELD_LABELS.get(pending, pending),
                        }
                    )
                arguments = (
                    {"zone": conversation_state.value("zone")}
                    if pricing_mode == "zone_starting"
                    else {}
                )
                rate_metadata.update(
                    {
                        "pricing_mode": pricing_mode,
                        "pricing_trigger_field": conversation_state.pricing_trigger_field(),
                        "trusted_zone_source": (
                            "validated_state" if pricing_mode == "zone_starting" else ""
                        ),
                    }
                )
            elif conversation_state is not None:
                pricing_mode = conversation_state.pricing_mode()
                shadowfax_surface_request = bool(
                    conversation_state.v5_company_pair_flow
                    and conversation_state.shadowfax_surface_rate_due
                    and conversation_state.is_confirmed("zone")
                )
                if shadowfax_surface_request:
                    pricing_mode = "zone_starting"
                    arguments = {
                        "zone": conversation_state.value("zone"),
                        "courier_partner": "Shadowfax",
                        "transport_mode": "Surface",
                    }
                    rate_metadata.update(
                        {
                            "pricing_mode": pricing_mode,
                            "pricing_trigger_field": "shadowfax_surface",
                            "trusted_zone_source": "validated_state",
                            "shadowfax_surface_request": True,
                        }
                    )
                if pricing_mode not in {"general_starting", "zone_starting"}:
                    if last_starting_rate_response:
                        return compact_json(
                            {
                                "status": "duplicate_suppressed",
                                "pricing_backend_called": False,
                                "spoken_response_instruction": (
                                    "Do not repeat the starting rate. Continue the normal "
                                    "qualification flow on the customer's next turn."
                                ),
                            }
                        )
                    return compact_json(
                        {
                            "status": "starting_rate_not_authorized",
                            "pricing_backend_called": False,
                            "spoken_response_instruction": (
                                "Do not speak a starting rate. Follow the authoritative pending "
                                "question."
                            ),
                        }
                    )
                if (
                    conversation_state.v5_company_pair_flow
                    and not shadowfax_surface_request
                    and conversation_state.verified_rate_presented()
                    and not conversation_state.starting_rate_due()
                ):
                    return compact_json(
                        {
                            "status": "duplicate_suppressed",
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "spoken_response_instruction": (
                                "Do not call the starting-rate tool again or clear retained "
                                "provider options. Follow only this current action: "
                                + conversation_state.guidance()
                            ),
                        }
                    )
                if not shadowfax_surface_request:
                    arguments = (
                        {"zone": conversation_state.value("zone")}
                        if pricing_mode == "zone_starting"
                        else {}
                    )
                    rate_metadata.update(
                        {
                            "pricing_mode": pricing_mode,
                            "pricing_trigger_field": conversation_state.pricing_trigger_field(),
                            "trusted_zone_source": (
                                "validated_state" if pricing_mode == "zone_starting" else ""
                            ),
                        }
                    )
            else:
                requested_zone = str(arguments.get("zone") or "").strip().upper()
                requested_zone = requested_zone.removeprefix("ZONE").strip()
                arguments = (
                    {"zone": requested_zone}
                    if requested_zone in {"A", "B", "C", "D", "E", "F"}
                    else {}
                )

        if tool_name == "calculate_shipkia_rate":
            if turn_processor is not None and not (
                conversation_state is not None and conversation_state.model_led_flow
            ):
                await turn_processor.wait_latest()
            if (
                conversation_state is not None
                and conversation_state.v4_strict_flow
                and conversation_state.requested_rate_type in {"Flat", "Flat Zonal"}
            ):
                return compact_json(
                    {
                        "status": "normal_rate_not_requested",
                        "worker_state_authoritative": True,
                        "pricing_backend_called": False,
                        "spoken_response_instruction": (
                            "The active pricing path is not Zonal. Do not quote a Zonal rate. Use "
                            "the worker-authorized Flat or Flat-Zonal catalog tool."
                        ),
                    }
                )
            ignored_model_fields: list[str] = []
            if conversation_state is not None and (
                not conversation_state.model_led_flow
                or conversation_state.direct_onboarding_flow
            ):
                arguments, ignored_model_fields = _authoritative_rate_request_arguments(
                    arguments,
                    conversation_state.rate_arguments(),
                    backend_argument_names,
                )
                if not conversation_state.pricing_ready():
                    pending = conversation_state.pending_field()
                    pricing_mode = conversation_state.pricing_mode()
                    if pricing_mode == "route_starting_pending":
                        trusted_route = (
                            conversation_state.next_route_for_lookup()
                            or conversation_state.active_route()
                        )
                        return compact_json(
                            {
                                "status": "route_lookup_required",
                                "worker_state_authoritative": True,
                                "pricing_backend_called": False,
                                "route_retained": bool(trusted_route),
                                "route": trusted_route,
                                "spoken_response_instruction": (
                                    "Do not ask for weight, payment mode, pincode, or permission. "
                                    "Call lookup_pincode_serviceability now with the retained city "
                                    "route and speak its verified 500 g Forward starting result."
                                ),
                            }
                        )
                    fingerprint = (
                        conversation_state.revision,
                        pricing_mode,
                        pending,
                    )
                    status = (
                        "starting_rate_required"
                        if pricing_mode in {"general_starting", "zone_starting"}
                        else (
                            "qualification_required"
                            if pending in conversation_state.optional_sequence()
                            else "shipment_details_required"
                        )
                    )
                    result = _rate_gate_response(
                        status,
                        pending,
                        (
                            "Pricing is blocked by the worker-controlled validated state. "
                            f"The next pending field is {_RATE_FIELD_LABELS.get(pending, pending)}."
                        ),
                    )
                    pending_label = _RATE_FIELD_LABELS.get(pending, pending)
                    result.update(
                        {
                            "worker_state_authoritative": True,
                            "pricing_backend_called": False,
                            "pricing_mode": pricing_mode,
                            "must_return_to_same_pending_question": bool(
                                pricing_mode == "pending"
                            ),
                            "required_next_question": (
                                pending_label if pricing_mode == "pending" else ""
                            ),
                            "spoken_response_instruction": (
                                "Call get_shipkia_starting_rate exactly once and speak only that "
                                "starting-rate response without a follow-up question."
                                if pricing_mode in {"general_starting", "zone_starting"}
                                else (
                                    "Briefly acknowledge the customer's rate request, then ask only "
                                    f"for their {pending_label} again. Do not ask for a pincode, "
                                    "weight, payment, service, or any later field. Do not claim a "
                                    "price was calculated."
                                )
                            ),
                        }
                    )
                    if ignored_model_fields:
                        result["ignored_model_fields"] = ignored_model_fields
                    if runtime and fingerprint not in blocked_rate_fingerprints:
                        blocked_rate_fingerprints.add(fingerprint)
                        runtime.record_tool_outcome(
                            tool_name,
                            status=status,
                            summary=f"blocked pending_field={pending}",
                        )
                    return compact_json(result)

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
                validation_error.update(
                    {
                        "pricing_backend_called": False,
                        "spoken_response_instruction": (
                            "No verified rate was returned. Do not speak any amount "
                            "or claim that Flat, Prepaid, or COD pricing is unavailable. Correct "
                            "the tool arguments from authoritative state and call the calculator "
                            "once again; if that is not possible, say only that the verified rate "
                            "is temporarily unavailable."
                        ),
                    }
                )
                if ignored_model_fields:
                    validation_error["ignored_model_fields"] = ignored_model_fields
                logger.info(
                    (
                        "Blocked calculate_shipkia_rate locally: status=%s "
                        "next_missing_field=%s invalid_fields=%s ignored_model_fields=%s"
                    ),
                    validation_error.get("status"),
                    validation_error.get("next_missing_field", ""),
                    validation_error.get("invalid_fields", []),
                    ignored_model_fields,
                )
                if runtime:
                    runtime.record_tool_outcome(
                        tool_name,
                        status=validation_error.get("status", "blocked"),
                        summary=(
                            f"blocked pending_field="
                            f"{validation_error.get('next_missing_field', '')} "
                            f"invalid_fields={validation_error.get('invalid_fields', [])} "
                            f"ignored_model_fields={ignored_model_fields}"
                        ),
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

        # All local authorization and argument validation has now succeeded.
        # Only a real backend attempt may authorize Gemini's automatic
        # function-result continuation. Previously this token was issued at
        # function entry, so blocked speculative calls could create duplicate
        # or stale speech.
        if runtime is not None and hasattr(runtime, "expect_realtime_tool_reply"):
            runtime.expect_realtime_tool_reply()

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
        if (
            tool_name != "lookup_pincode_serviceability"
            and cached
            and now - cached[0] < 15
        ):
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
                        if runtime:
                            runtime.record_tool_outcome(
                                tool_name,
                                status="error",
                                summary=f"HTTP {response.status}",
                            )
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
                    if (
                        tool_name == "lookup_pincode_serviceability"
                        and isinstance(result, dict)
                    ):
                        result = _voice_safe_pincode_serviceability_result(
                            result,
                            ask_monthly_shipment_quantity=(
                                conversation_state is None
                                or not conversation_state.is_handled("monthly_shipments")
                            ),
                            post_rate_pending_field=(
                                conversation_state.next_discovery_field()
                                if conversation_state is not None
                                and conversation_state.model_led_flow
                                else ""
                            ),
                        )
                        if conversation_state is not None:
                            if result.get("zone_verified"):
                                conversation_state.mark_route_zone_verified(
                                    str(result.get("zone") or ""),
                                    starting_presented=(result.get("response_type") == "zone_starting"),
                                    route_arguments=arguments,
                                )
                                if conversation_state.shadowfax_surface_rate_due:
                                    result["spoken_response_instruction"] = (
                                        "The route zone is now verified. Do not speak the generic "
                                        "route starting amount and do not ask for a zone. Call "
                                        "get_shipkia_starting_rate immediately; the worker will "
                                        "apply the requested Shadowfax Surface filter."
                                    )
                                remaining_routes = conversation_state.unresolved_route_count()
                                if remaining_routes:
                                    result["remaining_requested_routes"] = remaining_routes
                                    result["spoken_response_instruction"] = (
                                        f"{result.get('spoken_response_instruction', '')} The customer requested "
                                        f"{remaining_routes} more route rate in the same turn. After speaking this "
                                        "route's amount, call lookup_pincode_serviceability again immediately for "
                                        "the next queued route. Do not ask for locations again and do not say that "
                                        "rates are unavailable."
                                    ).strip()
                            elif result.get("response_type") == "general_starting":
                                conversation_state.mark_route_zone_lookup_unavailable(
                                    fallback_presented=True
                                )
                    if (
                        tool_name == "get_shipkia_starting_rate"
                        and isinstance(result, dict)
                    ):
                        pricing_mode = str(
                            rate_metadata.get("pricing_mode")
                            or result.get("response_type")
                            or "general_starting"
                        )
                        starting_status = result.get("status", "success")
                        amount_value = result.get("amount")
                        starting_basis = (
                            result.get("basis") if isinstance(result.get("basis"), dict) else {}
                        )
                        if starting_status != "success" or amount_value in (None, ""):
                            spoken_instruction = (
                                "Say briefly that the starting rate is temporarily unavailable. "
                                "Do not invent an amount, retry automatically, or ask the customer "
                                "to repeat a handled field."
                            )
                        elif pricing_mode == "general_starting":
                            if (
                                rate_metadata.get("pricing_trigger_field")
                                == "payment_type_both"
                            ):
                                spoken_instruction = (
                                    "The customer selected Both. Do not ask for permission, consent, "
                                    "or COD order value now. Say naturally: ShipKia ke Prepaid rates "
                                    f"Rs {float(amount_value):.2f} se start hote hain; exact Prepaid rate route, weight aur "
                                    "service par, aur COD rate order value par depend karega."
                                )
                            else:
                                spoken_instruction = (
                                    f"In one short sentence say that ShipKia rates start from Rs {float(amount_value):.2f} "
                                    "and the exact rate depends on route, weight and service. Do not "
                                    "call calculate_shipkia_rate."
                                )
                        elif rate_metadata.get("shadowfax_surface_request"):
                            spoken_instruction = (
                                f"Say directly: Zone {result.get('zone')} ke liye "
                                f"{starting_basis.get('service') or 'Shadowfax Surface'} ka "
                                f"GST-inclusive starting rate Rs {float(amount_value):.2f} hai. "
                                "Do not call it Flat, do not list E-Kart, do not ask the customer "
                                "to choose a zone, and do not ask for confirmation before speaking."
                            )
                        else:
                            spoken_instruction = (
                                f"In one short sentence say Zone {result.get('zone')} rates "
                                f"start from Rs {float(amount_value):.2f}, GST included. Do not "
                                "calculate a shipment rate and do not ask a follow-up question."
                            )
                        result = {
                            "status": starting_status,
                            "response_type": result.get(
                                "response_type", pricing_mode
                            ),
                            "pricing_mode": pricing_mode,
                            "pricing_trigger_field": rate_metadata.get(
                                "pricing_trigger_field", ""
                            ),
                            "trusted_zone_source": rate_metadata.get(
                                "trusted_zone_source", ""
                            ),
                            "zone": result.get("zone"),
                            "amount": result.get("amount"),
                            "currency": result.get("currency", "INR"),
                            "gst_inclusive": bool(result.get("gst_inclusive")),
                            "rate_card": result.get("rate_card"),
                            "message": result.get("message", ""),
                            "basis": starting_basis,
                            "available_courier_partners": list(
                                result.get("available_courier_partners") or []
                            ),
                            "starting_rate_options": list(
                                result.get("starting_rate_options") or []
                            ),
                            "starting_rate_options_note": result.get(
                                "starting_rate_options_note"
                            ),
                            "spoken_response_instruction": spoken_instruction,
                        }
                        if (
                            result.get("status") == "success"
                            and conversation_state is not None
                        ):
                            conversation_state.mark_starting_rate_presented()
                            if rate_metadata.get("shadowfax_surface_request"):
                                conversation_state.mark_shadowfax_surface_rate_presented()
                    if tool_name == "get_shipkia_flat_rates" and isinstance(result, dict):
                        result = _voice_flat_catalog_result(
                            result,
                            conversation_state=conversation_state,
                        )
                        if (
                            result.get("status") == "success"
                            and conversation_state is not None
                            and (
                                rate_metadata.get("direct_v5_flat_catalog")
                                or rate_metadata.get("direct_model_led_flat_catalog")
                            )
                        ):
                            conversation_state.mark_flat_catalog_presented(
                                result.get("flat_rate_options")
                                if isinstance(result.get("flat_rate_options"), list)
                                else None,
                                result.get("flat_additional_rate_options")
                                if isinstance(result.get("flat_additional_rate_options"), list)
                                else None,
                            )
                    if (
                        tool_name == "get_shipkia_flat_zonal_rates"
                        and isinstance(result, dict)
                    ):
                        result = _voice_flat_zonal_catalog_result(
                            result,
                            conversation_state=conversation_state,
                        )
                        if (
                            result.get("status") == "success"
                            and conversation_state is not None
                            and (
                                rate_metadata.get("direct_v5_flat_zonal_catalog")
                                or rate_metadata.get("direct_model_led_flat_zonal_catalog")
                            )
                        ):
                            conversation_state.mark_flat_zonal_catalog_presented(
                                result.get("zone_groups")
                                if isinstance(result.get("zone_groups"), list)
                                else None,
                                result.get("additional_weight")
                                if isinstance(result.get("additional_weight"), dict)
                                else None,
                            )
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
                                "returned Prepaid rate, clearly labelled Prepaid, and say that the "
                                "COD rate depends on order value. Do not ask for permission or "
                                "order value now, add a COD calculation, or call this a refusal."
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
                    if (
                        conversation_state is not None
                        and conversation_state.v4_strict_flow
                        and tool_name in {
                            "lookup_pincode_serviceability",
                            "calculate_shipkia_rate",
                            "get_shipkia_flat_rates",
                            "get_shipkia_flat_zonal_rates",
                            "get_shipkia_starting_rate",
                        }
                        and isinstance(result, dict)
                        and result.get("status") == "success"
                        and not result.get("remaining_requested_routes")
                    ):
                        post_rate_instruction = (
                                (
                                    "After stating the verified requested rate, ask only for the "
                                    "customer's approximate monthly shipment quantity."
                                    if not conversation_state.is_handled("monthly_shipments")
                                    else (
                                        (
                                            "After stating the verified requested rate, say that "
                                            "for this volume they will get a dedicated account "
                                            "manager for support and ticketing, then ask exactly: "
                                            "'Kya aap ShipKia ke saath aage badhna chahte hain?' "
                                            "Do not ask 'kuch aur'."
                                            if int(
                                                conversation_state.value("monthly_shipments") or 0
                                            ) > 500
                                            else "After stating the verified requested rate, ask "
                                            "exactly: 'Kya aap ShipKia ke saath aage badhna chahte "
                                            "hain?' Do not ask 'kuch aur'."
                                        )
                                        if conversation_state.direct_onboarding_flow
                                        else
                                        "After stating the verified requested rate, stop without "
                                        "asking another question."
                                        if conversation_state.anything_else_checkpoint_consumed
                                        else "After stating the verified requested rate, ask "
                                        "exactly: 'Kya aap kuch aur jaanna chahenge?'"
                                    )
                                )
                                if conversation_state.v5_company_pair_flow
                                else "After stating the verified requested rate, ask once whether "
                                "the customer would like to know anything else. If they say no, "
                                "thank them warmly for their time and close the call."
                            )
                        if tool_name == "get_shipkia_flat_zonal_rates":
                            post_rate_instruction = (
                                (
                                    "After stating both verified 500-gram zone-group rates and the "
                                    "additional-weight amount, say that for this volume they will "
                                    "get a dedicated account manager for support and ticketing, "
                                    "then ask exactly: 'Kya aap ShipKia ke saath aage badhna "
                                    "chahte hain?' Do not ask 'kuch aur'."
                                    if conversation_state.direct_onboarding_flow
                                    and int(conversation_state.value("monthly_shipments") or 0) > 500
                                    else "After stating both verified 500-gram zone-group rates and "
                                    "the additional-weight amount, ask exactly: 'Kya aap ShipKia ke "
                                    "saath aage badhna chahte hain?' Do not ask 'kuch aur'."
                                    if conversation_state.direct_onboarding_flow
                                    else "After stating both verified 500-gram zone-group rates and "
                                    "the additional-weight amount, stop without another question. "
                                    "Do not ask whether they want the rates and do not ask 'kuch aur'."
                                )
                                if conversation_state.is_handled("monthly_shipments")
                                else "After stating the complete verified Flat-Zonal catalog, ask "
                                "only for approximate monthly shipment quantity."
                            )
                        if conversation_state.model_led_flow:
                            discovery_field = (
                                ""
                                if conversation_state.pan_india_requested
                                else conversation_state.next_discovery_field()
                            )
                            if discovery_field:
                                discovery_label = _RATE_FIELD_LABELS.get(
                                    discovery_field,
                                    discovery_field.replace("_", " "),
                                )
                                post_rate_instruction = (
                                    "After stating the complete verified requested rate, ask only "
                                    f"for the customer's {discovery_label}. Do not ask monthly "
                                    "shipment quantity, offer onboarding, or ask to move forward yet."
                                )
                            elif not conversation_state.is_handled("monthly_shipments"):
                                post_rate_instruction = (
                                    "After stating the complete verified requested rate, ask only "
                                    "for approximate monthly shipment quantity. Discovery is complete; "
                                    "do not reopen a handled customer-information question."
                                )
                            else:
                                volume = int(
                                    conversation_state.value("monthly_shipments") or 0
                                )
                                manager_note = (
                                    " Briefly explain dedicated account-manager support for this "
                                    "volume."
                                    if volume > 500
                                    else ""
                                )
                                post_rate_instruction = (
                                    "After stating the complete verified requested rate, all sales "
                                    "discovery and monthly volume are handled."
                                    + manager_note
                                    + " Then ask exactly: 'Kya aap kuch aur jaanna chahenge?' "
                                    "Do not ask the move-forward question until this checkpoint is answered."
                                )
                        result = {**result, "post_rate_instruction": post_rate_instruction}
                        conversation_state.mark_pricing_verified(
                            tool_name,
                            payment_basis=str(
                                result.get("payment_basis")
                                or result.get("payment_type")
                                or conversation_state.value("payment_type")
                                or ""
                            ),
                        )
                    if (
                        conversation_state is not None
                        and tool_name
                        in {
                            "lookup_pincode_serviceability",
                            "calculate_shipkia_rate",
                            "get_shipkia_flat_rates",
                            "get_shipkia_flat_zonal_rates",
                            "get_shipkia_starting_rate",
                        }
                        and isinstance(result, dict)
                        and result.get("status") == "success"
                    ):
                        conversation_state.authorize_rate_result(result)
                    text = compact_json(result)
                    if tool_name == "get_shipkia_starting_rate":
                        last_starting_rate_response = text
                    if runtime:
                        outcome_status = (
                            result.get("status", "success")
                            if isinstance(result, dict)
                            else "success"
                        )
                        summary = ""
                        if isinstance(result, dict):
                            if tool_name in {
                                "lookup_pincode_serviceability",
                                "get_shipkia_starting_rate",
                            }:
                                summary = compact_json(
                                    {
                                        "response_type": result.get("response_type"),
                                        "pricing_mode": result.get("pricing_mode"),
                                        "trigger_field": result.get(
                                            "pricing_trigger_field"
                                        ),
                                        "zone": result.get("zone"),
                                        "amount": result.get("amount"),
                                        "currency": result.get("currency"),
                                        "gst_inclusive": result.get("gst_inclusive"),
                                        "pickup_location": result.get("pickup_location"),
                                        "delivery_location": result.get("delivery_location"),
                                        "pickup_pincode": result.get("pickup_pincode"),
                                        "delivery_pincode": result.get("delivery_pincode"),
                                        "rate_card_version": (
                                            result.get("rate_card") or {}
                                        ).get("version")
                                        if isinstance(result.get("rate_card"), dict)
                                        else "",
                                    },
                                    max_chars=500,
                                )
                            else:
                                summary = str(
                                    result.get("message")
                                    or result.get("response_scope")
                                    or result.get("selected_service")
                                    or ""
                                )
                        runtime.record_tool_outcome(
                            tool_name,
                            status=outcome_status,
                            summary=summary,
                        )
                    _TOOL_CACHE[key] = (now, text)
                    return text
        except Exception as exc:
            failed = True
            logger.exception("MCP %s connection failed: %s", tool_name, exc)
            if runtime:
                runtime.record_tool_outcome(
                    tool_name,
                    status="error",
                    summary="MCP connection failed",
                )
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
    conversation_state: GatedConversationState | None = None,
    turn_processor: GuardedTurnProcessor | None = None,
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
        if (
            tool_name not in system_prompt
            and tool_name != "get_shipkia_starting_rate"
        ):
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
        if tool_name == "lookup_pincode_serviceability":
            lookup_parameters = copy.deepcopy(raw_schema["parameters"])
            lookup_parameters.setdefault("type", "object")
            lookup_parameters.setdefault("properties", {})
            lookup_parameters["properties"].update(
                {
                    "pickup_location": {
                        "type": "string",
                        "description": "Customer-confirmed pickup city or precise location.",
                    },
                    "delivery_location": {
                        "type": "string",
                        "description": "Customer-confirmed delivery city or precise location.",
                    },
                    "pan_india": {
                        "type": "boolean",
                        "description": "True only for a Pan-India or All-India starting-rate request.",
                    },
                }
            )
            if conversation_state is not None and (
                conversation_state.v5_company_pair_flow
                or conversation_state.model_led_flow
            ):
                # V5 resolves its starting-rate zone from customer-stated
                # locations. Do not expose pincode inputs that Gemini can fill
                # speculatively or turn into extra customer questions.
                lookup_parameters["properties"].pop("pickup_pincode", None)
                lookup_parameters["properties"].pop("delivery_pincode", None)
            lookup_parameters["required"] = []
            raw_schema["parameters"] = lookup_parameters
            if conversation_state is not None and conversation_state.model_led_flow:
                raw_schema["description"] = (
                    "Resolve customer-stated pickup and delivery locations to a verified zone and "
                    "starting rate. Use pan_india only when the customer says Pan/All India."
                )
            else:
                raw_schema["description"] = (
                    f"{raw_schema['description']} Call once per queued route after the worker has confirmed "
                    "both city/locality endpoints, or after a Pan-India/All-India request. The "
                    "worker replaces model arguments with validated state. Speak the returned Zone A-F "
                    "starting amount immediately. Pan-India uses the returned Zone A starting basis. If "
                    "resolution fails, speak only the returned Rs 22 general starting headline."
                )
        elif tool_name == "calculate_shipkia_rate":
            raw_schema["parameters"], backend_argument_names = _augment_rate_tool_schema(
                raw_schema["parameters"]
            )
            if conversation_state is not None and (
                conversation_state.v5_company_pair_flow
                or conversation_state.model_led_flow
            ):
                properties = raw_schema["parameters"].setdefault("properties", {})
                properties.pop("pickup_pincode", None)
                properties.pop("delivery_pincode", None)
                required = raw_schema["parameters"].get("required", [])
                raw_schema["parameters"]["required"] = [
                    field
                    for field in required
                    if field not in {"pickup_pincode", "delivery_pincode"}
                ]
            if conversation_state is not None and conversation_state.model_led_flow:
                raw_schema["description"] = (
                    "Calculate a shipment-specific verified rate from customer-confirmed details. "
                    "Use only when the required route or zone, weight, payment basis, and COD order "
                    "value where applicable are known; never invent an argument."
                )
            else:
                raw_schema["description"] = (
                f"{raw_schema['description']} The worker owns an authoritative, evidence-backed "
                "gated state. Do not use this pricing tool to save partial answers and do not supply "
                "Unknown, Not Applicable, or guessed qualification values. Call it only when the "
                "per-turn authoritative state instruction says pricing_ready=true. The worker "
                "ignores model-provided gated fields and builds pricing inputs from validated "
                "customer turns. Ask payment mode once. Use payment_type=Both when the customer says both/dono; "
                "the worker will calculate Prepaid. For COD, call once with COD and no invented "
                "order value; the local response will require only order value before pricing. "
                "After a numeric value, call again with order_value. If the customer refuses or "
                "does not know a required pincode, weight, payment type, or COD value, do not call "
                "this calculator; the worker switches to get_shipkia_starting_rate. For later "
                "normal-rate or flat-rate requests in the same call, omit unchanged fields; the "
                "voice worker reuses them and must not ask the customer again. "
                "For an exact normal-rate request, omit zone. A customer-supplied approved Zone "
                "A, B, C, D, E or F belongs to get_shipkia_starting_rate and must never be "
                "forwarded to this calculator. Never ask the customer to identify a zone. Set "
                "rate_request_type=Flat for a flat-rate request. Never use flat_rate_options or "
                "flat_additional_rate_options as a service name. A service follow-up after a flat "
                "answer remains flat; set normal_rates_explicitly_requested=true only when the "
                "customer explicitly asks for normal courier rates. Use flat_response_scope=Best "
                "for the first generic flat request, More Options only when alternatives are "
                "requested, and Selected Service with the exact service after a selection. Never "
                "ask for monthly shipment volume; include it only if volunteered. If the customer "
                "changes a pickup or delivery place, retain the stated city/locality and use the "
                "route-zone starting-rate path. Never invent a pincode from a city name. If the "
                "customer explicitly cannot provide weight, payment type, or required COD value, "
                    "use the general starting rate immediately."
                )
        elif tool_name == "get_shipkia_starting_rate":
            starting_description = (
                ""
                if conversation_state is not None
                and conversation_state.v4_strict_flow
                else raw_schema["description"]
            )
            raw_schema["description"] = (
                "Return the verified starting rate for an explicitly requested Zone A-F, or the "
                "general starting headline when no zone is supplied. Never invent a zone."
                if conversation_state is not None and conversation_state.model_led_flow
                else (
                    f"{starting_description} The worker owns the trusted pricing mode and ignores "
                    "model-invented zones. Call once only when authoritative state says "
                    "pricing_mode=general_starting or zone_starting. Without a validated approved "
                    "zone, return the backend-verified general starting headline. Never infer or "
                    "supply an amount, retry automatically, or ask a follow-up question in the same "
                    "response."
                )
            )
        elif tool_name == "get_shipkia_flat_rates":
            properties = raw_schema["parameters"].setdefault("properties", {})
            properties.setdefault("payment_type", {})["enum"] = ["Prepaid", "COD"]
            properties.setdefault("response_scope", {})["enum"] = [
                "Starting",
                "Matching",
                "All",
            ]
            raw_schema["description"] = (
                "Return the complete verified Flat catalog for an explicit Flat request."
                if conversation_state is not None and conversation_state.model_led_flow
                else
                f"{raw_schema['description']} For V5, an explicit Flat request is a direct catalog "
                "request: call immediately and the worker forces response_scope=All and Prepaid, "
                "without qualification, route, weight, or payment questions. For older guarded "
                "flows, use Starting for a generic request, Matching when weight is supplied, and "
                "All only when every slab is requested. For COD, never invent order value."
            )
        elif tool_name == "get_shipkia_flat_zonal_rates":
            properties = raw_schema["parameters"].setdefault("properties", {})
            properties.setdefault("payment_type", {})["enum"] = ["Prepaid", "COD"]
            raw_schema["description"] = (
                "Return the complete verified Flat-Zonal catalog for an explicit Flat-Zonal request."
                if conversation_state is not None and conversation_state.model_led_flow
                else
                f"{raw_schema['description']} Call only when authoritative V5 state says "
                "pricing_mode=flat_zonal_catalog after the customer explicitly says Flat-Zonal, "
                "flat zonal, zonal-flat, or zone-wise flat. The worker forces the verified "
                "Prepaid catalog and blocks model-inferred Flat-Zonal calls."
            )
        tools.append(
            function_tool(
                make_mcp_forwarder(
                    tool_name,
                    task_id,
                    runtime,
                    conversation_state,
                    turn_processor,
                    backend_argument_names=backend_argument_names,
                ),
                raw_schema=raw_schema,
            )
        )
        registered_tool_names.append(tool_name)
        logger.info("Registered ShipKia tool: %s", tool_name)
    return tools, tuple(registered_tool_names)


def _authoritative_call_state_instruction(
    conversation_state: GatedConversationState,
    response_language: str,
) -> str:
    """Render the worker-owned state instruction used before and between turns."""
    handled_state = {
        field: {"status": field_state.status, "value": field_state.value}
        for field, field_state in conversation_state.fields.items()
        if conversation_state.is_handled(field)
    }
    authorized_amounts = sorted(conversation_state.authorized_rate_amounts)
    rate_authorization = (
        f"Only these exact ShipKia amounts are authorized: {authorized_amounts}."
        if authorized_amounts
        else (
            "No ShipKia numeric amount is authorized. Do not speak one until a pricing "
            "tool returns status success."
        )
    )
    language_lock = (
        "English only; do not use Hindi, Hinglish, or Devanagari."
        if response_language == "English"
        else "Natural Hinglish in Latin script only; never output Devanagari."
    )
    return (
        "\n\n## Current authoritative call state (worker-updated)\n"
        f"- Handled facts; never ask these again: {compact_json(handled_state, max_chars=6000)}\n"
        f"- Pending field: {conversation_state.pending_field() or '[none]'}\n"
        f"- Current action: {conversation_state.guidance()}\n"
        f"- Rate authorization: {rate_authorization}\n"
        f"- Response language lock: {language_lock}\n"
        "This section overrides any stale conversational assumption. Ask only the current "
        "action's question and never restart qualification."
    )


def _v6_runtime_state_context(
    conversation_state: GatedConversationState,
    response_language: str,
) -> str:
    """Append replaceable call facts to the single V6 prompt.

    This is deliberately data, not a response-specific mini-prompt.  The V6
    prompt owns wording and flow; the worker only refreshes facts that Gemini
    cannot safely infer from an audio transcript (handled fields, pending gate,
    pricing authorization, and close state).
    """
    full_snapshot = conversation_state.snapshot()
    always_present = {
        "pending_field",
        "pricing_ready",
        "pricing_mode",
        "requested_rate_type",
        "authorized_rate_amounts",
        "fields",
    }
    snapshot = {
        key: value
        for key, value in full_snapshot.items()
        if key in always_present or value not in (None, "", False, 0, [], {})
    }
    language = "English" if response_language == "English" else "Hinglish (Latin script)"
    return (
        "\n\n## Live call state (worker-owned data; replaces the previous snapshot)\n"
        f"- Response language: {language}\n"
        f"- State: {compact_json(snapshot, max_chars=8000)}\n"
        "This snapshot contains facts only. Apply the NATURAL SALES FLOW and PRICING rules "
        "from this V6 prompt to it."
    )


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
        conversation_state: GatedConversationState,
        turn_processor: GuardedTurnProcessor,
    ) -> None:
        if not system_prompt.strip():
            raise RuntimeError("Confluence did not provide the ShipKia system prompt.")

        # A prompt may describe tools that are unavailable for this dispatch
        # (for example, read-only Console calls do not expose CRM mutation
        # functions). Remove their callable names before Gemini sees the
        # prompt; a trailing warning alone does not reliably prevent native
        # function calling.
        instructions = system_prompt.strip()
        unavailable_tool_names = ALLOWED_TOOLS.difference(available_tool_names)
        for unavailable_name in sorted(unavailable_tool_names):
            instructions = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(unavailable_name)}(?![A-Za-z0-9_])",
                "an available tool",
                instructions,
            )
        crm_mutation_tools = {
            "create_or_update_shipkia_lead",
            "record_shipkia_call_progress",
            "create_shipkia_followup",
            "finalize_shipkia_call_outcome",
        }
        if not crm_mutation_tools.intersection(available_tool_names):
            instructions += (
                "\n\n## Runtime tool boundary\n"
                "This is a read-only Console call. Never call, invent, or mention a CRM save, "
                "progress, follow-up, or finalization function. Remember confirmed facts only "
                "in the current call state; the spoken sales flow must continue normally."
            )
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

        legacy_runtime_rules = """

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
- When a route is incomplete, ask only for the missing pickup or delivery endpoint. Never describe
  a missing endpoint as rates being updated, a system issue, or an inability to check rates, and
  never ask monthly shipment quantity until a verified starting/catalog/exact rate has been spoken.
- A rate amount or Flat/Prepaid/COD availability statement is verified only when the corresponding
  pricing tool returns success in the current turn. If a pricing call is blocked or errors, never
  reuse Rs 22 as an exact Prepaid/COD rate and never guess that a flat rate is unavailable.
- The worker-controlled authoritative gated-state instruction is the only source of which question
  is pending. Never advance because a reply is merely non-empty. An unrelated reply does not answer
  the pending field: handle it briefly and return to the same question.
- Keep the normal qualification and shipment-question flow. If authoritative state says
  pricing_mode=general_starting or zone_starting, call get_shipkia_starting_rate exactly once,
  speak only that starting-rate response, and ask no follow-up question in that same turn. Never
  call calculate_shipkia_rate in a starting-rate mode.
- A general or Pan-India starting response is: ShipKia rates start from Rs 22; the exact rate
  depends on route, weight and service. If a trusted approved Zone A-F is present, speak only the
  returned exact GST-inclusive starting amount for that zone. Never infer a zone from pincodes.
- Never call calculate_shipkia_rate until the authoritative instruction says pricing_mode=exact
  and pricing_ready=true.
- Before a rate, ask the optional qualification questions in their configured order. If the
  customer explicitly refuses one or clearly says they do not know it, the worker ends all remaining
  optional qualification and continues with only missing shipment inputs. Silence and unrelated
  answers are never refusal.
- If the customer says they currently use nothing, have no courier/aggregator selected, or are a
  new business with no shipping solution, treat current arrangement as Not Applicable and never ask
  arrangement, provider, current rate or current problem again. A future intention to use ShipKia
  does not reopen the current-arrangement question.
- For V5, collect pickup and delivery city/locality names and never request a pincode. For an older
  flow that explicitly requires a pincode, it must be an explicit six-digit customer or CRM value;
  never invent one from a city such as Delhi or Mumbai. If the customer explicitly cannot provide
  a required endpoint, weight, payment type or required COD order value, mark it handled and use
  the general starting response immediately. Silence, noise and unrelated replies never trigger
  this fallback. Weight is mandatory only for exact calculation and must never be assumed.
  Ask payment mode once. If the customer says both/dono or selects Prepaid and COD together, use
  payment_type="Both" and never ask for permission or COD order value. If the exact route is
  complete, give only the returned Prepaid rate and say the COD rate depends on order value. If the
  route is incomplete or Pan India, give the verified Rs 22 Prepaid starting rate and say the exact
  Prepaid rate depends on route, weight and service while the COD rate depends on order value.
- If the customer selects COD, checkpoint payment_type="COD". Before any price, ask only for the
  missing order value. After a numeric answer, call with order_value and give the exact verified COD
  result. Never say the route or rate is unavailable merely because order value was missing, and
  never offer other rates in that turn. If the customer explicitly refuses or does not know order
  value, use the general Rs 22 starting-rate response and do not imply a COD-inclusive total.
- If the customer explicitly refuses or does not know payment type itself, use the general Rs 22
  starting-rate response and never imply that they selected Prepaid.
- If calculate_shipkia_rate is blocked into a starting-rate mode, call
  get_shipkia_starting_rate once instead. Otherwise obey the returned worker-controlled pending
  field. Never fill a missing field from a guess, placeholder, or unrelated customer statement.
- Once calculate_shipkia_rate succeeds in this call, all qualification and unchanged shipment
  fields remain handled for every later normal-rate or flat-rate request. Never ask them again.
  Answer from the prior tool result when sufficient; otherwise call calculate_shipkia_rate with
  only changed or request-specific details. The voice worker restores omitted unchanged fields.
- Treat the supplied pickup and delivery endpoints as the active route. For a courier, service,
  weight, payment, or flat-rate follow-up on that route, reuse both endpoints and never ask for them
  again. In V5 a changed city/locality replaces that endpoint directly; never ask for its pincode.
- A normal result marked as a starting rate must be described with the supplied route,
  returned weight and payment basis in one direct, polite sentence. Give only the single returned
  verified lowest GST-inclusive "starting from" rate. Do not list alternatives, add a route or
  zone explanation, or ask a follow-up question. Never ask the customer for an internal zone.
- If the customer voluntarily states an approved Zone A, B, C, D, E or F, call
  get_shipkia_starting_rate with the worker-validated zone and give only its exact GST-inclusive
  starting amount. Do not calculate a customer-specific shipment rate, offer alternatives or ask
  another question in that response.
- For an explicit flat-rate request, call calculate_shipkia_rate with rate_request_type="Flat".
  Never send flat_rate_options or flat_additional_rate_options as the service. For the first
  generic request use flat_response_scope="Best" and speak only the one returned complete flat
  option. Use "More Options" only after the customer requests alternatives, and only name the
  returned choices without prices. After the customer names a service, use "Selected Service" and
  speak only its verified current-shipment amount; label a verified starting rate correctly and
  stop without a follow-up question.
- After a flat-rate result, any follow-up about one of its listed services remains a flat-detail
  request even if a draft tool call says Normal. A flat additional-weight component is not a
  complete flat shipment rate. Leave flat context only when the customer explicitly asks for
  normal courier rates; then set normal_rates_explicitly_requested=true.
- During rate and service-option exploration, do not add benefits, signup, callbacks, a normal-rate
  offer, or any unrelated sales question. Ask which listed service they want detailed only when
  they requested more options but did not select one.
- Never proactively ask whether the customer wants normal rates. Calculate or discuss normal rates
  only when the customer independently asks for them. If they decline normal rates once, do not
  mention or offer them again unless the customer later explicitly requests them.
- If saving fails, acknowledge internally and continue naturally; do not repeatedly ask the customer for the same answer.
- Never send a message or invoke a messaging channel from this voice worker.
"""
        v4_runtime_rules = """

## V4 voice runtime rules
- Follow each private turn direction silently. Never tell the customer that a direction, state,
  workflow, field, tool, policy, or system message exists.
- Ask one short question at a time. Never repeat a confirmed detail unless the customer explicitly
  corrects it, and never infer a missing answer from noise, silence, or unrelated text. An explicitly
  stated pickup/drop city is a valid V5 route endpoint and must be retained as stated.
- Complete consent and rate/onboarding intent before qualification. Qualification order is business
  name, business type, then current shipping arrangement. If no courier is currently used, skip
  provider, current rate, and current problem.
- For a V5 normal starting rate, ask once where shipments go from and to. Accept the pickup and
  delivery city/locality directly and never ask for a pincode. Once both are confirmed, resolve the
  zone and speak its returned starting rate before asking shipment
  quantity; do not delay it for weight or payment mode. Pan-India/All-India uses the resolver's Zone A
  starting basis. V5 has exactly three customer-facing structures: Zonal (route/Zone A-F based),
  Flat-Zonal (verified E-Kart Express Zone A-B and Zone C-F groups), and Flat (all-zone E-Kart
  Surface slabs). Never substitute one for another. Explicit Flat and Flat-Zonal requests return
  their own verified catalog immediately without route, weight, payment, or qualification questions.
- When the customer asks for multiple routes together, resolve and speak every queued route in order.
  Never reuse one route's zone for the next route. A successful tool amount must be spoken; never
  claim that rate checking failed after a successful result.
- A price may be spoken only from a successful pricing-tool result in the current conversation.
  Never guess, cache, or repeat an amount after a blocked or failed call.
- Never speak tool names, field names, JSON, metadata, record IDs, or implementation details.
- Keep the locked response language and speak naturally in short, complete sentences.
- When the customer asks about ShipKia, its working, benefits, or USPs, answer the actual question
  before resuming the sales flow. Use two or three relevant verified capabilities for a general or
  specific question; when the customer explicitly asks what all services/facilities are available or
  asks for detail, explain all four verified capabilities. Use natural conversational wording, but
  never invent a feature, guarantee, saving, discount, delivery promise, or numeric rate. Every
  amount and zone remains authorized only by a successful pricing-tool result.
"""
        if conversation_state.v5_company_pair_flow:
            v4_runtime_rules += """

## V5 quantity and close flow
- After a verified V5 rate, ask the monthly shipment quantity once when it is missing. Capture a numeric or ranged answer before
  moving on. Then ask exactly "Kya aap kuch aur jaanna chahenge?" Ask this checkpoint only once.
  Answer any requested follow-up fully without automatically appending the checkpoint again. After
  later Flat, Flat-Zonal, provider-rate, or repeated-rate answers, do not re-arm this checkpoint. After
  an acknowledgement, forgotten question, or no-current-question response, ask the exact ShipKia
  move-forward question once.
- If the customer asks for explanation, comparison, or quantity-specific pricing, answer that
  objection first using only the verified starting amount and an honest dedicated-plan/team
  explanation. Never repeat the bare move-forward question without answering the objection.
- A no/nahi response to the move-forward question, including no/nahi followed by a reason,
  authorizes the better-plan team-discussion close. Do not repeat the question after that decision.
"""
        else:
            v4_runtime_rules += """

## V4 post-rate close flow
- After giving the first requested verified rate, ask once whether the customer wants to know
  anything else. Never decide satisfaction yourself and never speak the satisfied/onboarding URL
  close from this general rule. That close is allowed only when the current private turn direction
  explicitly says onboarding-close authorization is TRUE and supplies the exact response.
  Yes/haan/ji/si, unclear audio, a partial word, silence, or thank-you by itself never authorizes
  this close.
"""
        if conversation_state.v4_strict_flow and not conversation_state.model_led_flow:
            instructions += v4_runtime_rules
        elif not conversation_state.model_led_flow:
            instructions += legacy_runtime_rules
        if not conversation_state.v4_strict_flow:
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
        self._base_instructions = instructions
        self._runtime = runtime
        self._conversation_state = conversation_state
        self._turn_processor = turn_processor
        self._response_language = "Hinglish"
        self._normal_rates_declined = False
        self._tools_by_name = dict(zip(available_tool_names, tools, strict=True))
        self._rate_tool_enabled = conversation_state.pricing_ready() and (
            not conversation_state.v4_strict_flow
            or conversation_state.requested_rate_type != "Flat"
        )
        self._flat_tool_enabled = (
            conversation_state.flat_catalog_due()
            or conversation_state.pricing_ready()
            and (
                not conversation_state.v4_strict_flow
                or conversation_state.requested_rate_type == "Flat"
            )
        )
        self._flat_zonal_tool_enabled = conversation_state.flat_zonal_catalog_due()
        self._starting_tool_enabled = conversation_state.starting_rate_due()
        initial_tools = self._active_tools()
        initial_instructions = instructions
        if conversation_state.model_led_flow:
            initial_instructions += _v6_runtime_state_context(
                conversation_state,
                self._response_language,
            )
        elif conversation_state.v4_strict_flow:
            # Native Gemini realtime can begin drafting before its first
            # user-turn callback completes. Seed the initial consent gate so
            # an ambiguous first utterance cannot jump to rates/onboarding and
            # then require a second corrective response.
            initial_instructions += _authoritative_call_state_instruction(
                conversation_state,
                self._response_language,
            )
        self._last_synced_instructions = initial_instructions
        super().__init__(instructions=initial_instructions, tools=initial_tools)

    def _active_tools(self) -> list:
        enabled = []
        expose_guarded_v5_pricing = (
            self._conversation_state.v5_company_pair_flow
            or self._conversation_state.model_led_flow
        )
        for name, tool in self._tools_by_name.items():
            if expose_guarded_v5_pricing and name in {
                "calculate_shipkia_rate",
                "get_shipkia_flat_rates",
                "get_shipkia_flat_zonal_rates",
                "get_shipkia_starting_rate",
            }:
                # Gemini Live retains function names within the same session.
                # Keep schemas stable so a later valid rate request cannot hit
                # "unknown AI function". V5 gates calls through state; V6 lets
                # the model choose the path while backend validation owns facts.
                enabled.append(tool)
                continue
            if name == "calculate_shipkia_rate" and not self._rate_tool_enabled:
                continue
            if name == "get_shipkia_flat_rates" and not self._flat_tool_enabled:
                continue
            if (
                name == "get_shipkia_flat_zonal_rates"
                and not self._flat_zonal_tool_enabled
            ):
                continue
            if name == "get_shipkia_starting_rate" and not self._starting_tool_enabled:
                continue
            enabled.append(tool)
        return enabled

    async def sync_pricing_tools(self) -> None:
        state_context = (
            _v6_runtime_state_context(
                self._conversation_state,
                self._response_language,
            )
            if self._conversation_state.model_led_flow
            else _authoritative_call_state_instruction(
                self._conversation_state,
                self._response_language,
            )
        )
        refreshed_instructions = self._base_instructions + state_context
        if refreshed_instructions != self._last_synced_instructions:
            await self.update_instructions(refreshed_instructions)
            self._last_synced_instructions = refreshed_instructions
        should_enable = self._conversation_state.pricing_ready() and (
            not self._conversation_state.v4_strict_flow
            or self._conversation_state.requested_rate_type != "Flat"
        )
        should_enable_flat = (
            self._conversation_state.flat_catalog_due()
            or self._conversation_state.pricing_ready()
            and (
                not self._conversation_state.v4_strict_flow
                or self._conversation_state.requested_rate_type == "Flat"
            )
        )
        should_enable_flat_zonal = self._conversation_state.flat_zonal_catalog_due()
        should_enable_starting = self._conversation_state.starting_rate_due()
        if (
            should_enable == self._rate_tool_enabled
            and should_enable_flat == self._flat_tool_enabled
            and should_enable_flat_zonal == self._flat_zonal_tool_enabled
            and should_enable_starting == self._starting_tool_enabled
        ):
            return
        self._rate_tool_enabled = should_enable
        self._flat_tool_enabled = should_enable_flat
        self._flat_zonal_tool_enabled = should_enable_flat_zonal
        self._starting_tool_enabled = should_enable_starting
        await self.update_tools(self._active_tools())
        await self._runtime.emit(
            "pricing_tool_availability_changed",
            calculate_shipkia_rate_enabled=should_enable,
            get_shipkia_flat_rates_enabled=should_enable_flat,
            get_shipkia_flat_zonal_rates_enabled=should_enable_flat_zonal,
            get_shipkia_starting_rate_enabled=should_enable_starting,
            pricing_mode=self._conversation_state.pricing_mode(),
            pending_field=self._conversation_state.pending_field(),
            state_snapshot=self._conversation_state.snapshot(),
            metrics=self._runtime.metrics(),
        )

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        customer_text = new_message.text_content or ""
        turn_id = str(getattr(new_message, "id", "") or f"user:{time.time()}")
        if self._conversation_state.model_led_flow:
            # The central V6 prompt owns wording, pacing, and sales behavior.
            # State extraction stays in the background and must not inject a
            # second, partially duplicated sales prompt into every turn.
            self._turn_processor.schedule(customer_text, turn_id=turn_id)
            self._response_language = _response_language_for_turn(
                customer_text,
                self._response_language,
            )
            return
        await self._turn_processor.process(customer_text, turn_id=turn_id)

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
        if self._conversation_state.v4_strict_flow and memory:
            memory = "\n".join(
                line for line in memory.splitlines() if line.startswith("CUSTOMER:")
            )
        if memory:
            self._runtime.record_memory_injection(memory)

        policy_parts = [self._conversation_state.guidance()]
        authorized_amounts = sorted(self._conversation_state.authorized_rate_amounts)
        if authorized_amounts:
            policy_parts.append(
                "ShipKia numeric-rate authorization is TRUE only for these exact backend-returned "
                f"amounts: {authorized_amounts}. Do not speak any other ShipKia amount."
            )
        else:
            policy_parts.append(
                "ShipKia numeric-rate authorization is FALSE. Do not speak any ShipKia amount "
                "until a pricing tool returns status success, even if you remember or infer one."
            )
        if self._conversation_state.onboarding_link_due:
            policy_parts.append(
                "Onboarding-close authorization is TRUE for this turn. Speak the approved WhatsApp "
                "onboarding-link close exactly once as directed, then end. Do not speak the URL aloud."
            )
        else:
            policy_parts.append(
                "Onboarding-close authorization is FALSE for this turn. Do not say the customer is "
                "satisfied, do not say you are sending an onboarding URL, and do not speak the signup "
                "URL even if you think unclear audio sounds like agreement. Only authoritative state "
                "may authorize that close."
            )
        if self._conversation_state.better_plan_close_due:
            policy_parts.append(
                "Better-plan close authorization is TRUE for this turn. Speak the exact approved "
                "team-discussion close once and end; do not ask another question or send onboarding."
            )
        if self._conversation_state.move_forward_question_due:
            if self._conversation_state.last_provider_options_query:
                policy_parts.append(
                    "The customer has asked a genuine courier/options follow-up. Answer it fully "
                    "from the worker-verified option list before any sales close. Do not ask the "
                    "move-forward question in the same response."
                )
            else:
                policy_parts.append(
                    "Move-forward decision is pending. Ask the exact ShipKia move-forward question and "
                    "wait for a clear yes or no; never convert thanks or satisfaction into yes."
                )
        elif self._conversation_state.anything_else_detail_due:
            policy_parts.append(
                "The customer said they want more information. Ask what they want to know; do not "
                "ask the ShipKia move-forward question yet."
            )
        elif self._conversation_state.anything_else_question_due:
            policy_parts.append(
                "The information checkpoint is pending. Ask exactly 'Kya aap kuch aur jaanna "
                "chahenge?' Only a clear no/nothing-else reply may unlock the move-forward question."
            )
        if memory:
            policy_parts.append(
                "Same-call memory (confirmed details remain handled; later explicit corrections "
                f"win):\n{memory}"
            )

        if self._conversation_state.v4_strict_flow:
            if self._conversation_state.flat_catalog_due():
                pricing_path_instruction = (
                    "This is a V5 direct flat-catalog enquiry. Call get_shipkia_flat_rates now "
                    "when directed above and speak every returned slab. Do not ask for business "
                    "details, pincodes, weight, payment mode, or permission first."
                )
            elif self._conversation_state.flat_zonal_catalog_due():
                pricing_path_instruction = (
                    "This is an explicit V5 Flat-Zonal enquiry. Call "
                    "get_shipkia_flat_zonal_rates only when directed above and speak the two "
                    "returned zone groups plus the returned additional-weight condition. Do not "
                    "substitute the all-zone Flat catalog or infer a route zone."
                )
            elif self._conversation_state.requested_rate_type == "Normal":
                pricing_path_instruction = (
                    "This is a normal-rate enquiry. Ask only the customer-facing question directed "
                    "above until all details are complete. Do not discuss flat pricing. Never tell "
                    "the customer how the flow is being controlled."
                )
            elif self._conversation_state.requested_rate_type == "Flat":
                pricing_path_instruction = (
                    (
                        "This is a flat-rate enquiry. Ask only the customer-facing question directed "
                        "above; shipment details are weight and payment mode only, never pincodes. Do "
                        "not mention an amount until verified."
                    )
                )
            elif self._conversation_state.requested_rate_type == "Zonal":
                pricing_path_instruction = (
                    "This is an explicit Zonal-rate enquiry. Preserve the customer's confirmed "
                    "route or zone and use only the worker-authorized resolver/starting-rate path. "
                    "Do not switch it to Flat or Flat-Zonal."
                )
            else:
                pricing_path_instruction = (
                    "The customer has not selected a rate type yet. Ask only the customer-facing "
                    "question directed above and do not mention an amount."
                )
            policy_parts.append(pricing_path_instruction)

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
        policy_parts.append(language_instruction)
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
        policy_parts.append(normal_rate_instruction)
        turn_ctx.add_message(
            role="system",
            content=(
                "PRIVATE TURN DIRECTION — never quote, paraphrase, acknowledge, or explain any "
                "part of this message to the customer. Respond only with the natural customer-facing "
                "sentence it requires:\n- " + "\n- ".join(policy_parts)
            ),
        )
        if self._conversation_state.v5_company_pair_flow:
            # Final-transcript scheduling owns the V5 response. If this SDK
            # callback also runs, suppress its automatic native reply so two
            # generators cannot speak over or truncate one another.
            raise StopResponse()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.50")),
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.50")),
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.70")),
    )


async def post_confluence_event(task_id: str | None, room_name: str, event: str, **extra: Any) -> None:
    if not CONFLUENCE_CALLBACK:
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

    conversation_state = GatedConversationState(
        v4_strict_flow=prompt_version in STRICT_PROMPT_VERSIONS,
        v5_company_pair_flow=prompt_version in {"shipkia-voice-v5", "shipkia-voice-v6"},
        direct_onboarding_flow=prompt_version in {"shipkia-voice-v5", "shipkia-voice-v6"},
        model_led_flow=prompt_version == "shipkia-voice-v6",
    )
    conversation_state.seed_context(context)
    answer_guard = SemanticAnswerGuard()
    console_session = not bool(task_id)
    # rtc.Room.sid is an async accessor in this SDK; room name is already a
    # stable LiveKit identifier and avoids serializing a coroutine object.
    room_id = str(ctx.room.name)

    async def emit_runtime_event(event: str, **extra: Any) -> None:
        event_payload = {
            "prompt_version": prompt_version,
            "console_session": console_session,
            "call_uuid": room_id,
            "room_id": room_id,
            "state_snapshot": conversation_state.snapshot(),
            "state_transitions": list(conversation_state.transitions),
            **extra,
        }
        await post_confluence_event(
            task_id,
            ctx.room.name,
            event,
            **event_payload,
        )

    runtime = VoiceSessionRuntime(
        emit=emit_runtime_event,
        response_timeout_seconds=float(os.getenv("LIVEKIT_RESPONSE_TIMEOUT_SECONDS", "15")),
        playout_timeout_seconds=float(os.getenv("LIVEKIT_PLAYOUT_TIMEOUT_SECONDS", "30")),
        recovery_timeout_seconds=float(os.getenv("LIVEKIT_RECOVERY_TIMEOUT_SECONDS", "8")),
        reconnect_grace_seconds=float(os.getenv("LIVEKIT_RECONNECT_GRACE_SECONDS", "20")),
        false_interruption_timeout_seconds=float(
            os.getenv("LIVEKIT_FALSE_INTERRUPTION_RECOVERY_SECONDS", "2.5")
        ),
        # Let LiveKit resume the same interrupted native-audio response. The
        # separate runtime regeneration used to race Gemini's own continuation
        # and repeatedly restart the same answer after brief mic activity.
        native_false_interruption_resume=True,
    )
    turn_processor = GuardedTurnProcessor(
        conversation_state=conversation_state,
        answer_guard=answer_guard,
        runtime=runtime,
    )
    tools, available_tool_names = await fetch_tools(
        task_id or CONSOLE_MCP_SCOPE,
        system_prompt,
        runtime,
        conversation_state,
        turn_processor,
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
            # Gemini Live does not support commit_audio, so its server-side AAD
            # must remain enabled for microphone turns to be transcribed.
            disabled=False,
            start_of_speech_sensitivity=_gemini_start_sensitivity(),
            end_of_speech_sensitivity=_gemini_end_sensitivity(),
            prefix_padding_ms=int(os.getenv("GEMINI_VAD_PREFIX_PADDING_MS", "200")),
            silence_duration_ms=int(os.getenv("GEMINI_VAD_SILENCE_DURATION_MS", "850")),
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
        context_window_compression=genai_types.ContextWindowCompressionConfig(
            trigger_tokens=int(os.getenv("GEMINI_CONTEXT_TRIGGER_TOKENS", "24000")),
            sliding_window=genai_types.SlidingWindow(
                target_tokens=int(os.getenv("GEMINI_CONTEXT_TARGET_TOKENS", "16000"))
            ),
        ),
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
                    os.getenv("LIVEKIT_INTERRUPTION_MIN_DURATION_SECONDS", "1.20")
                ),
                "min_words": int(os.getenv("LIVEKIT_INTERRUPTION_MIN_WORDS", "3")),
                "resume_false_interruption": True,
                "false_interruption_timeout": float(
                    os.getenv("LIVEKIT_FALSE_INTERRUPTION_TIMEOUT_SECONDS", "1.0")
                ),
                # Keep speech received during the brief AEC warm-up. The agent
                # may not stop playout immediately, but the customer's first
                # utterance must still become the next input turn.
                "discard_audio_if_uninterruptible": False,
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
        conversation_state=conversation_state,
        turn_processor=turn_processor,
    )
    turn_processor.set_state_changed_callback(assistant.sync_pricing_tools)

    call_done = asyncio.Event()
    participant_seen = bool(ctx.room.remote_participants)
    participant_audio_active = participant_seen
    close_reason = "maximum_call_duration"
    correction_active = False
    ignored_opening_noise: dict[str, float] = {}
    ignored_asr_noise: dict[str, float] = {}
    asr_noise_strikes: list[float] = []
    asr_noise_quarantine_until = 0.0
    information_reply_scheduled_epochs: set[int] = set()
    controlled_information_reply_epochs: set[int] = set()
    flow_correction_epochs: set[int] = set()
    high_volume_delivery_pending = False
    user_turn_unsettled = False

    def schedule_controlled_information_reply(
        state_task: asyncio.Task[None] | None,
        turn_epoch: int,
    ) -> None:
        if (
            state_task is None
            or turn_epoch in information_reply_scheduled_epochs
        ):
            return
        information_reply_scheduled_epochs.add(turn_epoch)
        controlled_at_schedule = _worker_owns_realtime_turn(conversation_state)
        if controlled_at_schedule:
            # Mark it before yielding so an incomplete native draft cannot
            # start a second, post-generation USP correction for this turn.
            controlled_information_reply_epochs.add(turn_epoch)

        async def reply_once() -> None:
            nonlocal high_volume_delivery_pending
            if controlled_at_schedule:
                try:
                    await session.interrupt()
                except RuntimeError:
                    pass
            try:
                await asyncio.shield(state_task)
            except asyncio.CancelledError:
                return
            except Exception:
                return
            controlled_after_state = _worker_owns_realtime_turn(conversation_state)
            controlled_turn = bool(controlled_at_schedule or controlled_after_state)
            detailed_services = conversation_state.last_detailed_usp_query
            provider_options = bool(
                conversation_state.last_provider_options_query
                and not conversation_state.last_provider_rates_query
            )
            newly_captured_high_volume = bool(
                conversation_state.last_monthly_quantity_captured
                and int(conversation_state.value("monthly_shipments") or 0) > 500
                and not conversation_state.anything_else_checkpoint_consumed
                and (
                    not conversation_state.model_led_flow
                    or conversation_state.sales_close_ready()
                )
            )
            high_volume = bool(
                newly_captured_high_volume or high_volume_delivery_pending
            )
            if (
                runtime.user_turn_count != turn_epoch
                or not participant_audio_active
                or not (controlled_turn or detailed_services or provider_options or high_volume)
            ):
                return
            controlled_information_reply_epochs.add(turn_epoch)
            if not controlled_at_schedule and controlled_turn:
                # State can enter the pricing boundary only after the guarded
                # classifier settles. Cancel a native draft before taking over.
                try:
                    await session.interrupt()
                except RuntimeError:
                    pass
            elif not controlled_turn:
                # A preceding serialized state update delayed classification.
                # Interrupt as soon as the detailed intent becomes authoritative.
                try:
                    await session.interrupt()
                except RuntimeError:
                    pass
            await assistant.sync_pricing_tools()
            if conversation_state.model_led_flow:
                # V6 has one prompt owner. The settled worker state and the
                # authorized tool list are refreshed above; do not append a
                # response-specific prompt that can conflict with prior turns.
                reply_instruction = ""
            elif detailed_services:
                reply_instruction = _detailed_services_reply_instruction(
                    assistant._response_language,
                    conversation_state,
                )
            elif provider_options:
                reply_instruction = _provider_options_reply_instruction(
                    assistant._response_language,
                    conversation_state,
                )
            elif high_volume:
                # Keep this obligation until the final assistant transcript
                # proves that the manager benefit was actually delivered. An
                # interrupted partial sentence must not advance to the close.
                high_volume_delivery_pending = True
                reply_instruction = _high_volume_manager_reply_instruction(
                    assistant._response_language,
                    int(conversation_state.value("monthly_shipments") or 0),
                )
            else:
                reply_instruction = conversation_state.guidance()
            reply_kwargs: dict[str, object] = {}
            if reply_instruction:
                reply_kwargs["instructions"] = reply_instruction
            if controlled_turn:
                # Stable session schemas remain registered, but each settled
                # V5/V6 turn can invoke only its single authoritative pricing
                # path. This prevents native Gemini from inventing an amount
                # before the KB-backed tool has run.
                reply_kwargs["tools"] = _authorized_controlled_reply_tools(
                    conversation_state
                )
            reply = session.generate_reply(**reply_kwargs)
            if hasattr(reply, "wait_for_playout"):
                await reply.wait_for_playout()
            else:
                await reply

        task = asyncio.create_task(reply_once())
        task.add_done_callback(VoiceSessionRuntime._log_task_exception)

    @ctx.room.on("participant_connected")
    def _participant_connected(participant):
        nonlocal participant_seen, participant_audio_active
        participant_seen = True
        participant_audio_active = True
        runtime.participant_connected()
        logger.info("Customer joined room: %s", participant.identity)

    @ctx.room.on("participant_disconnected")
    def _participant_disconnected(participant):
        nonlocal participant_audio_active
        participant_audio_active = False
        logger.info("Customer left room: %s", participant.identity)
        if participant_seen and not ctx.room.remote_participants:
            async def end_disconnected_call() -> None:
                nonlocal close_reason
                try:
                    await session.interrupt()
                except RuntimeError:
                    pass
                close_reason = "participant_disconnected"
                call_done.set()

            task = asyncio.create_task(end_disconnected_call())
            task.add_done_callback(VoiceSessionRuntime._log_task_exception)

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
            turn_id = f"user:{getattr(event, 'created_at', time.time())}"
            transcript = getattr(event, "transcript", "")
            logger.info(
                "Final customer transcript pending_field=%s language=%s text=%s",
                conversation_state.pending_field(),
                getattr(event, "language", None),
                " ".join(str(transcript or "").split())[:300],
            )
            normalized_transcript = _normalized_text(transcript)
            now_monotonic = time.monotonic()
            nonlocal asr_noise_quarantine_until
            terminal_close_presented = bool(
                conversation_state.onboarding_link_presented
                or conversation_state.better_plan_close_presented
                or conversation_state.unsatisfied_resolution_presented
                or conversation_state.polite_close_presented
            )
            noise_reason = _asr_noise_reason(
                transcript,
                language=getattr(event, "language", None),
            )
            if not participant_audio_active:
                noise_reason = "participant_disconnected"
            elif terminal_close_presented:
                noise_reason = "terminal_close_presented"
            elif (
                now_monotonic < asr_noise_quarantine_until
                and len(normalized_transcript.split()) <= 10
            ):
                noise_reason = "noise_quarantine"
            if noise_reason:
                ignored_asr_noise[normalized_transcript] = now_monotonic
                if noise_reason not in {
                    "participant_disconnected",
                    "terminal_close_presented",
                    "noise_quarantine",
                }:
                    asr_noise_strikes[:] = [
                        seen_at
                        for seen_at in asr_noise_strikes
                        if now_monotonic - seen_at <= 12.0
                    ]
                    asr_noise_strikes.append(now_monotonic)
                    if len(asr_noise_strikes) >= 2:
                        asr_noise_quarantine_until = now_monotonic + 10.0

                async def suppress_asr_noise() -> None:
                    if noise_reason not in {
                        "participant_disconnected",
                        "terminal_close_presented",
                    }:
                        try:
                            await session.interrupt()
                        except RuntimeError:
                            pass
                    await emit_runtime_event(
                        "asr_noise_ignored",
                        status="ignored",
                        noise_reason=noise_reason,
                        customer_text=str(transcript or "")[:120],
                    )

                task = asyncio.create_task(suppress_asr_noise())
                task.add_done_callback(VoiceSessionRuntime._log_task_exception)
                return
            if (
                not conversation_state.model_led_flow
                and _is_opening_noise_turn(transcript, conversation_state)
            ):
                ignored_opening_noise[normalized_transcript] = now_monotonic
                async def suppress_opening_noise() -> None:
                    try:
                        await session.interrupt()
                    except RuntimeError:
                        pass
                    await emit_runtime_event(
                        "opening_noise_ignored",
                        status="ignored",
                        customer_text=str(transcript or "")[:120],
                    )

                task = asyncio.create_task(suppress_opening_noise())
                task.add_done_callback(VoiceSessionRuntime._log_task_exception)
                return
            if runtime.add_user_turn(transcript, turn_id=turn_id):
                assistant._response_language = _response_language_for_turn(
                    transcript,
                    assistant._response_language,
                )
                state_task = turn_processor.schedule(transcript, turn_id=turn_id)
                schedule_controlled_information_reply(
                    state_task,
                    runtime.user_turn_count,
                )

    @session.on("conversation_item_added")
    def _conversation_item_added(event):
        nonlocal high_volume_delivery_pending, user_turn_unsettled
        item = getattr(event, "item", None)
        if not isinstance(item, ChatMessage):
            return
        if str(item.role) == "user":
            # Gemini may emit a native response while the customer is still
            # speaking, before this final transcript exists. From this point
            # the final turn is settled and deterministic state preparation
            # below can safely choose the single response owner.
            user_turn_unsettled = False
            turn_id = getattr(item, "id", None)
            customer_text = item.text_content or ""
            noise_key = _normalized_text(customer_text)
            ignored_noise_at = ignored_asr_noise.get(noise_key)
            if ignored_noise_at is not None and time.monotonic() - ignored_noise_at <= 5.0:
                ignored_asr_noise.pop(noise_key, None)
                return
            if not participant_audio_active:
                return
            item_noise_reason = _asr_noise_reason(
                customer_text,
                confidence=getattr(item, "transcript_confidence", None),
            )
            if item_noise_reason:
                ignored_asr_noise[noise_key] = time.monotonic()
                return
            ignored_at = ignored_opening_noise.get(noise_key)
            recently_ignored = bool(
                ignored_at is not None and time.monotonic() - ignored_at <= 5.0
            )
            if recently_ignored:
                ignored_opening_noise.pop(noise_key, None)
                return
            if (
                not conversation_state.model_led_flow
                and _is_opening_noise_turn(customer_text, conversation_state)
            ):
                return
            is_new_turn = runtime.add_user_turn(customer_text, turn_id=turn_id)
            assistant._response_language = _response_language_for_turn(
                customer_text,
                assistant._response_language,
            )
            # Gemini native realtime skips Agent.on_user_turn_completed when
            # server-side turn detection is enabled. This event is the fallback
            # that guarantees the authoritative state still sees every turn.
            if is_new_turn:
                state_task = turn_processor.schedule(customer_text, turn_id=turn_id)
                schedule_controlled_information_reply(
                    state_task,
                    runtime.user_turn_count,
                )
        elif str(item.role) == "assistant":
            agent_text = item.text_content or ""
            logger.info(
                "Final assistant transcript pending_field=%s text=%s",
                conversation_state.pending_field(),
                " ".join(agent_text.split())[:500],
            )
            if _high_volume_manager_delivery_complete(agent_text):
                high_volume_delivery_pending = False
            previous_agent_text = next(
                (
                    str(turn.get("text") or "")
                    for turn in reversed(runtime.turns)
                    if turn.get("role") == "AGENT"
                ),
                "",
            )
            latest_customer_text = next(
                (
                    str(turn.get("text") or "")
                    for turn in reversed(runtime.turns)
                    if turn.get("role") == "CUSTOMER"
                ),
                "",
            )
            response_turn_epoch = runtime.user_turn_count
            observed_flow_violation = _shipkia_flow_response_violation(
                agent_text=agent_text,
                customer_text=latest_customer_text,
                previous_agent_text=previous_agent_text,
                conversation_state=conversation_state,
            )
            flow_violation = (
                observed_flow_violation
                if _flow_violation_requires_correction(
                    observed_flow_violation,
                    model_led_flow=conversation_state.model_led_flow,
                    preverified_rate_flow=bool(
                        conversation_state.value("assistance_intent") == "Rates"
                        and not conversation_state.verified_rate_presented()
                    ),
                )
                else ""
            )
            if observed_flow_violation and not flow_violation:
                logger.warning(
                    "Observed advisory ShipKia flow violation=%s; allowing the single V6 "
                    "response owner to continue text=%s",
                    observed_flow_violation,
                    agent_text[:300],
                )
            if (
                assistant._response_language == "Hinglish"
                and _DEVANAGARI_RE.search(agent_text)
            ):
                # Script is a transcript-format preference, not a spoken-call
                # safety violation. Interrupting native Hindi audio and asking
                # the model to restate it caused a regenerate/repeat loop when
                # the replacement transcript was also Devanagari.
                logger.warning(
                    "Observed Hinglish response in Devanagari; allowing playout to avoid "
                    "a correction loop text=%s",
                    agent_text[:300],
                )
            runtime.add_agent_turn(agent_text, turn_id=getattr(item, "id", None))
            normalized_agent_text = _normalized_text(agent_text)
            claimed_amounts = _shipkia_rate_claim_amounts(agent_text)
            unverified_amounts = bool(
                claimed_amounts
                and not conversation_state.rate_claim_amounts_authorized(claimed_amounts)
            )
            owed_rate_presented = bool(
                conversation_state.rate_answer_owed
                and conversation_state.primary_rate_amount is not None
                and any(
                    abs(amount - conversation_state.primary_rate_amount) < 0.011
                    for amount in claimed_amounts
                )
            )
            claimed_pincodes = _assistant_pincode_claims(agent_text)
            confirmed_pincodes = {
                str(conversation_state.value(field) or "")
                for field in PINCODE_FIELDS
                if conversation_state.is_confirmed(field)
            }
            customer_spoken_pincodes = {
                pincode
                for turn in runtime.turns
                if turn.get("role") == "CUSTOMER"
                for pincode in re.findall(r"\b\d{6}\b", str(turn.get("text") or ""))
            }
            authorized_pincodes = confirmed_pincodes | customer_spoken_pincodes
            unverified_pincodes = [
                value for value in claimed_pincodes if value not in authorized_pincodes
            ]
            claimed_zones = _assistant_single_zone_claims(agent_text)
            confirmed_zone = (
                str(conversation_state.value("zone") or "").upper()
                if conversation_state.is_confirmed("zone")
                else ""
            )
            unverified_zones = [
                value for value in claimed_zones if value != confirmed_zone
            ]
            if (
                owed_rate_presented
                and not flow_violation
                and not unverified_amounts
                and not unverified_pincodes
                and not unverified_zones
            ):
                conversation_state.mark_owed_rate_presented()
            if (
                conversation_state.provider_rates_answer_due
                and not flow_violation
                and not unverified_amounts
                and not unverified_pincodes
                and not unverified_zones
                and _provider_rate_response_complete(agent_text, conversation_state)
            ):
                conversation_state.mark_provider_rates_presented()
            if unverified_amounts or unverified_pincodes or unverified_zones:
                logger.error(
                    "Blocked unverified ShipKia output amounts=%s pincodes=%s zones=%s text=%s",
                    claimed_amounts,
                    unverified_pincodes,
                    unverified_zones,
                    agent_text[:300],
                )

                async def correct_unverified_output() -> None:
                    nonlocal correction_active
                    if correction_active:
                        return
                    correction_active = True
                    try:
                        if (
                            runtime.user_turn_count != response_turn_epoch
                            or not participant_audio_active
                            or call_done.is_set()
                        ):
                            return
                        await session.interrupt()
                        await emit_runtime_event(
                            "unverified_pricing_output_blocked",
                            status="blocked",
                            claimed_amounts=claimed_amounts,
                            claimed_pincodes=claimed_pincodes,
                            unverified_pincodes=unverified_pincodes,
                            claimed_zones=claimed_zones,
                            unverified_zones=unverified_zones,
                            agent_text=agent_text[:500],
                        )
                        if (
                            runtime.user_turn_count != response_turn_epoch
                            or not participant_audio_active
                            or call_done.is_set()
                        ):
                            return
                        pending = conversation_state.pending_field()
                        pending_label = _RATE_FIELD_LABELS.get(
                            pending, pending.replace("_", " ")
                        ) if pending else ""
                        provider_rate_recovery = bool(
                            conversation_state.provider_rates_answer_due
                            and conversation_state.verified_starting_options
                        )
                        if provider_rate_recovery:
                            next_step = (
                                "Now list every retained worker-verified provider option with its "
                                "exact courier/service and GST-inclusive 500 g Forward starting "
                                "amount, then ask exactly: 'Kya aap kuch aur jaanna chahenge?' "
                                + conversation_state.guidance()
                            )
                        elif pending_label:
                            next_step = (
                                f"Then ask only for the customer's {pending_label}."
                            )
                        else:
                            next_step = (
                                "Say the verified rate will be shared after the required details "
                                "are checked; do not add another amount."
                            )
                        if unverified_pincodes:
                            route_correction = (
                                "Immediately correct the previous route in one short sentence. "
                                f"The only customer-confirmed pincodes are {sorted(authorized_pincodes)}; "
                                "do not repeat any other pincode. "
                            )
                        else:
                            route_correction = ""
                        amount_correction = (
                            "Present only the exact worker-verified provider options supplied "
                            "in the next instruction. "
                            if unverified_amounts and provider_rate_recovery
                            else (
                                "Do not repeat or refer to the blocked amount and do not speak "
                                "another numeric ShipKia amount. "
                                if unverified_amounts
                                else ""
                            )
                        )
                        zone_correction = (
                            "Correct the previous zone and say only the worker-verified "
                            f"Zone {confirmed_zone}; do not repeat another zone. "
                            if unverified_zones and confirmed_zone
                            else (
                                "Say the previous zone was not verified and should be ignored. "
                                if unverified_zones
                                else ""
                            )
                        )
                        session.generate_reply(
                            instructions=(
                                route_correction
                                + zone_correction
                                + amount_correction
                                + next_step
                            )
                        )
                    finally:
                        correction_active = False

                if not correction_active:
                    task = asyncio.create_task(correct_unverified_output())
                    task.add_done_callback(VoiceSessionRuntime._log_task_exception)
            if (
                flow_violation
                and not correction_active
                and not (unverified_amounts or unverified_pincodes or unverified_zones)
                and (
                    response_turn_epoch not in controlled_information_reply_epochs
                    or flow_violation.startswith(
                        ("skipped_pending:", "reasked_handled:")
                    )
                )
                and response_turn_epoch not in flow_correction_epochs
            ):
                flow_correction_epochs.add(response_turn_epoch)
                logger.error(
                    "Blocked ShipKia flow violation=%s text=%s",
                    flow_violation,
                    agent_text[:300],
                )

                async def correct_flow_output() -> None:
                    nonlocal correction_active
                    if correction_active:
                        return
                    correction_active = True
                    try:
                        if (
                            runtime.user_turn_count != response_turn_epoch
                            or not participant_audio_active
                            or call_done.is_set()
                        ):
                            return
                        await session.interrupt()
                        await emit_runtime_event(
                            "shipkia_flow_output_blocked",
                            status="blocked",
                            violation=flow_violation,
                            agent_text=agent_text[:500],
                        )
                        if (
                            runtime.user_turn_count != response_turn_epoch
                            or not participant_audio_active
                            or call_done.is_set()
                        ):
                            return
                        if (
                            flow_violation.startswith("repeated_pending:")
                            and _NOISE_ONLY_GREETING_RE.fullmatch(
                                _normalized_text(latest_customer_text)
                            )
                        ):
                            # A one-word mic/echo greeting must not cause the
                            # same pending question to be spoken yet again.
                            return
                        if flow_violation == "repeated_anything_else_checkpoint":
                            # The customer's requested follow-up was already
                            # answered in the interrupted draft. Generating a
                            # second model turn here previously made the model
                            # repeat the prohibited checkpoint verbatim.
                            return
                        if flow_violation == "usp_ignored":
                            usp_scope = (
                                "Explain all four verified facilities"
                                if conversation_state.last_detailed_usp_query
                                else "Use two or three verified facilities relevant to their question"
                            )
                            correction_direction = (
                                "The call is already in progress and consent is accepted. Never "
                                "greet, introduce ShipKia again, mention that a query was received, "
                                "or ask for consent. Answer the customer's current ShipKia "
                                "procedure/services/benefits question "
                                f"now. {usp_scope}: multi-courier shipment management; dedicated "
                                "account-manager support for ticketing; WhatsApp order confirmation "
                                "with call fallback; and WhatsApp plus IVR follow-up for delivery "
                                "NDR. Explain naturally without inventing a feature, guarantee, "
                                "saving, discount, delivery promise, or numeric rate. Then resume "
                                "only the current authoritative pending question."
                            )
                        elif flow_violation == "unsupported_usp_claim":
                            correction_direction = (
                                "Answer using only the verified ShipKia capabilities in the current "
                                "instructions. Do not claim or imply a guaranteed delivery, saving, "
                                "discount, or rate. Use natural wording, then resume the current "
                                "authoritative action."
                            )
                        elif flow_violation == "spoken_onboarding_url":
                            correction_direction = (
                                "Never speak the raw signup URL. If onboarding-close authorization "
                                "is active, say exactly once: 'Theek hai, main aapko WhatsApp par "
                                "onboarding ka link bhej raha hoon. Aap us link se apni onboarding "
                                "complete kar lijiye.' Otherwise follow only the current "
                                "authoritative action."
                            )
                        elif flow_violation == "unauthorized_onboarding_link":
                            correction_direction = (
                                "Do not offer or promise a signup/onboarding link. The customer "
                                "has not completed consultative discovery and has not answered an "
                                "authorized move-forward question. Continue only with this current "
                                "action: " + conversation_state.guidance()
                            )
                        elif flow_violation == "polite_close_omitted":
                            correction_direction = conversation_state.guidance()
                        elif flow_violation == "unverified_flat_zonal_claim":
                            correction_direction = (
                                "Do not describe or price Flat-Zonal unless the authoritative state "
                                "shows its matching catalog was successfully presented. Do not reuse "
                                "Flat amounts as Flat-Zonal amounts. Follow only this current action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "flat_zonal_catalog_omitted":
                            correction_direction = conversation_state.guidance()
                        elif flow_violation == "flat_catalog_omitted":
                            correction_direction = conversation_state.guidance()
                        elif flow_violation == "unexpected_anything_else_checkpoint":
                            correction_direction = (
                                "The one information checkpoint is not authorized at this stage. "
                                "Do not ask whether the customer wants to know anything else. "
                                "The factual side-query answer in the interrupted draft was already "
                                "delivered. Never repeat, paraphrase, summarize, or acknowledge that "
                                "answer. Speak only the current authoritative action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "restarted_opening":
                            correction_direction = (
                                "The call is already in progress and consent was accepted. Never "
                                "greet, introduce ShipKia again, mention that a query was received, "
                                "or ask for consent. Continue only with this current action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "reopened_rate_intent":
                            correction_direction = (
                                "The customer's rate enquiry is already active. Do not ask whether "
                                "they want rates, pricing, or a rate check again. Continue directly "
                                "with the current useful discovery or pricing action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "unverified_route_rate_availability":
                            correction_direction = (
                                "Do not claim that a route rate is available or unavailable until "
                                "a pricing tool succeeds. Use the retained route and follow only "
                                "this authoritative action: " + conversation_state.guidance()
                            )
                        elif flow_violation == "owed_rate_omitted":
                            correction_direction = conversation_state.guidance()
                        elif flow_violation == "qualification_bridge_omitted":
                            correction_direction = (
                                "Say exactly: 'Rates batane se pehle main aapse kuch zaroori "
                                "details jaan lena chahunga. Aapke business ya brand ka naam kya "
                                "hai?' Ask no other question in this turn."
                            )
                        elif flow_violation.startswith("reasked_handled:"):
                            repeated_field = flow_violation.split(":", 1)[1]
                            repeated_label = _RATE_FIELD_LABELS.get(
                                repeated_field,
                                repeated_field.replace("_", " "),
                            )
                            correction_direction = (
                                f"The customer's {repeated_label} is already handled in the "
                                "authoritative state. Do not ask for it again, reconfirm it, or "
                                "restart discovery. Continue only with this current action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation.startswith("skipped_pending:"):
                            skipped_field = flow_violation.split(":", 1)[1]
                            skipped_label = _RATE_FIELD_LABELS.get(
                                skipped_field,
                                skipped_field.replace("_", " "),
                            )
                            correction_direction = (
                                f"The authoritative {skipped_label} answer is still unconfirmed. "
                                "Do not continue to any later question or infer the ambiguous "
                                "customer audio. Follow only this current action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "unauthorized_better_plan":
                            correction_direction = (
                                "The customer's latest audio did not clearly authorize a better-plan "
                                "close. Do not promise one and do not infer yes, no, satisfaction, or "
                                "dissatisfaction. Follow only the current authoritative action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "advanced_after_optional_refusal":
                            correction_direction = (
                                "The customer explicitly refused the optional question. End the "
                                "remaining optional qualification immediately; do not ask the "
                                "refused field or any later optional discovery question. Continue "
                                "only with this current authoritative action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "repeated_move_forward":
                            correction_direction = (
                                "Do not repeat the same move-forward sentence. Briefly acknowledge "
                                "the customer's latest words and ask one short yes-or-no clarification "
                                "without restarting discovery or repeating any rate."
                            )
                        elif flow_violation == "repeated_resolution_close":
                            correction_direction = (
                                "The approved resolution close was already spoken. Do not repeat "
                                "the team, solution, or better-plan promise. Give only one brief "
                                "polite farewell and end."
                            )
                        elif flow_violation == "contradicted_verified_route":
                            correction_direction = (
                                "The route and zone are already worker-verified. Do not say they "
                                "are unverified, call a pricing tool, or ask for a city or pincode "
                                "again. Follow only this current authoritative action: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "verified_rate_omitted":
                            correction_direction = (
                                f"State the already worker-verified Zone "
                                f"{conversation_state.value('zone')} starting rate Rs "
                                f"{conversation_state.primary_rate_amount:.2f}, GST included, "
                                "then ask only for the customer's monthly shipment quantity. "
                                "Do not ask anything else yet and do not call another pricing tool."
                            )
                        elif flow_violation == "provider_rates_incomplete":
                            correction_direction = (
                                "The customer asked for all individual provider rates. List every "
                                "worker-verified option with its courier/service and exact "
                                "GST-inclusive 500 g Forward starting amount. Do not omit an "
                                "option, invent a seventh rate, or call another pricing tool. "
                                "Follow this authoritative data and close question: "
                                + conversation_state.guidance()
                            )
                        elif flow_violation == "anything_else_detail_not_requested":
                            correction_direction = (
                                "The customer already said yes to knowing more. Do not repeat the "
                                "anything-else question. Ask only: 'Ji, aap kya jaanna chahenge?'"
                            )
                        elif flow_violation.startswith("repeated_pending:"):
                            correction_direction = (
                                "The pending question was already asked in the immediately previous "
                                "assistant response. Do not repeat or rephrase it; wait for the "
                                "customer's answer."
                            )
                        else:
                            correction_direction = conversation_state.guidance()
                        reply = session.generate_reply(
                            instructions=(
                                "The previous draft violated the ShipKia sales flow and was interrupted. "
                                "Do not repeat it. The call is already in progress; never greet, "
                                "introduce ShipKia, mention that a query was received, or ask for "
                                "conversation consent again. " + correction_direction
                            )
                        )
                        if hasattr(reply, "wait_for_playout"):
                            await reply.wait_for_playout()
                        else:
                            await reply
                    finally:
                        correction_active = False

                task = asyncio.create_task(correct_flow_output())
                task.add_done_callback(VoiceSessionRuntime._log_task_exception)
            if (
                conversation_state.qualification_bridge_due()
                and _QUALIFICATION_BRIDGE_RE.search(normalized_agent_text)
            ):
                conversation_state.mark_qualification_bridge_presented()
            if (
                not flow_violation
                and not (unverified_amounts or unverified_pincodes or unverified_zones)
                and _AGENT_ANYTHING_ELSE_RE.search(normalized_agent_text)
            ):
                conversation_state.mark_anything_else_question_presented()
            if (
                not flow_violation
                and not (unverified_amounts or unverified_pincodes or unverified_zones)
                and (
                    "auth dot shipkia dot com slash signup" in normalized_agent_text
                    or "auth.shipkia.com/signup" in normalized_agent_text
                    or (
                        "whatsapp" in normalized_agent_text
                        and "onboarding" in normalized_agent_text
                        and "link" in normalized_agent_text
                    )
                )
            ):
                conversation_state.mark_onboarding_link_presented()
            if (
                not flow_violation
                and conversation_state.polite_close_due
                and _AGENT_POLITE_FAREWELL_RE.search(normalized_agent_text)
            ):
                conversation_state.mark_polite_close_presented()
            if (
                not flow_violation
                and not (unverified_amounts or unverified_pincodes or unverified_zones)
                and "better plan" in normalized_agent_text
                and "team" in normalized_agent_text
                and "discuss" in normalized_agent_text
                and "thank you for calling shipkia" in normalized_agent_text
            ):
                conversation_state.mark_better_plan_close_presented()
            if (
                not flow_violation
                and not (unverified_amounts or unverified_pincodes or unverified_zones)
                and conversation_state.unsatisfied_resolution_due
                and "team" in normalized_agent_text
                and "discuss" in normalized_agent_text
                and ("solution" in normalized_agent_text or "better plan" in normalized_agent_text)
            ):
                conversation_state.mark_unsatisfied_resolution_presented()

    @session.on("speech_created")
    def _speech_created(event):
        handle = getattr(event, "speech_handle", None)
        if handle is None:
            return
        speech_id = str(getattr(handle, "id", ""))
        source = str(getattr(event, "source", "generate_reply"))
        user_initiated = bool(getattr(event, "user_initiated", False))
        expected_tool_reply = bool(
            not user_initiated
            and runtime.consume_expected_realtime_tool_reply()
        )
        if _suppress_unsolicited_realtime_speech(
            controlled_flow=_worker_owns_realtime_turn(conversation_state),
            user_turn_unsettled=user_turn_unsettled,
            user_initiated=user_initiated,
            expected_tool_reply=expected_tool_reply,
        ):
            # This callback is emitted synchronously before LiveKit schedules
            # the realtime generation, so cancellation prevents both the
            # speculative audio fragment and its eager function calls.
            handle.interrupt(force=True)
            logger.info(
                "Suppressed unsolicited realtime speech id=%s source=%s "
                "pending_field=%s user_turn_unsettled=%s",
                speech_id,
                source,
                conversation_state.pending_field(),
                user_turn_unsettled,
            )
        runtime.track_agent_speech(
            speech_id,
            source=source,
            # LiveKit/Gemini can resume its own server-started native draft.
            # Explicit worker-owned replies cannot be resumed by that path and
            # need the runtime's false-interruption fallback instead.
            native_resume_eligible=not user_initiated,
        )
        logger.info(
            "Agent speech created id=%s source=%s user_initiated=%s "
            "expected_tool_reply=%s",
            speech_id,
            source,
            user_initiated,
            expected_tool_reply,
        )

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
            if not interrupted and conversation_state.flat_zonal_catalog_delivery_due:
                latest_agent_text = next(
                    (
                        str(turn.get("text") or "")
                        for turn in reversed(runtime.turns)
                        if turn.get("role") == "AGENT"
                    ),
                    "",
                )
                if _flat_zonal_catalog_response_complete(
                    latest_agent_text,
                    conversation_state,
                ):
                    conversation_state.mark_flat_zonal_catalog_delivered()
            if not interrupted and conversation_state.flat_catalog_delivery_due:
                latest_agent_text = next(
                    (
                        str(turn.get("text") or "")
                        for turn in reversed(runtime.turns)
                        if turn.get("role") == "AGENT"
                    ),
                    "",
                )
                if _flat_catalog_response_complete(latest_agent_text, conversation_state):
                    conversation_state.mark_flat_catalog_delivered()
            if (
                conversation_state.onboarding_link_presented
                or conversation_state.better_plan_close_presented
                or conversation_state.unsatisfied_resolution_presented
                or conversation_state.polite_close_presented
            ):
                # The approved terminal response is the last turn. End even if
                # late microphone noise interrupted its tail; otherwise native
                # realtime can start a new hallucinated turn during disconnect.
                call_done.set()

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

    @session.on("user_state_changed")
    def _user_state_changed(event):
        nonlocal user_turn_unsettled
        new_state = str(getattr(event, "new_state", ""))
        if new_state == "speaking":
            user_turn_unsettled = True
            runtime.clear_expected_realtime_tool_replies()
        logger.info(
            "Customer audio state changed old=%s new=%s user_turn_unsettled=%s",
            str(getattr(event, "old_state", "")),
            new_state,
            user_turn_unsettled,
        )

    @session.on("session_usage_updated")
    def _session_usage_updated(event):
        runtime.record_session_usage(getattr(event, "usage", None))

    @session.on("error")
    def _session_error(event):
        runtime.record_error(getattr(event, "error", event))

    async def recover_from_silence(customer_text: str, reason: str) -> None:
        recovery_turn_epoch = runtime.user_turn_count
        if conversation_state.model_led_flow:
            # Re-render the same central V6 prompt with current state. No
            # apology/resume micro-prompt is added to the conversation.
            await assistant.sync_pricing_tools()
            if runtime.user_turn_count != recovery_turn_epoch:
                return
            reply = session.generate_reply(
                tools=_authorized_controlled_reply_tools(conversation_state)
            )
            if hasattr(reply, "wait_for_playout"):
                await reply.wait_for_playout()
            else:
                await reply
            return
        memory = runtime.same_call_context(
            current_user_text=customer_text,
            max_turns=_SAME_CALL_MEMORY_MAX_TURNS,
            max_chars=_SAME_CALL_MEMORY_MAX_CHARS,
        )
        if reason == "false_interruption":
            if _worker_owns_realtime_turn(conversation_state):
                recovery_direction = (
                    "The worker-owned response was cut off without a real customer turn. Do not "
                    "repeat a handled question, greet, or apologize. Continue only with the current "
                    "authoritative action: " + conversation_state.guidance()
                )
            else:
                recovery_direction = (
                    "The previous speech was cut off by brief microphone activity, but the customer "
                    "did not begin a real new turn. Continue and finish the interrupted thought "
                    "naturally. Do not greet again, restart the whole answer, or apologize."
                )
        else:
            recovery_direction = (
                "The customer is still connected and the previous response stalled. Apologize once "
                "for the short delay, answer their latest request in one short natural turn, and "
                "continue the ShipKia conversation."
            )
        if runtime.user_turn_count != recovery_turn_epoch:
            return
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

    async def guarded_text_input(sess: AgentSession, event) -> None:
        """Apply the same state gate to Console/chat turns before generation."""
        async with sess._claim_user_turn():
            await sess.interrupt()
            customer_text = str(getattr(event, "text", "") or "")
            new_message = ChatMessage(role="user", content=[customer_text])
            turn_id = str(getattr(new_message, "id", "") or f"text:{time.time()}")
            runtime.add_user_turn(customer_text, turn_id=turn_id)
            turn_context = assistant.chat_ctx.copy()
            try:
                await assistant.on_user_turn_completed(turn_context, new_message)
            except StopResponse:
                # Text input has no competing automatic native response; its
                # single explicit generate_reply below remains the owner.
                pass
            if conversation_state.model_led_flow:
                await assistant.sync_pricing_tools()
                sess.generate_reply(
                    user_input=new_message,
                    chat_ctx=turn_context,
                    tools=_authorized_controlled_reply_tools(conversation_state),
                    input_modality="text",
                )
                return
            pending = conversation_state.pending_field()
            if conversation_state.starting_rate_due():
                response_directive = (
                    "Response-specific worker directive: call get_shipkia_starting_rate exactly "
                    "once and follow only its returned spoken instructions. Do not call another "
                    "pricing tool."
                )
            elif pending:
                pending_label = _RATE_FIELD_LABELS.get(pending, pending.replace("_", " "))
                response_directive = (
                    "Response-specific worker directive: after one brief natural acknowledgement, "
                    f"ask only for the customer's {pending_label}. This is the authoritative next "
                    "question and overrides the generic qualification sequence. Do not ask about "
                    "any business, provider, current rate, challenge, pincode, weight, payment, "
                    "order value, or service field other than that single pending field."
                )
            elif (
                conversation_state.v4_strict_flow
                and conversation_state.requested_rate_type == "Flat"
            ):
                response_directive = (
                    "Response-specific worker directive: all applicable Flat fields are handled. "
                    "Do not ask a qualification question or request a pincode. Call "
                    "get_shipkia_flat_rates once and answer only from its verified result."
                )
            elif (
                conversation_state.v5_company_pair_flow
                and conversation_state.requested_rate_type == "Flat Zonal"
                and conversation_state.flat_zonal_catalog_due()
            ):
                response_directive = (
                    "Response-specific worker directive: this is an explicit Flat-Zonal request. "
                    "Call get_shipkia_flat_zonal_rates once and answer only from its verified "
                    "zone-group result. Do not call the Flat or Zonal pricing tools."
                )
            elif conversation_state.pricing_ready():
                response_directive = (
                    "Response-specific worker directive: all applicable gated fields are handled. "
                    "Do not ask any qualification question again. Call calculate_shipkia_rate once "
                    "and answer only from its validated result."
                )
            else:
                response_directive = (
                    "Response-specific worker directive: no pricing path is ready. Do not call a "
                    "pricing tool or mention an amount; follow the authoritative turn instruction."
                )
            sess.generate_reply(
                user_input=new_message,
                chat_ctx=turn_context,
                instructions=response_directive,
                input_modality="text",
            )

    async def shutdown_callback() -> None:
        guard_timeout = min(
            6.0,
            float(os.getenv("SHIPKIA_ANSWER_GUARD_TIMEOUT_SECONDS", "5")) + 0.5,
        )
        try:
            await asyncio.wait_for(
                turn_processor.wait_latest(),
                timeout=guard_timeout,
            )
        except asyncio.TimeoutError:
            conversation_state.record_guard_error(
                "final_turn_guard_timeout",
                turn_id=f"shutdown:{time.time()}",
            )
            await emit_runtime_event(
                "gated_state_checkpoint",
                classifier_error="final_turn_guard_timeout",
                state_snapshot=conversation_state.snapshot(),
                state_transitions=list(conversation_state.transitions),
                transcript=runtime.transcript(),
                metrics=runtime.metrics(),
            )
        await runtime.finish(close_reason)

    ctx.add_shutdown_callback(shutdown_callback)
    await session.start(
        agent=assistant,
        room=ctx.room,
        room_options=RoomOptions(
            text_input=TextInputOptions(text_input_cb=guarded_text_input),
            audio_input=True,
            video_input=False,
            audio_output=True,
            text_output=True,
            close_on_disconnect=False,
        ),
        record=False,
    )
    await emit_runtime_event("agent_started", status="running")

    if not call_done.is_set():
        try:
            if prompt_version == "shipkia-voice-v6":
                # Initial V6 state already says consent is pending and its
                # central prompt owns the exact opening behavior.
                reply = session.generate_reply()
            elif prompt_version in EXACT_OPENING_PROMPT_VERSIONS:
                opening_instruction = (
                    "Begin the call now in natural conversational Hinglish using only step 1 of "
                    "the central sales prompt. Do not open only in English."
                )
                reply = session.generate_reply(instructions=opening_instruction)
            else:
                opening_instruction = (
                    "The ShipKia call has connected. Say exactly once: \"Namaste! Main ShipKia ka "
                    "assistant hoon. Humein aapki shipping query mili thi. Batayein, aap rates check "
                    "karna chahenge ya onboarding mein help chahiye?\" Do not add another greeting, "
                    "introduction, question, or translation. Then wait for the customer's answer. "
                    "Use known customer context and do not mention tools or internal systems."
                )
                reply = session.generate_reply(instructions=opening_instruction)
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
            # Dev mode otherwise keeps no warm job process. On WSL, loading
            # Silero VAD from a Windows-mounted checkout can occasionally take
            # longer than the SDK's 10-second default, causing the first test
            # dispatch to be killed before the agent joins the room.
            num_idle_processes=int(os.getenv("LIVEKIT_NUM_IDLE_PROCESSES", "1")),
            initialize_process_timeout=float(
                os.getenv("LIVEKIT_INITIALIZE_PROCESS_TIMEOUT", "60")
            ),
            # Python's forkserver cannot reliably inherit its Unix socket
            # descriptors when the worker is launched through wsl.exe.
            multiprocessing_context="spawn",
        )
    )
