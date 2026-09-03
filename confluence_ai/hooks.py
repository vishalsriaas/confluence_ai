app_name = "confluence_ai"
app_title = "Confluence AI"
app_publisher = "SRIAAS"
app_description = "Standalone Frappe AI agent orchestration platform"
app_email = "webdevelopersriaas@gmail.com"
app_license = "MIT"

required_apps = ["frappe"]

app_include_js = ["/assets/confluence_ai/js/company_switcher.js?v=20260716-wa-chat-hub"]

tenant_doctypes = [
    "AI Access Token",
    "AI ACP Event",
    "AI Agent",
    "AI Agent Group",
    "AI Call Log",
    "AI Channel Account",
    "AI Error Log",
    "AI Event Route",
    "AI Do Not Follow Up",
    "AI Fresh Follow Up Settings",
    "AI Fresh Follow Up Workflow",
    "AI Knowledge Category",
    "AI Knowledge Chunk",
    "AI Knowledge Document",
    "AI MCP Server",
    "AI MCP Tool",
    "AI Provider Event",
    "AI Sales Call Outcome",
    "AI Sales Disease Route",
    "AI Sales Follow Up",
    "AI Sales Lead",
    "AI Task",
    "AI Task Attempt",
    "AI Task Batch",
    "AI Task Template",
    "AI Tool Permission",
    "AI Webhook Event",
    "AI WhatsApp Template Map",
    "AI Repeat Follow Up Workflow",
    "Order Confirmation Workflow",
    "Chat Action Log",
    "Chat AI Suggestion",
    "Chat Assignment Rule",
    "Chat Channel Account",
    "Chat Channel Session",
    "Chat Contact",
    "Chat Contact Channel Profile",
    "Chat Conversation",
    "Chat Message",
    "Chat Queue Event",
    "WA AI Knowledge Base",
    "WA AI Tool Permission",
    "WA Channel Context",
    "WA Channel Pipeline Map",
    "WA Lead AI Insight",
    "WA Lead OCR Result",
    "WA LLM Provider",
    "WA MCP Server",
    "WA MCP Tool Endpoint",
]

doctype_js = {doctype: "public/js/company_form_defaults.js" for doctype in tenant_doctypes}
doctype_js["Order Confirmation Settings"] = "public/js/order_confirmation_settings_tenant.js"
doctype_js["AI Repeat Follow Up Settings"] = "public/js/company_form_defaults.js"

after_install = "confluence_ai.install.after_install"
after_migrate = "confluence_ai.install.after_migrate"

scheduler_events = {
    "all": [
        "confluence_ai.services.dispatcher.enqueue_ready_batches",
        "confluence_ai.services.scheduler.process_deadlines",
    ],
    "cron": {
        "* * * * *": [
            "confluence_ai.services.order_confirmation.process_due_workflows",
            "confluence_ai.services.fresh_followup.process_due_workflows",
            "confluence_ai.services.repeat_followup.process_due_workflows",
            "confluence_ai.services.vobiz.process_missing_recording_callbacks",
            "confluence_ai.services.call_disposition.process_stale_missing_transcript_dispositions",
        ],
    },
}

doc_events = {
    "*": {
        "before_insert": "confluence_ai.tenant.apply_company_to_doc",
        "before_save": "confluence_ai.tenant.apply_company_to_blank_doc",
    },
    "Chat Message": {
        "after_insert": "confluence_ai.services.order_confirmation.on_chat_message_after_insert",
    },
    "Order Confirmation Settings": {
        "before_save": "confluence_ai.tenant.protect_order_confirmation_settings",
    },
    "AI Repeat Follow Up Settings": {
        "before_save": "confluence_ai.tenant.apply_company_to_blank_doc",
    },
}


