import datetime
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from livekit.agents import (
    APIConnectOptions,
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
    llm,
    metrics,
)
from livekit.plugins import silero
from livekit.plugins.google.beta import realtime

load_dotenv(".env.local", override=False)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("confluence_good_agent")

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "universal_agent")
BACKGROUND_MCP_TOOLS = {
    " ".join(name.strip().lower().split())
    for name in os.getenv(
        "BACKGROUND_MCP_TOOLS",
        "send_mapped_whatsapp_template",
    ).split(",")
    if name.strip()
}
_BACKGROUND_MCP_DEDUPE: dict[tuple[str, str, str], float] = {}
_MCP_RESULT_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_MCP_CALLED_BY_TASK: dict[tuple[str, str], float] = {}
STATEFUL_MCP_TOOLS = {
    "get_repeat_workflow_state",
    "get_current_required_step",
    "get_current_speech_unit",
    "mark_repeat_step_complete",
    "mark_repeat_step_interrupted",
    "resume_repeat_pending_step",
    "log_repeat_followup_outcome",
}
ORDER_SAFETY_MCP_TOOL = os.getenv("ORDER_SAFETY_MCP_TOOL", "").strip()
ADDRESS_VERIFICATION_MCP_TOOL = os.getenv("ADDRESS_VERIFICATION_MCP_TOOL", "send_mapped_whatsapp_template").strip()
WHATSAPP_SEND_TOOLS = {
    "send_mapped_whatsapp_template",
    "send_whatsapp_message",
}
GENERIC_WHATSAPP_MESSAGE_PATTERNS = (
    r"^\s*(information|details|link|video|testimonial|token details)\s*\.?\s*$",
    r"treatment details and information",
    r"details and follow-up",
    r"details and information",
    r"regarding the enquiry",
    r"requested details",
    r"^\s*(जानकारी|टोकन डिटेल्स)\s*$",
)


def normalize_gemini_audio_name(audio_name: object) -> str:
    value = str(audio_name or "").strip()
    if not value:
        return "Kore"
    if not re.match(r"^[A-Za-z][A-Za-z0-9_-]{1,40}$", value):
        logger.warning("Invalid Gemini audio_name %r; falling back to Kore", value)
        return "Kore"
    return value


