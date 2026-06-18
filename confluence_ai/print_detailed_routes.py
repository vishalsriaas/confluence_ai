import frappe
def run():
    routes = frappe.get_all("AI Event Route", fields=["name", "route_name", "enabled", "event_value", "dispatch_mode", "source_system"])
    for r in routes:
        doc = frappe.get_doc("AI Event Route", r.name)
        print("Name:", doc.name)
        print("Route Name:", doc.route_name)
        print("Enabled:", doc.enabled)
        print("Event Key Field:", doc.event_key_field)
        print("Event Value:", doc.event_value)
        print("Source System:", doc.source_system)
