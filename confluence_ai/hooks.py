app_name = "confluence_ai"
app_title = "Confluence AI"
app_publisher = "SRIAAS"
app_description = "Standalone Frappe AI agent orchestration platform"
app_email = "webdevelopersriaas@gmail.com"
app_license = "MIT"

required_apps = ["frappe"]

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
        ],
    },
}

doc_events = {
    "Chat Message": {
        "after_insert": "confluence_ai.services.order_confirmation.on_chat_message_after_insert",
    },
}

fixtures = [
    {"dt": "Workspace", "filters": [["module", "=", "Confluence AI"]]},
    {"dt": "Role", "filters": [["name", "in", ["Confluence AI Manager", "Confluence AI Operator"]]]},
]
