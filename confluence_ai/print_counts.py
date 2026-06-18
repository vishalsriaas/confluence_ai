import frappe
def run():
    tasks = frappe.get_all("AI Task", filters={"task_batch": "batch-445"}, fields=["name", "status", "context_json"])
    for t in tasks:
        print(f"Task: {t.name} | Status: {t.status} | Context: {t.context_json}")
