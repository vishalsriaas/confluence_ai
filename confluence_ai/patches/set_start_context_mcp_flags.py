import frappe


def execute():
    if not frappe.db.has_column("AI Agent MCP Tool", "run_at_call_start"):
        return

    frappe.db.sql(
        """
        update `tabAI Agent MCP Tool`
        set run_at_call_start = 1
        where coalesce(run_at_call_start, 0) = 0
          and coalesce(calling_condition, '') like %s
        """,
        ("%START CONTEXT TOOL%",),
    )
