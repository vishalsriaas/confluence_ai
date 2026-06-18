import requests
import json

def run():
    url = "http://127.0.0.1:8003/api/method/confluence_ai.api.mcp.gateway"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    }
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Army-Token": "token-396:token_secret_value",
        "X-Confluence-Task-ID": "task-446"
    }
    
    resp = requests.post(url, json=payload, headers=headers)
    print("Status:", resp.status_code)
    print("Response JSON:")
    print(json.dumps(resp.json(), indent=2))
