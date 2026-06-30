from __future__ import annotations

import frappe

from confluence_ai.services.auth import require_access
from confluence_ai.services.inbound_sales import resolve_latest_inbound_metadata
from confluence_ai.services.utils import get_request_json


@frappe.whitelist(allow_guest=True, methods=["POST"])
def resolve_call() -> dict:
    """Resolve prepared inbound-call metadata for the LiveKit worker."""
    require_access("mcp")
    payload = get_request_json()
    return resolve_latest_inbound_metadata(payload)
