import frappe


def execute():
    frappe.reload_doc("confluence_ai", "doctype", "ai_mcp_tool")

    if not frappe.db.has_column("AI MCP Tool", "include_related_messages"):
        return
    if not frappe.db.has_column("AI Agent MCP Tool", "run_at_call_start"):
        return

    frappe.db.sql(
        """
        update `tabAI MCP Tool` tool
        set
            tool.include_related_messages = 1,
            tool.related_message_doctype = coalesce(nullif(tool.related_message_doctype, ''), 'Chat Message'),
            tool.related_message_conversation_field = coalesce(nullif(tool.related_message_conversation_field, ''), 'conversation'),
            tool.related_message_body_field = coalesce(nullif(tool.related_message_body_field, ''), 'body'),
            tool.related_message_direction_field = coalesce(nullif(tool.related_message_direction_field, ''), 'direction'),
            tool.related_message_sender_field = coalesce(nullif(tool.related_message_sender_field, ''), 'sender_type'),
            tool.related_message_time_field = coalesce(nullif(tool.related_message_time_field, ''), 'creation'),
            tool.related_message_limit = coalesce(nullif(tool.related_message_limit, 0), 20)
        where tool.operation_type = 'Read'
          and tool.client_doctype = 'Chat Conversation'
          and exists (
              select 1
              from `tabAI Agent MCP Tool` allowed
              where allowed.tool = tool.name
                and coalesce(allowed.run_at_call_start, 0) = 1
          )
        """
    )
