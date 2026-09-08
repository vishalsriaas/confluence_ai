import frappe


def execute():
    # Only initialize newly introduced settings. Preserve an explicit disable.
    for field, value in (
        ("enable_vobiz_transcript_recovery", "1"),
        ("transcript_recovery_grace_minutes", "5"),
        ("transcript_recovery_retry_minutes", "5"),
        ("transcript_recovery_max_checks", "3"),
    ):
        exists = frappe.db.sql(
            "select value from tabSingles where doctype=%s and field=%s",
            ("Confluence AI Settings", field),
        )
        if not exists:
            frappe.db.set_single_value("Confluence AI Settings", field, value)
