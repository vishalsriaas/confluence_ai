from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

import frappe

from confluence_ai.services.utils import now


def _request_headers() -> dict:
    request = getattr(frappe.local, "request", None)
    if not request:
        return {}
    return {str(key).lower(): value for key, value in dict(request.headers).items()}


def _bearer_token() -> str:
    headers = _request_headers()
    mcp_token = headers.get("x-mcp-token")
    if mcp_token:
        return mcp_token

    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-agent-army-token") or ""


def require_access(scope: str) -> str:
    token_value = _bearer_token()
    if ":" not in token_value:
        frappe.throw("Missing Confluence AI access token", frappe.PermissionError)

    token_key, secret = token_value.split(":", 1)
    if not frappe.db.exists("AI Access Token", token_key):
        frappe.throw("Invalid Confluence AI access token", frappe.PermissionError)

    doc = frappe.get_doc("AI Access Token", token_key)
    if not doc.enabled:
        frappe.throw("Confluence AI access token is disabled", frappe.PermissionError)

    expected = doc.get_password("token_secret") or ""
    if not hmac.compare_digest(expected, secret):
        frappe.throw("Invalid Confluence AI access token", frappe.PermissionError)

    scopes = {item.strip() for item in (doc.scope or "").split(",") if item.strip()}
    if scope and scope not in scopes and "*" not in scopes:
        frappe.throw(f"Confluence AI token missing scope: {scope}", frappe.PermissionError)

    if doc.expires_at and frappe.utils.get_datetime(doc.expires_at) < datetime.now():
        frappe.throw("Confluence AI access token expired", frappe.PermissionError)

    frappe.db.set_value("AI Access Token", doc.name, "last_used_at", now(), update_modified=False)
    return doc.name


def sign_payload(secret: str, body: str | bytes) -> str:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hmac.new((secret or "").encode("utf-8"), body or b"", hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: str | bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature)
