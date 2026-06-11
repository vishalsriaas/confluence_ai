app_name = "agent_army"
app_title = "Agent Army"
app_publisher = "SRIAAS"
app_description = "Standalone Frappe AI agent orchestration platform"
app_email = "webdevelopersriaas@gmail.com"
app_license = "MIT"

required_apps = ["frappe"]

after_install = "agent_army.install.after_install"
after_migrate = "agent_army.install.after_migrate"

scheduled_events = {
    "all": [
        "agent_army.services.dispatcher.enqueue_ready_batches",
        "agent_army.services.scheduler.process_deadlines",
    ],
}

fixtures = [
    {"dt": "Workspace", "filters": [["module", "=", "Agent Army"]]},
    {"dt": "Role", "filters": [["name", "in", ["Agent Army Manager", "Agent Army Operator"]]]},
]
