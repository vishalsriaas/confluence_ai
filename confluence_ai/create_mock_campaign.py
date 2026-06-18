import frappe

def create_tool(name, desc):
    if frappe.db.exists("AI MCP Tool", {"tool_name": name}):
        frappe.delete_doc("AI MCP Tool", frappe.db.get_value("AI MCP Tool", {"tool_name": name}), force=True)
    t = frappe.new_doc("AI MCP Tool")
    t.tool_name = name
    t.description = desc
    t.enabled = 1
    t.client_doctype = "AI Agent"
    t.operation_type = "Read"
    t.append("input_parameters", {
        "parameter_name": "name",
        "type": "string",
        "required": 1,
        "description": "Agent name parameter"
    })
    t.append("match_filters", {
        "client_field": "name",
        "value_source": "From Tool Arguments",
        "source_value": "name",
        "is_filter": 1
    })
    t.append("fields_to_read", {
        "client_field": "agent_name",
        "value_source": "From Tool Arguments",
        "source_value": "name",
        "is_filter": 0
    })
    t.insert(ignore_permissions=True)
    return t

def run():
    # Create Tools
    alpha_get = create_tool("alpha_get_items", "Route A - get items tool")
    alpha_update = create_tool("alpha_update_disposition", "Route A - update disposition tool")
    beta_get = create_tool("beta_get_items", "Route B - get items tool")
    beta_update = create_tool("beta_update_disposition", "Route B - update disposition tool")

    # Create Routes
    # Route A
    route_a_name = "Route A Campaign"
    if frappe.db.exists("AI Event Route", {"route_name": route_a_name}):
        frappe.delete_doc("AI Event Route", frappe.db.get_value("AI Event Route", {"route_name": route_a_name}), force=True)
        
    route_a = frappe.new_doc("AI Event Route")
    route_a.route_name = route_a_name
    route_a.enabled = 1
    route_a.event_key_field = "event"
    route_a.event_value = "route_a"
    route_a.task_template = "tmpl-88"
    route_a.target_agent = "agent-86"
    route_a.dispatch_mode = "Batch"
    route_a.batch_records_field = "records"
    route_a.batch_label = "Route A Campaign"
    route_a.append("allowed_tools", {"tool": alpha_get.name})
    route_a.append("allowed_tools", {"tool": alpha_update.name})
    route_a.insert(ignore_permissions=True)

    # Route B
    route_b_name = "Route B Campaign"
    if frappe.db.exists("AI Event Route", {"route_name": route_b_name}):
        frappe.delete_doc("AI Event Route", frappe.db.get_value("AI Event Route", {"route_name": route_b_name}), force=True)
        
    route_b = frappe.new_doc("AI Event Route")
    route_b.route_name = route_b_name
    route_b.enabled = 1
    route_b.event_key_field = "event"
    route_b.event_value = "route_b"
    route_b.task_template = "tmpl-88"
    route_b.target_agent = "agent-86"
    route_b.dispatch_mode = "Batch"
    route_b.batch_records_field = "records"
    route_b.batch_label = "Route B Campaign"
    route_b.append("allowed_tools", {"tool": beta_get.name})
    route_b.append("allowed_tools", {"tool": beta_update.name})
    route_b.insert(ignore_permissions=True)

    frappe.db.commit()
    print("Mock Route A and Route B campaign data populated successfully!")