permission_query_conditions = {
    "AI Company": "confluence_ai.tenant.ai_company_query_condition",
    "AI Access Token": "confluence_ai.tenant.ai_access_token_query_condition",
    "AI ACP Event": "confluence_ai.tenant.ai_acp_event_query_condition",
    "AI Agent": "confluence_ai.tenant.ai_agent_query_condition",
    "AI Agent Group": "confluence_ai.tenant.ai_agent_group_query_condition",
    "AI Call Log": "confluence_ai.tenant.ai_call_log_query_condition",
    "AI Channel Account": "confluence_ai.tenant.ai_channel_account_query_condition",
    "AI Error Log": "confluence_ai.tenant.ai_error_log_query_condition",
    "AI Event Route": "confluence_ai.tenant.ai_event_route_query_condition",
    "AI Do Not Follow Up": "confluence_ai.tenant.ai_do_not_follow_up_query_condition",
    "AI Fresh Follow Up Settings": "confluence_ai.tenant.ai_fresh_follow_up_settings_query_condition",
    "AI Fresh Follow Up Workflow": "confluence_ai.tenant.ai_fresh_follow_up_workflow_query_condition",
    "AI Knowledge Category": "confluence_ai.tenant.ai_knowledge_category_query_condition",
    "AI Knowledge Chunk": "confluence_ai.tenant.ai_knowledge_chunk_query_condition",
    "AI Knowledge Document": "confluence_ai.tenant.ai_knowledge_document_query_condition",
    "AI MCP Server": "confluence_ai.tenant.ai_mcp_server_query_condition",
    "AI MCP Tool": "confluence_ai.tenant.ai_mcp_tool_query_condition",
    "AI Provider Event": "confluence_ai.tenant.ai_provider_event_query_condition",
    "AI Sales Call Outcome": "confluence_ai.tenant.ai_sales_call_outcome_query_condition",
    "AI Sales Disease Route": "confluence_ai.tenant.ai_sales_disease_route_query_condition",
    "AI Sales Follow Up": "confluence_ai.tenant.ai_sales_follow_up_query_condition",
    "AI Sales Lead": "confluence_ai.tenant.ai_sales_lead_query_condition",
    "AI Task": "confluence_ai.tenant.ai_task_query_condition",
    "AI Task Attempt": "confluence_ai.tenant.ai_task_attempt_query_condition",
    "AI Task Batch": "confluence_ai.tenant.ai_task_batch_query_condition",
    "AI Task Template": "confluence_ai.tenant.ai_task_template_query_condition",
    "AI Tool Permission": "confluence_ai.tenant.ai_tool_permission_query_condition",
    "AI Webhook Event": "confluence_ai.tenant.ai_webhook_event_query_condition",
    "AI WhatsApp Template Map": "confluence_ai.tenant.ai_whatsapp_template_map_query_condition",
    "AI Repeat Follow Up Workflow": "confluence_ai.tenant.ai_repeat_follow_up_workflow_query_condition",
    "Order Confirmation Workflow": "confluence_ai.tenant.order_confirmation_workflow_query_condition",
    "Chat Action Log": "confluence_ai.tenant.chat_action_log_query_condition",
    "Chat AI Suggestion": "confluence_ai.tenant.chat_ai_suggestion_query_condition",
    "Chat Assignment Rule": "confluence_ai.tenant.chat_assignment_rule_query_condition",
    "Chat Channel Account": "confluence_ai.tenant.chat_channel_account_query_condition",
    "Chat Channel Session": "confluence_ai.tenant.chat_channel_session_query_condition",
    "Chat Contact Channel Profile": "confluence_ai.tenant.chat_contact_channel_profile_query_condition",
    "Chat Queue Event": "confluence_ai.tenant.chat_queue_event_query_condition",
    "WA AI Knowledge Base": "confluence_ai.tenant.wa_ai_knowledge_base_query_condition",
    "WA AI Tool Permission": "confluence_ai.tenant.wa_ai_tool_permission_query_condition",
    "WA Channel Context": "confluence_ai.tenant.wa_channel_context_query_condition",
    "WA Channel Pipeline Map": "confluence_ai.tenant.wa_channel_pipeline_map_query_condition",
    "WA Lead AI Insight": "confluence_ai.tenant.wa_lead_ai_insight_query_condition",
    "WA Lead OCR Result": "confluence_ai.tenant.wa_lead_ocr_result_query_condition",
    "WA LLM Provider": "confluence_ai.tenant.wa_llm_provider_query_condition",
    "WA MCP Server": "confluence_ai.tenant.wa_mcp_server_query_condition",
    "WA MCP Tool Endpoint": "confluence_ai.tenant.wa_mcp_tool_endpoint_query_condition",
}

has_permission = {
    "AI Company": "confluence_ai.tenant.ai_company_has_permission",
}

fixtures = [
    {"dt": "Workspace", "filters": [["module", "=", "Confluence AI"]]},
    {"dt": "Role", "filters": [["name", "in", ["Confluence AI Manager", "Confluence AI Operator"]]]},
]
