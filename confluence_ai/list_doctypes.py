import frappe
import json

def run():
    workspaces = frappe.get_all('Workspace', fields=['name', 'title', 'module'])
    print("WORKSPACES:")
    print(json.dumps(workspaces, indent=2))
    
    try:
        build_ws = frappe.get_doc('Workspace', 'Build')
        print("\nBUILD WORKSPACE SCHEMA:")
        print(json.dumps(build_ws.as_dict(), indent=2, default=str))
    except Exception as e:
        print(f"\nCould not get Build workspace: {e}")
