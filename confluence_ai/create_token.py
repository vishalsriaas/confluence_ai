import frappe
def run():
    token_key = "local_mcp_test_bearer"
    token_secret = "token_secret_value"
    
    # Clean up existing token if exists
    if frappe.db.exists("AI Access Token", {"token_key": token_key}):
        frappe.delete_doc("AI Access Token", frappe.db.get_value("AI Access Token", {"token_key": token_key}), force=True)
        
    doc = frappe.new_doc("AI Access Token")
    doc.enabled = 1
    doc.token_key = token_key
    doc.token_name = "Local MCP Test Token"
    doc.token_secret = token_secret
    doc.scope = "mcp, ingest, status, control, webhook"
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"Created token: {token_key}:{token_secret}")
