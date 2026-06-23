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
        if call_log.from_number and endpoints.get("outbound_phone_number") == call_log.from_number:
            channels.append(row.name)

    seen = set()
    result = []
    for name in channels:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _vobiz_auth_headers(call_log) -> dict:
    auth_id = None
    auth_token = None

    for channel_name in _channel_candidates(call_log):
        channel = frappe.get_doc("AI Channel Account", channel_name)
        auth_id = channel.get("vobiz_auth_id") or auth_id
        auth_token = channel.get_password("vobiz_auth_token", raise_exception=False) or auth_token
        if auth_id and auth_token:
            break

    if not auth_id:
        parsed = urlparse(call_log.external_recording_url or call_log.recording_url or "")
        parts = [part for part in parsed.path.split("/") if part]
        if "Account" in parts:
            idx = parts.index("Account")
            if len(parts) > idx + 1:
                auth_id = parts[idx + 1]

    if not auth_id or not auth_token:
        frappe.throw("Vobiz recording auth is not configured. Add Vobiz Auth ID and Vobiz Auth Token to the matching AI Channel Account.")

    return {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}


@frappe.whitelist()
def recording_audio(call_log: str):
    doc = frappe.get_doc("AI Call Log", call_log)
    url = doc.external_recording_url or doc.recording_url
    if not url:
        frappe.throw("No recording URL found for this call log.")

    response = requests.get(url, headers=_vobiz_auth_headers(doc), timeout=60)
    if not response.ok:
        frappe.throw(f"Vobiz recording fetch failed with HTTP {response.status_code}: {response.text[:200]}")

    frappe.local.response.filename = f"{doc.name}.wav"
    frappe.local.response.filecontent = response.content
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.headers = {
        "Content-Type": response.headers.get("Content-Type") or "audio/wav",
        "Cache-Control": "private, max-age=300",
    }
