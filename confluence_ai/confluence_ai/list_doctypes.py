import frappe

def run():
    workspaces = frappe.get_all('Workspace', fields=['name', 'title', 'category', 'is_standard', 'public'])
    print("WORKSPACES:")
    for w in workspaces:
        print(f"- Name: {w.name}, Title: {w.title}, Category: {w.category}")