def normalize_phone_for_context(value: object, *, prefer_ten_digit: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        ten_digit = digits[-10:]
        if prefer_ten_digit:
            return ten_digit
        return f"+91{ten_digit}" if len(ten_digit) == 10 else f"+{digits}"
    return text


def normalize_context_phone_fields(context: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(context or {})
    raw_customer = (
        normalized.get("customer_phone")
        or normalized.get("phone")
        or normalized.get("phone_number")
        or normalized.get("mobile")
    )
    clean_customer = normalize_phone_for_context(raw_customer, prefer_ten_digit=True)
    if clean_customer:
        normalized.setdefault("raw_customer_phone", raw_customer)
        normalized["customer_phone"] = clean_customer
        normalized["phone"] = clean_customer
        normalized["phone_number"] = clean_customer
        if raw_customer and str(raw_customer).strip() != clean_customer:
            stale_summary = str(normalized.get("patient_summary") or "")
            if "No existing patient/customer record" in stale_summary:
                normalized["customer_type"] = "unknown_until_mcp"
                normalized["patient_summary"] = (
                    "Pre-call lookup used the raw provider phone number. "
                    "Use the normalized 10 digit phone with the configured customer-check MCP before deciding whether this is new or repeat."
                )
                normalized["repeat_customer_details"] = ""
            sales_brief = str(normalized.get("sales_brief") or "")
            if "No existing patient/customer record" in sales_brief or "Customer Type: new" in sales_brief:
                normalized["sales_brief"] = (
                    "## Customer Brief\n"
                    f"- Phone: {clean_customer}\n"
                    "- Customer Type: unknown until MCP check\n\n"
                    "Call the configured customer-check MCP with this normalized phone first. "
                    "Use the MCP result as the source of truth for existing patient/customer context."
                )

    raw_called = normalized.get("called_number") or normalized.get("inbound_phone_number")
    clean_called = normalize_phone_for_context(raw_called)
    if clean_called:
        normalized.setdefault("raw_called_number", raw_called)
        normalized["called_number"] = clean_called
        normalized["inbound_phone_number"] = clean_called
    return normalized


def truthy_argument(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "haan", "ha", "confirmed"}


def normalize_hinglish_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    replacements = {
        "हाँ": " haan ",
        "हां": " haan ",
        "जी": " ji ",
        "ठीक": " theek ",
        "कर दो": " kar do ",
        "करना": " karna ",
        "लेना": " lena ",
        "लेनी": " leni ",
        "लेने": " lene ",
        "करवाना": " karwana ",
        "करवानी": " karwani ",
        "करवाने": " karwane ",
        "चाह": " chah ",
        "चाहूँ": " chahun ",
        "चाहूं": " chahun ",
        "चाहेंगे": " chahenge ",
        "चाहता": " chahta ",
        "चाहती": " chahti ",
        "चाहते": " chahte ",
        "चाहूँगा": " chahunga ",
        "चाहूंगा": " chahunga ",
        "चाहूंगी": " chahungi ",
        "चाहूँगी": " chahungi ",
        "कन्फर्म": " confirm ",
        "ट्रीटमेंट": " treatment ",
        "इलाज": " treatment ",
        "ऑर्डर": " order ",
        "आर्डर": " order ",
        "प्रोसीड": " proceed ",
        "पेमेंट": " payment ",
        "कैश": " cash ",
        "डिलीवरी": " delivery ",
        "महीना": " mahina ",
        "महीने": " mahine ",
        "कोर्स": " course ",
        "पिन": " pin ",
        "कोड": " code ",
        "लैंडमार्क": " landmark ",
        "एड्रेस": " address ",
        "पता": " address ",
        "घर": " ghar ",
        "मकान": " house ",
        "सेक्टर": " sector ",
        "फेज": " phase ",
        "सुशांत": " sushant ",
        "लोक": " lok ",
        "दिल्ली": " delhi ",
        "गुड़गांव": " gurgaon ",
        "गुरुग्राम": " gurugram ",
        "ऑनलाइन": " online ",
        "पेट": " pet ",
        "दर्द": " dard ",
        "दिन": " din ",
        "हफ्ते": " hafte ",
        "महीनों": " mahino ",
        "साल": " saal ",
        "बी": " b ",
        "नाइनटी": " ninety ",
        "नाइंटी": " ninety ",
        "डबल": " double ",
        "वन": " one ",
        "टू": " two ",
        "थ्री": " three ",
        "फोर": " four ",
        "फाइव": " five ",
        "सिक्स": " six ",
        "सेवन": " seven ",
        "एट": " eight ",
        "नाइन": " nine ",
        "जीरो": " zero ",
        "ज़ीरो": " zero ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.replace("ji रो", " zero ")
    text = text.replace("chah ेंगे", " chahenge ")
    return re.sub(r"\s+", " ", text).strip()


def order_consent_detected(user_turns: list[str]) -> bool:
    text = normalize_hinglish_text("\n".join(user_turns[-12:]))
    latest = normalize_hinglish_text(user_turns[-1] if user_turns else "")
    if not text or not latest:
        return False

    confusion_or_objection = any(
        phrase in latest
        for phrase in (
            "kaunsa order",
            "konsa order",
            "kaun sa order",
            "kya order",
            "kya confirm",
            "confirm kya",
            "kya kar do",
            "pehle batao",
            "pahle batao",
            "aap kaun",
            "aap ho kaun",
            "dawai kya",
            "dawaiya kya",
            "medicine kya",
            "dawa kya",
            "samjha nahi",
            "samajh nahi",
            "pata nahi",
            "pagal",
        )
    )
    if confusion_or_objection:
        return False

    consent_signal = any(
        phrase in latest
        for phrase in (
            "order confirm kar do",
            "order confirm karo",
            "order kar do",
            "haan confirm kar do",
            "ha confirm kar do",
            "haan ji confirm kar do",
            "ji confirm kar do",
            "haan order kar do",
            "haan ji order kar do",
            "haan ji kar do",
            "ha ji kar do",
            "start kar do",
            "shuru kar do",
            "haan kar do",
            "haan karwana",
            "karwana chahta",
            "karwana chahti",
            "karwana chahunga",
            "karwana chahungi",
            "karwana chahte",
            "karvana chahta",
            "karvana chahti",
            "karna chahta",
            "karna chahti",
            "karna chahunga",
            "karna chahungi",
            "lena chahta",
            "lena chahti",
            "lena chahunga",
            "lena chahungi",
            "lena chah",
            "treatment lena",
            "proceed kar",
            "cash on delivery kar do",
            "cod kar do",
            "confirm karta",
            "confirm karti",
        )
    )
    order_context = any(
        word in text
        for word in (
            "order",
            "treatment start",
            "start treatment",
            "treatment",
            "proceed",
            "confirm",
            "course",
            "mahina",
            "mahine",
            "delivery",
            "cod",
            "cash on delivery",
            "address",
            "pin code",
            "landmark",
        )
    )
    action_context = any(
        word in text
        for word in (
            "order",
            "treatment start",
            "start treatment",
            "proceed",
            "confirm",
            "cod",
            "cash on delivery",
            "start kar",
            "shuru kar",
            "karwana",
            "karna chahta",
            "karna chahti",
            "lena chahta",
            "lena chahti",
            "treatment lena",
        )
    )
    fulfillment_context = any(
        word in text
        for word in (
            "delivery",
            "cod",
            "cash on delivery",
            "address",
            "pin code",
            "landmark",
        )
    )
    short_yes_confirmation = bool(
        order_context
        and action_context
        and fulfillment_context
        and any(
            phrase in latest
            for phrase in (
                "ji haan",
                "haan ji",
                "haan",
                "ha ji",
                "yes",
                "theek hai",
            )
        )
    )
    if consent_signal and not order_context:
        return False

    return (consent_signal and order_context) or short_yes_confirmation


def address_details_detected(user_turns: list[str]) -> bool:
    text = normalize_hinglish_text("\n".join(user_turns[-30:]))
    has_address_context = any(
        word in text
        for word in (
            "address",
            "delivery",
            "ghar",
            "house",
            "sector",
            "phase",
            "sushant",
            " lok ",
            "flat",
            "plot",
            "colony",
            "road",
        )
    )
    has_pin_or_landmark = (
        "pin code" in text
        or "landmark" in text
        or re.search(r"\b\d{6}\b", text)
        or bool(extract_spoken_pin_code(user_turns))
    )
    return bool(text and has_address_context and has_pin_or_landmark)


def extract_probable_customer_name(user_turns: list[str]) -> str:
    noise = {
        "haan",
        "ha",
        "ji",
        "theek",
        "ok",
        "hmm",
        "yes",
        "no",
        "nahi",
        "nahin",
        "course",
        "order",
        "confirm",
        "kar",
        "karo",
        "do",
        "start",
        "shuru",
        "proceed",
        "कन्फर्म",
        "कंफर्म",
        "कर",
        "दो",
        "करो",
        "आर्डर",
        "ऑर्डर",
        "ट्रीटमेंट",
        "address",
        "pin",
        "code",
        "landmark",
        "delivery",
        "cash",
        "payment",
        "online",
        "address",
        "pincode",
        "symptom",
        "dard",
        "pet",
        "delhi",
        "gurgaon",
        "gurugram",
        "main",
        "mai",
        "mere",
        "liye",
        "kya",
        "mam",
        "maam",
        "मैम",
        "क्या",
        "है",
        "मेरे",
        "लिए",
        "से",
        "मैं",
    }
    for turn in reversed(user_turns[-30:]):
        raw = str(turn or "").strip()
        explicit_name_match = re.search(
            r"(?:mera naam|my name is|name is|naam hai|naam)\s+([A-Za-z\u0900-\u097F ]{2,40})",
            raw,
            flags=re.IGNORECASE,
        )
        if explicit_name_match:
            candidate = re.sub(r"\s+", " ", explicit_name_match.group(1)).strip(" .।,")
            candidate = re.sub(r"\s+(hai|hein|he|है|हैं)$", "", candidate, flags=re.IGNORECASE).strip(" .।,")
            candidate_words = [word for word in candidate.split() if word.strip()]
            normalized_candidate = normalize_hinglish_text(candidate)
            if (
                1 <= len(candidate_words) <= 4
                and not any(token in normalized_candidate for token in ("confirm", "order", "address", "treatment", "dawai", "medicine"))
            ):
                return candidate
        cleaned = re.sub(r"[^A-Za-z\u0900-\u097F ]+", " ", raw)
        words = [word for word in cleaned.split() if word.strip()]
        if not (1 <= len(words) <= 4):
            continue
        normalized_turn = normalize_hinglish_text(" ".join(words))
        normalized_words = {normalize_hinglish_text(word) for word in words}
        if normalized_words.intersection(noise):
            continue
        if any(
            token in normalized_turn
            for token in (
                "address",
                "pin",
                "code",
                "delivery",
                "cash",
                "online",
                "order",
                "treatment",
                "course",
                "confirm",
                "kar do",
                "karo",
                "start",
                "shuru",
                "proceed",
                "कन्फर्म",
                "कंफर्म",
                "कर दो",
                "आर्डर",
                "ऑर्डर",
                "ट्रीटमेंट",
                "pet",
                "dard",
                "delhi",
                "gurgaon",
                "gurugram",
                "phase",
                "sector",
                "sushant",
                " lok ",
                "din",
                "mahine",
            )
        ):
            continue
        if re.search(r"\d", normalized_turn):
            continue
        return " ".join(words)
    return "Unknown"


def compact_customer_turn_summary(user_turns: list[str], max_chars: int = 2200) -> str:
    lines = [f"- {str(turn).strip()}" for turn in user_turns[-30:] if str(turn).strip()]
    summary = "\n".join(lines)
    return summary[-max_chars:]


def extract_spoken_pin_code(user_turns: list[str]) -> str:
    digit_words = {
        "zero": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    for turn in reversed(user_turns[-20:]):
        normalized = normalize_hinglish_text(turn)
        direct = re.search(r"\b\d{6}\b", normalized)
        if direct:
            return direct.group(0)
        tokens = re.findall(r"[a-z]+|\d", normalized)
        digits: list[str] = []
        repeat_next = False
        for token in tokens:
            if token == "double":
                repeat_next = True
                continue
            digit = digit_words.get(token) or (token if token.isdigit() else "")
            if not digit:
                if len(digits) >= 6:
                    break
                if digits:
                    digits = []
                repeat_next = False
                continue
            digits.append(digit)
            if repeat_next:
                digits.append(digit)
                repeat_next = False
            if len(digits) == 6:
                return "".join(digits)
            if len(digits) > 6:
                digits = digits[-6:]
                return "".join(digits)
    return ""


def extract_live_sales_facts(user_turns: list[str], dynamic_context: dict[str, Any]) -> dict[str, str]:
    summary = compact_customer_turn_summary(user_turns)
    normalized_summary = normalize_hinglish_text(summary)
    address = compact_address_turn_summary(user_turns) if address_details_detected(user_turns) else ""
    pin_code = extract_spoken_pin_code(user_turns)
    if pin_code and pin_code not in address:
        address = f"{address} | PIN: {pin_code}".strip(" |")

    payment_mode = ""
    if "cash on delivery" in normalized_summary or "cod" in normalized_summary:
        payment_mode = "Cash on Delivery"
    elif "cash" in normalized_summary and "delivery" in normalized_summary:
        payment_mode = "Cash on Delivery"
    elif "online payment" in normalized_summary or "pay online" in normalized_summary:
        payment_mode = "Online"

    location = ""
    for city in ("delhi", "gurgaon", "gurugram"):
        if city in normalized_summary:
            location = city.title()
            break

    duration = ""
    for turn in reversed(user_turns[-20:]):
        normalized = normalize_hinglish_text(turn)
        if any(token in normalized for token in ("din", "day", "hafte", "week", "mahina", "mahine", "month", "saal", "year")):
            duration = str(turn).strip()
            break

    symptoms = ""
    symptom_turns = [
        str(turn).strip()
        for turn in user_turns[-20:]
        if any(token in normalize_hinglish_text(turn) for token in ("pet", "dard", "pain", "symptom", "takleef", "problem"))
    ]
    if symptom_turns:
        symptoms = " | ".join(symptom_turns[-3:])

    return {
        "customer_name": "" if extract_probable_customer_name(user_turns) == "Unknown" else extract_probable_customer_name(user_turns),
        "phone": normalize_phone_for_context(
            dynamic_context.get("customer_phone")
            or dynamic_context.get("phone")
            or dynamic_context.get("phone_number")
            or dynamic_context.get("raw_customer_phone"),
            prefer_ten_digit=True,
        ),
        "disease_or_concern": str(
            dynamic_context.get("disease_or_concern")
            or dynamic_context.get("concern")
            or dynamic_context.get("department")
            or "customer concern"
        ).strip() or "customer concern",
        "summary": summary,
        "address": address,
        "payment_mode": payment_mode,
        "location": location,
        "duration": duration,
        "symptoms": symptoms,
    }


def compact_address_turn_summary(user_turns: list[str], max_chars: int = 900) -> str:
    selected: list[str] = []
    for turn in user_turns[-20:]:
        normalized = normalize_hinglish_text(turn)
        if any(
            token in normalized
            for token in (
                "address",
                "pin",
                "code",
                "landmark",
                "sector",
                "phase",
                "market",
                "delivery",
                "ghar",
                "house",
                "sushant",
                " lok ",
                "flat",
                "plot",
                "colony",
                "road",
            )
        ) or extract_spoken_pin_code([turn]):
            selected.append(str(turn).strip())
    if not selected:
        selected = [str(turn).strip() for turn in user_turns[-8:] if str(turn).strip()]
    return " | ".join(selected)[-max_chars:]


def record_mcp_tool_called(task_id: Optional[str], tool_name: object) -> None:
    name = str(tool_name or "").strip()
    if not task_id or not name:
        return
    _MCP_CALLED_BY_TASK[(str(task_id), name)] = time.monotonic()


def mcp_tool_was_called(task_id: Optional[str], tool_name: object) -> bool:
    name = str(tool_name or "").strip()
    if not task_id or not name:
        return False
    called_at = _MCP_CALLED_BY_TASK.get((str(task_id), name))
    if not called_at:
        return False
    ttl = float(os.getenv("MCP_CALLED_REGISTRY_TTL_SECONDS", "7200") or "7200")
    if time.monotonic() - called_at > ttl:
        _MCP_CALLED_BY_TASK.pop((str(task_id), name), None)
        return False
    return True


def mcp_schema_required_fields(tool_spec: dict[str, Any]) -> list[str]:
    schema = tool_spec.get("inputSchema") or tool_spec.get("parameters") or {}
    required = schema.get("required") if isinstance(schema, dict) else []
    return [str(item) for item in required or [] if str(item or "").strip()]


def mcp_schema_properties(tool_spec: dict[str, Any]) -> dict[str, Any]:
    schema = tool_spec.get("inputSchema") or tool_spec.get("parameters") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    return properties if isinstance(properties, dict) else {}


def address_summary_customer_ready(summary: str) -> bool:
    normalized = normalize_hinglish_text(summary)
    if len(normalized) < 20:
        return False
    if re.search(r"\[[^\]]*(address|pincode|pin code|landmark|city|state)[^\]]*\]", summary, re.I):
        return False
    if any(token in normalized for token in ("full address", "customer address", "your address", "city state pincode")):
        return False
    if any(
        token in normalized
        for token in (
            "share karein",
            "share kar dijiye",
            "please share",
            "kripya apna",
            "poora address",
            "house no/street/area",
        )
    ):
        return False
    has_address_part = any(
        token in normalized
        for token in ("address", "ghar", "house", "sector", "phase", "market", "near", "landmark", "colony", "road")
    )
    has_pin = bool(re.search(r"\b\d{6}\b", normalized))
    return has_address_part or has_pin


def mcp_result_indicates_success(result: object) -> bool:
    if isinstance(result, list):
        return any(mcp_result_indicates_success(item) for item in result)
    if not isinstance(result, dict):
        return False
    lowered = json.dumps(result, default=str).lower()
    if any(token in lowered for token in ('"status": "error"', '"status": "failed"', '"status": "blocked"', '"ok": false')):
        return False
    if result.get("ok") is True or result.get("success") is True or result.get("sent") is True:
        return True
    status = str(result.get("status") or result.get("delivery_status") or "").strip().lower()
    if status in {"success", "succeeded", "sent", "queued", "queued_background"}:
        return True
    for key in ("message", "result", "data", "body"):
        if mcp_result_indicates_success(result.get(key)):
            return True
    return False


def extract_patient_from_tool_result(result: object) -> str:
    if isinstance(result, list):
        for item in result:
            patient = extract_patient_from_tool_result(item)
            if patient:
                return patient
        return ""
    if not isinstance(result, dict):
        return ""
    for key in ("patient", "patient_id", "patient_name_id"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    record_name = str(result.get("name") or "").strip()
    if record_name and (
        str(result.get("doctype") or "").strip().lower() == "patient"
        or record_name.upper().startswith(("HLC-PAT-", "PAT-"))
    ):
        return record_name
    for key in ("records", "data", "result", "message", "body"):
        nested = result.get(key)
        patient = extract_patient_from_tool_result(nested)
        if patient:
            return patient
    return ""


def is_whatsapp_tool(tool_name: str) -> bool:
    normalized = " ".join(str(tool_name or "").strip().lower().split())
    return normalized in WHATSAPP_SEND_TOOLS or "whatsapp" in normalized


def is_background_mcp_tool(tool_name: str) -> bool:
    normalized = " ".join(str(tool_name or "").strip().lower().split())
    compact = normalized.replace(" ", "_").replace("-", "_")
    return (
        normalized in BACKGROUND_MCP_TOOLS
        or compact in BACKGROUND_MCP_TOOLS
        or is_whatsapp_tool(tool_name)
    )


def is_draft_record_tool(tool_name: str) -> bool:
    compact = " ".join(str(tool_name or "").strip().lower().split()).replace(" ", "_").replace("-", "_")
    return (
        "create_draft_patient_encounter" in compact
        or ("draft" in compact and "encounter" in compact)
        or ("create" in compact and "patient_encounter" in compact)
    )


def extract_whatsapp_message_argument(arguments: dict[str, object]) -> str:
    for key in ("message", "body", "text", "content", "details"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def build_customer_ready_whatsapp_message(arguments: dict[str, object]) -> str:
    existing = extract_whatsapp_message_argument(arguments)
    normalized = " ".join(existing.lower().split())
    if existing and len(normalized) >= int(os.getenv("WHATSAPP_MIN_MESSAGE_CHARS", "80") or "80"):
        if not any(re.search(pattern, normalized, re.I) for pattern in GENERIC_WHATSAPP_MESSAGE_PATTERNS):
            return existing

    # Universal worker rule: never invent business-specific WhatsApp content here.
    # Company, disease, template body, links, pricing, address, and offers must come
    # from the Confluence prompt/tool arguments or the MCP/template-map layer.
    return existing


def validate_whatsapp_message_argument(arguments: dict[str, object]) -> Optional[str]:
    message = extract_whatsapp_message_argument(arguments)
    if not message:
        return "WhatsApp message is missing. Pass a complete customer-facing message in the message field."

    normalized = " ".join(message.lower().split())
    intent = " ".join(str(arguments.get("intent") or "").lower().split())
    if len(normalized) < int(os.getenv("WHATSAPP_MIN_MESSAGE_CHARS", "80") or "80"):
        return "WhatsApp message is too short. Pass the real details the customer should receive, not a label."

    for pattern in GENERIC_WHATSAPP_MESSAGE_PATTERNS:
        if re.search(pattern, normalized, re.I):
            return "WhatsApp message is generic. Pass the actual customer-facing information, not a placeholder."

    if "address" in intent and not address_summary_customer_ready(message):
        return "Address verification WhatsApp must contain the confirmed address or PIN shared by the customer, not a request to share address."

    return None


def normalize_mapped_whatsapp_arguments(arguments: dict[str, object]) -> dict[str, object]:
    """Keep mapped WhatsApp arguments dynamic for a universal worker."""
    normalized = dict(arguments or {})
    normalized.setdefault("intent", "Information")
    return normalized


def normalize_json_string_children(value: Any) -> Any:
    """Convert JSON-string child rows back into structured values before MCP calls."""
    if isinstance(value, list):
        normalized_items: list[Any] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        item = json.loads(stripped)
                    except json.JSONDecodeError:
                        pass
            normalized_items.append(normalize_json_string_children(item))
        return normalized_items
    if isinstance(value, dict):
        return {key: normalize_json_string_children(item) for key, item in value.items()}
    return value


def schema_from_sample(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, list):
        schema: dict[str, Any] = {"type": "array"}
        if value:
            schema["items"] = schema_from_sample(value[0])
        return schema
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(key): schema_from_sample(item) for key, item in value.items()},
        }
    return {"type": "string"}


def enrich_schema_from_expected_json(input_schema: dict[str, Any], expected_json: object) -> dict[str, Any]:
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}
    expected_text = str(expected_json or "").strip()
    if not expected_text:
        return input_schema
    try:
        expected = json.loads(expected_text)
    except json.JSONDecodeError:
        return input_schema
    if not isinstance(expected, dict):
        return input_schema

    schema = dict(input_schema)
    properties = dict(schema.get("properties") or {})
    for key, sample in expected.items():
        field_schema = dict(properties.get(key) or {})
        inferred = schema_from_sample(sample)
        if not field_schema:
            properties[key] = inferred
            continue
        if field_schema.get("type") == "array" and "items" not in field_schema and isinstance(sample, list):
            field_schema["items"] = inferred.get("items", {})
        elif field_schema.get("type") == "object" and "properties" not in field_schema and isinstance(sample, dict):
            field_schema["properties"] = inferred.get("properties", {})
        properties[key] = field_schema
    schema["properties"] = properties
    return schema


def confluence_http_timeout() -> float:
    return float(os.getenv("CONFLUENCE_HTTP_TIMEOUT_SECONDS", "4.0") or "4.0")


def inbound_resolve_timeout() -> float:
    return float(os.getenv("INBOUND_RESOLVE_TIMEOUT_SECONDS", "1.0") or "1.0")


def confluence_http_attempts() -> int:
    return int(os.getenv("CONFLUENCE_HTTP_ATTEMPTS", "2") or "2")


def confluence_retry_delay() -> float:
    return float(os.getenv("CONFLUENCE_HTTP_RETRY_DELAY_SECONDS", "0.25") or "0.25")


def mcp_cache_seconds() -> float:
    return float(os.getenv("MCP_RESULT_CACHE_SECONDS", "120") or "120")


def is_stateful_mcp_tool(tool_name: str) -> bool:
    normalized = str(tool_name or "").strip()
    return normalized in STATEFUL_MCP_TOOLS


def inbound_participant_wait_seconds() -> float:
    return float(os.getenv("INBOUND_PARTICIPANT_WAIT_SECONDS", "2.0") or "2.0")


def participant_wait_seconds() -> float:
    return float(os.getenv("LIVEKIT_PARTICIPANT_WAIT_SECONDS", "5.0") or "5.0")


def confluence_gateway_url() -> str:
    return (os.getenv("MCP_SERVER_URL") or "").strip()


def build_mcp_headers(task_id: Optional[str] = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if token:
        headers[(os.getenv("MCP_AUTH_HEADER") or "X-MCP-Token").strip()] = token
    if task_id:
        headers["X-Confluence-Task-ID"] = task_id
    return headers


def derive_confluence_method_url(method_path: str, explicit_env: str | None = None) -> str:
    configured = (os.getenv(explicit_env or "") or "").strip() if explicit_env else ""
    if configured:
        return configured

    gateway = confluence_gateway_url()
    if not gateway:
        return ""
    if "confluence_ai.api.mcp.gateway" in gateway:
        return gateway.replace("confluence_ai.api.mcp.gateway", method_path)
    if "/api/" in gateway:
        base = gateway.split("/api/", 1)[0].rstrip("/")
        return f"{base}/api/method/{method_path}"
    return ""


def livekit_webhook_url() -> str:
    return derive_confluence_method_url(
        "confluence_ai.api.webhook.receive_livekit",
        "CONFLUENCE_LIVEKIT_WEBHOOK_URL",
    )


def inbound_resolver_url() -> str:
    return derive_confluence_method_url(
        "confluence_ai.api.inbound.resolve_call",
        "CONFLUENCE_INBOUND_RESOLVE_URL",
    )


def unwrap_frappe_response(response_json: dict) -> dict:
    message = response_json.get("message")
    if isinstance(message, dict):
        return message
    return response_json


def confluence_base_url() -> str:
    configured = (
        os.getenv("CONFLUENCE_BASE_URL")
        or os.getenv("baseurl")
        or os.getenv("BASEURL")
        or ""
    ).strip()
    if configured:
        return configured.rstrip("/")

    gateway = confluence_gateway_url()
    if "/api/" in gateway:
        return gateway.split("/api/", 1)[0].rstrip("/")
    return ""


def confluence_resource_headers(task_id: Optional[str] = None) -> dict[str, str]:
    headers = build_mcp_headers(task_id)
    token = (
        os.getenv("CONFLUENCE_AUTHORIZATION")
        or os.getenv("authorization")
        or os.getenv("AUTHORIZATION")
        or ""
    ).strip()
    if token:
        headers["Authorization"] = token if token.lower().startswith("token ") else f"token {token}"
    return headers


def extract_knowledge_title(loader_text: object) -> Optional[str]:
    match = re.search(r'Load\s+AI\s+Knowledge\s+Document\s+"([^"]+)"', str(loader_text or ""), re.I)
    return match.group(1).strip() if match else None


def stage_prompt_lookup(stage_prompts: Any) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if not isinstance(stage_prompts, list):
        return lookup
    for stage in stage_prompts:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage_id") or "").strip()
        if stage_id:
            lookup[stage_id] = stage
    return lookup


def direct_stage_prompt_text(stage: Optional[dict[str, Any]], *, max_chars: Optional[int] = None) -> str:
    text = str((stage or {}).get("system_prompt") or "").strip()
    if not text:
        return ""
    if extract_knowledge_title(text):
        return ""
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n...TRUNCATED"
    return text


def stage_prompt_display_title(stage: Optional[dict[str, Any]], stage_id: str) -> str:
    prompt_text = str((stage or {}).get("system_prompt") or "")
    knowledge_title = extract_knowledge_title(prompt_text)
    if knowledge_title:
        return knowledge_title
    return str((stage or {}).get("stage_name") or stage_id).strip() or stage_id


def normalize_medicine_lookup(value: object) -> str:
    text = str(value or "").lower()
    text = text.replace("ओआईएल", "oil")
    return re.sub(r"[^a-z0-9]+", "", text)


def repeat_medicine_items_from_context(context: Optional[dict]) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    summary = context.get("medicine_summary") if isinstance(context.get("medicine_summary"), dict) else {}
    prescriptions = summary.get("drug_prescription") if isinstance(summary, dict) else []
    if not isinstance(prescriptions, list):
        prescriptions = []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(prescriptions, start=1):
        if not isinstance(item, dict):
            continue
        display = (
            item.get("sr_medication_name_print")
            or item.get("medication")
            or item.get("drug_name")
            or f"Medicine {index}"
        )
        items.append(
            {
                "index": index,
                "display_name": display,
                "drug_name": item.get("drug_name") or "",
                "medication": item.get("medication") or "",
                "print_name": item.get("sr_medication_name_print") or "",
                "dosage": item.get("dosage") or "",
                "dosage_form": item.get("dosage_form") or "",
                "instruction": item.get("sr_drug_instruction") or "",
                "period": item.get("period") or "",
            }
        )
    return items


def find_repeat_medicine_match(query: object, medicines: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    needle = normalize_medicine_lookup(query)
    if not needle:
        return None
    for item in medicines:
        for key in (
            item.get("display_name"),
            item.get("drug_name"),
            item.get("medication"),
            item.get("print_name"),
        ):
            hay = normalize_medicine_lookup(key)
            if hay and (needle == hay or needle in hay or hay in needle):
                return item
    return None


def compact_tool_result(value: Any, *, max_string: int = 260, max_list: int = 2, depth: int = 0) -> Any:
    """Keep MCP responses small enough for realtime voice turns."""
    if depth > 4:
        return "...TRUNCATED"
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...TRUNCATED"
    if isinstance(value, list):
        compacted = [compact_tool_result(item, max_string=max_string, max_list=max_list, depth=depth + 1) for item in value[:max_list]]
        if len(value) > max_list:
            compacted.append({"truncated_count": len(value) - max_list})
        return compacted
    if isinstance(value, dict):
        return {
            key: compact_tool_result(item, max_string=max_string, max_list=max_list, depth=depth + 1)
            for key, item in value.items()
        }
    return value


def safe_tool_json(value: Any, *, max_chars: int = 2500) -> str:
    compacted = compact_tool_result(value)
    text = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "...TRUNCATED"
    return text


def full_tool_json(value: Any, *, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "...TRUNCATED"
    return text


def parse_dispatch_metadata(ctx: JobContext) -> dict[str, Any]:
    candidates = [
        getattr(ctx.room, "metadata", None),
        getattr(getattr(ctx.job, "room", None), "metadata", None),
        getattr(ctx.job, "metadata", None),
    ]

    for raw in candidates:
        if not raw:
            continue
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse LiveKit metadata JSON: %s", exc)
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def parse_simulation_metadata(ctx: JobContext) -> dict[str, Any]:
    """Read scenario userdata as metadata during LiveKit simulations only."""
    try:
        simulation_context_fn = getattr(ctx, "simulation_context", None)
        if not callable(simulation_context_fn):
            return {}
        simulation = simulation_context_fn()
        if not simulation:
            return {}
        userdata_fn = getattr(simulation, "userdata", None)
        userdata = userdata_fn() if callable(userdata_fn) else getattr(simulation, "userdata", None)
        if not isinstance(userdata, dict):
            return {}
        metadata = userdata.get("metadata") if isinstance(userdata.get("metadata"), dict) else userdata
        if not isinstance(metadata, dict):
            return {}
        logger.info("Loaded LiveKit simulation metadata keys=%s", sorted(metadata.keys()))
        return metadata
    except Exception as exc:
        logger.warning("Failed to load LiveKit simulation metadata: %s", exc)
        return {}


def extract_room_telephony(ctx: JobContext) -> dict[str, Any]:
    details = {
        "room_name": ctx.room.name if ctx.room else None,
        "caller_phone": None,
        "called_number": None,
        "call_uuid": None,
        "trunk_id": None,
        "domain": None,
        "participants": [],
    }
    if not ctx.room:
        return details

    for participant in ctx.room.remote_participants.values():
        identity = getattr(participant, "identity", "") or ""
        attrs = dict(getattr(participant, "attributes", None) or {})
        metadata = getattr(participant, "metadata", None)
        parsed_metadata = {}
        if metadata:
            try:
                parsed_metadata = json.loads(metadata)
            except Exception:
                parsed_metadata = {}

        details["participants"].append(
            {"identity": identity, "attributes": attrs, "metadata": parsed_metadata}
        )

        if not details["caller_phone"]:
            if identity.startswith("sip_"):
                phone = identity.replace("sip_", "")
                details["caller_phone"] = "+" + phone if phone and not phone.startswith("+") else phone
            elif identity.startswith("+") or identity.isdigit():
                details["caller_phone"] = identity

        merged = {**parsed_metadata, **attrs}
        for key in ("sip.from", "sip.phoneNumber", "lk.sip.from", "from", "From", "caller_phone"):
            if merged.get(key) and not details["caller_phone"]:
                details["caller_phone"] = merged[key]
        for key in (
            "sip.to",
            "lk.sip.to",
            "sip.trunkPhoneNumber",
            "lk.sip.trunkPhoneNumber",
            "to",
            "To",
            "called_number",
        ):
            if merged.get(key) and not details["called_number"]:
                details["called_number"] = merged[key]
        for key in ("sip.callID", "sip.callId", "lk.sip.callID", "call_uuid", "CallUUID", "SIPCallID"):
            if merged.get(key) and not details["call_uuid"]:
                details["call_uuid"] = merged[key]
        for key in ("sip.trunkID", "sip.trunkId", "lk.sip.trunkID", "trunk_id", "TrunkID"):
            if merged.get(key) and not details["trunk_id"]:
                details["trunk_id"] = merged[key]
        for key in ("sip.domain", "domain", "Domain"):
            if merged.get(key) and not details["domain"]:
                details["domain"] = merged[key]

    return details


async def resolve_inbound_metadata_from_confluence(payload: dict) -> dict[str, Any]:
    url = inbound_resolver_url()
    if not url:
        logger.warning("Inbound resolver URL is not configured; skipping inbound metadata resolve.")
        return {}

    import aiohttp
    import asyncio

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=build_mcp_headers(),
                timeout=inbound_resolve_timeout(),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.warning("Inbound resolve failed HTTP %s: %s", response.status, body[:500])
                    return {}
                return unwrap_frappe_response(json.loads(body))
    except Exception as exc:
        logger.exception("Inbound Confluence metadata resolve failed: %s", exc)
        return {}


async def resolve_inbound_metadata_with_retries(ctx: JobContext) -> dict[str, Any]:
    import asyncio

    attempts = int(os.getenv("INBOUND_TASK_RESOLVE_ATTEMPTS", "1") or "1")
    delay_seconds = float(
        os.getenv("INBOUND_TASK_RESOLVE_DELAY_SECONDS", str(confluence_retry_delay())) or "0.25"
    )
    last_result: dict[str, Any] = {}

    for attempt in range(1, attempts + 1):
        payload = extract_room_telephony(ctx)
        if not (
            payload.get("caller_phone")
            or payload.get("called_number")
            or payload.get("call_uuid")
            or payload.get("trunk_id")
        ):
            return {}

        logger.info(
            "Resolving inbound metadata attempt=%s caller=%s called=%s call_uuid=%s trunk=%s",
            attempt,
            payload.get("caller_phone"),
            payload.get("called_number"),
            payload.get("call_uuid"),
            payload.get("trunk_id"),
        )
        last_result = await resolve_inbound_metadata_from_confluence(payload)
        metadata = last_result.get("metadata") if isinstance(last_result, dict) else None
        if isinstance(metadata, dict) and (
            metadata.get("task") or metadata.get("system_prompt") or metadata.get("context")
        ):
            return last_result
        if attempt < attempts:
            await asyncio.sleep(delay_seconds)

    return last_result if isinstance(last_result, dict) else {}


async def fetch_knowledge_document_content_from_chunks(
    title: str,
    task_id: Optional[str] = None,
    *,
    max_chunks: int = 24,
    max_chars: int = 24000,
) -> str:
    base_url = confluence_base_url()
    if not base_url or not title:
        return ""

    import aiohttp

    doc_name = ""
    doc_url = f"{base_url}/api/resource/AI%20Knowledge%20Document"
    doc_params = {
        "fields": json.dumps(["name", "title"]),
        "filters": json.dumps([["title", "=", title], ["enabled", "=", 1]]),
        "limit_page_length": 1,
    }
    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    doc_url,
                    params=doc_params,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        rows = (json.loads(body).get("data") or [])
                        if rows and isinstance(rows[0], dict):
                            doc_name = str(rows[0].get("name") or "")
                            break
                    else:
                        logger.error(
                            "Knowledge document chunk fallback lookup failed title=%s attempt=%s HTTP %s: %s",
                            title,
                            attempt,
                            response.status,
                            body[:500],
                        )
        except Exception as exc:
            logger.exception("Knowledge document chunk fallback lookup error title=%s attempt=%s: %s", title, attempt, exc)
        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    chunk_filters: list[list[object]]
    if doc_name:
        chunk_filters = [["document", "=", doc_name], ["enabled", "=", 1], ["index_status", "=", "Indexed"]]
    else:
        chunk_filters = [["title", "like", f"{title}%"], ["enabled", "=", 1], ["index_status", "=", "Indexed"]]

    chunk_url = f"{base_url}/api/resource/AI%20Knowledge%20Chunk"
    chunk_params = {
        "fields": json.dumps(["title", "document", "chunk_index", "content"]),
        "filters": json.dumps(chunk_filters),
        "order_by": "chunk_index asc",
        "limit_page_length": max(1, int(max_chunks or 24)),
    }
    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    chunk_url,
                    params=chunk_params,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        chunks = json.loads(body).get("data") or []
                        text = "\n\n".join(
                            str(chunk.get("content") or "").strip()
                            for chunk in chunks
                            if str(chunk.get("content") or "").strip()
                        ).strip()
                        if text:
                            if len(text) > max_chars:
                                text = text[:max_chars].rstrip() + "\n...TRUNCATED"
                            logger.warning(
                                "Reconstructed knowledge document from chunks title=%s document=%s chunks=%s chars=%s",
                                title,
                                doc_name or "title-prefix",
                                len(chunks),
                                len(text),
                            )
                            return text
                    else:
                        logger.error(
                            "Knowledge document chunk fallback fetch failed title=%s attempt=%s HTTP %s: %s",
                            title,
                            attempt,
                            response.status,
                            body[:500],
                        )
        except Exception as exc:
            logger.exception("Knowledge document chunk fallback fetch error title=%s attempt=%s: %s", title, attempt, exc)
        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    return ""


async def fetch_knowledge_document_content(title: str, task_id: Optional[str] = None) -> str:
    base_url = confluence_base_url()
    if not base_url:
        logger.warning("Confluence base URL is not configured; cannot load stage document %r.", title)
        return ""

    import aiohttp
    url = f"{base_url}/api/resource/AI%20Knowledge%20Document"
    params = {
        "fields": json.dumps(["name", "title", "enabled", "content"]),
        "filters": json.dumps([["title", "=", title], ["enabled", "=", 1]]),
        "limit_page_length": 1,
    }

    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            "Knowledge document fetch failed title=%s attempt=%s HTTP %s: %s",
                            title,
                            attempt,
                            response.status,
                            body[:500],
                        )
                    else:
                        parsed = json.loads(body)
                        rows = parsed.get("data") or []
                        if rows and isinstance(rows[0], dict):
                            content = str(rows[0].get("content") or "")
                            if content.strip():
                                return content
                            logger.warning("Knowledge document row had empty content for title=%r", title)
                            break
                        logger.warning("Knowledge document not found or empty for title=%r", title)
                        break
        except Exception as exc:
            logger.exception("Knowledge document fetch error title=%s attempt=%s: %s", title, attempt, exc)

        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    return await fetch_knowledge_document_content_from_chunks(title, task_id=task_id)


async def search_confluence_knowledge_chunks(
    query: str,
    *,
    task_id: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    base_url = confluence_base_url()
    if not base_url or not query.strip():
        return []

    import aiohttp

    url = (
        f"{base_url.rstrip('/')}/api/method/"
        "confluence_ai.services.knowledge_base.search"
    )
    payload = {"query": query, "limit": int(limit or 5)}
    if agent:
        payload["agent"] = agent

    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            "Confluence knowledge chunk search failed query=%r attempt=%s HTTP %s: %s",
                            query[:120],
                            attempt,
                            response.status,
                            body[:500],
                        )
                    else:
                        parsed = json.loads(body)
                        rows = unwrap_frappe_response(parsed)
                        if isinstance(rows, dict) and "message" in rows:
                            rows = rows["message"]
                        if isinstance(rows, list):
                            return [row for row in rows if isinstance(row, dict)]
                        logger.warning("Unexpected knowledge search response for query=%r: %s", query[:120], body[:500])
                        return []
        except Exception as exc:
            logger.exception("Confluence knowledge chunk search error query=%r attempt=%s: %s", query[:120], attempt, exc)

        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    return []


def format_chunk_search_results(chunks: list[dict[str, Any]], *, max_chars: int) -> str:
    parts: list[str] = []
    used = 0
    for index, item in enumerate(chunks, start=1):
        source = item.get("title") or item.get("document") or item.get("chunk") or f"Chunk {index}"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        block = f"{index}. {source}\n{content}"
        remaining = max_chars - used
        if remaining <= 180:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip() + "\n...TRUNCATED"
        parts.append(block)
        used += len(block)
        if used >= max_chars:
            break
    return "\n\n".join(parts).strip()


def normalize_section_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"`+", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_").upper()


SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "STAGE_CARD": ("STAGE_CARD", "Stage Card", "Runtime Stage Card"),
    "REQUIRED_SCRIPT": ("REQUIRED_SCRIPT", "Required Script", "Mandatory Rule", "Core Runtime Rule"),
    "CORE_TREATMENT_EXPLANATION": (
        "CORE_TREATMENT_EXPLANATION",
        "Core treatment explanation",
        "Transition From Disease To Treatment",
    ),
    "PRICE_OBJECTION": (
        "PRICE_OBJECTION",
        "Price and trust objection handling",
        "Price/trust objection handling before close",
        "Price objection",
        "Pricing",
    ),
    "DISCOUNT_OBJECTION": (
        "DISCOUNT_OBJECTION",
        "Discount objection",
        "Price/trust objection handling before close",
        "Price and trust objection handling",
    ),
    "MEDICINE_NAME_QUESTION": (
        "MEDICINE_NAME_QUESTION",
        "Medicine-name objection",
        "Medicine Name Question",
    ),
    "TRUST_BUILDING": (
        "TRUST_BUILDING",
        "Trust points",
        "Trust Building",
        "Price and trust objection handling",
    ),
    "TESTIMONIAL_WHATSAPP": (
        "TESTIMONIAL_WHATSAPP",
        "Testimonial WhatsApp",
        "Trust points",
        "Price and trust objection handling",
    ),
    "ORDER_READY_TRANSITION": (
        "ORDER_READY_TRANSITION",
        "Order consent",
        "Start Closing",
        "Transition To Starting The Treatment",
    ),
    "ADDRESS_VERIFICATION": (
        "ADDRESS_VERIFICATION",
        "WhatsApp verification",
        "After Delivery Details",
        "Delivery Details",
    ),
    "DRAFT_ENCOUNTER": (
        "DRAFT_ENCOUNTER",
        "Draft encounter",
        "Mandatory MCP Actions",
    ),
}


def infer_section_from_query(query: str) -> str:
    normalized = " ".join(str(query or "").strip().lower().split())
    if any(word in normalized for word in ("discount", "kam", "less", "offer")):
        return "DISCOUNT_OBJECTION"
    if any(word in normalized for word in ("price", "cost", "mahanga", "mehenga", "expensive", "budget", "payment")):
        return "PRICE_OBJECTION"
    if any(word in normalized for word in ("medicine", "dawai", "medecine", "name", "kaun si")):
        return "MEDICINE_NAME_QUESTION"
    if any(word in normalized for word in ("trust", "proof", "testimonial", "video", "youtube", "bharosa")):
        return "TESTIMONIAL_WHATSAPP"
    if any(word in normalized for word in ("address", "pincode", "landmark", "delivery")):
        return "ADDRESS_VERIFICATION"
    if any(word in normalized for word in ("draft", "encounter", "order create", "patient encounter")):
        return "DRAFT_ENCOUNTER"
    if any(word in normalized for word in ("order", "confirm", "start", "proceed")):
        return "ORDER_READY_TRANSITION"
    if any(word in normalized for word in ("treatment", "solution", "process")):
        return "CORE_TREATMENT_EXPLANATION"
    return ""


def _markdown_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{2,6})\s+(.+?)\s*$", line or "")
    if not match:
        return None
    title = re.sub(r"\s+#*$", "", match.group(2)).strip()
    return len(match.group(1)), title


def extract_markdown_section(content: str, section_name: str, *, max_chars: int) -> str:
    if not content or not section_name:
        return ""

    wanted = {normalize_section_key(section_name)}
    for alias in SECTION_ALIASES.get(normalize_section_key(section_name), ()):
        wanted.add(normalize_section_key(alias))

    lines = content.splitlines()
    start = None
    start_level = None
    for index, line in enumerate(lines):
        heading = _markdown_heading(line)
        if not heading:
            continue
        level, title = heading
        if normalize_section_key(title) in wanted:
            start = index
            start_level = level
            break

    if start is None or start_level is None:
        return ""

    end = len(lines)
    for index in range(start + 1, len(lines)):
        heading = _markdown_heading(lines[index])
        if heading and heading[0] <= start_level:
            end = index
            break

    block = "\n".join(lines[start:end]).strip()
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n...TRUNCATED"
    return block


def extract_customer_facing_quotes(section_text: str, *, max_chars: int = 1200) -> str:
    text = str(section_text or "")
    if not text:
        return ""
    quotes = [match.strip() for match in re.findall(r'"([^"]+)"', text, flags=re.S) if match.strip()]
    if not quotes:
        cleaned = re.sub(r"^#{2,6}\s+.*$", "", text, flags=re.M)
        cleaned = re.sub(r"(?im)^\s*(say|say exactly|start only|after all|use only|if caller|never say|do not).*$", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned[:max_chars].rstrip()
    output = "\n".join(quotes)
    if len(output) > max_chars:
        output = output[:max_chars].rstrip() + "\n...TRUNCATED"
    return output


def extract_patient_specific_options(section_text: str, *, max_chars: int = 1800) -> list[dict[str, str]]:
    text = str(section_text or "")
    if not text:
        return []
    options: list[dict[str, str]] = []
    pattern = re.compile(r"If\s+([^\n:]+?)\s*,?\s*say:\s*\n\"([^\"]+)\"", flags=re.IGNORECASE)
    for match in pattern.finditer(text):
        condition = " ".join(match.group(1).split()).strip()
        say = " ".join(match.group(2).split()).strip()
        if condition and say:
            options.append({"condition": condition, "say": say})
    rendered = json.dumps(options, ensure_ascii=False)
    if len(rendered) > max_chars:
        return options[: max(1, len(options) // 2)]
    return options


def select_patient_specific_options(
    options: list[dict[str, str]],
    *,
    confirmed_facts: str = "",
    selected_conditions: Any = None,
) -> list[dict[str, str]]:
    if not options:
        return []

    def _norm(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))

    selected_texts: list[str] = []
    if isinstance(selected_conditions, str):
        selected_texts = [selected_conditions]
    elif isinstance(selected_conditions, list):
        selected_texts = [str(item) for item in selected_conditions if str(item).strip()]
    selected_norms = [_norm(item) for item in selected_texts if _norm(item)]
    facts_norm = _norm(confirmed_facts)
    fact_tokens = set(facts_norm.split())
    stopwords = {
        "is",
        "are",
        "was",
        "were",
        "the",
        "a",
        "an",
        "and",
        "or",
        "if",
        "then",
        "confirmed",
        "mention",
        "mentioned",
    }
    matched: list[dict[str, str]] = []
    for option in options:
        condition = str(option.get("condition") or "")
        condition_norm = _norm(condition)
        if not condition_norm:
            continue
        explicitly_selected = any(condition_norm in item or item in condition_norm for item in selected_norms)
        condition_tokens = [token for token in condition_norm.split() if token not in stopwords]
        token_matches = [token for token in condition_tokens if token in fact_tokens]
        phrase_selected = bool(condition_tokens and " ".join(condition_tokens[:2]) in facts_norm)
        enough_overlap = len(token_matches) >= min(2, len(condition_tokens))
        if explicitly_selected or phrase_selected or enough_overlap:
            matched.append(option)
    return matched



def extract_stage_runtime_sections(content: str, *, stage_id: str, max_chars: int) -> str:
    if not content:
        return ""

    section_names = ["STAGE_CARD"]
    if (stage_id or "").strip().lower() == "treatment_explanation":
        section_names.extend(
            [
                "CORE_TREATMENT_EXPLANATION",
                "PRICE_OBJECTION",
                "DISCOUNT_OBJECTION",
                "MEDICINE_NAME_QUESTION",
                "TRUST_BUILDING",
                "TESTIMONIAL_WHATSAPP",
                "ORDER_READY_TRANSITION",
                "REQUIRED_SCRIPT",
            ]
        )
    elif (stage_id or "").strip().lower() == "closing_order":
        section_names.extend(
            [
                "PRICE_OBJECTION",
                "DISCOUNT_OBJECTION",
                "TRUST_BUILDING",
                "TESTIMONIAL_WHATSAPP",
                "ORDER_READY_TRANSITION",
                "ADDRESS_VERIFICATION",
                "DRAFT_ENCOUNTER",
                "REQUIRED_SCRIPT",
            ]
        )
    else:
        section_names.append("REQUIRED_SCRIPT")

    parts: list[str] = []
    used = 0
    for section_name in section_names:
        remaining = max_chars - used
        if remaining <= 180:
            break
        section = extract_markdown_section(content, section_name, max_chars=remaining)
        if section and section not in parts:
            parts.append(section)
            used += len(section)

    return "\n\n".join(parts).strip()


def prefer_chunks_from_title(chunks: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    if not chunks or not title:
        return chunks
    prefix = title.strip().lower()
    exact = [
        chunk
        for chunk in chunks
        if str(chunk.get("title") or "").strip().lower().startswith(prefix)
    ]
    return exact or chunks


async def fetch_document_chunks_ordered(
    title: str,
    *,
    task_id: Optional[str] = None,
    max_chunks: int = 6,
    max_chars: int = 6000,
) -> str:
    base_url = confluence_base_url()
    if not base_url or not title:
        return ""

    import aiohttp

    doc_name = ""
    doc_url = f"{base_url.rstrip('/')}/api/resource/AI%20Knowledge%20Document"
    doc_params = {
        "fields": json.dumps(["name", "title"]),
        "filters": json.dumps([["title", "=", title], ["enabled", "=", 1], ["status", "=", "Published"]]),
        "limit_page_length": 1,
    }
    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    doc_url,
                    params=doc_params,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        rows = (json.loads(body).get("data") or [])
                        if rows:
                            doc_name = rows[0].get("name") or ""
                            break
                    else:
                        logger.error("Knowledge document lookup failed title=%s HTTP %s: %s", title, response.status, body[:300])
        except Exception as exc:
            logger.exception("Knowledge document lookup error title=%s attempt=%s: %s", title, attempt, exc)
        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    if not doc_name:
        return ""

    chunk_url = f"{base_url.rstrip('/')}/api/resource/AI%20Knowledge%20Chunk"
    chunk_params = {
        "fields": json.dumps(["title", "document", "chunk_index", "content"]),
        "filters": json.dumps([["document", "=", doc_name], ["enabled", "=", 1], ["index_status", "=", "Indexed"]]),
        "limit_page_length": int(max_chunks or 6),
        "order_by": "chunk_index asc",
    }
    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    chunk_url,
                    params=chunk_params,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        chunks = json.loads(body).get("data") or []
                        logger.info("Loaded ordered Confluence chunks title=%s document=%s chunks=%s", title, doc_name, len(chunks))
                        return format_chunk_search_results(chunks, max_chars=max_chars)
                    logger.error("Knowledge chunk ordered fetch failed title=%s HTTP %s: %s", title, response.status, body[:300])
        except Exception as exc:
            logger.exception("Knowledge chunk ordered fetch error title=%s attempt=%s: %s", title, attempt, exc)
        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    return ""


async def fetch_stage_brief_from_confluence_chunks(
    *,
    stage_id: str,
    title: str,
    task_id: Optional[str],
    agent: Optional[str],
) -> str:
    max_chars = runtime_stage_prompt_limit(stage_id)
    if os.getenv("STAGE_SECTION_RUNTIME", "1").strip().lower() in {"1", "true", "yes"}:
        document_content = await fetch_knowledge_document_content(title, task_id=task_id)
        section_brief = extract_stage_runtime_sections(document_content, stage_id=stage_id, max_chars=max_chars)
        if section_brief:
            logger.info(
                "Loaded exact Confluence stage sections title=%s stage_id=%s chars=%s",
                title,
                stage_id,
                len(section_brief),
            )
            return (
                section_brief
                + "\n\nRuntime note: this active stage brief came from exact Confluence KB sections. "
                "For objection-specific wording, call retrieve_stage_prompt_snippets with section such as PRICE_OBJECTION, MEDICINE_NAME_QUESTION, TRUST_BUILDING, TESTIMONIAL_WHATSAPP, ADDRESS_VERIFICATION, or DRAFT_ENCOUNTER."
            )

    ordered = await fetch_document_chunks_ordered(
        title,
        task_id=task_id,
        max_chunks=int(os.getenv("STAGE_ORDERED_BRIEF_CHUNKS", "6") or "6"),
        max_chars=max_chars,
    )
    if ordered:
        brief = ordered
    else:
        query = (
            f"{title} {stage_id} stage ownership goal main response rules transition "
            "mcp trigger objection safety closing pricing whatsapp"
        )
        chunks = await search_confluence_knowledge_chunks(
            query,
            task_id=task_id,
            agent=agent,
            limit=int(os.getenv("STAGE_RAG_BRIEF_CHUNKS", "4") or "4"),
        )
        brief = format_chunk_search_results(chunks, max_chars=max_chars)
    if not brief:
        return ""
    return (
        brief
        + "\n\nRuntime note: this active stage brief came from Confluence AI Knowledge Chunks. "
        "If more exact wording is needed, call retrieve_stage_prompt_snippets with a focused query."
    )


def runtime_stage_prompt_limit(stage_id: str) -> int:
    normalized = (stage_id or "").strip().lower()
    default_limit = int(os.getenv("STAGE_PROMPT_MAX_CHARS_PER_DOC", "3000") or "3000")
    stage_limits = {
        "orchestrator": int(os.getenv("ORCHESTRATOR_PROMPT_MAX_CHARS", "3000") or "3000"),
        "intake_history": int(os.getenv("INTAKE_PROMPT_MAX_CHARS", "6500") or "6500"),
        "disease_education": int(os.getenv("DISEASE_PROMPT_MAX_CHARS", "2200") or "2200"),
        "treatment_explanation": int(os.getenv("TREATMENT_PROMPT_MAX_CHARS", "2600") or "2600"),
        "closing_order": int(os.getenv("CLOSING_PROMPT_MAX_CHARS", "2600") or "2600"),
        "handoff": int(os.getenv("HANDOFF_PROMPT_MAX_CHARS", "2500") or "2500"),
        "safety_handoff": int(os.getenv("HANDOFF_PROMPT_MAX_CHARS", "2500") or "2500"),
    }
    return stage_limits.get(normalized, default_limit)


async def expand_stage_prompt_documents(
    system_prompt: Optional[str],
    stage_prompts: Any,
    task_id: Optional[str] = None,
    agent: Optional[str] = None,
    initial_stage_id: Optional[str] = None,
) -> str:
    base_prompt = (system_prompt or "").strip()
    if not isinstance(stage_prompts, list) or not stage_prompts:
        return base_prompt

    if os.getenv("STAGE_PROMPT_AUTOLOAD", "0").strip().lower() not in {"1", "true", "yes"}:
        stage_map = build_stage_map_prompt(stage_prompts)
        initial_stage_id = (
            str(initial_stage_id or "").strip()
            or os.getenv("INITIAL_STAGE_ID", "intake_history").strip()
            or "intake_history"
        )
        initial_section = ""
        if os.getenv("STAGE_PROMPT_PRELOAD_INITIAL", "1").strip().lower() in {"1", "true", "yes"}:
            for stage in stage_prompts:
                if not isinstance(stage, dict):
                    continue
                stage_id = str(stage.get("stage_id") or "").strip()
                if stage_id != initial_stage_id:
                    continue
                title = extract_knowledge_title(stage.get("system_prompt"))
                if title:
                    content = await fetch_stage_brief_from_confluence_chunks(
                        stage_id=stage_id,
                        title=title,
                        task_id=task_id,
                        agent=agent,
                    )
                    source_line = f"Knowledge Document: {title}"
                else:
                    content = direct_stage_prompt_text(stage, max_chars=runtime_stage_prompt_limit(stage_id))
                    source_line = f"Configured Stage Prompt: {stage_prompt_display_title(stage, stage_id)}"
                if content.strip():
                    initial_section = "\n\n".join(
                        [
                            f"## Active Initial Stage: {stage_id}",
                            source_line,
                            content,
                            f"Start the call by following only this active initial stage. Do not wait to call load_stage_prompt for {stage_id}. Do not use later stages until this stage is complete.",
                        ]
                    )
                    logger.info(
                        "Preloaded initial stage prompt brief stage_id=%s title=%s chars=%s",
                        stage_id,
                        title or stage_prompt_display_title(stage, stage_id),
                        len(content),
                    )
                else:
                    logger.warning("Initial stage prompt content not found stage_id=%s title=%s", stage_id, title or "")
                break
        if stage_map:
            logger.info("Using lightweight Confluence stage map only; stage prompt chunks will load on demand.")
            return base_prompt + "\n\n" + stage_map + initial_section
        return base_prompt

    mode = os.getenv("STAGE_PROMPT_LOADING_MODE", "active").strip().lower()
    if mode not in {"active", "compact_all"}:
        mode = "active"

    sections: list[str] = []
    seen_titles: set[str] = set()
    initial_stage_id = (
        str(initial_stage_id or "").strip()
        or os.getenv("INITIAL_STAGE_ID", "intake_history").strip()
        or "intake_history"
    )
    stages_to_load: list[dict[str, Any]] = []

    if mode == "active":
        for stage in stage_prompts:
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id") or "").strip()
            if stage_id == "orchestrator":
                stages_to_load.append(stage)
                break
        for stage in stage_prompts:
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id") or "").strip()
            if stage_id == initial_stage_id:
                stages_to_load.append(stage)
                break
        if not stages_to_load:
            stages_to_load = [stage for stage in stage_prompts if isinstance(stage, dict)][:1]
    else:
        max_docs = int(os.getenv("STAGE_PROMPT_MAX_DOCS", "6") or "6")
        stages_to_load = [stage for stage in stage_prompts if isinstance(stage, dict)][:max_docs]

    max_chars = int(os.getenv("STAGE_PROMPT_MAX_CHARS_PER_DOC", "2200") or "2200")
    total_max_chars = int(os.getenv("STAGE_PROMPT_TOTAL_MAX_CHARS", "9000") or "9000")
    for stage in stages_to_load:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage_id") or "").strip() or stage_prompt_display_title(stage, "stage")
        title = extract_knowledge_title(stage.get("system_prompt"))
        if title:
            if title in seen_titles:
                continue
            seen_titles.add(title)
            content = await fetch_stage_brief_from_confluence_chunks(
                stage_id=stage_id,
                title=title,
                task_id=task_id,
                agent=agent,
            )
            source_line = f"Knowledge Document: {title}"
        else:
            content = direct_stage_prompt_text(stage, max_chars=max_chars)
            source_line = f"Configured Stage Prompt: {stage_prompt_display_title(stage, stage_id)}"
        if not content.strip():
            logger.warning(
                "No Confluence KB chunks found for stage_id=%s title=%s task=%s. Stage prompt will not be expanded.",
                stage_id,
                title or stage_prompt_display_title(stage, stage_id),
                task_id,
            )
            continue
        sections.append(
            "\n".join(
                [
                    f"## Voice Stage Prompt: {stage_id}",
                    source_line,
                    content,
                ]
            )
        )

    if not sections:
        return base_prompt

    stage_text = "\n\n".join(sections)
    if len(stage_text) > total_max_chars:
        stage_text = stage_text[:total_max_chars] + "\n...TRUNCATED FOR VOICE STABILITY"

    logger.info(
        "Loaded %s %s stage prompt documents for task=%s chars=%s",
        len(sections),
        mode,
        task_id,
        len(stage_text),
    )
    heading = "Loaded Active Voice Stage Prompts" if mode == "active" else "Loaded Compact Multi-Stage Voice Prompts"
    return base_prompt + f"\n\n## {heading}\n" + stage_text


def stage_prompt_titles(stage_prompts: Any) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not isinstance(stage_prompts, list):
        return titles
    for stage in stage_prompts:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage_id") or "").strip()
        if stage_id and str(stage.get("system_prompt") or "").strip():
            titles[stage_id] = stage_prompt_display_title(stage, stage_id)
    return titles


def build_stage_map_prompt(stage_prompts: Any) -> str:
    titles = stage_prompt_titles(stage_prompts)
    if not titles:
        return ""
    preferred_order = [
        "orchestrator",
        "intake_history",
        "disease_education",
        "treatment_explanation",
        "closing_order",
        "handoff",
        "safety_handoff",
    ]
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for stage_id in preferred_order:
        if stage_id in titles:
            ordered.append((stage_id, titles[stage_id]))
            seen.add(stage_id)
    for stage_id, title in titles.items():
        if stage_id not in seen:
            ordered.append((stage_id, title))

    if len(ordered) == 1:
        stage_id, title = ordered[0]
        return "\n".join(
            [
                "## Confluence Single-Stage Map",
                f"Only one stage is configured: `{stage_id}` as `{title}`.",
                "Stay locked on this stage for the whole call.",
                "Do not call load_stage_prompt, do not transition to another stage, and do not speak content from any unconfigured stage.",
                "If the customer asks for a next step after this stage is complete, obey the active stage prompt's exact completion rule.",
            ]
        )

    lines = [
        "## Confluence Multi-Stage Map",
        "Follow all configured stages in order. Do not skip a stage unless a safety trigger requires handoff.",
        "The configured initial stage is loaded at call start. Do not call load_stage_prompt for the same stage again. Only call load_stage_prompt when moving to a different next stage. For exact wording or detailed handling inside a stage, call retrieve_stage_prompt_snippets with an exact section key when possible.",
        "Hard transition rule: before speaking treatment, process, price, order, address, payment, or confirmation content, first call load_stage_prompt for the correct next stage if that stage is not already active. Never answer treatment/order content from memory while current_stage is disease_education.",
        "Useful section keys when the attached prompt defines them: PRICE_OBJECTION, DISCOUNT_OBJECTION, MEDICINE_NAME_QUESTION, TRUST_BUILDING, TESTIMONIAL_WHATSAPP, ORDER_READY_TRANSITION, ADDRESS_VERIFICATION, DRAFT_ENCOUNTER.",
    ]
    for index, (stage_id, title) in enumerate(ordered, start=1):
        lines.append(f"{index}. `{stage_id}` configured as `{title}`")
    return "\n".join(lines)


def make_stage_prompt_loader_tool(
    stage_prompts: Any,
    task_id: Optional[str],
    agent: Optional[str] = None,
    initial_stage_id: Optional[str] = None,
    runtime_stage_state: Optional[dict[str, Any]] = None,
):
    titles = stage_prompt_titles(stage_prompts)
    if not titles:
        return None
    if len(titles) <= 1 and os.getenv("ENABLE_SINGLE_STAGE_LOADER", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    stages = stage_prompt_lookup(stage_prompts)

    from livekit.agents.llm import function_tool
    loaded_stage_ids: set[str] = {
        str(initial_stage_id or "").strip()
        or os.getenv("INITIAL_STAGE_ID", "intake_history").strip()
        or "intake_history"
    }
    stage_state = runtime_stage_state if isinstance(runtime_stage_state, dict) else {}
    stage_state.setdefault("current_stage", next(iter(loaded_stage_ids)))

    async def load_stage_prompt(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        stage_id = str(arguments.get("stage_id") or arguments.get("stage") or "").strip()
        if not stage_id:
            return json.dumps(
                {
                    "status": "error",
                    "message": "stage_id is required.",
                    "available_stage_ids": sorted(titles),
                }
            )
        title = titles.get(stage_id)
        if not title:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown stage_id: {stage_id}",
                    "available_stage_ids": sorted(titles),
                }
            )
        if stage_id in loaded_stage_ids:
            stage_state["current_stage"] = stage_id
            stage_state["updated_at"] = time.monotonic()
            return json.dumps(
                {
                    "status": "already_loaded",
                    "current_stage": stage_id,
                    "knowledge_document": title,
                    "instruction": (
                        "This stage prompt is already active in the session. "
                        "Do not restart the greeting or repeat previous stage content. "
                        "Continue from the latest customer answer and the pending question."
                    ),
                },
                ensure_ascii=False,
            )
        stage = stages.get(stage_id) or {}
        knowledge_title = extract_knowledge_title(stage.get("system_prompt"))
        if knowledge_title:
            content = await fetch_stage_brief_from_confluence_chunks(
                stage_id=stage_id,
                title=knowledge_title,
                task_id=task_id,
                agent=agent,
            )
        else:
            content = direct_stage_prompt_text(stage, max_chars=runtime_stage_prompt_limit(stage_id))
        if not content.strip():
            return json.dumps(
                {
                    "status": "error",
                    "message": f"No configured stage prompt content found for stage_id: {stage_id}",
                }
            )
        logger.info(
            "Loaded stage prompt via tool stage_id=%s title=%s runtime_chars=%s",
            stage_id,
            title,
            len(content),
        )
        loaded_stage_ids.add(stage_id)
        stage_state["current_stage"] = stage_id
        stage_state["updated_at"] = time.monotonic()
        return json.dumps(
            {
                "status": "success",
                "current_stage": stage_id,
                "knowledge_document": title,
                "instruction": (
                    "Use this prompt as the active stage now. Do not speak the whole prompt; follow its ordered speaking rules. "
                    "If this is a medicine stage, the two-sentence brevity rule is disabled until every listed medicine is spoken. "
                    "Do not continue following the previous stage except for confirmed customer facts. "
                    "If the customer interrupted, answer the clear interruption once and continue from the same unfinished point; do not skip ahead. "
                    "If exact wording or detailed objection handling is needed, call retrieve_stage_prompt_snippets with an exact section key when possible."
                ),
                "prompt": content,
            },
            ensure_ascii=False,
        )

    raw_schema = {
        "name": "load_stage_prompt",
        "description": "Load a compact prompt brief for the next Confluence multi-stage workflow stage. Use before moving into a new stage.",
        "parameters": {
            "type": "object",
            "properties": {
                "stage_id": {
                    "type": "string",
                    "enum": sorted(titles),
                    "description": "The Confluence stage_id to load, for example any stage_id configured on this AI Agent.",
                }
            },
            "required": ["stage_id"],
        },
    }
    return function_tool(load_stage_prompt, raw_schema=raw_schema)


def make_stage_prompt_retriever_tool(stage_prompts: Any, task_id: Optional[str], agent: Optional[str] = None):
    titles = stage_prompt_titles(stage_prompts)
    if not titles:
        return None
    stages = stage_prompt_lookup(stage_prompts)

    from livekit.agents.llm import function_tool

    async def retrieve_stage_prompt_snippets(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        stage_id = str(arguments.get("stage_id") or arguments.get("stage") or "").strip()
        query = str(arguments.get("query") or "").strip()
        section = str(arguments.get("section") or "").strip()
        if not stage_id:
            return json.dumps(
                {
                    "status": "error",
                    "message": "stage_id is required.",
                    "available_stage_ids": sorted(titles),
                }
            )
        if not query:
            return json.dumps({"status": "error", "message": "query is required."})
        title = titles.get(stage_id)
        if not title:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown stage_id: {stage_id}",
                    "available_stage_ids": sorted(titles),
                }
            )
        stage = stages.get(stage_id) or {}
        knowledge_title = extract_knowledge_title(stage.get("system_prompt"))
        selected_section = normalize_section_key(section) if section else infer_section_from_query(query)
        if selected_section and os.getenv("STAGE_SECTION_RETRIEVAL", "1").strip().lower() in {"1", "true", "yes"}:
            document_content = (
                await fetch_knowledge_document_content(knowledge_title, task_id=task_id)
                if knowledge_title
                else direct_stage_prompt_text(stage)
            )
            max_chars = int(os.getenv("RAG_PROMPT_MAX_CHARS", "1800") or "1800")
            exact = extract_markdown_section(document_content, selected_section, max_chars=max_chars)
            if exact:
                logger.info(
                    "Retrieved exact stage section stage_id=%s title=%s section=%s chars=%s",
                    stage_id,
                    title,
                    selected_section,
                    len(exact),
                )
                return json.dumps(
                    {
                        "status": "success",
                        "current_stage": stage_id,
                        "knowledge_document": title,
                        "query": query,
                        "section": selected_section,
                        "instruction": (
                            "Use this exact section only for the current customer need. "
                            "Answer naturally in one compact customer-facing turn, then return to the pending stage flow."
                        ),
                        "snippet": exact,
                    },
                    ensure_ascii=False,
                )

        if not knowledge_title:
            direct_prompt = direct_stage_prompt_text(stage, max_chars=int(os.getenv("RAG_PROMPT_MAX_CHARS", "4200") or "4200"))
            return json.dumps(
                {
                    "status": "success" if direct_prompt else "error",
                    "current_stage": stage_id,
                    "knowledge_document": title,
                    "query": query,
                    "instruction": "Use this current stage prompt excerpt only, answer briefly, then return to the unfinished stage.",
                    "snippets": [{"title": title, "text": direct_prompt}] if direct_prompt else [],
                },
                ensure_ascii=False,
            )

        chunks = await search_confluence_knowledge_chunks(
            f"{knowledge_title} {stage_id} {query}",
            task_id=task_id,
            agent=agent,
            limit=int(os.getenv("RAG_PROMPT_MAX_SNIPPETS", "4") or "4"),
        )
        chunks = prefer_chunks_from_title(chunks, knowledge_title)
        max_chars = int(os.getenv("RAG_PROMPT_MAX_CHARS", "4200") or "4200")
        snippets = [
            {
                "title": chunk.get("title"),
                "document": chunk.get("document"),
                "chunk": chunk.get("chunk"),
                "score": chunk.get("score"),
                "text": str(chunk.get("content") or "")[:max_chars],
            }
            for chunk in chunks
            if str(chunk.get("content") or "").strip()
        ]
        used = 0
        trimmed = []
        for snippet in snippets:
            text = snippet["text"]
            remaining = max_chars - used
            if remaining <= 180:
                break
            if len(text) > remaining:
                snippet = dict(snippet)
                snippet["text"] = text[:remaining].rstrip() + "\n...TRUNCATED"
            trimmed.append(snippet)
            used += len(snippet["text"])
        snippets = trimmed
        logger.info(
            "Retrieved stage prompt snippets from Confluence chunks stage_id=%s title=%s query=%r snippets=%s chars=%s",
            stage_id,
            title,
            query[:120],
            len(snippets),
            sum(len(snippet.get("text", "")) for snippet in snippets),
        )
        return json.dumps(
            {
                "status": "success",
                "current_stage": stage_id,
                "knowledge_document": title,
                "query": query,
                "instruction": "Use only these relevant snippets for the current customer need. Do not repeat unrelated sections.",
                "snippets": snippets,
            },
            ensure_ascii=False,
        )

    raw_schema = {
        "name": "retrieve_stage_prompt_snippets",
        "description": (
            "Retrieve only the relevant small sections from the full Confluence KB prompt for the current stage. "
            "Use for exact wording, objections, WhatsApp content, closing/next-action details, or safety details when those sections exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stage_id": {
                    "type": "string",
                    "enum": sorted(titles),
                    "description": "Current stage_id configured on this AI Agent.",
                },
                "query": {
                    "type": "string",
                    "description": "Focused search query, for example the exact objection, WhatsApp message topic, or next-action consent.",
                },
                "section": {
                    "type": "string",
                    "enum": sorted(SECTION_ALIASES),
                    "description": "Optional exact section key to retrieve. Prefer this over broad query when known.",
                },
            },
            "required": ["stage_id", "query"],
        },
    }
    return function_tool(retrieve_stage_prompt_snippets, raw_schema=raw_schema)


TREATMENT_CHECKPOINT_POINTS: tuple[dict[str, str], ...] = (
    {"id": "treatment_transition", "label": "treatment transition", "section": "1. Treatment Transition"},
    {"id": "customized_treatment", "label": "customized treatment", "section": "2. Customized Treatment"},
    {"id": "included_support", "label": "what is included", "section": "3. What Is Included"},
    {"id": "medicine_adjustment", "label": "medicine adjustment in same plan", "section": "4. Medicine Adjustment"},
    {"id": "patient_specific_logic", "label": "patient-specific treatment logic", "section": "5. Patient-Specific Treatment Logic"},
    {"id": "price_and_value", "label": "price and value", "section": "6. Price And Value"},
    {"id": "final_clarity_check", "label": "final clarity check", "section": "7. Final Clarity Check"},
)


def make_treatment_checkpoint_tools(
    runtime_stage_state: Optional[dict[str, Any]] = None,
    stage_prompts: Any = None,
    task_id: Optional[str] = None,
    agent: Optional[str] = None,
    dynamic_context: Optional[dict[str, Any]] = None,
):
    """Tiny treatment-only checkpoint.

    It deliberately stores only the next unfinished point id. Treatment content
    remains in Confluence prompt/KB, so this does not create a hardcoded sales
    script in the worker.
    """
    if os.getenv("TREATMENT_CHECKPOINT_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return []

    from livekit.agents.llm import function_tool

    stage_state = runtime_stage_state if isinstance(runtime_stage_state, dict) else {}
    checkpoint = stage_state.setdefault(
        "treatment_checkpoint",
        {
            "next_index": 0,
            "completed": [],
        },
    )
    point_ids = [point["id"] for point in TREATMENT_CHECKPOINT_POINTS]
    stages = stage_prompt_lookup(stage_prompts)
    treatment_stage = stages.get("treatment_explanation") or {}
    context = dynamic_context if isinstance(dynamic_context, dict) else {}

    def _default_confirmed_facts() -> str:
        keys = (
            "patient_name",
            "age",
            "city",
            "diagnosis",
            "doctor_finding",
            "disease_or_concern",
            "grade",
            "sgpt",
            "sgot",
            "lft",
            "symptoms",
            "main_problem",
            "current_treatment",
            "treatment_response",
        )
        facts = []
        for key in keys:
            value = context.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            facts.append(f"{key}: {value}")
        return "; ".join(facts)

    def _current_stage() -> str:
        return str(stage_state.get("current_stage") or "").strip()

    def _initial_treatment_tool_not_ready() -> bool:
        return not stage_state.get("greeting_played") and not stage_state.get("user_turn_seen")

    async def _treatment_prompt_content() -> str:
        cached = str(checkpoint.get("source_prompt") or "")
        if cached:
            return cached

        knowledge_title = extract_knowledge_title(treatment_stage.get("system_prompt"))
        content = ""
        if knowledge_title:
            content = await fetch_knowledge_document_content(knowledge_title, task_id=task_id)
        if not content:
            content = direct_stage_prompt_text(treatment_stage, max_chars=24000)
        if not content:
            chunks = await search_confluence_knowledge_chunks(
                "treatment_explanation treatment points in order price value medicine adjustment final clarity",
                task_id=task_id,
                agent=agent,
                limit=8,
            )
            content = "\n\n".join(str(chunk.get("content") or "") for chunk in chunks if str(chunk.get("content") or "").strip())
        checkpoint["source_prompt"] = content
        return content

    async def _required_text_for_point(point: dict[str, str]) -> str:
        content = await _treatment_prompt_content()
        section = str(point.get("section") or point.get("label") or "").strip()
        text = extract_markdown_section(content, section, max_chars=int(os.getenv("TREATMENT_POINT_MAX_CHARS", "1400") or "1400"))
        if text:
            return text
        if content and point.get("id") == "price_and_value":
            text = extract_markdown_section(content, "Price And Value", max_chars=int(os.getenv("TREATMENT_POINT_MAX_CHARS", "1400") or "1400"))
            if text:
                return text
        return ""

    async def _next_payload(status: str = "success") -> dict[str, Any]:
        next_index = int(checkpoint.get("next_index") or 0)
        next_index = max(0, min(next_index, len(TREATMENT_CHECKPOINT_POINTS)))
        checkpoint["next_index"] = next_index
        completed = [point_id for point_id in checkpoint.get("completed", []) if point_id in point_ids]
        if next_index >= len(TREATMENT_CHECKPOINT_POINTS):
            return {
                "status": status,
                "current_stage": _current_stage(),
                "all_treatment_points_complete": True,
                "completed_points": completed,
                "instruction": (
                    "All treatment explanation points are complete. Do not repeat treatment explanation. "
                    "Wait for the customer answer, then follow the active prompt for the next step."
                ),
            }
        point = TREATMENT_CHECKPOINT_POINTS[next_index]
        required_text = await _required_text_for_point(point)
        customer_text = extract_customer_facing_quotes(
            required_text,
            max_chars=int(os.getenv("TREATMENT_POINT_CUSTOMER_TEXT_MAX_CHARS", "1400") or "1400"),
        )
        return {
            "status": status,
            "current_stage": _current_stage(),
            "all_treatment_points_complete": False,
            "next_point_id": point["id"],
            "next_point_number": next_index + 1,
            "next_point_label": point["label"],
            "required_text": required_text,
            "customer_text": customer_text,
            "completed_points": completed,
            "instruction": (
                "Immediately speak this next unfinished treatment point now; do not wait for another customer turn. "
                "Use customer_text as the customer-facing wording. Use required_text only for conditions/rules. "
                "Do not invent prices, package details, support details, or medical claims outside required_text. "
                "Do not ask a mini-permission or mini-clarity question after this point. "
                "The only question allowed in treatment_explanation is the final_clarity_check line. "
                "For patient_specific_logic, speak every customer_text line that matches confirmed facts from the conversation, including Grade 2 and high SGPT if confirmed. "
                "If the caller interrupted with a clear question, answer it briefly first, then continue from this point. "
                "After speaking this point, call mark_treatment_point_complete with this point_id and a short spoken_summary. "
                "Then continue to the next unfinished point without asking for order/address/payment."
            ),
        }

    async def _remaining_script_payload(status: str = "success", arguments: Optional[dict[str, object]] = None) -> dict[str, Any]:
        arguments = dict(arguments or {})
        confirmed_facts = str(arguments.get("confirmed_facts") or _default_confirmed_facts() or "").strip()
        selected_conditions = arguments.get("selected_patient_specific_conditions")
        next_index = int(checkpoint.get("next_index") or 0)
        next_index = max(0, min(next_index, len(TREATMENT_CHECKPOINT_POINTS)))
        checkpoint["next_index"] = next_index
        completed = [point_id for point_id in checkpoint.get("completed", []) if point_id in point_ids]
        if next_index >= len(TREATMENT_CHECKPOINT_POINTS):
            return {
                "status": status,
                "current_stage": _current_stage(),
                "all_treatment_points_complete": True,
                "forced_final_reply": "Theek hai.",
                "completed_points": completed,
                "instruction": (
                    "All treatment explanation points are complete. Do not repeat treatment explanation. "
                    "If the customer only acknowledged with haan/ok/theek hai/clear hai, copy forced_final_reply exactly as your full next response. "
                    "Do not add any other word, goodbye, thanks, next step, order/start/address/payment/WhatsApp, or extra sentence."
                ),
            }

        remaining = []
        script_blocks = []
        script_parts = []
        for index, point in enumerate(TREATMENT_CHECKPOINT_POINTS[next_index:], start=next_index):
            required_text = await _required_text_for_point(point)
            customer_text = extract_customer_facing_quotes(
                required_text,
                max_chars=int(os.getenv("TREATMENT_POINT_CUSTOMER_TEXT_MAX_CHARS", "1400") or "1400"),
            )
            patient_specific_options = (
                extract_patient_specific_options(required_text)
                if point["id"] == "patient_specific_logic"
                else []
            )
            selected_patient_specific_options = (
                select_patient_specific_options(
                    patient_specific_options,
                    confirmed_facts=confirmed_facts,
                    selected_conditions=selected_conditions,
                )
                if point["id"] == "patient_specific_logic"
                else []
            )
            remaining.append(
                {
                    "point_id": point["id"],
                    "point_number": index + 1,
                    "point_label": point["label"],
                    "required_text": required_text,
                    "customer_text": customer_text,
                    "patient_specific_options": patient_specific_options,
                    "selected_patient_specific_options": selected_patient_specific_options,
                }
            )
            if point["id"] == "patient_specific_logic":
                if selected_patient_specific_options:
                    selected_text = "\n".join(option["say"] for option in selected_patient_specific_options if option.get("say"))
                    script_blocks.append({"point_id": point["id"], "mode": "exact_selected", "text": selected_text})
                    script_parts.append(selected_text)
                else:
                    script_blocks.append(
                        {
                            "point_id": point["id"],
                            "mode": "conditional",
                            "instruction": (
                                "Choose and speak only options whose condition is confirmed by the conversation or metadata. "
                                "Skip unrelated conditions completely."
                            ),
                            "options": patient_specific_options,
                        }
                    )
            elif customer_text:
                script_blocks.append({"point_id": point["id"], "mode": "exact", "text": customer_text})
                script_parts.append(customer_text)

        return {
            "status": status,
            "current_stage": _current_stage(),
            "all_treatment_points_complete": False,
            "confirmed_facts_used": confirmed_facts,
            "remaining_point_ids": [point["point_id"] for point in remaining],
            "completed_points": completed,
            "remaining_points": remaining,
            "script_blocks": script_blocks,
            "customer_script": "\n".join(script_parts),
            "instruction": (
                "Speak script_blocks continuously in natural Hinglish without saying section headings, point ids, "
                "mode names, or tool/rule names. For exact and exact_selected blocks, speak the text naturally. "
                "For conditional blocks, choose and speak only options whose condition is confirmed by the conversation or metadata; "
                "skip every unrelated condition completely. Do not stop after each paragraph and do not ask mini-permission questions. "
                "If the caller says filler words like haan, ok, theek hai, hmm, or clear hai while you are explaining, "
                "treat that as listening acknowledgement only and continue the remaining script_blocks. "
                "The only treatment-stage question allowed is the final clarity line from the script. "
                "Do not ask for order, address, payment, COD, courier, or WhatsApp in treatment_explanation. "
                "After the full remaining script is spoken, call mark_treatment_script_complete with remaining_point_ids."
            ),
        }

    async def get_treatment_continuation_script(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        if _initial_treatment_tool_not_ready():
            return json.dumps(
                {
                    "status": "not_ready",
                    "current_stage": _current_stage(),
                    "instruction": (
                        "Do not start treatment explanation before the customer's first final turn. "
                        "Greet briefly and wait for the customer to ask for treatment or solution."
                    ),
                },
                ensure_ascii=False,
            )
        if _current_stage() != "treatment_explanation":
            return json.dumps(
                {
                    "status": "inactive",
                    "current_stage": _current_stage(),
                    "instruction": "Use this checkpoint only while current_stage is treatment_explanation.",
                },
                ensure_ascii=False,
            )
        return json.dumps(await _remaining_script_payload(arguments=arguments), ensure_ascii=False)

    async def get_treatment_next_point(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        if _initial_treatment_tool_not_ready():
            return json.dumps(
                {
                    "status": "not_ready",
                    "current_stage": _current_stage(),
                    "instruction": (
                        "Do not start treatment explanation before the customer's first final turn. "
                        "Greet briefly and wait for the customer to ask for treatment or solution."
                    ),
                },
                ensure_ascii=False,
            )
        if _current_stage() != "treatment_explanation":
            return json.dumps(
                {
                    "status": "inactive",
                    "current_stage": _current_stage(),
                    "instruction": "Use this checkpoint only while current_stage is treatment_explanation.",
                },
                ensure_ascii=False,
            )
        payload = await _remaining_script_payload(status="use_continuation_script", arguments=arguments)
        payload["instruction"] = (
            "Use this as the treatment continuation script. Speak script_blocks continuously and then call "
            "mark_treatment_script_complete. Do not use point-by-point completion."
        )
        return json.dumps(payload, ensure_ascii=False)

    async def mark_treatment_point_complete(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        point_id = str(arguments.get("point_id") or "").strip()
        spoken_summary = str(arguments.get("spoken_summary") or "").strip()
        if _current_stage() != "treatment_explanation":
            return json.dumps(
                {
                    "status": "inactive",
                    "current_stage": _current_stage(),
                    "instruction": "Treatment checkpoint was not updated because current_stage is not treatment_explanation.",
                },
                ensure_ascii=False,
            )
        if point_id not in point_ids:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Unknown treatment point_id.",
                    "allowed_point_ids": point_ids,
                },
                ensure_ascii=False,
            )
        completed = list(checkpoint.get("completed") or [])
        if point_id in completed:
            payload = await _next_payload(status="already_completed")
            payload["message"] = "This treatment point was already completed; duplicate completion ignored."
            payload["instruction"] = (
                "Do not call mark_treatment_point_complete again for already completed points. "
                "If all treatment points are complete and the caller acknowledged the final clarity check, say exactly 'Theek hai.' and wait."
            )
            return json.dumps(payload, ensure_ascii=False)
        expected_index = int(checkpoint.get("next_index") or 0)
        expected_index = max(0, min(expected_index, len(TREATMENT_CHECKPOINT_POINTS) - 1))
        expected_point_id = TREATMENT_CHECKPOINT_POINTS[expected_index]["id"]
        if point_id != expected_point_id:
            return json.dumps(
                {
                    "status": "not_updated",
                    "message": "Treatment point must be completed in order.",
                    "expected_point_id": expected_point_id,
                    "received_point_id": point_id,
                    **(await _next_payload(status="not_updated")),
                },
                ensure_ascii=False,
            )
        if len(spoken_summary) < 12:
            return json.dumps(
                {
                    "status": "not_updated",
                    "message": "spoken_summary is required before marking a treatment point complete.",
                    **(await _next_payload(status="not_updated")),
                },
                ensure_ascii=False,
            )
        if point_id not in completed:
            completed.append(point_id)
        checkpoint["completed"] = completed
        completed_index = point_ids.index(point_id)
        checkpoint["next_index"] = max(int(checkpoint.get("next_index") or 0), completed_index + 1)
        checkpoint["updated_at"] = time.monotonic()
        return json.dumps(await _next_payload(status="updated"), ensure_ascii=False)

    async def mark_treatment_script_complete(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        received = arguments.get("completed_point_ids") or arguments.get("point_ids") or []
        if isinstance(received, str):
            received_ids = [item.strip() for item in received.split(",") if item.strip()]
        elif isinstance(received, list):
            received_ids = [str(item).strip() for item in received if str(item).strip()]
        else:
            received_ids = []
        spoken_summary = str(arguments.get("spoken_summary") or "").strip()
        if _current_stage() != "treatment_explanation":
            return json.dumps(
                {
                    "status": "inactive",
                    "current_stage": _current_stage(),
                    "instruction": "Treatment checkpoint was not updated because current_stage is not treatment_explanation.",
                },
                ensure_ascii=False,
            )
        invalid = [point_id for point_id in received_ids if point_id not in point_ids]
        if invalid or not received_ids:
            return json.dumps(
                {
                    "status": "error",
                    "message": "completed_point_ids must contain valid treatment point ids.",
                    "allowed_point_ids": point_ids,
                    "invalid_point_ids": invalid,
                },
                ensure_ascii=False,
            )
        if len(spoken_summary) < 12:
            return json.dumps(
                {
                    "status": "not_updated",
                    "message": "spoken_summary is required before marking treatment script complete.",
                    **(await _remaining_script_payload(status="not_updated")),
                },
                ensure_ascii=False,
            )

        completed = list(checkpoint.get("completed") or [])
        for point_id in received_ids:
            if point_id not in completed:
                completed.append(point_id)
        checkpoint["completed"] = [point_id for point_id in point_ids if point_id in completed]
        next_index = 0
        for point_id in checkpoint["completed"]:
            next_index = max(next_index, point_ids.index(point_id) + 1)
        checkpoint["next_index"] = next_index
        checkpoint["updated_at"] = time.monotonic()
        return json.dumps(await _remaining_script_payload(status="updated", arguments=arguments), ensure_ascii=False)

    get_schema = {
        "name": "get_treatment_next_point",
        "description": (
            "Return the next unfinished treatment_explanation point. Use only inside treatment_explanation, "
            "as a fallback if get_treatment_continuation_script is unavailable."
        ),
        "parameters": {"type": "object", "properties": {}},
    }
    continuation_schema = {
        "name": "get_treatment_continuation_script",
        "description": (
            "Return all remaining treatment_explanation points from the active Confluence prompt. "
            "Use this before explaining treatment so the agent speaks the whole treatment flow continuously "
            "instead of stopping between points or jumping to closing. Pass confirmed_facts from the conversation "
            "so patient-specific lines are selected correctly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirmed_facts": {
                    "type": "string",
                    "description": (
                        "Compact confirmed patient facts relevant to treatment, for example "
                        "'fatty liver grade 2, SGPT high, gas/heaviness, no current medicine'."
                    ),
                },
                "selected_patient_specific_conditions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional condition labels from patient-specific logic that are confirmed. "
                        "Leave empty if unsure and use confirmed_facts instead."
                    ),
                },
            },
        },
    }
    mark_schema = {
        "name": "mark_treatment_point_complete",
        "description": "Fallback only. Mark one treatment_explanation point complete only after it was spoken.",
        "parameters": {
            "type": "object",
            "properties": {
                "point_id": {
                    "type": "string",
                    "enum": point_ids,
                    "description": "The treatment point id just spoken to the caller.",
                },
                "spoken_summary": {
                    "type": "string",
                    "description": "Short summary of what was just spoken to the caller for this treatment point.",
                },
            },
            "required": ["point_id", "spoken_summary"],
        },
    }
    mark_script_schema = {
        "name": "mark_treatment_script_complete",
        "description": "Mark the remaining treatment_explanation script points complete after the whole customer_script was spoken.",
        "parameters": {
            "type": "object",
            "properties": {
                "completed_point_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": point_ids},
                    "description": "The treatment point ids that were spoken to the caller from the continuation script.",
                },
                "spoken_summary": {
                    "type": "string",
                    "description": "Short summary of the treatment script spoken to the caller.",
                },
            },
            "required": ["completed_point_ids", "spoken_summary"],
        },
    }
    return [
        function_tool(get_treatment_continuation_script, raw_schema=continuation_schema),
        function_tool(mark_treatment_script_complete, raw_schema=mark_script_schema),
        function_tool(get_treatment_next_point, raw_schema=get_schema),
        function_tool(mark_treatment_point_complete, raw_schema=mark_schema),
    ]


def make_repeat_medicine_context_tools(dynamic_context: Optional[dict]):
    """Expose repeat-followup medicine source-of-truth tools from call metadata only.

    These tools are registered only for repeat_followup calls, so normal sales,
    order confirmation, inbound, and other agents are not affected.
    """
    context = dynamic_context if isinstance(dynamic_context, dict) else {}
    is_repeat_followup = bool(
        context.get("repeat_followup_compacted")
        or context.get("full_encounter_available_via_tool")
        or str(context.get("event") or "").strip().lower() == "repeat_followup"
    )
    medicines = repeat_medicine_items_from_context(context)
    if not is_repeat_followup or not medicines:
        return []

    from livekit.agents.llm import function_tool

    async def get_active_repeat_medicine_list(raw_arguments: dict[str, object]) -> str:
        return json.dumps(
            {
                "status": "success",
                "medicine_count": len(medicines),
                "medicine_names": [item.get("display_name") for item in medicines],
                "medicines": medicines,
                "required_medicine_script": context.get("required_medicine_script") or "",
                "instruction": (
                    "This is the ONLY medicine source of truth for this call. "
                    "Speak every medicine in order. Do not use sales KB, old call memory, or any unlisted medicine."
                ),
            },
            ensure_ascii=False,
            default=str,
        )

    async def verify_active_repeat_medicine(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        query = str(arguments.get("medicine_name") or arguments.get("query") or arguments.get("name") or "").strip()
        match = find_repeat_medicine_match(query, medicines)
        if match:
            return json.dumps(
                {
                    "status": "found",
                    "medicine_name": query,
                    "medicine": match,
                    "medicine_count": len(medicines),
                    "medicine_names": [item.get("display_name") for item in medicines],
                    "customer_safe_answer": (
                        f"Haan ji, {match.get('display_name')} aapki prescription list mein hai. "
                        f"Dosage {match.get('dosage') or 'clear nahi'}; instruction {match.get('instruction') or 'clear nahi'}; "
                        f"duration {match.get('period') or 'clear nahi'}."
                    ),
                },
                ensure_ascii=False,
                default=str,
            )
        return json.dumps(
            {
                "status": "not_found",
                "medicine_name": query,
                "medicine_count": len(medicines),
                "medicine_names": [item.get("display_name") for item in medicines],
                "customer_safe_answer": (
                    "Is exact naam se medicine current prescription list mein nahi dikh rahi. "
                    "Main medicine guess nahi karungi; team se verify karwa deti hoon."
                ),
            },
            ensure_ascii=False,
            default=str,
        )

    list_schema = {
        "name": "get_active_repeat_medicine_list",
        "description": "Repeat follow-up only: get the exact medicine list from this call's Patient Encounter metadata. Mandatory before speaking medicine names or dosage.",
        "parameters": {"type": "object", "properties": {}},
    }
    verify_schema = {
        "name": "verify_active_repeat_medicine",
        "description": "Repeat follow-up only: verify whether a customer-mentioned medicine exists in this call's Patient Encounter drug_prescription.",
        "parameters": {
            "type": "object",
            "properties": {
                "medicine_name": {"type": "string", "description": "Medicine name or partial name mentioned by the customer."}
            },
            "required": ["medicine_name"],
        },
    }
    return [
        function_tool(get_active_repeat_medicine_list, raw_schema=list_schema),
        function_tool(verify_active_repeat_medicine, raw_schema=verify_schema),
    ]


async def post_livekit_task_event(
    *,
    task_id: Optional[str],
    room_name: Optional[str],
    event: str,
    status: str,
    reason: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    if not task_id and not room_name:
        return

    url = livekit_webhook_url()
    if not url:
        logger.warning("LiveKit webhook URL is not configured; skipping callback.")
        return

    import aiohttp

    payload = {
        "event": event,
        "status": status,
        "task": task_id,
        "room_name": room_name,
        "room": room_name,
        "reason": reason,
    }
    if extra:
        payload.update(extra)

    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=build_mcp_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        logger.info("LiveKit callback posted: %s", payload)
                        return
                    logger.error(
                        "LiveKit callback failed attempt=%s HTTP %s: %s",
                        attempt,
                        response.status,
                        body[:500],
                    )
        except Exception as exc:
            logger.exception("Failed to post LiveKit callback attempt=%s: %s", attempt, exc)

        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())


async def call_confluence_mcp_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    task_id: Optional[str],
) -> dict[str, Any]:
    gateway_url = confluence_gateway_url()
    if not gateway_url:
        return {"status": "error", "message": "MCP_SERVER_URL is not configured."}

    import aiohttp

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    gateway_url,
                    json=payload,
                    headers=build_mcp_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            "Safety MCP %s failed attempt=%s HTTP %s: %s",
                            tool_name,
                            attempt,
                            response.status,
                            body[:500],
                        )
                    else:
                        parsed = unwrap_frappe_response(json.loads(body))
                        if parsed.get("error"):
                            logger.error("Safety MCP %s returned error: %s", tool_name, parsed["error"])
                            return {"status": "error", "message": parsed["error"].get("message", "Tool error")}
                        result = parsed.get("result", {})
                        record_mcp_tool_called(task_id, tool_name)
                        return result.get("data", result) if isinstance(result, dict) else {"status": "success", "result": result}
        except Exception as exc:
            logger.exception("Safety MCP %s connection error attempt=%s: %s", tool_name, attempt, exc)
        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())
    return {"status": "error", "message": "MCP connection failed."}


