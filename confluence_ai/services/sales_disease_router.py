from __future__ import annotations

import json
import re
from typing import Any

import frappe

from confluence_ai.services.sales_context import SALES_EVENT_VALUES
from confluence_ai.services.utils import parse_json_list


MATCH_FIELDS = (
    "disease_or_concern",
    "disease",
    "disease_name",
    "condition",
    "concern",
    "product_interest",
    "campaign",
    "profile_key",
    "payload_json.disease_or_concern",
    "payload_json.disease",
    "payload_json.condition",
    "payload_json.product_interest",
    "payload_json.profile_key",
)


def resolve_sales_disease_route(
    context: dict,
    payload: dict | None = None,
    *,
    route: "frappe.Document | None" = None,
) -> dict:
    """Return target override metadata for disease-specific sales calls."""
    if not _doctype_exists("AI Sales Disease Route"):
        return {}

    event = (context.get("event") or (payload or {}).get("event") or getattr(route, "event_value", "") or "").strip()
    if event and event not in SALES_EVENT_VALUES:
        return {}

    source_system = (context.get("source_system") or (payload or {}).get("source_system") or "").strip()
    haystack = _build_match_text(context, payload or {})
    rows = frappe.get_all(
        "AI Sales Disease Route",
        filters={"enabled": 1},
        fields=[
            "name",
            "route_name",
            "event_values",
            "source_system",
            "disease_key",
            "aliases_json",
            "target_agent",
            "channel_account",
            "profile_key",
            "outbound_phone_number",
            "sip_trunk_id",
            "sip_uri",
            "is_default",
            "priority",
        ],
        order_by="is_default asc, priority asc, creation asc",
    )

    default_row = None
    for row in rows:
        if not _event_allowed(row.get("event_values"), event):
            continue
        if row.get("source_system") and row.get("source_system") != source_system:
            continue
        if row.get("is_default"):
            default_row = default_row or row
            continue

        aliases = _aliases_for_row(row)
        matched_alias = _find_alias(haystack, aliases)
        if matched_alias:
            return _as_selection(row, matched_alias=matched_alias)

    return _as_selection(default_row, matched_alias="default") if default_row else {}


def resolve_inbound_sales_route(payload: dict) -> dict:
    """Resolve a sales route from a Vobiz inbound webhook.

    Inbound calls should be mapped by the Vobiz trunk/customer-facing number,
    not by the outbound disease route. The strongest match is TrunkID, with
    optional DID/domain fallback for accounts where Vobiz sends alternate IDs.
    """
    if not _doctype_exists("AI Sales Disease Route"):
        return {}

    trunk_id = str(payload.get("TrunkID") or payload.get("trunk_id") or "").strip()
    called_number = str(payload.get("To") or payload.get("to") or payload.get("to_number") or "").strip()
    domain = str(payload.get("Domain") or payload.get("domain") or "").strip()

    rows = frappe.get_all(
        "AI Sales Disease Route",
        filters={"enabled": 1},
        fields=[
            "name",
            "route_name",
            "disease_key",
            "target_agent",
            "channel_account",
            "profile_key",
            "outbound_phone_number",
            "sip_trunk_id",
            "sip_uri",
            "inbound_vobiz_trunk_id",
            "inbound_phone_number",
            "inbound_domain",
            "is_default",
            "priority",
        ],
        order_by="priority asc, creation asc",
    )

    for row in rows:
        if trunk_id and _same_token(trunk_id, row.get("inbound_vobiz_trunk_id")):
            return _as_selection(row, matched_alias="inbound_trunk_id")

    for row in rows:
        if called_number and _same_phone(called_number, row.get("inbound_phone_number")):
            return _as_selection(row, matched_alias="inbound_phone_number")

    for row in rows:
        if domain and _same_token(domain, row.get("inbound_domain")):
            return _as_selection(row, matched_alias="inbound_domain")

    return {}


def apply_sales_route_context(context: dict, selection: dict) -> dict:
    if not selection:
        return context
    enriched = dict(context)
    disease_key = selection.get("disease_key")
    if disease_key and not enriched.get("disease_or_concern"):
        enriched["disease_or_concern"] = disease_key
    enriched["selected_sales_route"] = {
        "route": selection.get("route"),
        "route_name": selection.get("route_name"),
        "disease_key": disease_key,
        "matched_alias": selection.get("matched_alias"),
        "target_agent": selection.get("target_agent"),
        "channel_account": selection.get("channel_account"),
        "outbound_phone_number": selection.get("outbound_phone_number"),
        "sip_trunk_id": selection.get("sip_trunk_id"),
        "sip_uri": selection.get("sip_uri"),
        "profile_key": selection.get("profile_key"),
    }
    if selection.get("profile_key") and not enriched.get("profile_key"):
        enriched["profile_key"] = selection["profile_key"]
    if selection.get("outbound_phone_number") and not enriched.get("outbound_phone_number"):
        enriched["outbound_phone_number"] = selection["outbound_phone_number"]
    return enriched


def _as_selection(row: dict | None, *, matched_alias: str) -> dict:
    if not row:
        return {}
    return {
        "route": row.get("name"),
        "route_name": row.get("route_name"),
        "disease_key": row.get("disease_key"),
        "matched_alias": matched_alias,
        "target_agent": row.get("target_agent"),
        "channel_account": row.get("channel_account"),
        "profile_key": row.get("profile_key"),
        "outbound_phone_number": row.get("outbound_phone_number"),
        "sip_trunk_id": row.get("sip_trunk_id"),
        "sip_uri": row.get("sip_uri"),
    }


def _aliases_for_row(row: dict) -> list[str]:
    aliases = []
    if row.get("disease_key"):
        aliases.append(row["disease_key"])
    raw = row.get("aliases_json")
    if raw:
        parsed = parse_json_list(raw, "Aliases JSON")
        if isinstance(parsed, list):
            aliases.extend(str(item) for item in parsed if item)
    return [_normalize(alias) for alias in aliases if _normalize(alias)]


def _find_alias(haystack: str, aliases: list[str]) -> str | None:
    if not haystack:
        return None
    for alias in aliases:
        pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        if re.search(pattern, haystack):
            return alias
    return None


def _event_allowed(event_values: str | None, event: str) -> bool:
    if not event_values:
        return True
    allowed = {item.strip() for item in event_values.split(",") if item.strip()}
    return not allowed or event in allowed


def _build_match_text(context: dict, payload: dict) -> str:
    values: list[str] = []
    for source in (context, payload):
        for field in MATCH_FIELDS:
            value = _get_nested_value(source, field)
            if value not in (None, ""):
                values.append(str(value))
    payload_json = context.get("payload_json") or payload.get("payload_json")
    if isinstance(payload_json, dict):
        values.extend(str(payload_json.get(key) or "") for key in ("items", "notes", "tags"))
    return _normalize(" ".join(values))


def _get_nested_value(data: dict, path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if isinstance(current, str) and part == "payload_json":
            try:
                current = json.loads(current)
            except Exception:
                return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _normalize(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9+]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _same_token(left: str | None, right: str | None) -> bool:
    return bool(left and right and str(left).strip().lower() == str(right).strip().lower())


def _same_phone(left: str | None, right: str | None) -> bool:
    left_digits = re.sub(r"\D+", "", str(left or ""))
    right_digits = re.sub(r"\D+", "", str(right or ""))
    if not left_digits or not right_digits:
        return False
    return left_digits == right_digits or left_digits[-10:] == right_digits[-10:]


def _doctype_exists(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))
