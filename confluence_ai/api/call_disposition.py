from __future__ import annotations

import frappe


@frappe.whitelist(methods=["POST"])
def process(call_log: str, force: int = 0) -> dict:
    from confluence_ai.services.call_disposition import process_call_log

    return process_call_log(call_log, force=bool(int(force or 0)))