def is_repeat_followup_context(context: dict[str, Any]) -> bool:
    return bool(
        context.get("repeat_followup_compacted")
        or context.get("full_encounter_available_via_tool")
        or str(context.get("event") or "").strip().lower() == "repeat_followup"
    )


async def hydrate_repeat_runtime_routing(
    context: dict[str, Any],
    *,
    task_id: Optional[str],
) -> tuple[dict[str, Any], bool]:
    """Repeat follow-up uses the normal Gemini prompt-driven runtime."""
    return context, False


async def maybe_create_confirmed_sales_order(
    *,
    user_turns: list[str],
    task_id: Optional[str],
    room_name: Optional[str],
    dynamic_context: dict[str, Any],
    safety_state: dict[str, Any],
    trigger: str,
) -> None:
    if safety_state.get("draft_encounter_created"):
        return
    if safety_state.get("draft_encounter_in_progress"):
        return
    if os.getenv("ORDER_SAFETY_NET_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}:
        return
    if not task_id or not ORDER_SAFETY_MCP_TOOL:
        return
    if mcp_tool_was_called(task_id, ORDER_SAFETY_MCP_TOOL):
        safety_state["draft_encounter_created"] = True
        return
    runtime_stage_state = safety_state.get("runtime_stage_state") if isinstance(safety_state.get("runtime_stage_state"), dict) else {}
    current_stage = str(
        runtime_stage_state.get("current_stage")
        or dynamic_context.get("active_stage_id")
        or dynamic_context.get("current_stage")
        or ""
    ).strip()
    closing_address_trigger = current_stage == "closing_order" and address_details_detected(user_turns)
    if not order_consent_detected(user_turns) and not closing_address_trigger:
        return
    safety_state["draft_encounter_in_progress"] = True

    try:
        debounce_seconds = float(os.getenv("ORDER_SAFETY_NET_DEBOUNCE_SECONDS", "8") or "8")
        if trigger == "live_user_turn" and debounce_seconds > 0:
            await asyncio.sleep(debounce_seconds)

        latest_user_turns = list(safety_state.get("all_user_turns") or user_turns)
        current_stage = str(
            runtime_stage_state.get("current_stage")
            or dynamic_context.get("active_stage_id")
            or dynamic_context.get("current_stage")
            or ""
        ).strip()
        closing_address_trigger = current_stage == "closing_order" and address_details_detected(latest_user_turns)
        if not order_consent_detected(latest_user_turns) and not closing_address_trigger:
            return
        live_facts = extract_live_sales_facts(latest_user_turns, dynamic_context)
        if not live_facts.get("customer_name"):
            logger.info("Order safety-net waiting for live customer name before draft encounter task=%s", task_id)
            return

        phone = normalize_phone_for_context(
            live_facts.get("phone")
            or dynamic_context.get("customer_phone")
            or dynamic_context.get("phone")
            or dynamic_context.get("phone_number")
            or dynamic_context.get("raw_customer_phone"),
            prefer_ten_digit=True,
        )
        customer_name = live_facts.get("customer_name") or extract_probable_customer_name(latest_user_turns)
        concern = str(
            live_facts.get("disease_or_concern")
            or dynamic_context.get("disease_or_concern")
            or dynamic_context.get("concern")
            or dynamic_context.get("department")
            or "customer concern"
        ).strip() or "customer concern"
        customer_summary = compact_customer_turn_summary(latest_user_turns)
        summary = (
            "Auto safety-net created this draft because caller clearly confirmed order/start treatment, "
            "but the realtime model did not call the draft encounter MCP during the conversation.\n\n"
            f"Detected customer name: {customer_name}\n"
            f"Phone: {phone}\n"
            f"Concern: {concern}\n"
            f"Address/order details from customer turns:\n{customer_summary}"
        )
        args = {
            "customer_name": customer_name,
            "phone": phone,
            "disease_or_concern": concern,
            "summary": summary,
            "next_action": "Customer confirmed order/start treatment. Continue the configured order and prescription flow without promising a callback or handoff.",
            "address": live_facts.get("address") or "",
            "payment_mode": live_facts.get("payment_mode") or "",
            "location": live_facts.get("location") or "",
            "duration": live_facts.get("duration") or "",
            "symptoms": live_facts.get("symptoms") or "",
            "source_system": str(dynamic_context.get("source_system") or "LiveKit call"),
            "order_confirmed": True,
            "patient_agreed_to_proceed": True,
            "start_treatment_confirmed": True,
            "explicit_customer_consent": True,
            "closing_address_trigger": closing_address_trigger,
            "current_stage": current_stage,
        }
        default_item_code = os.getenv("ORDER_SAFETY_DEFAULT_ITEM_CODE", "").strip()
        if default_item_code and "items" not in args:
            args["items"] = [
                {
                    "sr_item_code": default_item_code,
                    "sr_item_name": os.getenv("ORDER_SAFETY_DEFAULT_ITEM_NAME", default_item_code).strip() or default_item_code,
                    "sr_item_uom": os.getenv("ORDER_SAFETY_DEFAULT_ITEM_UOM", "Nos").strip() or "Nos",
                    "sr_item_qty": int(os.getenv("ORDER_SAFETY_DEFAULT_ITEM_QTY", "1") or "1"),
                    "sr_item_rate": float(os.getenv("ORDER_SAFETY_DEFAULT_ITEM_RATE", "0") or "0"),
                }
            ]
        result = await call_confluence_mcp_tool(
            tool_name=ORDER_SAFETY_MCP_TOOL,
            arguments=args,
            task_id=task_id,
        )
        record_mcp_tool_called(task_id, ORDER_SAFETY_MCP_TOOL)
        safety_state["draft_encounter_created"] = True
        safety_state["draft_encounter_result"] = result
        logger.info("Order safety-net result trigger=%s task=%s result=%s", trigger, task_id, safe_tool_json(result, max_chars=1000))
        await post_livekit_task_event(
            task_id=task_id,
            room_name=room_name,
            event="agent_debug",
            status="order_safety_net_triggered",
            reason=trigger,
            extra={"tool": ORDER_SAFETY_MCP_TOOL, "result": result},
        )
    finally:
        safety_state["draft_encounter_in_progress"] = False


async def maybe_send_address_verification_whatsapp(
    *,
    user_turns: list[str],
    task_id: Optional[str],
    room_name: Optional[str],
    dynamic_context: dict[str, Any],
    safety_state: dict[str, Any],
    trigger: str,
) -> None:
    if safety_state.get("address_whatsapp_sent"):
        return
    if os.getenv("ADDRESS_VERIFICATION_SAFETY_NET_ENABLED", "1").strip().lower() not in {"1", "true", "yes"}:
        return
    if not task_id or not ADDRESS_VERIFICATION_MCP_TOOL:
        return
    if mcp_tool_was_called(task_id, ADDRESS_VERIFICATION_MCP_TOOL):
        safety_state["address_whatsapp_sent"] = True
        return
    runtime_stage_state = safety_state.get("runtime_stage_state") if isinstance(safety_state.get("runtime_stage_state"), dict) else {}
    current_stage = str(
        runtime_stage_state.get("current_stage")
        or dynamic_context.get("active_stage_id")
        or dynamic_context.get("current_stage")
        or ""
    ).strip()
    order_ready = (
        order_consent_detected(user_turns)
        or bool(safety_state.get("draft_encounter_created"))
        or bool(safety_state.get("draft_encounter_in_progress"))
    )
    if current_stage and current_stage != "closing_order" and not order_ready:
        return
    if not address_details_detected(user_turns):
        return

    phone = normalize_phone_for_context(
        dynamic_context.get("customer_phone")
        or dynamic_context.get("phone")
        or dynamic_context.get("phone_number")
        or dynamic_context.get("raw_customer_phone"),
        prefer_ten_digit=True,
    )
    if not phone:
        return

    address_summary = compact_address_turn_summary(user_turns)
    if not address_summary or not address_summary_customer_ready(address_summary):
        return

    customer_name = extract_probable_customer_name(user_turns)
    concern = str(
        dynamic_context.get("disease_or_concern")
        or dynamic_context.get("concern")
        or dynamic_context.get("department")
        or "customer concern"
    ).strip() or "customer concern"
    message = (
        "Ji, delivery/address verification ke liye maine ye details note ki hain: "
        f"{address_summary}. Kripya isi WhatsApp par confirm ya correction bhej dijiye."
    )
    args = {
        "phone": phone,
        "customer_name": customer_name if customer_name != "Unknown" else "",
        "customer_requested": True,
        "explicit_customer_request": True,
        "send_confirmed_by_customer": True,
        "intent": "Address Verification",
        "disease_or_concern": concern,
        "message": message,
        "details": message,
    }
    profile_key = str(dynamic_context.get("profile_key") or dynamic_context.get("whatsapp_profile_key") or "").strip()
    if profile_key:
        args["profile_key"] = profile_key

    result = await call_confluence_mcp_tool(
        tool_name=ADDRESS_VERIFICATION_MCP_TOOL,
        arguments=args,
        task_id=task_id,
    )
    record_mcp_tool_called(task_id, ADDRESS_VERIFICATION_MCP_TOOL)
    safety_state["address_whatsapp_result"] = result
    if mcp_result_indicates_success(result):
        safety_state["address_whatsapp_sent"] = True
    logger.info("Address verification safety-net result trigger=%s task=%s result=%s", trigger, task_id, safe_tool_json(result, max_chars=1000))
    await post_livekit_task_event(
        task_id=task_id,
        room_name=room_name,
        event="agent_debug",
        status="address_whatsapp_safety_net_triggered",
        reason=trigger,
        extra={"tool": ADDRESS_VERIFICATION_MCP_TOOL, "result": result},
    )


async def ensure_task_company(task_id: Optional[str], fallback_company: Optional[str] = None) -> None:
    if not task_id:
        return

    base_url = confluence_base_url()
    if not base_url:
        return

    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            task_url = f"{base_url.rstrip('/')}/api/resource/AI%20Task/{quote(task_id)}"
            async with session.get(
                task_url,
                headers=confluence_resource_headers(task_id),
                timeout=confluence_http_timeout(),
            ) as response:
                if response.status != 200:
                    logger.warning("Could not read task %s for company backfill HTTP %s", task_id, response.status)
                    return
                task = unwrap_frappe_response(await response.json()).get("data", {})

            if task.get("company"):
                return

            company = fallback_company or ""
            agent_name = task.get("assigned_agent") or task.get("target_agent") or ""
            if not company and agent_name:
                agent_url = f"{base_url.rstrip('/')}/api/resource/AI%20Agent/{quote(agent_name)}"
                async with session.get(
                    agent_url,
                    headers=confluence_resource_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    if response.status == 200:
                        agent = unwrap_frappe_response(await response.json()).get("data", {})
                        company = agent.get("company") or ""

            if not company:
                return

            async with session.put(
                task_url,
                json={"company": company},
                headers=confluence_resource_headers(task_id),
                timeout=confluence_http_timeout(),
            ) as response:
                body = await response.text()
                if response.status == 200:
                    logger.info("Backfilled task company task=%s company=%s", task_id, company)
                else:
                    logger.warning(
                        "Could not backfill task company task=%s company=%s HTTP %s: %s",
                        task_id,
                        company,
                        response.status,
                        body[:300],
                    )
    except Exception as exc:
        logger.exception("Task company backfill failed task=%s: %s", task_id, exc)


def make_mcp_forwarder(
    tool_name: str,
    gateway_url: str,
    task_id: Optional[str],
    dynamic_context: Optional[dict[str, Any]] = None,
):
    import aiohttp

    def normalized_phone(value: object) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        return digits[-10:] if len(digits) >= 10 else digits

    def dedupe_key(arguments: dict[str, object]) -> tuple[str, str, str]:
        phone = normalized_phone(
            arguments.get("phone")
            or arguments.get("customer_phone")
            or arguments.get("phone_number")
            or arguments.get("mobile")
            or ""
        )
        if tool_name == "send_mapped_whatsapp_template":
            message = str(arguments.get("message") or "").strip()
            template = str(arguments.get("template_name") or "").strip()
            intent = str(arguments.get("intent") or "").strip()
            payload_key = json.dumps(
                {"phone": phone, "message": message, "template": template, "intent": intent},
                sort_keys=True,
                default=str,
            )
            return (task_id or "", tool_name, payload_key)
        return (task_id or "", tool_name, json.dumps(arguments, sort_keys=True, default=str))

    def cleanup_cache(now: float) -> None:
        ttl = max(mcp_cache_seconds(), float(os.getenv("BACKGROUND_MCP_DEDUPE_SECONDS", "90") or "90"))
        for cache in (_MCP_RESULT_CACHE, _BACKGROUND_MCP_DEDUPE):
            for key, value in list(cache.items()):
                started_at = value[0] if isinstance(value, tuple) else value
                if now - started_at > ttl:
                    cache.pop(key, None)

    async def call_gateway(payload: dict, headers: dict[str, str]) -> str:
        import asyncio

        for attempt in range(1, confluence_http_attempts() + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        gateway_url,
                        json=payload,
                        headers=headers,
                        timeout=confluence_http_timeout(),
                    ) as response:
                        body = await response.text()
                        if response.status != 200:
                            logger.error(
                                "MCP tool %s failed attempt=%s HTTP %s: %s",
                                tool_name,
                                attempt,
                                response.status,
                                body[:500],
                            )
                        else:
                            parsed = unwrap_frappe_response(json.loads(body))
                            if parsed.get("error"):
                                logger.error("MCP tool %s returned error: %s", tool_name, parsed["error"])
                                return json.dumps(
                                    {
                                        "status": "error",
                                        "message": parsed["error"].get("message", "Tool returned an error."),
                                    }
                                )
                            result = parsed.get("result", {})
                            if tool_name == "get_repeat_encounter_full_data":
                                max_chars = int(os.getenv("FULL_ENCOUNTER_TOOL_MAX_CHARS", "20000") or "20000")
                                return full_tool_json(result.get("data", result), max_chars=max_chars)
                            return safe_tool_json(result.get("data", result))
            except Exception as exc:
                logger.exception("MCP tool %s connection error attempt=%s: %s", tool_name, attempt, exc)

            if attempt < confluence_http_attempts():
                await asyncio.sleep(confluence_retry_delay())

        return json.dumps(
            {
                "status": "error",
                "message": "Tool connection failed. Continue naturally and note it for the team if needed.",
            }
        )

    async def run_background(payload: dict, headers: dict[str, str], key: tuple[str, str, str]) -> None:
        result = await call_gateway(payload, headers)
        record_mcp_tool_called(task_id, tool_name)
        logger.info("Background MCP tool %s completed: %s", tool_name, result[:500])

    async def forwarder(raw_arguments: dict[str, object]) -> str:
        import asyncio

        arguments = normalize_json_string_children(dict(raw_arguments or {}))
        context = dynamic_context if isinstance(dynamic_context, dict) else {}
        if mcp_tool_was_called(task_id, tool_name) and (
            is_draft_record_tool(tool_name)
        ):
            logger.info("Suppressing duplicate MCP tool call %s for task=%s", tool_name, task_id)
            return json.dumps({"status": "duplicate_suppressed"})
        if is_draft_record_tool(tool_name) and not any(
            truthy_argument(arguments.get(key))
            for key in (
                "customer_consented",
                "order_confirmed",
                "patient_agreed_to_proceed",
                "start_treatment_confirmed",
                "explicit_customer_consent",
            )
        ):
            logger.warning(
                "Blocked draft record MCP tool %s because clear consent flag was missing. keys=%s",
                tool_name,
                sorted(arguments.keys()),
            )
            return json.dumps(
                {
                    "status": "blocked",
                    "message": (
                        "Draft record blocked because clear customer consent was not confirmed. "
                        "Continue the active flow, confirm consent and required details first."
                    ),
                }
            )
        if is_draft_record_tool(tool_name) and not str(arguments.get("patient") or "").strip():
            patient = str(
                context.get("patient")
                or context.get("patient_id")
                or context.get("sr_patient")
                or ""
            ).strip()
            lookup_tool = str(context.get("_startup_customer_check_tool") or "").strip()
            phone = (
                arguments.get("phone")
                or arguments.get("customer_phone")
                or arguments.get("phone_number")
                or context.get("customer_phone")
                or context.get("phone")
                or context.get("phone_number")
            )
            if not patient and lookup_tool and phone:
                lookup_result = await call_confluence_mcp_tool(
                    tool_name=lookup_tool,
                    arguments={"phone": normalize_phone_for_context(phone, prefer_ten_digit=True)},
                    task_id=task_id,
                )
                patient = extract_patient_from_tool_result(lookup_result)
                if patient:
                    context.setdefault("patient", patient)
                    context.setdefault("patient_id", patient)
            if patient:
                arguments["patient"] = patient
        if is_whatsapp_tool(tool_name) and not any(
            truthy_argument(arguments.get(key))
            for key in (
                "customer_requested",
                "customer_asked",
                "customer_consented",
                "explicit_customer_request",
                "send_confirmed_by_customer",
            )
        ):
            logger.warning(
                "Blocked WhatsApp MCP tool %s because customer_requested flag was missing. keys=%s",
                tool_name,
                sorted(arguments.keys()),
            )
            return json.dumps(
                {
                    "status": "blocked",
                    "message": "WhatsApp send blocked because customer did not explicitly request or consent. Ask the customer first.",
                }
            )
        if is_whatsapp_tool(tool_name):
            if tool_name == "send_mapped_whatsapp_template":
                arguments = normalize_mapped_whatsapp_arguments(arguments)
            validation_error = validate_whatsapp_message_argument(arguments)
            if validation_error:
                repaired_message = build_customer_ready_whatsapp_message(arguments)
                arguments["message"] = repaired_message
                arguments.setdefault("details", repaired_message)
                validation_error = validate_whatsapp_message_argument(arguments)
            if validation_error:
                logger.warning(
                    "Blocked WhatsApp MCP tool %s because message was not customer-ready after repair: %s keys=%s",
                    tool_name,
                    validation_error,
                    sorted(arguments.keys()),
                )
                return json.dumps(
                    {
                        "status": "blocked",
                        "message": validation_error,
                    }
                )
            logger.info(
                "WhatsApp MCP tool %s using customer-ready message chars=%s",
                tool_name,
                len(extract_whatsapp_message_argument(arguments)),
            )
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        headers = build_mcp_headers(task_id)
        logger.info("Forwarding dynamic MCP tool call %s with keys=%s", tool_name, sorted(arguments.keys()))
        key = dedupe_key(arguments)
        now = time.monotonic()
        cleanup_cache(now)
        draft_record_tool = is_draft_record_tool(tool_name)
        if draft_record_tool:
            record_mcp_tool_called(task_id, tool_name)

        if is_background_mcp_tool(tool_name) and not draft_record_tool:
            dedupe_seconds = float(os.getenv("BACKGROUND_MCP_DEDUPE_SECONDS", "90") or "90")
            last_started = _BACKGROUND_MCP_DEDUPE.get(key)
            if last_started and now - last_started < dedupe_seconds:
                logger.info("Skipping duplicate background MCP tool call %s key=%s", tool_name, key)
                return json.dumps({"status": "duplicate_suppressed"})
            _BACKGROUND_MCP_DEDUPE[key] = now
            asyncio.create_task(run_background(payload, headers, key))
            return json.dumps({"status": "queued_background", "message": "Tool queued once; do not call this same tool again for the same details."})

        if not is_stateful_mcp_tool(tool_name):
            cached = _MCP_RESULT_CACHE.get(key)
            if cached and now - cached[0] < mcp_cache_seconds():
                logger.info("Returning cached MCP tool result %s key=%s", tool_name, key)
                return cached[1]
        else:
            logger.info("Bypassing MCP cache for stateful tool %s", tool_name)

        result = await call_gateway(payload, headers)
        record_mcp_tool_called(task_id, tool_name)
        if not is_stateful_mcp_tool(tool_name):
            _MCP_RESULT_CACHE[key] = (now, result)
        return result

    return forwarder


def prompt_allows_tool(tool_name: str, system_prompt: Optional[str]) -> bool:
    if not system_prompt:
        return True
    return tool_name.strip() in system_prompt


async def fetch_agent_mcp_rule_map(
    *,
    agent: Optional[str],
    task_id: Optional[str],
) -> dict[str, dict[str, Any]]:
    if not agent:
        return {}
    base_url = confluence_base_url()
    if not base_url:
        return {}

    import aiohttp

    rules: dict[str, dict[str, Any]] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url.rstrip('/')}/api/resource/AI%20Agent/{quote(str(agent))}",
                headers=confluence_resource_headers(task_id),
                timeout=confluence_http_timeout(),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.warning("Could not fetch agent MCP rules agent=%s HTTP %s: %s", agent, response.status, body[:300])
                    return {}
                doc = unwrap_frappe_response(json.loads(body)).get("data", {})

            for row in doc.get("allowed_mcp_tools") or []:
                if not isinstance(row, dict):
                    continue
                tool_id = str(row.get("tool") or "").strip()
                tool_name = str(row.get("tool_name") or "").strip()
                expected_json = str(row.get("expected_json") or "").strip()
                if not tool_name and tool_id:
                    async with session.get(
                        f"{base_url.rstrip('/')}/api/resource/AI%20MCP%20Tool/{quote(tool_id)}",
                        headers=confluence_resource_headers(task_id),
                        timeout=confluence_http_timeout(),
                    ) as response:
                        body = await response.text()
                        if response.status == 200:
                            tool_doc = unwrap_frappe_response(json.loads(body)).get("data", {})
                            tool_name = str(tool_doc.get("tool_name") or "").strip()
                            expected_json = expected_json or str(tool_doc.get("expected_json") or "").strip()
                        else:
                            logger.warning("Could not fetch MCP tool name tool=%s HTTP %s: %s", tool_id, response.status, body[:300])
                if not tool_name:
                    continue
                rules[tool_name] = {
                    "tool_id": tool_id,
                    "calling_condition": str(row.get("calling_condition") or "").strip(),
                    "expected_json": expected_json,
                }
    except Exception as exc:
        logger.exception("Failed to fetch agent MCP rules agent=%s: %s", agent, exc)
    return rules


async def fetch_dynamic_mcp_tool_specs(
    task_id: Optional[str],
    system_prompt: Optional[str] = None,
    *,
    agent: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch route-scoped MCP tool specs from Confluence."""
    if not task_id:
        logger.info("No Confluence task id found; not loading dynamic MCP tools.")
        return []

    gateway_url = confluence_gateway_url()
    if not gateway_url:
        logger.warning("MCP_SERVER_URL is not configured; no dynamic MCP tools loaded.")
        return []

    import aiohttp
    import asyncio

    rule_map = await fetch_agent_mcp_rule_map(agent=agent, task_id=task_id)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"task_id": task_id},
    }

    for attempt in range(1, confluence_http_attempts() + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    gateway_url,
                    json=payload,
                    headers=build_mcp_headers(task_id),
                    timeout=confluence_http_timeout(),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            "MCP tools/list failed attempt=%s HTTP %s: %s",
                            attempt,
                            response.status,
                            body[:500],
                        )
                    else:
                        parsed = unwrap_frappe_response(json.loads(body))
                        if parsed.get("error"):
                            logger.error("MCP tools/list returned error: %s", parsed["error"])
                            return []

                        tool_specs: list[dict[str, Any]] = []
                        for tool in parsed.get("result", {}).get("tools", []):
                            tool_name = tool.get("name")
                            if not tool_name:
                                continue
                            if tool_name not in rule_map and not prompt_allows_tool(tool_name, system_prompt):
                                logger.info("Skipping MCP tool not mentioned in prompt: %s", tool_name)
                                continue
                            spec = dict(tool)
                            spec["name"] = str(tool_name)
                            spec.setdefault("inputSchema", {"type": "object", "properties": {}, "required": []})
                            if tool_name in rule_map:
                                spec.update(rule_map[tool_name])
                            tool_specs.append(spec)
                            logger.info(
                                "Loaded dynamic MCP tool spec: %s has_condition=%s",
                                tool_name,
                                bool(spec.get("calling_condition")),
                            )
                        return tool_specs
        except Exception as exc:
            logger.exception("Failed to fetch dynamic MCP tools attempt=%s: %s", attempt, exc)

        if attempt < confluence_http_attempts():
            await asyncio.sleep(confluence_retry_delay())

    return []


def build_dynamic_mcp_tools(
    tool_specs: list[dict[str, Any]],
    task_id: Optional[str],
    dynamic_context: Optional[dict[str, Any]] = None,
) -> list:
    from livekit.agents.llm import function_tool

    gateway_url = confluence_gateway_url()
    if not gateway_url:
        return []

    registered_tools = []
    for tool in tool_specs:
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            continue
        input_schema = enrich_schema_from_expected_json(
            tool.get("inputSchema", {"type": "object", "properties": {}, "required": []}),
            tool.get("expected_json"),
        )
        raw_schema = {
            "name": tool_name,
            "description": tool.get("description", ""),
            "parameters": input_schema,
        }
        if is_whatsapp_tool(tool_name):
            parameters = raw_schema.setdefault(
                "parameters",
                {"type": "object", "properties": {}, "required": []},
            )
            properties = parameters.setdefault("properties", {})
            properties["customer_requested"] = {
                "type": "boolean",
                "description": "Set true only when the customer explicitly asked or agreed to receive this WhatsApp message.",
            }
            raw_schema["description"] = (
                str(raw_schema.get("description") or "")
                + " Only call after explicit customer request/consent and pass customer_requested=true."
            ).strip()
        registered_tools.append(
            function_tool(
                make_mcp_forwarder(tool_name, gateway_url, task_id, dynamic_context),
                raw_schema=raw_schema,
            )
        )
        logger.info("Registered dynamic MCP tool: %s", tool_name)
    return registered_tools


async def fetch_dynamic_mcp_tools(
    task_id: Optional[str],
    system_prompt: Optional[str] = None,
    *,
    agent: Optional[str] = None,
) -> list:
    tool_specs = await fetch_dynamic_mcp_tool_specs(task_id, system_prompt, agent=agent)
    return build_dynamic_mcp_tools(tool_specs, task_id)


def mcp_condition_autocall_enabled() -> bool:
    return os.getenv("MCP_CONDITION_AUTOCALL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def is_startup_customer_check_tool(tool_spec: dict[str, Any]) -> bool:
    name = str(tool_spec.get("name") or "").strip().lower().replace("-", "_")
    text = " ".join(
        str(tool_spec.get(key) or "")
        for key in ("name", "description", "calling_condition")
    ).lower()
    positive = (
        "check_customer",
        "customer_check",
        "check customer",
        "find_patient",
        "find patient",
        "lookup_patient",
        "lookup patient",
        "patient context",
        "customer context",
        "sales context",
    )
    negative = (
        "create",
        "send",
        "whatsapp",
        "template",
        "update",
        "change",
        "draft",
        "order",
        "delete",
    )
    return any(token in name or token in text for token in positive) and not any(token in name for token in negative)


def build_contextual_mcp_arguments(
    tool_spec: dict[str, Any],
    *,
    user_turns: list[str],
    dynamic_context: dict[str, Any],
    supplied_arguments: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    args = dict(supplied_arguments or {})
    properties = mcp_schema_properties(tool_spec)
    required = set(mcp_schema_required_fields(tool_spec))
    known_keys = set(properties.keys()) | required | set(args.keys())
    tool_name = str(tool_spec.get("name") or "")

    phone = normalize_phone_for_context(
        dynamic_context.get("customer_phone")
        or dynamic_context.get("phone")
        or dynamic_context.get("phone_number")
        or dynamic_context.get("raw_customer_phone"),
        prefer_ten_digit=True,
    )
    concern = str(
        dynamic_context.get("disease_or_concern")
        or dynamic_context.get("concern")
        or dynamic_context.get("department")
        or "customer concern"
    ).strip() or "customer concern"
    customer_name = extract_probable_customer_name(user_turns)
    summary = compact_customer_turn_summary(user_turns)
    normalized_summary = normalize_hinglish_text(summary)
    live_facts = extract_live_sales_facts(user_turns, dynamic_context)
    draft_tool = is_draft_record_tool(tool_name)

    if draft_tool:
        for field in (
            "customer_name",
            "phone",
            "disease_or_concern",
            "summary",
            "address",
            "payment_mode",
            "location",
            "duration",
            "symptoms",
        ):
            value = live_facts.get(field)
            if value:
                args[field] = value
        if not live_facts.get("customer_name"):
            args.pop("customer_name", None)

    for key in known_keys:
        if args.get(key) not in (None, ""):
            continue
        normalized_key = key.strip().lower()
        if normalized_key in {"phone", "customer_phone", "phone_number", "mobile", "mobile_no", "whatsapp", "whatsapp_number"}:
            args[key] = phone
        elif normalized_key in {"company", "company_key", "tenant"}:
            args[key] = dynamic_context.get("company") or dynamic_context.get("company_key") or ""
        elif normalized_key in {"customer_name", "patient_name", "name", "full_name"}:
            args[key] = live_facts.get("customer_name") if draft_tool else customer_name
        elif normalized_key in {"disease_or_concern", "concern", "department", "disease"}:
            args[key] = concern
        elif normalized_key in {"summary", "notes", "note", "description", "call_summary"}:
            args[key] = summary
        elif normalized_key in {"source_system", "source"}:
            args[key] = dynamic_context.get("source_system") or "LiveKit call"
        elif normalized_key in {"next_action", "action"}:
            args[key] = "Follow the configured Confluence MCP action for this customer."
        elif normalized_key in {"address", "delivery_address"}:
            args[key] = live_facts.get("address") or (compact_address_turn_summary(user_turns) if address_details_detected(user_turns) else "")
        elif normalized_key in {"payment_mode", "payment", "payment_preference"}:
            args[key] = live_facts.get("payment_mode") or ("Cash on Delivery" if "cod" in normalized_summary or "cash" in normalized_summary else "")
        elif normalized_key in {"age", "patient_age"}:
            args[key] = dynamic_context.get("age") or dynamic_context.get("patient_age") or ""
        elif normalized_key in {"location", "city"}:
            args[key] = live_facts.get("location") or dynamic_context.get("location") or dynamic_context.get("city") or ""

    if draft_tool:
        args.setdefault("customer_consented", True)
        args.setdefault("order_confirmed", True)
        args.setdefault("patient_agreed_to_proceed", True)
        args.setdefault("start_treatment_confirmed", True)
        args.setdefault("explicit_customer_consent", True)
        args.setdefault("summary", live_facts.get("summary") or summary)
        args.setdefault("phone", live_facts.get("phone") or phone)
        if live_facts.get("customer_name"):
            args.setdefault("customer_name", live_facts["customer_name"])
        args.setdefault("disease_or_concern", live_facts.get("disease_or_concern") or concern)
        default_item_code = os.getenv("ORDER_SAFETY_DEFAULT_ITEM_CODE", "").strip()
        if default_item_code and "items" in properties and "items" not in args:
            args["items"] = [
                {
                    "sr_item_code": default_item_code,
                    "sr_item_name": os.getenv("ORDER_SAFETY_DEFAULT_ITEM_NAME", default_item_code).strip() or default_item_code,
                    "sr_item_uom": os.getenv("ORDER_SAFETY_DEFAULT_ITEM_UOM", "Nos").strip() or "Nos",
                    "sr_item_qty": int(os.getenv("ORDER_SAFETY_DEFAULT_ITEM_QTY", "1") or "1"),
                    "sr_item_rate": float(os.getenv("ORDER_SAFETY_DEFAULT_ITEM_RATE", "0") or "0"),
                }
            ]

    if is_whatsapp_tool(tool_name):
        args.setdefault("customer_requested", True)
        args.setdefault("explicit_customer_request", True)
        if tool_name == "send_mapped_whatsapp_template":
            args = normalize_mapped_whatsapp_arguments(args)

    return args


def missing_required_mcp_arguments(tool_spec: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in mcp_schema_required_fields(tool_spec):
        value = arguments.get(key)
        if value in (None, "", [], {}):
            missing.append(key)
            continue
        if key.strip().lower() in {"customer_name", "patient_name", "name", "full_name"}:
            if str(value or "").strip().lower() in {"unknown", "na", "n/a", "not known"}:
                missing.append(key)
    return missing


def compact_mcp_specs_for_autocall(tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for spec in tool_specs:
        tool_name = str(spec.get("name") or "").strip()
        if not tool_name:
            continue
        condition = str(spec.get("calling_condition") or "").strip()
        if not condition:
            continue
        compacted.append(
            {
                "name": tool_name,
                "description": str(spec.get("description") or "")[:500],
                "calling_condition": condition[:900],
                "required": mcp_schema_required_fields(spec),
                "properties": list(mcp_schema_properties(spec).keys())[:30],
            }
        )
    return compacted


async def assess_mcp_autocalls(
    *,
    user_turns: list[str],
    dynamic_context: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    task_id: Optional[str],
    room_name: Optional[str],
) -> list[dict[str, Any]]:
    candidates = [
        spec for spec in tool_specs
        if str(spec.get("calling_condition") or "").strip()
        and not mcp_tool_was_called(task_id, spec.get("name"))
    ]
    if not candidates:
        return []

    model_name = os.getenv("MCP_CONDITION_AUTOCALL_MODEL", os.getenv("CLASSIFIER_MODEL", "gemini-2.5-flash"))
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    recent_text = "\n".join(f"- {turn}" for turn in user_turns[-10:] if str(turn).strip())
    context_preview = json.dumps(dynamic_context or {}, ensure_ascii=False, default=str)[:1200]
    tool_preview = json.dumps(compact_mcp_specs_for_autocall(candidates), ensure_ascii=False, default=str)
    prompt = f"""
You are a strict MCP action matcher for a live voice call.
Your only job is to decide whether any attached Confluence MCP tool should be called NOW.

Rules:
- Use only the attached tool calling_condition, required fields, recent final user turns, and context.
- Call a tool only if the latest user turn and recent context clearly satisfy that tool's calling_condition.
- Do not call tools for vague acknowledgement like haan/ok unless the recent context is a direct confirmation for that exact action.
- Do not call write/action tools without clear customer consent/request.
- Do not call WhatsApp/send tools unless the customer explicitly asked/agreed to receive that exact message.
- If required data is missing, do not call the tool.
- Return at most 2 calls.
- Return only JSON.

Task: {task_id or ""}
Room: {room_name or ""}

Context:
{context_preview}

Attached MCP tools:
{tool_preview}

Recent final user turns:
{recent_text}

Return JSON schema:
{{"calls":[{{"tool_name":"exact tool name","confidence":0.0,"arguments":{{}},"reason":"short reason"}}]}}
""".strip()

    def generate() -> str:
        client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=location,
        )
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        return getattr(response, "text", "") or ""

    try:
        result_text = await asyncio.to_thread(generate)
        parsed = json.loads(result_text or "{}")
        calls = parsed.get("calls") if isinstance(parsed, dict) else []
        return [call for call in calls or [] if isinstance(call, dict)]
    except Exception as exc:
        logger.exception("MCP condition autocall assessment failed: %s", exc)
        return []


async def maybe_autocall_mcp_from_conditions(
    *,
    user_turns: list[str],
    task_id: Optional[str],
    room_name: Optional[str],
    dynamic_context: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    safety_state: dict[str, Any],
    trigger: str,
) -> None:
    if not mcp_condition_autocall_enabled() or not task_id or not tool_specs:
        return
    if safety_state.get("mcp_condition_autocall_in_progress"):
        return
    safety_state["mcp_condition_autocall_in_progress"] = True
    try:
        grace = float(os.getenv("MCP_CONDITION_AUTOCALL_GRACE_SECONDS", "0.8") or "0.8")
        if grace > 0:
            await asyncio.sleep(grace)
        min_confidence = float(os.getenv("MCP_CONDITION_AUTOCALL_MIN_CONFIDENCE", "0.84") or "0.84")
        max_calls = int(os.getenv("MCP_CONDITION_AUTOCALL_MAX_CALLS_PER_TURN", "2") or "2")
        spec_by_name = {str(spec.get("name") or ""): spec for spec in tool_specs}
        selected = await assess_mcp_autocalls(
            user_turns=user_turns,
            dynamic_context=dynamic_context,
            tool_specs=tool_specs,
            task_id=task_id,
            room_name=room_name,
        )
        fired = 0
        for call in selected:
            if fired >= max_calls:
                break
            tool_name = str(call.get("tool_name") or "").strip()
            spec = spec_by_name.get(tool_name)
            if not spec or mcp_tool_was_called(task_id, tool_name):
                continue
            if is_draft_record_tool(tool_name) and safety_state.get("draft_encounter_created"):
                logger.info("Skipping draft MCP autocall %s because a draft record was already created.", tool_name)
                continue
            try:
                confidence = float(call.get("confidence") or 0)
            except Exception:
                confidence = 0.0
            if confidence < min_confidence:
                logger.info("Skipping MCP autocall %s because confidence %.2f < %.2f", tool_name, confidence, min_confidence)
                continue
            arguments = build_contextual_mcp_arguments(
                spec,
                user_turns=user_turns,
                dynamic_context=dynamic_context,
                supplied_arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
            )
            if is_whatsapp_tool(tool_name) and validate_whatsapp_message_argument(arguments):
                logger.info("Skipping WhatsApp MCP autocall %s because message is not customer-ready.", tool_name)
                continue
            missing = missing_required_mcp_arguments(spec, arguments)
            if missing:
                logger.info("Skipping MCP autocall %s because required args are missing: %s", tool_name, missing)
                continue
            result = await call_confluence_mcp_tool(tool_name=tool_name, arguments=arguments, task_id=task_id)
            record_mcp_tool_called(task_id, tool_name)
            if is_draft_record_tool(tool_name):
                safety_state["draft_encounter_created"] = True
                safety_state["draft_encounter_result"] = result
            fired += 1
            logger.info(
                "MCP condition autocall fired tool=%s trigger=%s confidence=%.2f result=%s",
                tool_name,
                trigger,
                confidence,
                safe_tool_json(result, max_chars=1000),
            )
            await post_livekit_task_event(
                task_id=task_id,
                room_name=room_name,
                event="agent_debug",
                status="mcp_condition_autocall_triggered",
                reason=trigger,
                extra={"tool": tool_name, "confidence": confidence, "result": result},
            )
    finally:
        safety_state["mcp_condition_autocall_in_progress"] = False


async def maybe_run_startup_customer_check_mcp(
    *,
    task_id: Optional[str],
    room_name: Optional[str],
    dynamic_context: dict[str, Any],
    tool_specs: list[dict[str, Any]],
) -> None:
    if os.getenv("STARTUP_CUSTOMER_CHECK_MCP_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if not task_id:
        return
    for spec in tool_specs:
        tool_name = str(spec.get("name") or "").strip()
        if not is_startup_customer_check_tool(spec) or mcp_tool_was_called(task_id, tool_name):
            continue
        args = build_contextual_mcp_arguments(spec, user_turns=[], dynamic_context=dynamic_context)
        missing = missing_required_mcp_arguments(spec, args)
        if missing:
            logger.info("Skipping startup customer-check MCP %s because required args are missing: %s", tool_name, missing)
            continue
        result = await call_confluence_mcp_tool(tool_name=tool_name, arguments=args, task_id=task_id)
        record_mcp_tool_called(task_id, tool_name)
        dynamic_context["_startup_customer_check_tool"] = tool_name
        dynamic_context.setdefault("startup_mcp_results", {})[tool_name] = result
        patient = extract_patient_from_tool_result(result)
        if patient:
            dynamic_context.setdefault("patient", patient)
            dynamic_context.setdefault("patient_id", patient)
        logger.info("Startup customer-check MCP fired tool=%s result=%s", tool_name, safe_tool_json(result, max_chars=1000))
        await post_livekit_task_event(
            task_id=task_id,
            room_name=room_name,
            event="agent_debug",
            status="startup_customer_check_mcp_triggered",
            reason="call_start",
            extra={"tool": tool_name, "result": result},
        )
        return


class DeterministicIntakeController:
    """Deterministic intake-only controller for the single-stage SRIAAS test flow."""

    FINAL_LINE = "Ji, maine aapki history complete note kar li hai."
    AFTER_FINAL_LINE = "Ji, is stage mein sirf history complete karni thi."
    PRICE_LINE = "Initial one-month treatment ka cost generally 5000 se 6000 ke range mein hota hai."
    DEFERRAL_LINE = "Iska exact answer history complete hone ke baad hi sahi hoga. Main pehle case ki basic picture complete kar leti hoon."

    FIELD_ORDER = (
        "self_or_family",
        "relation",
        "patient_name",
        "patient_age",
        "city",
        "height_weight",
        "reports_available",
        "doctor_finding",
        "weight_change",
        "main_problem",
        "duration_progression",
        "symptoms",
        "current_treatment",
        "treatment_response",
        "major_illness",
        "hospitalization",
        "lifestyle",
        "main_expectation",
    )

    RELATION_WORDS = {
        "father": "father",
        "pitaji": "father",
        "pita": "father",
        "mother": "mother",
        "mata": "mother",
        "wife": "wife",
        "husband": "husband",
        "brother": "brother",
        "sister": "sister",
        "son": "son",
        "daughter": "daughter",
        "uncle": "uncle",
        "aunt": "aunt",
        "friend": "friend",
        "bhai": "brother",
        "behen": "sister",
        "beta": "son",
        "beti": "daughter",
        "papa": "father",
        "mummy": "mother",
    }

    SIDE_QUESTION_PATTERN = re.compile(
        r"\b(treatment|dawai|medicine|ayurved|safe|safety|kidney|transplant|side effect|herb|result|reverse|cure|doctor|team|order|start|cod|payment|diet|alcohol|sharab|next step|aage|process)\b",
        re.I,
    )
    PRICE_PATTERN = re.compile(r"\b(price|cost|kitna|paise|payment|cod|discount|prepaid|charge)\b", re.I)

    def __init__(self, *, caller_phone: Optional[str] = None) -> None:
        self.caller_phone = caller_phone or ""
        self.fields: dict[str, str] = {}
        self.summary_asked = False
        self.final_spoken = False
        self.last_reply = ""

    def active(self) -> bool:
        return True

    def ingest_user(self, text: str) -> None:
        clean = self._clean(text)
        if not clean:
            return
        if self.final_spoken:
            return
        if self.summary_asked:
            if self._is_confirmation(clean):
                self.fields["summary_confirmed"] = "yes"
            elif self._is_correction(clean):
                self.fields["summary_correction"] = clean
                self.summary_asked = False
            return
        pending_before = self.next_missing_field()
        self._extract_obvious_details(clean)
        pending_after = self.next_missing_field()
        if pending_after and pending_after == pending_before:
            self._capture_pending_field(pending_after, clean)
            self._normalize_dependent_fields()

    def next_reply(self) -> str:
        if self.final_spoken:
            return self.AFTER_FINAL_LINE
        if self.summary_asked:
            if self.fields.get("summary_confirmed"):
                self.final_spoken = True
                return self.final_summary_reply()
            if self.fields.get("summary_correction"):
                correction = self.fields.pop("summary_correction")
                self.summary_asked = True
                return f"Ji, main correct kar leti hoon: {correction}. Kya ab summary sahi hai ji?"
            return "Kya maine aapka case sahi samjha? Ismein kuch add ya correct karna chahenge?"
        missing = self.next_missing_field()
        if missing:
            return self.question_for(missing)
        self.summary_asked = True
        return self.summary_question()

    def forced_side_reply(self, text: str) -> Optional[str]:
        clean = self._clean(text)
        if not clean or self.final_spoken or self.summary_asked:
            return None
        if self.PRICE_PATTERN.search(clean):
            return f"{self.PRICE_LINE} {self.question_for(self.next_missing_field())}"
        if self.SIDE_QUESTION_PATTERN.search(clean):
            return f"{self.DEFERRAL_LINE} {self.question_for(self.next_missing_field())}"
        return None

    def next_missing_field(self) -> Optional[str]:
        if self.fields.get("self_or_family") == "self":
            skip = {"relation"}
        else:
            skip = set()
        if self.fields.get("current_treatment", "").lower() in {"no", "none", "not applicable", "nahi"}:
            self.fields.setdefault("treatment_response", "not applicable")
        for field in self.FIELD_ORDER:
            if field in skip:
                continue
            if field == "relation" and self.fields.get("self_or_family") != "family":
                continue
            if field == "treatment_response" and self.fields.get("treatment_response"):
                continue
            if not self.fields.get(field):
                return field
        return None

    def question_for(self, field: Optional[str]) -> str:
        if not field:
            return self.summary_question()
        family = self.fields.get("self_or_family") == "family"
        patient = self.fields.get("patient_name") or "patient"
        if field == "self_or_family":
            return "Namaskar ji. Main Vaani, Sriaas Ayurvedic Hospital se bol rahi hoon. Aapne liver ke liye enquiry ki thi. Ye enquiry aapke liye hai ya family member ke liye?"
        if field == "relation":
            return "Ji bilkul. Aap patient ke kya lagte hain?"
        if field == "patient_name":
            return "Patient ka naam please bata dijiye." if family else "Thank you ji confirm karne ke liye. History note karne ke liye, aapka full name aur age kya hai?"
        if field == "patient_age":
            return "Unki age kitni hai?" if family else f"{patient} ji, age kitni hai?"
        if field == "city":
            return "Aap kis city se baat kar rahe hain?"
        if field == "height_weight":
            return "Patient ki approximate height aur weight kya hai ji? Exact nahi pata ho toh not available bol sakte hain." if family else f"{patient} ji, aapki approximate height aur weight kya hai? Exact nahi pata ho toh not available bol sakte hain."
        if field == "reports_available":
            return "Kya patient ke paas liver reports available hain, jaise Ultrasound, FibroScan, LFT, CT/MRI, biopsy, discharge summary, ya prescription?"
        if field == "doctor_finding":
            return "Doctor ne exact diagnosis kya bataya tha?"
        if field == "weight_change":
            return "Pichle 6 mahino mein weight kam hua hai, badha hai, ya lagbhag same hai?"
        if field == "main_problem":
            return "Patient ko liver se related abhi sabse zyada kya problem ya concern face ho raha hai?" if family else "Liver se related abhi aapko sabse zyada kya problem ya concern face ho raha hai?"
        if field == "duration_progression":
            return "Ye symptoms kitne samay se chal rahe hain? Aur tab se problem badh rahi hai, kam ho rahi hai, ya lagbhag same hai?"
        if field == "symptoms":
            return "Iske alawa bhook kam lagna, weakness, pet mein pain/sujan, vomiting, jaundice, itching, digestion issue, confusion, zyada neend, ya weight loss jaisi koi problem hai?"
        if field == "current_treatment":
            return "Is problem ke liye pehle ya abhi koi treatment chal raha hai? Agar haan, toh kaunsi medicines le rahe hain?"
        if field == "treatment_response":
            return "Us treatment ya medicines se ab tak kitna farq mehsoos hua?"
        if field == "major_illness":
            return "Kya diabetes, BP, thyroid, kidney problem, heart problem, cancer ya kisi aur serious illness ki history hai?"
        if field == "hospitalization":
            return "Kya pehle kabhi liver ki wajah se hospital mein admit hona pada tha?"
        if field == "lifestyle":
            return "Alcohol ya smoking, tobacco, gutkha ya kisi aur nasha ki koi history rahi hai?"
        if field == "main_expectation":
            return "Aapki main expectation kya hai ji - reports samajhna, treatment option dekhna, symptoms control karna, ya liver condition improve karna?"
        return "Ji, next detail bata dijiye."

    def summary_question(self) -> str:
        parts = [
            f"ye enquiry {self.fields.get('self_or_family', 'noted')} ke liye hai",
            f"naam {self.fields.get('patient_name', 'not available')}",
            f"age {self.fields.get('patient_age', 'not available')}",
            f"city {self.fields.get('city', 'not available')}",
            f"height/weight {self.fields.get('height_weight', 'not available')}",
            f"reports {self.fields.get('reports_available', 'not available')}",
            f"doctor finding {self.fields.get('doctor_finding', 'not available')}",
            f"main problem {self.fields.get('main_problem', 'not available')}",
            f"duration/progression {self.fields.get('duration_progression', 'not available')}",
            f"symptoms {self.fields.get('symptoms', 'not available')}",
            f"current treatment {self.fields.get('current_treatment', 'not available')}",
            f"treatment response {self.fields.get('treatment_response', 'not applicable')}",
            f"major illness {self.fields.get('major_illness', 'not available')}",
            f"hospitalization {self.fields.get('hospitalization', 'not available')}",
            f"lifestyle history {self.fields.get('lifestyle', 'not available')}",
            f"main expectation {self.fields.get('main_expectation', 'not available')}",
        ]
        return "Toh ji, main aapka case summarize kar deti hoon: " + ", ".join(parts) + ". Kya maine aapka case sahi samjha? Ismein kuch add ya correct karna chahenge?"

    def final_summary_reply(self) -> str:
        parts = [
            f"patient {self.fields.get('patient_name', 'not available')}",
            f"age {self.fields.get('patient_age', 'not available')}",
            f"city {self.fields.get('city', 'not available')}",
            f"reports/finding {self.fields.get('doctor_finding') or self.fields.get('reports_available', 'not available')}",
            f"main concern {self.fields.get('main_problem', 'not available')}",
            f"duration {self.fields.get('duration_progression', 'not available')}",
            f"symptoms {self.fields.get('symptoms', 'not available')}",
            f"current treatment {self.fields.get('current_treatment', 'not available')}",
            f"major illness {self.fields.get('major_illness', 'not available')}",
            f"expectation {self.fields.get('main_expectation', 'not available')}",
        ]
        return "Final history summary: " + ", ".join(parts) + f". {self.FINAL_LINE}"

    def _capture_pending_field(self, field: str, text: str) -> None:
        if self._is_filler(text):
            return
        if field == "self_or_family":
            lower = text.lower()
            rel = self._relation_from_text(lower)
            if rel:
                self.fields["self_or_family"] = "family"
                self.fields.setdefault("relation", rel)
            elif "family" in lower or "member" in lower:
                self.fields["self_or_family"] = "family"
            elif re.search(r"\b(mere liye|meri|mera|main|myself|for me|self)\b", lower, re.I):
                self.fields["self_or_family"] = "self"
            return
        if field == "relation":
            rel = self._relation_from_text(text.lower())
            if rel:
                self.fields["relation"] = rel
            else:
                self.fields["relation"] = self._short(text)
            return
        if field == "patient_name":
            name = self._extract_name(text)
            if name:
                self.fields["patient_name"] = name
            age = self._extract_age(text)
            if age:
                self.fields["patient_age"] = age
            return
        if field == "patient_age":
            age = self._extract_age(text)
            if age:
                self.fields["patient_age"] = age
            elif self._not_available(text):
                self.fields["patient_age"] = "not available"
            return
        if field == "city":
            self.fields["city"] = self._short(text)
            return
        if field == "height_weight":
            self.fields["height_weight"] = "not available" if self._not_available(text) else self._short(text)
            return
        if field == "reports_available":
            self.fields["reports_available"] = "not available" if self._negative(text) else self._short(text)
            if not self.fields.get("doctor_finding") and self._looks_like_diagnosis(text):
                self.fields["doctor_finding"] = self._short(text)
            return
        if field == "doctor_finding":
            self.fields["doctor_finding"] = "not available" if self._not_available(text) else self._short(text)
            return
        if field == "weight_change":
            self.fields["weight_change"] = self._short(text)
            return
        if field == "main_problem":
            self.fields["main_problem"] = self._short(text)
            if self._has_duration(text):
                self.fields.setdefault("duration_progression", self._short(text))
            return
        if field == "duration_progression":
            if self._has_duration(text) or self._not_available(text):
                self.fields["duration_progression"] = self._short(text)
            return
        if field == "symptoms":
            self.fields["symptoms"] = self._short(text)
            return
        if field == "current_treatment":
            self.fields["current_treatment"] = "no" if self._negative(text) and not re.search(r"\b(metformin|insulin|bp|tablet|medicine|dawai|ecosprin|immuno|lactulose|diuretic)\b", text, re.I) else self._short(text)
            return
        if field == "treatment_response":
            self.fields["treatment_response"] = self._short(text)
            return
        if field in {"major_illness", "hospitalization", "lifestyle", "main_expectation"}:
            self.fields[field] = self._short(text)
            return

    def _extract_obvious_details(self, text: str) -> None:
        lower = text.lower()
        rel = self._relation_from_text(lower)
        if rel and not self.fields.get("self_or_family"):
            self.fields["self_or_family"] = "family"
            self.fields.setdefault("relation", rel)
        elif ("family" in lower or "member" in lower) and not self.fields.get("self_or_family"):
            self.fields["self_or_family"] = "family"
        elif re.search(r"\b(mere liye|for me|myself)\b", lower) and not self.fields.get("self_or_family"):
            self.fields["self_or_family"] = "self"
        name = self._extract_name(text)
        if name and not self.fields.get("patient_name"):
            self.fields["patient_name"] = name
        age = self._extract_age(text)
        if age and not self.fields.get("patient_age"):
            self.fields["patient_age"] = age
        city = self._extract_city(text)
        if city and not self.fields.get("city"):
            self.fields["city"] = city
        if self._has_duration(text) and not self.fields.get("duration_progression") and self.fields.get("main_problem"):
            self.fields["duration_progression"] = self._short(text)

    def _normalize_dependent_fields(self) -> None:
        current = self.fields.get("current_treatment", "").lower()
        if current in {"no", "none", "not applicable", "nahi"}:
            self.fields.setdefault("treatment_response", "not applicable")

    def _relation_from_text(self, lower: str) -> str:
        for key, value in self.RELATION_WORDS.items():
            if re.search(rf"\b{re.escape(key)}\b", lower):
                return value
        return ""

    def _extract_name(self, text: str) -> str:
        patterns = [
            r"patient(?: ka)? naam\s+([A-Za-z][A-Za-z .'-]{1,40})",
            r"naam\s+([A-Za-z][A-Za-z .'-]{1,40})",
            r"about\s+(?:my\s+\w+\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"calling about\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"^\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)[, ]+\d{1,3}\b",
            r"\b(main|mein)\s+([A-Z][a-z]+)\s+(?:bol|hoon|hu)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                value = match.group(match.lastindex or 1)
                if value.lower() in {"main", "mein"} and match.lastindex and match.lastindex >= 2:
                    value = match.group(2)
                value = self._clean_name(value)
                if value:
                    return value
        if self.next_missing_field() == "patient_name":
            candidate = re.split(r"[,.;]", text.strip())[0]
            candidate = self._clean_name(candidate)
            if candidate and len(candidate.split()) <= 3 and not self._is_filler(candidate):
                return candidate
        return ""

    def _extract_age(self, text: str) -> str:
        match = re.search(r"\b(\d{1,3})\s*(?:saal|years?|yrs?|age)?\b", text, re.I)
        if match:
            age = int(match.group(1))
            if 1 <= age <= 110:
                return str(age)
        return ""

    def _extract_city(self, text: str) -> str:
        match = re.search(r"(?:from|city|location|se baat|se hoon|se hun|se)\s+([A-Z][A-Za-z .,-]{2,60})", text, re.I)
        if match:
            return self._short(match.group(1))
        return ""

    def _looks_like_diagnosis(self, text: str) -> bool:
        return bool(re.search(r"\b(fatty liver|grade\s*[123]|fibro|fibrosis|cirrhosis|hepatitis|ultrasound|usg|f\d|kpa)\b", text, re.I))

    def _has_duration(self, text: str) -> bool:
        return bool(re.search(r"\b(\d+(?:-\d+)?\s*(din|day|days|week|weeks|hafte|hafton|mahine|mahino|month|months|saal|saalon|year|years)|months?|weeks?|years?|din|hafte|hafton|mahine|mahino|saal|saalon|recent|pichle|lagbhag)\b", text, re.I))

    def _is_confirmation(self, text: str) -> bool:
        return bool(re.search(r"\b(haan|yes|sahi|correct|bilkul|theek|right)\b", text, re.I)) and not self._is_correction(text)

    def _is_correction(self, text: str) -> bool:
        return bool(re.search(r"\b(correct kar|add|galat|nahi|change|actually|correction)\b", text, re.I))

    def _negative(self, text: str) -> bool:
        return bool(re.search(r"\b(no|nahi|nahin|koi nahi|not taking|not on|nothing|none)\b", text, re.I))

    def _not_available(self, text: str) -> bool:
        return bool(re.search(r"\b(pata nahi|yaad nahi|not available|available nahi|malum nahi|maalum nahi|don't know|dont know|no idea|confirm nahi)\b", text, re.I))

    def _is_filler(self, text: str) -> bool:
        return self._clean(text).lower() in {"haan", "ha", "hmm", "ok", "okay", "hello", "ji", "theek", "theek hai", "yes", "no"}

    def _clean_name(self, value: str) -> str:
        value = re.sub(r"\b(patient|ka|naam|hai|ji|mr|mrs|age|years|old|saal|main|mein|bol|raha|rahi|hoon|hun)\b", " ", value, flags=re.I)
        value = re.sub(r"\d+", " ", value)
        value = re.sub(r"[^A-Za-z .'-]", " ", value)
        value = " ".join(value.split()).strip(" .'-")
        if not value or value.lower() in self.RELATION_WORDS or value.lower() in {"patient", "father", "mother", "uncle", "family"}:
            return ""
        return value.title()

    def _short(self, text: str, limit: int = 170) -> str:
        text = self._clean(text)
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(str(text or "").strip().split())


class Assistant(Agent):
    def __init__(
        self,
        *,
        caller_phone: Optional[str] = None,
        system_prompt: Optional[str] = None,
        personality: Optional[str] = None,
        dynamic_context: Optional[dict] = None,
        runtime_stage_state: Optional[dict[str, Any]] = None,
        tools: Optional[list] = None,
    ) -> None:
        context = dynamic_context or {}
        self._runtime_stage_state = runtime_stage_state if isinstance(runtime_stage_state, dict) else None

        if system_prompt and system_prompt.strip():
            instructions = system_prompt.strip()
        else:
            instructions = (
                "You are a neutral Hinglish voice assistant.\n\n"
                "No Confluence AI system prompt was provided. Speak in natural Hinglish/Roman Hindi by default. "
                "Use English only if the customer speaks English first. Stay neutral and do not invent a company, "
                "product, disease, offer, order, NDR, treatment, policy, or workflow.\n"
                "Ask one useful question at a time and mention only facts provided by metadata, tools, or the customer."
            )

        if personality and personality.strip():
            instructions += f"\n\n## Personality\n{personality.strip()}"

        is_repeat_followup = bool(
            context.get("repeat_followup_compacted")
            or context.get("full_encounter_available_via_tool")
            or str(context.get("event") or "").strip().lower() == "repeat_followup"
        )
        simple_repeat_followup = is_repeat_followup and truthy_argument(context.get("simple_followup_mode"))
        single_stage_lock = str(context.get("single_stage_lock") or "").strip()
        deterministic_intake_enabled = (
            single_stage_lock == "intake_history"
            and os.getenv("DETERMINISTIC_INTAKE_CONTROLLER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._intake_controller = (
            DeterministicIntakeController(caller_phone=caller_phone) if deterministic_intake_enabled else None
        )
        self._deterministic_reply: Optional[str] = None
        context_lines = []
        if caller_phone:
            context_lines.append(
                "Caller phone number is already known: "
                f"{caller_phone}. Do not ask for it unless the customer wants to use a different number."
            )
        priority_context_keys = (
            "event",
            "workflow",
            "patient_name",
            "patient_department",
            "tracking_summary",
            "medicine_summary",
            "required_medicine_script",
            "required_diet_script",
            "strict_followup_script",
            "diet_chart_summary",
        )
        ordered_context_items = []
        seen_context_keys = set()
        if is_repeat_followup:
            for key in priority_context_keys:
                if key in context:
                    ordered_context_items.append((key, context.get(key)))
                    seen_context_keys.add(key)
        for key, value in context.items():
            if key in seen_context_keys:
                continue
            ordered_context_items.append((key, value))

        for key, value in ordered_context_items:
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            value = str(value)
            max_value_chars = 9000 if is_repeat_followup and key in {
                "required_medicine_script",
                "required_diet_script",
                "strict_followup_script",
                "medicine_summary",
                "diet_chart_summary",
            } else 700
            if len(value) > max_value_chars:
                value = value[:max_value_chars] + "...TRUNCATED"
            context_lines.append(f"{key}: {value}")

        if context_lines:
            default_context_chars = "18000" if is_repeat_followup else "2200"
            max_context_chars = int(os.getenv("CONTEXT_METADATA_MAX_CHARS", default_context_chars) or default_context_chars)
            metadata_lines: list[str] = []
            used = 0
            for line in context_lines:
                rendered = f"- {line}"
                remaining = max_context_chars - used
                if remaining <= 80:
                    break
                if len(rendered) > remaining:
                    rendered = rendered[:remaining].rstrip() + "...TRUNCATED"
                metadata_lines.append(rendered)
                used += len(rendered)
            instructions += "\n\n## Context & Metadata\n" + "\n".join(metadata_lines)

        if simple_repeat_followup:
            instructions += (
                "\n\n## Runtime Rules\n"
                "- This repeat-followup call is in simple_followup_mode. Do not use stage/state tools or internal workflow labels.\n"
                "- Use simple_followup_script as the main source and speak it naturally in this order: delivery, order tracking only if needed, medicines, diet, close.\n"
                "- Avoid robotic wording like 'stage', 'step', 'medicine item', 'field', 'metadata', or 'tool'.\n"
                "- If interrupted, answer briefly and continue the unfinished sentence/section. Do not jump to diet before completing medicines.\n"
                "- Do not offer WhatsApp proactively.\n"
                "- Ask only useful customer questions: delivery received at start, and doubts at the end. Do not ask permission before every explanation."
            )
            super().__init__(instructions=instructions, tools=tools or [])
            return

        instructions += (
            "\n\n## Runtime Rules\n"
            "- Follow the Confluence AI system prompt, personality, and metadata exactly.\n"
            "- Do not use any hardcoded business flow from this worker.\n"
            "- Hybrid architecture rule: LiveKit keeps realtime conversation fast, but Confluence KB chunks remain the source of truth for the configured stage prompt details.\n"
            "- Stage guard: maintain a compact internal call_state with current_stage, pending_question, answered_fields, stage_completion_status, and next_allowed_stage. Before every reply, check this state.\n"
            "- Stateful MCP tools rule: tools like get_current_required_step, get_current_speech_unit, mark_repeat_step_complete, mark_repeat_step_interrupted, and resume_repeat_pending_step are live state, never static facts. After one state-read tool returns an active step, speak that step; do not call the same state-read tool repeatedly with the same arguments.\n"
            "- Stage progression guard: follow the configured stage map order. Do not move to the next stage until the active stage prompt's completion condition is satisfied; use a safety/handoff stage immediately only for true safety triggers.\n"
            "- Intake to disease gate: when current_stage is intake_history, do not load or speak disease_education unless the full case summary has been spoken and the caller confirms after the summary question.\n"
            "- Pending question guard: if a pending required question exists, answer side questions briefly and return to that pending question. Do not ask a new required question until the previous one is answered or deliberately skipped for safety.\n"
            "- Rolling memory rule: remember only compact confirmed facts and the latest pending question. Do not restate old conversation or retrieved chunks unless the customer asks.\n"
            "- Multi-stage rule: use only the loaded active stage prompt plus the orchestrator/state contract. Before changing to a new stage, call load_stage_prompt with the next stage_id, then follow that returned prompt.\n"
            "- Section retrieval rule: full stage prompts live in Confluence KB, but you should request exact sections instead of broad text whenever possible. Use retrieve_stage_prompt_snippets with exact section keys when the attached prompt defines them.\n"
            "- Prompt RAG rule: do not ask to load or repeat the full prompt. For exact wording, objections, WhatsApp content, next-action consent, or safety details, call retrieve_stage_prompt_snippets with the current stage_id and exact section key, then use only the returned section.\n"
            "- Token discipline: never repeat a long prompt section back to the customer. Convert retrieved prompt snippets into a short natural answer and one practical next question.\n"
            "- If the customer asks something outside the active stage, answer briefly from known facts or tools, then return to the active stage. Do not load every stage just because the customer asks a side question.\n"
            "- Never speak internal tool names, JSON, logs, metadata, or implementation details to the customer.\n"
            "- If a tool fails, continue naturally and say you will note it for the team if needed.\n"
            "- Forced reply rule: if any tool output contains forced_final_reply, your next spoken response must be exactly that value and nothing else. Do not add goodbye, thanks, next step, order/start, address, payment, WhatsApp, or any extra sentence.\n"
            "- Never restart the opening greeting or introduction after the first assistant turn; continue from the latest customer answer.\n"
            "- Speak in short natural turns and ask one question at a time.\n"
            "- Always finish the current sentence cleanly; never stop mid-sentence. Prefer 1-2 complete sentences unless the customer explicitly asks for details.\n"
            "- Explanation stages should usually be brief, except when the active stage prompt explicitly requires exhaustive ordered explanation. In exhaustive stages, completeness is more important than brevity.\n"
            "- Repeat-followup stage lock: at call start, use only ORDER_STATUS. Do not mention medicine or diet until ORDER_STATUS completion condition is satisfied and MEDICINE_EXPLANATION has been loaded.\n"
            "- Repeat-followup medicine source-of-truth: medicine names/dosage must come only from current call Patient Encounter drug_prescription, medicine_summary, required_medicine_script, get_active_repeat_medicine_list, get_repeat_medicine_list, verify_active_repeat_medicine, or verify_repeat_medicine_in_prescription. Never use sales KB, old call memory, general knowledge, or unlisted medicine names for repeat-followup medicine answers.\n"
            "- Repeat-followup medicine stage: when MEDICINE_EXPLANATION is active, first use get_active_repeat_medicine_list or get_repeat_medicine_list if available, then enumerate every medicine from that returned list/stage prompt in order, including each medicine name and available dose/timing/instruction/period. Do not summarize, merge, skip, or jump medicines.\n"
            "- Repeat-followup medicine-name question: if customer asks whether a medicine is included, e.g. 'Neuro M Oil milega?', call verify_active_repeat_medicine or verify_repeat_medicine_in_prescription before answering. If found, say it is in the prescription and give its dose/instruction/period. If not found, say it is not visible and team will verify; do not guess.\n"
            "- Repeat-followup resume rule: keep an internal medicine_index. If the customer interrupts during medicine explanation, answer briefly and resume from the same medicine_index or next unspoken medicine. DIET_EXPLANATION can load only after every listed medicine has been spoken.\n"
            "- Repeat-followup diet stage: when DIET_EXPLANATION is active, explain concrete allowed foods and avoid/parhej foods from that stage prompt. Do not send WhatsApp in this Agent 1 version.\n"
            "- Do not ask permission before normal explanations. Avoid lines like 'kya main bataun', 'kya main clear karun', or 'aage bataun' unless the customer is about to consent to order, payment, WhatsApp send, report review, or doctor handoff.\n"
            "- Flow-first speech rule: never ask 'kya aap jaanna chahenge', 'kya main aage bataun', 'kya main explain karun', 'kya ye clear hai', or similar approval-style lines after every sentence. For the normal configured flow, speak the next required point directly.\n"
            "- Use clarity checks only once at the end of a complete stage, not after every paragraph. If the next flow step is known, continue to that step.\n"
            "- If the next step is an ordinary explanation, continue directly in one short explanation and then ask one practical question.\n"
            "- Never continue an explanation in a loop. If you already explained a topic once, do not explain it again unless the customer asks a specific question.\n"
            "- If the customer says haan, hmm, ok, theek hai, or asks to continue, do not repeat the same explanation; move to the next missing detail.\n"
            "- If the customer says you are repeating, immediately apologize once and move to the next practical step.\n"
            "- Natural acknowledgement rule: words like ji, haan, hmm, ok, acha, and theek hai can be used as normal listening/filler. Accept them naturally and continue the active prompt flow without sounding strict or robotic.\n"
            "- Do not treat acknowledgement/filler or background noise as a new fact, final consent, or reason to jump stage. If the caller's answer is genuinely unclear and the active prompt still needs that detail, ask once in simple natural words.\n"
            "- If the customer interrupts with a clear question, answer that exact question first, then continue the same prompt point. If the interruption is only filler or noise, continue the unfinished point naturally.\n"
            "- Treatment checkpoint rule: when current_stage is treatment_explanation and get_treatment_continuation_script is available, use it before continuing treatment after any interruption or pause. Speak the returned customer_script continuously from the active Confluence prompt, do not stop between points, then call mark_treatment_script_complete. Use get_treatment_next_point only as fallback. Do not use treatment checkpoint tools in other stages.\n"
            "- If a state completion tool says blocked_incomplete_step, immediately speak the returned active_speech_unit completely. Do not call get_current_required_step again unless you have spoken something after the blocked response.\n"
            "- Final-action consent is valid only if the customer clearly agrees to the specific action defined by the active prompt after required details are discussed.\n"
            "- Never tell the customer you are creating a backend record. Say only natural customer-facing words like 'main aapki details note kar rahi hoon'.\n"
            "- If customer hesitates or asks a concern/question, stop the final-action flow and answer that concern first. Do not collect final-action details yet.\n"
            "- WhatsApp MCP rule: call WhatsApp/send/template tools only after the customer explicitly asks or agrees to receive WhatsApp details/link/address/video/testimonial. When calling it, pass customer_requested=true. Never call WhatsApp tools just because you explained a disease or treatment.\n"
            "- WhatsApp message rule: the message argument must be the complete real customer-facing text from the active prompt, customer facts, or retrieved section. Never pass labels/placeholders like information, details, treatment details and information, token details, video, link, or testimonial. If you are unsure what exact content to send, retrieve the correct stage section or ask one clarifying question; do not invent company, disease, address, pricing, offers, or links."
        )

        if single_stage_lock:
            instructions += (
                "\n\n## Single-Stage Runtime Lock\n"
                f"- Only `{single_stage_lock}` is configured for this agent right now.\n"
                "- This lock overrides any generic multi-stage runtime rule.\n"
                "- Do not call or mention load_stage_prompt.\n"
                "- Do not transition, forward, hand off, create callbacks, offer WhatsApp, or speak content from unconfigured stages.\n"
                "- If the active stage says to stop after completion, stop with that exact completion line and do not add a next-step sentence."
                "\n- If the active stage is intake_history, patient name and age are hard gates: do not ask city, height, reports, symptoms, treatment, or summary until both are captured."
                "\n- In single-stage intake_history, do not treat vague phrases like 'mera naam hai', 'naam baad mein', 'father', 'patient', or only a relation as a patient name."
                "\n- In single-stage intake_history, city is also required before height, reports, symptoms, or summary."
                "\n- In single-stage intake_history, side questions about treatment, safety, alcohol, diet, doctor, results, compatibility, Ayurveda, medicine, supplements, or next steps must use the active prompt's deferral line only. Do not answer them from general knowledge."
                "\n- In single-stage intake_history, never say future-action phrases like doctor ko dikhayenge, team connect, case forward, follow-up, hospital le jaayen, alcohol avoid, safe hota hai, or Ayurvedic treatment se."
                "\n- In single-stage intake_history, price/cost/discount/prepaid/COD/payment questions are the only side questions that may be answered directly; use the exact cost line from the active prompt."
                "\n- In single-stage intake_history, after summary confirmation give one concise final history summary, then end with the exact completion sentence. Do not add advice, disease education, treatment explanation, WhatsApp, callback, or next-step content."
            )

        super().__init__(instructions=instructions, tools=tools or [])


    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = str(getattr(new_message, "text_content", None) or getattr(new_message, "raw_text_content", None) or "").strip()
        if text and self._runtime_stage_state is not None:
            self._runtime_stage_state["user_turn_seen"] = True
            self._runtime_stage_state["last_user_transcript"] = text
        if self._intake_controller is None:
            return await super().on_user_turn_completed(turn_ctx, new_message)
        price_requested = bool(self._intake_controller.PRICE_PATTERN.search(text or ""))
        self._intake_controller.ingest_user(text)
        reply = self._intake_controller.next_reply()
        if price_requested and not self._intake_controller.final_spoken:
            reply = f"{self._intake_controller.PRICE_LINE} {reply}"
        self._deterministic_reply = reply
        return None

    def llm_node(self, chat_ctx, tools, model_settings):
        if self._intake_controller is not None:
            if self._deterministic_reply:
                reply = self._deterministic_reply
                self._deterministic_reply = None
                return reply
            return self._intake_controller.next_reply()
        return super().llm_node(chat_ctx, tools, model_settings)


def prewarm(proc: JobProcess):
    min_speech_duration = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.18"))
    min_silence_duration = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.25"))
    activation_threshold = float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.6"))

    logger.info(
        "Loading Silero VAD activation_threshold=%s min_speech=%ss min_silence=%ss",
        activation_threshold,
        min_speech_duration,
        min_silence_duration,
    )
    if hasattr(silero.VAD, "load"):
        proc.userdata["vad"] = silero.VAD.load(
            min_speech_duration=min_speech_duration,
            min_silence_duration=min_silence_duration,
            activation_threshold=activation_threshold,
        )
    else:
        proc.userdata["vad"] = silero.VAD()
    logger.info("Silero VAD loaded successfully.")


def setup_google_credentials() -> None:
    credential_candidates = [
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        "/etc/secrets/creds.json",
        "creds.json",
    ]
    credentials_path = next(
        (
            os.path.abspath(path)
            for path in credential_candidates
            if path and os.path.exists(path)
        ),
        None,
    )

    if credentials_path and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        logger.info("Using Google credentials file: %s", credentials_path)
    elif not credentials_path:
        logger.error("No Google credentials file found. Checked GOOGLE_APPLICATION_CREDENTIALS, /etc/secrets/creds.json, and ./creds.json.")

    if not os.getenv("GOOGLE_CLOUD_PROJECT") and credentials_path:
        try:
            with open(credentials_path, "r", encoding="utf-8") as f:
                project_id = json.load(f).get("project_id")
            if project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
                logger.info("Using Google Cloud project from creds.json: %s", project_id)
        except Exception as exc:
            logger.warning("Failed to read project_id from creds.json: %s", exc)


async def run_background_classifier(
    *,
    transcript: str,
    recent_user_turns: list[str],
    task_id: Optional[str],
    room_name: Optional[str],
    system_prompt: Optional[str],
    dynamic_context: dict[str, Any],
) -> None:
    """Passive classifier for monitoring/routing signals only; it never changes the live reply."""
    import asyncio

    if os.getenv("CLASSIFIER_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        return

    model_name = os.getenv("CLASSIFIER_MODEL", "gemini-2.5-flash")
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    context_preview = json.dumps(dynamic_context or {}, ensure_ascii=False, default=str)[:800]
    prompt_preview = (system_prompt or "")[:500]
    recent_text = "\n".join(f"- {turn}" for turn in recent_user_turns[-6:])

    classifier_prompt = f"""
You are a passive call classifier. Analyze the latest final user transcript for monitoring only.
Do not produce instructions for the voice agent and do not invent business behavior.
Return compact JSON with these keys:
language, intent_summary, sentiment, urgency, unanswered_question, needs_tool, risk_flags.

Task ID: {task_id or ""}
Room: {room_name or ""}
System prompt preview:
{prompt_preview}

Context preview:
{context_preview}

Recent final user turns:
{recent_text}

Latest transcript:
{transcript}
""".strip()

    def generate() -> str:
        client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=location,
        )
        response = client.models.generate_content(
            model=model_name,
            contents=classifier_prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        return getattr(response, "text", "") or ""

    try:
        result = await asyncio.to_thread(generate)
        logger.info("Background classifier result task=%s room=%s result=%s", task_id, room_name, result[:1000])
    except Exception as exc:
        logger.exception("Background classifier failed: %s", exc)


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    await ctx.connect()

    metadata = parse_dispatch_metadata(ctx)
    simulation_metadata = parse_simulation_metadata(ctx)
    if simulation_metadata:
        metadata = {**metadata, **simulation_metadata}
    system_prompt = metadata.get("system_prompt")
    personality = metadata.get("personality")
    dynamic_context = metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
    stage_prompts = metadata.get("stage_prompts") if isinstance(metadata.get("stage_prompts"), list) else []
    task_id = metadata.get("task")
    audio_name = (
        metadata.get("audio_name")
        or metadata.get("voice_name")
        or metadata.get("voice")
        or dynamic_context.get("audio_name")
        or dynamic_context.get("voice_name")
        or os.getenv("GEMINI_LIVE_VOICE")
        or "Kore"
    )

    import asyncio

    participant_waited = False
    if not task_id:
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=inbound_participant_wait_seconds())
            participant_waited = True
        except asyncio.TimeoutError:
            logger.warning(
                "No participant available for inbound metadata resolve within %.1f seconds.",
                inbound_participant_wait_seconds(),
            )

        telephony = extract_room_telephony(ctx)
        inbound_room = bool(
            telephony.get("caller_phone")
            or telephony.get("called_number")
            or telephony.get("call_uuid")
            or telephony.get("trunk_id")
            or ctx.room.name.startswith("sip-")
        )
        if inbound_room:
            inbound_result = await resolve_inbound_metadata_with_retries(ctx)
            inbound_metadata = inbound_result.get("metadata") if isinstance(inbound_result, dict) else None
            if isinstance(inbound_metadata, dict):
                system_prompt = inbound_metadata.get("system_prompt") or system_prompt
                personality = inbound_metadata.get("personality") or personality
                dynamic_context = inbound_metadata.get("context") or dynamic_context or {}
                stage_prompts = (
                    inbound_metadata.get("stage_prompts")
                    if isinstance(inbound_metadata.get("stage_prompts"), list)
                    else stage_prompts
                )
                task_id = inbound_metadata.get("task") or inbound_result.get("task") or task_id
                audio_name = (
                    inbound_metadata.get("audio_name")
                    or inbound_metadata.get("voice_name")
                    or inbound_metadata.get("voice")
                    or dynamic_context.get("audio_name")
                    or dynamic_context.get("voice_name")
                    or audio_name
                )

    dynamic_context = normalize_context_phone_fields(dynamic_context)
    dynamic_context, repeat_runtime_routing_unavailable = await hydrate_repeat_runtime_routing(
        dynamic_context,
        task_id=task_id,
    )
    configured_stage_titles = stage_prompt_titles(stage_prompts)
    if len(configured_stage_titles) == 1:
        dynamic_context["single_stage_lock"] = next(iter(configured_stage_titles))
    diagnostics_enabled = (
        truthy_argument(dynamic_context.get("livekit_diagnostics_enabled"))
        or truthy_argument(os.getenv("LIVEKIT_DIAGNOSTICS_ENABLED"))
    )
    try:
        diagnostics_max_events = int(
            dynamic_context.get("livekit_diagnostics_max_events")
            or os.getenv("LIVEKIT_DIAGNOSTICS_MAX_EVENTS")
            or 200
        )
    except (TypeError, ValueError):
        diagnostics_max_events = 200
    diagnostics_max_events = max(20, min(diagnostics_max_events, 1000))
    diagnostic_seq = 0

    async def emit_livekit_diagnostic(status: str, reason: Optional[str] = None, extra: Optional[dict] = None) -> None:
        nonlocal diagnostic_seq
        if not diagnostics_enabled:
            return
        diagnostic_seq += 1
        details = {
            "diag_seq": diagnostic_seq,
            "monotonic_ms": int(time.monotonic() * 1000),
        }
        if extra:
            details.update(extra)
        await post_livekit_task_event(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            event="agent_diagnostic",
            status=status,
            reason=reason,
            extra=details,
        )

    await emit_livekit_diagnostic(
        "diagnostics_ready",
        reason="enabled_from_confluence_settings",
        extra={"details": {"max_events": diagnostics_max_events}},
    )

    simple_repeat_followup = bool(
        (
            dynamic_context.get("repeat_followup_compacted")
            or dynamic_context.get("full_encounter_available_via_tool")
            or str(dynamic_context.get("event") or "").strip().lower() == "repeat_followup"
        )
        and truthy_argument(dynamic_context.get("simple_followup_mode"))
    )
    logger.info(
        "Repeat follow-up runtime task=%s repeat=%s strategy=prompt_driven",
        task_id,
        is_repeat_followup_context(dynamic_context),
    )
    await ensure_task_company(task_id, fallback_company=dynamic_context.get("company") or metadata.get("company"))
    agent_name_for_rag = (
        metadata.get("agent")
        or metadata.get("ai_agent")
        or metadata.get("target_agent")
        or metadata.get("assigned_agent")
        or dynamic_context.get("agent")
        or dynamic_context.get("ai_agent")
        or dynamic_context.get("target_agent")
        or dynamic_context.get("assigned_agent")
    )
    system_prompt = await expand_stage_prompt_documents(
        system_prompt,
        stage_prompts,
        task_id=task_id,
        agent=agent_name_for_rag,
        initial_stage_id=str(dynamic_context.get("active_stage_id") or "").strip() or None,
    )
    runtime_stage_state = {
        "current_stage": str(dynamic_context.get("active_stage_id") or os.getenv("INITIAL_STAGE_ID", "intake_history") or "intake_history").strip(),
        "updated_at": time.monotonic(),
    }

    is_sip_room = bool(ctx.room and ctx.room.name.startswith("sip-"))
    dynamic_mcp_tool_specs: list[dict[str, Any]] = []
    if is_sip_room and os.getenv("LOAD_MCP_TOOLS_BEFORE_GREETING", "1").strip().lower() not in {"1", "true", "yes"}:
        logger.info("Skipping MCP tools before first SIP greeting to reduce answer latency.")
        tools = []
    else:
        dynamic_mcp_tool_specs = await fetch_dynamic_mcp_tool_specs(task_id, system_prompt, agent=agent_name_for_rag)
        await maybe_run_startup_customer_check_mcp(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            dynamic_context=dynamic_context,
            tool_specs=dynamic_mcp_tool_specs,
        )
        tools = build_dynamic_mcp_tools(dynamic_mcp_tool_specs, task_id, dynamic_context)
    repeat_medicine_tools = [] if simple_repeat_followup else make_repeat_medicine_context_tools(dynamic_context)
    if repeat_medicine_tools:
        tools.extend(repeat_medicine_tools)
        logger.info("Registered repeat-followup local medicine guard tools for task=%s", task_id)
    stage_prompt_loader = None if simple_repeat_followup else make_stage_prompt_loader_tool(
        stage_prompts,
        task_id,
        agent=agent_name_for_rag,
        initial_stage_id=str(dynamic_context.get("active_stage_id") or "").strip() or None,
        runtime_stage_state=runtime_stage_state,
    )
    if stage_prompt_loader:
        tools.append(stage_prompt_loader)
        logger.info("Registered stage prompt loader tool for task=%s", task_id)
    stage_prompt_retriever = None if simple_repeat_followup else make_stage_prompt_retriever_tool(stage_prompts, task_id, agent=agent_name_for_rag)
    if stage_prompt_retriever:
        tools.append(stage_prompt_retriever)
        logger.info("Registered stage prompt retriever tool for task=%s", task_id)
    if (
        not simple_repeat_followup
        and "treatment_explanation" in stage_prompt_titles(stage_prompts)
    ):
        treatment_checkpoint_tools = make_treatment_checkpoint_tools(
            runtime_stage_state,
            stage_prompts=stage_prompts,
            task_id=task_id,
            agent=agent_name_for_rag,
            dynamic_context=dynamic_context,
        )
        if treatment_checkpoint_tools:
            tools.extend(treatment_checkpoint_tools)
            logger.info("Registered treatment checkpoint tools for task=%s", task_id)
    await emit_livekit_diagnostic(
        "tools_loaded",
        reason="before_session_start",
        extra={
            "details": {
                "dynamic_tool_count": len(dynamic_mcp_tool_specs),
                "total_tool_count": len(tools),
                "stage_prompt_count": len(stage_prompts),
            }
        },
    )

    setup_google_credentials()

    model_name = os.getenv("GEMINI_LIVE_MODEL") or "gemini-live-2.5-flash-native-audio"
    audio_name = normalize_gemini_audio_name(audio_name)
    vertex_location = os.getenv("VERTEX_LOCATION", "us-central1")
    temperature = float(os.getenv("GEMINI_TEMPERATURE", "0.65"))
    endpoint_min_delay = float(os.getenv("LIVEKIT_ENDPOINT_MIN_DELAY", "0.35") or "0.35")
    endpoint_max_delay = float(os.getenv("LIVEKIT_ENDPOINT_MAX_DELAY", "1.2") or "1.2")
    logger.info(
        "Configuring Gemini Live via Vertex AI model=%s voice=%s location=%s task=%s tools=%s endpoint=%s-%s interruption_enabled=%s",
        model_name,
        audio_name,
        vertex_location,
        task_id,
        len(tools),
        endpoint_min_delay,
        endpoint_max_delay,
        True,
    )
    model = realtime.RealtimeModel(
        model=model_name,
        voice=audio_name,
        vertexai=True,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=vertex_location,
        temperature=temperature,
        realtime_input_config=genai_types.RealtimeInputConfig(
            activity_handling=genai_types.ActivityHandling.NO_INTERRUPTION,
        ),
    )

    session = AgentSession(
        llm=model,
        vad=ctx.proc.userdata.get("vad"),
        turn_handling=TurnHandlingOptions(
            turn_detection="realtime_llm",
            endpointing={"mode": "fixed", "min_delay": endpoint_min_delay, "max_delay": endpoint_max_delay},
            interruption={
                "enabled": True,
                "min_duration": float(os.getenv("LIVEKIT_INTERRUPTION_MIN_DURATION", "0.9") or "0.9"),
                "min_words": int(os.getenv("LIVEKIT_INTERRUPTION_MIN_WORDS", "2") or "2"),
                "false_interruption_timeout": float(os.getenv("LIVEKIT_FALSE_INTERRUPTION_TIMEOUT", "1.2") or "1.2"),
                "resume_false_interruption": True,
                "discard_audio_if_uninterruptible": False,
            },
            preemptive_generation={"enabled": False, "preemptive_tts": False},
        ),
        tools=[],
        aec_warmup_duration=None,
        user_away_timeout=None,
    )

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)
        if diagnostics_enabled:
            metric = getattr(ev, "metrics", None)
            asyncio.create_task(
                emit_livekit_diagnostic(
                    "metrics_collected",
                    extra={
                        "metric_type": type(metric).__name__,
                        "details": safe_tool_json(metric, max_chars=800),
                    },
                )
            )

    classifier_state = {
        "user_turns_since_run": 0,
        "last_run_at": time.monotonic(),
        "recent_user_turns": [],
    }
    order_safety_state = {
        "all_user_turns": [],
        "draft_encounter_created": False,
        "address_whatsapp_sent": False,
        "runtime_stage_state": runtime_stage_state,
    }

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev):
        if not getattr(ev, "is_final", False):
            return

        transcript = (getattr(ev, "transcript", "") or "").strip()
        if not transcript:
            return

        runtime_stage_state["user_turn_seen"] = True
        runtime_stage_state["last_user_transcript"] = transcript

        all_user_turns = order_safety_state["all_user_turns"]
        all_user_turns.append(transcript)
        del all_user_turns[:-40]

        address_detected = address_details_detected(all_user_turns)
        order_detected = False
        current_stage = str(runtime_stage_state.get("current_stage") or "").strip()
        if diagnostics_enabled:
            asyncio.create_task(
                emit_livekit_diagnostic(
                    "user_transcript_final",
                    extra={
                        "transcript": transcript,
                        "is_final": True,
                        "turn_count": len(all_user_turns),
                        "current_stage": current_stage,
                        "details": {
                            "address_detected": address_detected,
                            "order_consent_detected": order_detected,
                        },
                    },
                )
            )

        asyncio.create_task(
            maybe_autocall_mcp_from_conditions(
                user_turns=list(all_user_turns),
                task_id=task_id,
                room_name=ctx.room.name if ctx.room else None,
                dynamic_context=dynamic_context,
                tool_specs=dynamic_mcp_tool_specs,
                safety_state=order_safety_state,
                trigger="live_user_turn",
            )
        )

        if address_detected:
            if diagnostics_enabled:
                asyncio.create_task(
                    emit_livekit_diagnostic(
                        "address_verification_scheduled",
                        reason="address_detected",
                        extra={"trigger": "live_user_turn", "current_stage": current_stage},
                    )
                )
            asyncio.create_task(
                maybe_send_address_verification_whatsapp(
                    user_turns=list(all_user_turns),
                    task_id=task_id,
                    room_name=ctx.room.name if ctx.room else None,
                    dynamic_context=dynamic_context,
                    safety_state=order_safety_state,
                    trigger="live_user_turn",
                )
            )

        recent_user_turns = classifier_state["recent_user_turns"]
        recent_user_turns.append(transcript)
        del recent_user_turns[:-8]

        classifier_state["user_turns_since_run"] += 1
        now = time.monotonic()
        elapsed = now - classifier_state["last_run_at"]
        if classifier_state["user_turns_since_run"] < 2 and elapsed < 12.0:
            return

        classifier_state["user_turns_since_run"] = 0
        classifier_state["last_run_at"] = now
        asyncio.create_task(
            run_background_classifier(
                transcript=transcript,
                recent_user_turns=list(recent_user_turns),
                task_id=task_id,
                room_name=ctx.room.name if ctx.room else None,
                system_prompt=system_prompt,
                dynamic_context=dynamic_context,
            )
        )

    call_done = asyncio.Event()
    participant_seen = bool(ctx.room.remote_participants)

    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant):
        nonlocal participant_seen
        participant_seen = True
        logger.info("Remote participant connected: %s", participant.identity)

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        logger.info("Remote participant disconnected: %s", participant.identity)
        if participant_seen and not ctx.room.remote_participants:
            call_done.set()

    @session.on("close")
    def _on_session_close(ev):
        logger.info("Agent session closed: reason=%s error=%s", ev.reason, ev.error)
        if diagnostics_enabled:
            asyncio.create_task(
                emit_livekit_diagnostic(
                    "session_close",
                    reason=str(getattr(ev, "reason", "") or ""),
                    extra={
                        "error": str(getattr(ev, "error", "") or ""),
                        "exception_type": type(getattr(ev, "error", None)).__name__
                        if getattr(ev, "error", None)
                        else "",
                    },
                )
            )
        call_done.set()

    async def log_usage():
        await emit_livekit_diagnostic(
            "shutdown_callback_started",
            reason="before_final_safety_checks",
            extra={"details": {"captured_user_turns": len(order_safety_state.get("all_user_turns") or [])}},
        )
        await maybe_autocall_mcp_from_conditions(
            user_turns=list(order_safety_state.get("all_user_turns") or []),
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            dynamic_context=dynamic_context,
            tool_specs=dynamic_mcp_tool_specs,
            safety_state=order_safety_state,
            trigger="call_shutdown",
        )
        await maybe_send_address_verification_whatsapp(
            user_turns=list(order_safety_state.get("all_user_turns") or []),
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            dynamic_context=dynamic_context,
            safety_state=order_safety_state,
            trigger="call_shutdown",
        )
        logger.info("Usage: %s", usage_collector.get_summary())
        await emit_livekit_diagnostic("usage_collected", reason="before_call_ended_callback")
        await post_livekit_task_event(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            event="call_ended",
            status="completed",
            reason="livekit_agent_session_shutdown",
            extra={"ended_at": datetime.datetime.now().isoformat()},
        )

    ctx.add_shutdown_callback(log_usage)

    if not participant_waited:
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=participant_wait_seconds())
            participant_seen = True
        except asyncio.TimeoutError:
            logger.warning(
                "No remote participant joined within %.1f seconds; starting session anyway.",
                participant_wait_seconds(),
            )
        except RuntimeError as exc:
            logger.warning("Room disconnected while waiting for participant: %s", exc)
            return

    caller_phone = None
    for participant in ctx.room.remote_participants.values():
        identity = participant.identity
        if identity.startswith("sip_"):
            caller_phone = identity.replace("sip_", "")
            caller_phone = normalize_phone_for_context(caller_phone)
            break
        if identity.startswith("+") or identity.isdigit():
            caller_phone = normalize_phone_for_context(identity)
            break

    nc = None
    if os.getenv("LIVEKIT_NOISE_CANCELLATION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}:
        try:
            from livekit.plugins import noise_cancellation

            nc = noise_cancellation.BVCTelephony()
            logger.info("Enabling BVCTelephony noise cancellation.")
        except Exception as exc:
            logger.warning("Could not load noise cancellation plugin: %s", exc)

    assistant = Assistant(
        caller_phone=caller_phone,
        system_prompt=system_prompt,
        personality=personality,
        dynamic_context=dynamic_context,
        runtime_stage_state=runtime_stage_state,
        tools=tools,
    )

    await post_livekit_task_event(
        task_id=task_id,
        room_name=ctx.room.name if ctx.room else None,
        event="agent_debug",
        status="starting_session",
        reason="before_session_start",
        extra={
            "google_application_credentials": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
            "google_credentials_exists": bool(
                os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                and os.path.exists(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""))
            ),
            "google_cloud_project": os.getenv("GOOGLE_CLOUD_PROJECT"),
            "model": model_name,
            "voice": audio_name,
            "location": vertex_location,
            "has_system_prompt": bool(system_prompt),
            "task_id": task_id,
            "remote_participants": list(ctx.room.remote_participants.keys()),
        },
    )
    await emit_livekit_diagnostic(
        "starting_session",
        reason="before_session_start",
        extra={
            "details": {
                "model": model_name,
                "voice": audio_name,
                "location": vertex_location,
                "has_system_prompt": bool(system_prompt),
                "remote_participant_count": len(ctx.room.remote_participants),
            }
        },
    )

    await session.start(
        agent=assistant,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            audio_enabled=True,
            video_enabled=False,
            noise_cancellation=nc,
            pre_connect_audio=True,
        ),
    )

    is_repeat_followup = bool(
        dynamic_context.get("repeat_followup_compacted")
        or dynamic_context.get("full_encounter_available_via_tool")
        or str(dynamic_context.get("event") or "").strip().lower() == "repeat_followup"
    )
    repeat_stage = str(
        dynamic_context.get("current_journey_stage")
        or dynamic_context.get("repeat_followup_stage")
        or dynamic_context.get("active_prompt_stage")
        or ""
    ).strip().upper()
    repeat_agent_number = str(
        dynamic_context.get("agent_sequence")
        or dynamic_context.get("followup_agent_number")
        or dynamic_context.get("repeat_agent_number")
        or ""
    ).strip()
    is_repeat_agent_2_followup = bool(
        "RADHA_REPEAT_AGENT_2_HEALTH_COUNSELLOR_V2" in (system_prompt or "")
        or repeat_stage in {"AGENT_2", "AGENT2", "FOLLOW_UP_2", "FOLLOWUP_2"}
        or repeat_agent_number == "2"
    )
    if system_prompt and is_repeat_followup and is_repeat_agent_2_followup:
        greeting_instruction = (
            "The call has just connected. This is Agent 2 follow-up. "
            "Greet briefly in Hindi/Hinglish as Radha from sriaas treatment-support team. "
            "Do not say Agent 2 to the customer. Do not ask whether the medicine package was received. "
            "Do not mention diet. Ask only this first question: Abhi aapki tabiyat kaisi hai? "
            "Then wait for the customer answer."
        )
    elif system_prompt and is_repeat_followup and simple_repeat_followup:
        greeting_instruction = (
            "The call has just connected. Greet briefly in Hindi/Hinglish as Radha from sriaas treatment-support team and ask only this first question: "
            "Aapko medicine package receive ho gaya hai? "
            "Do not mention stages, tools, medicine explanation, or diet in the first greeting. "
            "After the customer answers, continue naturally from simple_followup_script."
        )
    elif system_prompt and is_repeat_followup:
        greeting_instruction = (
            "The call has just connected. Greet briefly in Hindi/Hinglish as Radha from sriaas treatment-support team. "
            "Use only the active initial stage ORDER_STATUS now. "
            "The first spoken reply must include both a short intro and the delivery question: ask whether the medicine/order has been received. "
            "Never stop after only the intro. If a state tool returns an opening step, still ask the delivery question in the same first reply. "
            "Handle only order delivery or tracking in this first stage. "
            "Do not mention medicine explanation or diet in the greeting. "
            "After ORDER_STATUS is truly complete, call load_stage_prompt for MEDICINE_EXPLANATION before speaking about medicines."
        )
    elif system_prompt:
        greeting_instruction = (
            "The call has just connected. Follow the Confluence AI system prompt, personality, "
            "context metadata, and Active Initial Stage exactly. Do not add any default business flow that is not present "
            "in the selected Confluence AI prompt. Do not call any tool before the first greeting audio is complete. "
            "Greet briefly and ask the first intake question from the active intake stage."
        )
    else:
        greeting_instruction = (
            "The call has just connected. Greet briefly in natural Hinglish/Roman Hindi and ask how you can help. "
            "Use English only if the customer speaks English first. No Confluence AI system prompt was provided, "
            "so do not invent a company or workflow."
        )

    try:
        await post_livekit_task_event(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            event="agent_debug",
            status="generating_greeting",
            reason="before_generate_reply",
        )
        await emit_livekit_diagnostic("generating_greeting", reason="before_generate_reply")
        reply = session.generate_reply(instructions=greeting_instruction)
        if hasattr(reply, "wait_for_playout"):
            await reply.wait_for_playout()
        else:
            await reply
        runtime_stage_state["greeting_played"] = True
        await post_livekit_task_event(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            event="agent_debug",
            status="greeting_played",
            reason="after_first_greeting_playout",
        )
        await emit_livekit_diagnostic("greeting_played", reason="after_first_greeting_playout")
        if tools:
            logger.info("%s MCP tools were available from session start.", len(tools))
            await post_livekit_task_event(
                task_id=task_id,
                room_name=ctx.room.name if ctx.room else None,
                event="agent_debug",
                status="tools_available",
                reason="from_session_start",
                extra={"tool_count": len(tools)},
            )
            await emit_livekit_diagnostic(
                "tools_available",
                reason="from_session_start",
                extra={"details": {"tool_count": len(tools)}},
            )
    except Exception as exc:
        logger.exception("First greeting generation/playout failed: %s", exc)
        await post_livekit_task_event(
            task_id=task_id,
            room_name=ctx.room.name if ctx.room else None,
            event="agent_debug",
            status="greeting_failed",
            reason=str(exc),
            extra={"exception_type": type(exc).__name__},
        )
        await emit_livekit_diagnostic(
            "greeting_failed",
            reason=str(exc),
            extra={"exception_type": type(exc).__name__},
        )
        raise

    max_call_seconds = int(os.getenv("LIVEKIT_AGENT_MAX_CALL_SECONDS", "900"))
    try:
        await asyncio.wait_for(call_done.wait(), timeout=max_call_seconds)
    except asyncio.TimeoutError:
        logger.info("Max call keepalive reached; ending agent job.")


worker_options = WorkerOptions(
    entrypoint_fnc=entrypoint,
    prewarm_fnc=prewarm,
    agent_name=AGENT_NAME,
)
server = AgentServer.from_server_options(worker_options)


if __name__ == "__main__":
    cli.run_app(server)
