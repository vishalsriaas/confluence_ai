import frappe
import json

def run():
    accounts = frappe.get_all('AI Channel Account', fields=['name', 'account_name', 'channel_type', 'base_url', 'endpoint_paths_json'])
    print("ALL CHANNEL ACCOUNTS:")
    print(json.dumps(accounts, indent=2))
