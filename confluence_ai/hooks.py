app_name = "confluence_ai"
app_title = "Confluence AI"
app_publisher = "SRIAAS"
app_description = "Standalone Frappe AI agent orchestration platform"
app_email = "webdevelopersriaas@gmail.com"
app_license = "MIT"

required_apps = ["frappe"]

after_install = "confluence_ai.install.after_install"
after_migrate = "confluence_ai.install.after_migrate"

scheduled_events = {
    "all": [
        "confluence_ai.services.dispatcher.enqueue_ready_batches",
        "confluence_ai.services.scheduler.process_deadlines",
    ],
}

fixtures = [
    {"dt": "Workspace", "filters": [["module", "=", "Confluence AI"]]},
    {"dt": "Role", "filters": [["name", "in", ["Confluence AI Manager", "Confluence AI Operator"]]]},
]
