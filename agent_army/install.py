from __future__ import annotations

import frappe


def after_install() -> None:
    ensure_roles()
    ensure_settings()


def after_migrate() -> None:
    ensure_roles()
    ensure_settings()


def ensure_roles() -> None:
    for role_name in ("Agent Army Manager", "Agent Army Operator"):
        if not frappe.db.exists("Role", role_name):
            role = frappe.new_doc("Role")
            role.role_name = role_name
            role.desk_access = 1
            role.insert(ignore_permissions=True)


def ensure_settings() -> None:
    if frappe.db.exists("Agent Army Settings", "Agent Army Settings"):
        return

    settings = frappe.new_doc("Agent Army Settings")
    settings.update(
        {
            "doctype": "Agent Army Settings",
            "ingest_queue": "agent_ingest",
            "dispatch_queue": "agent_dispatch",
            "whatsapp_queue": "agent_whatsapp",
            "voice_queue": "agent_voice",
            "llm_queue": "agent_llm",
            "callbacks_queue": "agent_callbacks",
            "deadline_queue": "agent_deadline",
            "default_timezone": "Asia/Kolkata",
            "default_task_timeout_seconds": 900,
            "dispatch_batch_size": 500,
        }
    )
    settings.insert(ignore_permissions=True)
