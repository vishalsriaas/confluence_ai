from __future__ import annotations

import json
from urllib.parse import urlparse

import frappe
import requests


def _parse_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _channel_candidates(call_log) -> list:
    filters = []
    if call_log.trunk_id:
        filters.append({"trunk_id": call_log.trunk_id})

    channels = []
    for flt in filters:
        channels.extend(frappe.get_all("AI Channel Account", filters=flt, pluck="name"))

    if frappe.db.exists("DocType", "AI Sales Disease Route"):
        route_filters = []
        if call_log.trunk_id:
            route_filters.append({"inbound_vobiz_trunk_id": call_log.trunk_id})
        if call_log.domain:
            route_filters.append({"inbound_domain": call_log.domain})
        if call_log.to_number:
            route_filters.append({"inbound_phone_number": call_log.to_number})

        for flt in route_filters:
            for row in frappe.get_all("AI Sales Disease Route", filters=flt, fields=["channel_account"]):
                if row.channel_account:
                    channels.append(row.channel_account)

    # Vobiz callback trunk IDs may be provider UUIDs while LiveKit stores ST_* IDs,
    # so also match by sip_uri/domain or outbound phone in endpoint_paths_json.
    for row in frappe.get_all(
        "AI Channel Account",
        fields=["name", "endpoint_paths_json"],
        filters={"enabled": 1},
    ):
        endpoints = _parse_json(row.endpoint_paths_json)
        if call_log.domain and endpoints.get("sip_uri") == call_log.domain:
            channels.append(row.name)
        if call_log.domain and endpoints.get("inbound_domain") == call_log.domain:
            channels.append(row.name)
        if call_log.trunk_id and endpoints.get("inbound_vobiz_trunk_id") == call_log.trunk_id:
            channels.append(row.name)
        if call_log.trunk_id and endpoints.get("vobiz_trunk_id") == call_log.trunk_id:
            channels.append(row.name)
        if call_log.from_number and endpoints.get("outbound_phone_number") == call_log.from_number:
            channels.append(row.name)

    seen = set()
    result = []
    for name in channels:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _vobiz_auth_candidates(call_log) -> list[dict[str, str]]:
    parsed = urlparse(call_log.external_recording_url or call_log.recording_url or "")
    url_account_id = None
    parts = [part for part in parsed.path.split("/") if part]
    if "Account" in parts:
        idx = parts.index("Account")
        if len(parts) > idx + 1:
            url_account_id = parts[idx + 1]

    candidates: list[dict[str, str]] = []
    channel_names = list(_channel_candidates(call_log))

    if url_account_id:
        for row in frappe.get_all(
            "AI Channel Account",
            filters={"enabled": 1, "vobiz_auth_id": url_account_id},
            pluck="name",
        ):
            channel_names.append(row)

    seen_channels = set()
    for channel_name in channel_names:
        if channel_name in seen_channels:
            continue
        seen_channels.add(channel_name)
        channel = frappe.get_doc("AI Channel Account", channel_name)
        auth_id = channel.get("vobiz_auth_id")
        auth_token = channel.get_password("vobiz_auth_token", raise_exception=False)
        if auth_id and auth_token:
            candidates.append({"X-Auth-ID": auth_id, "X-Auth-Token": auth_token})
        # Some Vobiz recording URLs contain the provider account ID even when
        # the channel row stores a SIP/trunk account alias. Try the URL account
        # with the same token before failing with a 401/403.
        if url_account_id and auth_token and url_account_id != auth_id:
            candidates.append({"X-Auth-ID": url_account_id, "X-Auth-Token": auth_token})

    if not candidates:
        frappe.throw("Vobiz recording auth is not configured. Add Vobiz Auth ID and Vobiz Auth Token to the matching AI Channel Account.")

    unique = []
    seen = set()
    for headers in candidates:
        key = (headers["X-Auth-ID"], headers["X-Auth-Token"])
        if key not in seen:
            seen.add(key)
            unique.append(headers)
    return unique


@frappe.whitelist()
def recording_audio(call_log: str):
    doc = frappe.get_doc("AI Call Log", call_log)
    url = doc.external_recording_url or doc.recording_url
    if not url:
        frappe.throw("No recording URL found for this call log.")

    last_response = None
    for headers in _vobiz_auth_candidates(doc):
        response = requests.get(url, headers=headers, timeout=60)
        if response.ok:
            break
        last_response = response
    else:
        response = last_response

    if not response or not response.ok:
        detail = response.text[:200] if response is not None else "No response"
        status_code = response.status_code if response is not None else "unknown"
        frappe.throw(f"Vobiz recording fetch failed with HTTP {status_code}: {detail}")

    frappe.local.response.filename = f"{doc.name}.wav"
    frappe.local.response.filecontent = response.content
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.headers = {
        "Content-Type": response.headers.get("Content-Type") or "audio/wav",
        "Cache-Control": "private, max-age=300",
    }
