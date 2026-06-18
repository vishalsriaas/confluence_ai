import frappe
def run():
    routes = frappe.get_all("AI Event Route", fields=["name", "route_name", "enabled", "event_value", "dispatch_mode", "source_system"])
    for r in routes:
        print(f"Name: {r.name} | Route Name: {r.route_name} | Enabled: {r.enabled} | Match Event: {r.event_value} | Mode: {r.dispatch_mode} | Source: {r.source_system}")
