import frappe
def run():
    print(frappe.get_all("AI Access Token", fields=["name", "scope", "enabled"]))
