import json
import requests
from urllib.parse import quote, urljoin
import frappe
from confluence_ai.services.auth import require_access
from confluence_ai.services.utils import as_json, create_error, get_request_json, parse_json_object, record_provider_event
from confluence_ai.services.mcp import assert_tool_allowed







@frappe.whitelist(allow_guest=True, methods=["POST"])
def gateway() -> dict:
    # 1. Authenticate the Bearer Token
    require_access("mcp")

    # 2. Parse the request JSON-RPC
    req = get_request_json()
    method = req.get("method")
    params = req.get("params", {})
    req_id = req.get("id", 1)

    # 3. Retrieve task_id from headers or params
    task_id = frappe.request.headers.get("X-Confluence-Task-ID") or params.get("task_id")
    
    if method == "tools/list":
        try:
            tools = get_allowed_tools(task_id)
            result_tools = []
            for tool in tools:
                input_schema = {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
                for p in tool.input_parameters:
                    input_schema["properties"][p.parameter_name] = {
                        "type": p.type,
                        "description": p.description
                    }
                    if p.required:
                        input_schema["required"].append(p.parameter_name)
                
                result_tools.append({
                    "name": tool.tool_name,
                    "description": tool.description,
                    "inputSchema": input_schema
                })
            
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": result_tools
                }
            }
        except Exception as e:
            frappe.log_error(title="MCP tools/list failed", message=frappe.get_traceback())
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)}
            }

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        try:
            # Load tool configuration
            tool_list = frappe.get_all("AI MCP Tool", filters={"tool_name": tool_name}, limit=1)
            if not tool_list:
                error_message = f"Tool not found: {tool_name}"
                log_mcp_error(
                    tool_name or "unknown",
                    error_message,
                    arguments,
                    task_id,
                    error_type="MCP Tool Not Found",
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": error_message}
                }
            
            tool = frappe.get_doc("AI MCP Tool", tool_list[0].name)
            
            # Check permission: Verify if the tool is in the list of allowed tools for this task/route
            allowed_tools = [t.name for t in get_allowed_tools(task_id)]
            if tool.name not in allowed_tools:
                error_message = f"Tool {tool_name} is not allowed for the active campaign route."
                log_mcp_error(
                    tool_name,
                    error_message,
                    arguments,
                    task_id,
                    error_type="MCP Tool Not Allowed",
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": error_message}
                }
            
            builtin_result = execute_builtin_sales_tool(tool.tool_name, arguments, task_id)
            if builtin_result is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": builtin_result
                }

            # Execute the tool call
            result = execute_mcp_tool(tool, arguments, task_id)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result
            }
        except Exception as e:
            frappe.log_error(title=f"MCP tools/call failed: {tool_name}", message=frappe.get_traceback())
            log_mcp_error(
                tool_name or "unknown",
                str(e),
                arguments,
                task_id,
                error_type="MCP Tool Call Failed",
                exc=e,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)}
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


def execute_builtin_sales_tool(tool_name: str, arguments: dict, task_id: str | None) -> dict | None:
    """Handle Confluence AI sales tools that need orchestration around configured MCP mappings."""
    if tool_name not in {
        "search_sales_knowledge",
        "lookup_patient_sales_context",
        "create_or_update_lead",
        "create_sales_followup",
        "log_sales_call_outcome",
        "create_draft_patient_encounter_from_sales",
    }:
        return None

    agent = None
    if task_id and frappe.db.exists("AI Task", task_id):
        task_values = frappe.db.get_value("AI Task", task_id, ["assigned_agent", "target_agent"], as_dict=True)
        agent = task_values.assigned_agent or task_values.target_agent

    from confluence_ai.services import sales_context

    if tool_name == "search_sales_knowledge":
        from confluence_ai.services.knowledge_base import retrieve_knowledge

        query = arguments.get("query") or arguments.get("question") or ""
        limit = int(arguments.get("limit") or 5)
        return {"status": "success", "data": retrieve_knowledge(query, agent=agent, limit=limit)}
    if tool_name == "lookup_patient_sales_context":
        return sales_context.lookup_patient_sales_context(arguments, agent=agent)
    if tool_name == "create_or_update_lead":
        return sales_context.create_or_update_lead(arguments, task_id=task_id, agent=agent)
    if tool_name == "create_sales_followup":
        return sales_context.create_sales_followup(arguments, task_id=task_id, agent=agent)
    if tool_name == "log_sales_call_outcome":
        return sales_context.log_sales_call_outcome(arguments, task_id=task_id, agent=agent)
    if tool_name == "create_draft_patient_encounter_from_sales":
        return sales_context.create_draft_patient_encounter_from_sales(arguments, task_id=task_id, agent=agent)
    return None


def get_allowed_tools(task_id: str | None) -> list["frappe.Document"]:
    """Returns the list of allowed MCP tools dynamically scoped to the task's campaign route."""
    if not task_id or not frappe.db.exists("AI Task", task_id):
        # Fallback: return all enabled tools if not scoped by task
        tools_list = frappe.get_all("AI MCP Tool", filters={"enabled": 1})
        return [frappe.get_doc("AI MCP Tool", t.name) for t in tools_list]

    task = frappe.get_doc("AI Task", task_id)
    if not task.task_batch:
        tools_list = frappe.get_all("AI MCP Tool", filters={"enabled": 1})
        return [frappe.get_doc("AI MCP Tool", t.name) for t in tools_list]

    batch = frappe.get_doc("AI Task Batch", task.task_batch)
    
    # Resolve Event Route
    route_id = frappe.db.get_value("AI Event Route", {"route_name": batch.batch_label or batch.source_system})
    if not route_id:
        route_id = frappe.db.get_value("AI Event Route", {"route_name": batch.source_system})
        
    if not route_id:
        tools_list = frappe.get_all("AI MCP Tool", filters={"enabled": 1})
        return [frappe.get_doc("AI MCP Tool", t.name) for t in tools_list]

    route = frappe.get_doc("AI Event Route", route_id)
    if not route.allowed_tools:
        return []

    tools = []
    for r_tool in route.allowed_tools:
        if frappe.db.exists("AI MCP Tool", r_tool.tool):
            t_doc = frappe.get_doc("AI MCP Tool", r_tool.tool)
            if t_doc.enabled:
                tools.append(t_doc)
    return tools


def get_server_headers(server: "frappe.Document") -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = server.get("api_key")
    api_secret = server.get_password("api_secret", raise_exception=False) if server.meta.has_field("api_secret") else None
    
    if api_key and api_secret:
        headers["Authorization"] = f"token {api_key}:{api_secret}"
        return headers
        
    token = server.get_password("bearer_token", raise_exception=False) if server.meta.has_field("bearer_token") else None
    if token:
        headers["Authorization"] = token if token.startswith(("Bearer", "token")) else f"Bearer {token}"
        
    return headers


def validate_filter_arguments(tool: "frappe.Document", arguments: dict):
    """Ensures required filters have values supplied and blocks empty filter updates."""
    op_type = tool.operation_type or "Read"
    if op_type == "Update" and not tool.match_filters:
        frappe.throw("Validation Error: Update operations must have at least one search filter configured to prevent mass record updates.")
        
    for m in tool.match_filters:
        if m.value_source == "From Tool Arguments":
            src = m.source_value or m.client_field
            val = arguments.get(src)
            if val is None or val == "":
                frappe.throw(f"Validation Error: Required filter argument '{src}' was not provided by the agent to locate records.")


def execute_mcp_tool(tool: "frappe.Document", arguments: dict, task_id: str | None) -> dict:
    """Executes the tool natively (using REST API of client ERP mapped to server config)."""
    arguments = enrich_order_confirmation_issue_arguments(tool, dict(arguments or {}), task_id)
    validate_filter_arguments(tool, arguments)
    
    server = frappe.get_doc("AI MCP Server", tool.server) if tool.server else None
    if not server:
        # Fallback to local DB execution if no server links
        return execute_local_db_tool(tool, arguments)

    server_url = server.server_url or ""
    headers = get_server_headers(server)

    client_doctype = tool.client_doctype
    op_type = tool.operation_type or "Read"

    if op_type == "Read":
        filters = []
        for m in tool.match_filters:
            val = resolve_mapping_value(m, arguments)
            filters.append([m.client_field, "=", val])
        
        fields = [m.client_field for m in tool.fields_to_read]
        if not fields:
            fields = ["*"]

        url = urljoin(server_url.rstrip("/") + "/", f"api/resource/{client_doctype}")
        params = {
            "filters": json.dumps(filters),
            "fields": json.dumps(fields)
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        # Log response
        result = {"status_code": response.status_code, "ok": response.ok, "body": response.json().get("data", []) if response.ok else response.text[:4000]}
        record_mcp_event(tool.tool_name, arguments, result, task_id)
        
        if not response.ok:
            frappe.throw(f"Client ERP Tool Read failed: {response.text[:200]}")
        return {"status": "success", "data": result["body"]}

    elif op_type == "Create":
        doc_data = {}
        for m in tool.fields_to_write:
            doc_data[m.client_field] = resolve_mapping_value(m, arguments)

        url = urljoin(server_url.rstrip("/") + "/", f"api/resource/{client_doctype}")
        response = requests.post(url, headers=headers, json=doc_data, timeout=30)
        
        result = {"status_code": response.status_code, "ok": response.ok, "body": response.json().get("data", {}) if response.ok else response.text[:4000]}
        record_mcp_event(tool.tool_name, arguments, result, task_id)
        
        if not response.ok:
            frappe.throw(f"Client ERP Tool Create failed: {response.text[:200]}")
        return {"status": "success", "data": result["body"]}

    elif op_type == "Update":
        filters = []
        for m in tool.match_filters:
            val = resolve_mapping_value(m, arguments)
            filters.append([m.client_field, "=", val])

        url_get = urljoin(server_url.rstrip("/") + "/", f"api/resource/{client_doctype}")
        response_get = requests.get(url_get, headers=headers, params={"filters": json.dumps(filters)}, timeout=30)
        
        if not response_get.ok:
            frappe.throw(f"Client ERP Tool Update (Fetch ID) failed: {response_get.text[:200]}")
            
        names = [d.get("name") for d in response_get.json().get("data", [])]
        if not names:
            message = f"No matching {client_doctype} records found to update"
            result = {
                "status_code": response_get.status_code,
                "ok": False,
                "message": message,
                "filters": filters,
            }
            record_mcp_event(tool.tool_name, arguments, result, task_id, error=message)
            log_mcp_error(
                tool.tool_name,
                message,
                arguments,
                task_id,
                error_type="MCP Update No Matching Records",
                response=result,
            )
            frappe.throw(message)

        doc_data = {}
        for m in tool.fields_to_write:
            doc_data[m.client_field] = resolve_mapping_value(m, arguments)

        updates = []
        for name_id in names:
            url_put = urljoin(server_url.rstrip("/") + "/", f"api/resource/{client_doctype}/{name_id}")
            response_put = requests.put(url_put, headers=headers, json=doc_data, timeout=30)
            
            result_put = {"name": name_id, "status_code": response_put.status_code, "ok": response_put.ok, "body": response_put.json().get("data", {}) if response_put.ok else response_put.text[:4000]}
            updates.append(result_put)
            
            if not response_put.ok:
                frappe.throw(f"Client ERP Tool Update for ID {name_id} failed: {response_put.text[:200]}")
                
        record_mcp_event(tool.tool_name, arguments, {"updates": updates}, task_id)
        return {"status": "success", "updated": len(names), "records": names}

    raise ValueError(f"Operation {op_type} not supported.")


def execute_local_db_tool(tool: "frappe.Document", arguments: dict) -> dict:
    """Fallback database logic if no server connection is configured."""
    validate_filter_arguments(tool, arguments)
    
    client_doctype = tool.client_doctype
    op_type = tool.operation_type or "Read"

    if op_type == "Read":
        filters = {}
        for m in tool.match_filters:
            filters[m.client_field] = resolve_mapping_value(m, arguments)
        
        fields = [m.client_field for m in tool.fields_to_read]
        if not fields:
            fields = ["*"]

        data = frappe.get_all(client_doctype, filters=filters, fields=fields)
        return {"status": "success", "data": data}

    elif op_type == "Create":
        doc_data = {}
        for m in tool.fields_to_write:
            doc_data[m.client_field] = resolve_mapping_value(m, arguments)

        doc = frappe.new_doc(client_doctype)
        doc.update(doc_data)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "success", "data": doc.as_dict()}

    elif op_type == "Update":
        filters = {}
        for m in tool.match_filters:
            filters[m.client_field] = resolve_mapping_value(m, arguments)

        names = frappe.get_all(client_doctype, filters=filters, pluck="name")
        if not names:
            message = f"No matching {client_doctype} records found to update"
            result = {"ok": False, "message": message, "filters": filters}
            record_mcp_event(tool.tool_name, arguments, result, None, error=message)
            log_mcp_error(
                tool.tool_name,
                message,
                arguments,
                None,
                error_type="MCP Update No Matching Records",
                response=result,
            )
            frappe.throw(message)

        doc_data = {}
        for m in tool.fields_to_write:
            doc_data[m.client_field] = resolve_mapping_value(m, arguments)

        for name_id in names:
            frappe.db.set_value(client_doctype, name_id, doc_data, update_modified=True)
        
        frappe.db.commit()
        return {"status": "success", "updated": len(names), "records": names}

    raise ValueError(f"Operation {op_type} not supported locally.")


def enrich_order_confirmation_issue_arguments(tool: "frappe.Document", arguments: dict, task_id: str | None) -> dict:
    """Append task/order context to issue descriptions so operators never get thin tickets."""
    tool_name = (tool.tool_name or "").lower()
    if "create_order_confirmation_issue" not in tool_name:
        return arguments

    task_context = get_task_context_for_mcp(task_id)
    if not task_context:
        return arguments

    payload_json = task_context.get("payload_json") if isinstance(task_context.get("payload_json"), dict) else {}
    data = dict(task_context)
    data.update({k: v for k, v in payload_json.items() if v not in (None, "", [], {})})

    detail_lines = build_order_issue_detail_lines(data, arguments)
    if not detail_lines:
        return arguments

    original_description = str(arguments.get("description") or "").strip()
    detail_block = "\n".join(detail_lines)
    if "ORDER / CUSTOMER DETAILS" not in original_description:
        arguments["description"] = (
            f"{original_description}\n\nORDER / CUSTOMER DETAILS:\n{detail_block}"
            if original_description
            else f"ORDER / CUSTOMER DETAILS:\n{detail_block}"
        )

    if not arguments.get("subject"):
        encounter = data.get("patient_encounter") or data.get("encounter_id") or "Order"
        patient = data.get("patient_name") or data.get("customer_name") or data.get("order_patient_name") or "Customer"
        arguments["subject"] = f"Order confirmation issue - {patient} - {encounter}"

    return arguments


def get_task_context_for_mcp(task_id: str | None) -> dict:
    if not task_id or not frappe.db.exists("AI Task", task_id):
        return {}
    task = frappe.get_doc("AI Task", task_id)
    context = parse_json_object(task.context_json, "AI Task Context") if task.context_json else {}
    return context


def build_order_issue_detail_lines(data: dict, arguments: dict) -> list[str]:
    def pick(*keys):
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}):
                return value
        return ""

    lines = []
    simple_fields = [
        ("Patient/Customer Name", pick("patient_name", "customer_name", "order_patient_name")),
        ("Patient Encounter", pick("patient_encounter", "encounter_id")),
        ("Customer Phone", pick("customer_phone", "phone_number", "phone", "mobile")),
        ("Source System", pick("source_system")),
        ("Event", pick("event")),
        ("Total Amount", pick("total_amount")),
        ("Advance Paid", pick("total_advance_paid")),
        ("Remaining Amount", pick("remaining_amount")),
        ("Delivery Address", pick("address")),
    ]
    for label, value in simple_fields:
        if value not in (None, "", [], {}):
            lines.append(f"- {label}: {value}")

    items = pick("items")
    if isinstance(items, list) and items:
        lines.append("- Items:")
        for idx, item in enumerate(items, start=1):
            if isinstance(item, dict):
                lines.append(
                    "  "
                    + f"{idx}. name={item.get('name', '')}, qty={item.get('qty', '')}, "
                    + f"rate={item.get('rate', '')}, amount={item.get('amount', '')}"
                )
            else:
                lines.append(f"  {idx}. {item}")

    payments = pick("payments")
    if isinstance(payments, list) and payments:
        lines.append("- Payments:")
        for idx, payment in enumerate(payments, start=1):
            if isinstance(payment, dict):
                lines.append(f"  {idx}. mode={payment.get('mode', '')}, amount={payment.get('amount', '')}")
            else:
                lines.append(f"  {idx}. {payment}")

    spoken_summary = str(arguments.get("description") or "").strip()
    if spoken_summary:
        lines.append(f"- Customer Spoken Issue / Agent Note: {spoken_summary}")

    return lines


def resolve_mapping_value(mapping: "frappe.Document", arguments: dict):
    if mapping.value_source == "From Tool Arguments":
        src = mapping.source_value or mapping.client_field
        return arguments.get(src)
    elif mapping.value_source == "Static Value":
        return mapping.source_value
    elif mapping.value_source == "System Function":
        if mapping.source_value == "now":
            return frappe.utils.now()
        return mapping.source_value
    return None


def log_mcp_error(
    tool_name: str,
    message: str,
    request: dict,
    task_id: str | None,
    *,
    error_type: str = "MCP Error",
    response: dict | None = None,
    exc: Exception | None = None,
) -> str | None:
    agent = None
    task_batch = None
    if task_id and frappe.db.exists("AI Task", task_id):
        task = frappe.get_doc("AI Task", task_id)
        agent = task.assigned_agent or task.target_agent
        task_batch = task.task_batch

    payload = {
        "tool": tool_name,
        "request": request,
        "response": response or {},
    }
    return create_error(
        error_type,
        message,
        source="MCP",
        task=task_id,
        task_batch=task_batch,
        agent=agent,
        payload=payload,
        exc=exc,
    )


def record_mcp_event(tool_name: str, request: dict, response: dict, task_id: str | None, error: str | None = None):
    agent = None
    if task_id and frappe.db.exists("AI Task", task_id):
        task_values = frappe.db.get_value("AI Task", task_id, ["assigned_agent", "target_agent"], as_dict=True)
        agent = task_values.assigned_agent or task_values.target_agent
        
    record_provider_event(
        provider="MCP",
        operation=tool_name,
        status="Succeeded" if response.get("ok", True) and not error else "Failed",
        agent=agent,
        task=task_id,
        request=request,
        response=response,
        error=error,
    )


@frappe.whitelist()
def print_routes():
    import frappe
    routes = frappe.get_all("AI Event Route", fields=["*"])
    res = []
    for r in routes:
        res.append(f"ROUTE: {r.name} | {r.route_name} | Enabled: {r.enabled} | EventKey: {r.event_key_field} | EventVal: {r.event_value} | SourceSys: {r.source_system}")
    return "\n".join(res)


@frappe.whitelist()
def fetch_client_schema(server: str, client_doctype: str) -> list[dict]:
    """Fetches list of fields for client_doctype from the client ERP server."""
    if not server or not client_doctype:
        frappe.throw("MCP Server and Client DocType are required")

    server_doc = frappe.get_doc("AI MCP Server", server)
    server_url = server_doc.server_url or ""
    headers = get_server_headers(server_doc)

    # Query the client ERP for metadata using getdoctype whitelisted API
    url = urljoin(server_url.rstrip("/") + "/", "api/method/frappe.desk.form.load.getdoctype")
    try:
        response = requests.get(url, headers=headers, params={"doctype": client_doctype}, timeout=15)
        if not response.ok:
            frappe.throw(f"Failed to fetch metadata from Client ERP: {response.text[:200]}")
        
        data = response.json()
        docs = data.get("docs", [])
        if not docs:
            # Try parsing "message" or nested structures if standard docs format is wrapped differently
            docs = data.get("message", {}).get("docs", []) if isinstance(data.get("message"), dict) else []
            
        if not docs:
            frappe.throw("No DocType schema documents returned. Verify if the DocType exists on the Client ERP.")
            
        meta = docs[0]
        fields = meta.get("fields", [])
        
        # Standard database fields for all doctypes in Frappe
        standard_fields = [
            {"fieldname": "name", "fieldtype": "Link", "label": "ID (Name)"},
            {"fieldname": "owner", "fieldtype": "Data", "label": "Owner"},
            {"fieldname": "creation", "fieldtype": "Datetime", "label": "Created On"},
            {"fieldname": "modified", "fieldtype": "Datetime", "label": "Last Modified"},
            {"fieldname": "modified_by", "fieldtype": "Data", "label": "Modified By"},
            {"fieldname": "docstatus", "fieldtype": "Int", "label": "Document Status"},
            {"fieldname": "idx", "fieldtype": "Int", "label": "Index"}
        ]
        
        # Include fields (excluding layout elements, formatting fields, buttons, etc.)
        excluded_types = ["Section Break", "Column Break", "Tab Break", "HTML", "Button"]
        result = list(standard_fields)
        for f in fields:
            if f.get("fieldtype") not in excluded_types:
                result.append({
                    "fieldname": f.get("fieldname"),
                    "fieldtype": f.get("fieldtype"),
                    "label": f.get("label")
                })
        return result
    except Exception as e:
        frappe.log_error(title=f"fetch_client_schema failed for server {server}, doctype {client_doctype}", message=frappe.get_traceback())
        frappe.throw(f"Error fetching schema: {str(e)}")


@frappe.whitelist()
def get_generated_curl(tool_name: str) -> str:
    if not tool_name or not frappe.db.exists("AI MCP Tool", tool_name):
        return ""
    tool = frappe.get_doc("AI MCP Tool", tool_name)
    
    server_url = ""
    headers = {"Content-Type": "application/json"}
    if tool.server:
        server = frappe.get_doc("AI MCP Server", tool.server)
        server_url = server.server_url or ""
        api_key = server.get("api_key")
        if api_key:
            headers["Authorization"] = f"token {api_key}:<api_secret>"
        else:
            headers["Authorization"] = "Bearer <token>"
    else:
        headers["Authorization"] = "Bearer <token>"
        
    headers_str = " \\\n  ".join([f'-H "{k}: {v}"' for k, v in headers.items()])
    
    # Check if using custom external URL config or native mapping
    if tool.endpoint_url:
        # Custom REST API
        url = urljoin(server_url.rstrip("/") + "/", tool.endpoint_url)
        method = tool.http_method or "POST"
        
        if method == "GET":
            query_params = []
            for p in tool.input_parameters:
                query_params.append(f"--data-urlencode '{p.parameter_name}=<{p.parameter_name}>'")
                
            params_str = " \\\n  ".join(query_params)
            if params_str:
                return f"curl -g -G '{url}' \\\n  {params_str} \\\n  {headers_str}"
            else:
                return f"curl -g -G '{url}' \\\n  {headers_str}"
        else:
            args = {}
            for p in tool.input_parameters:
                args[p.parameter_name] = f"<{p.parameter_name}>"
            body_json = json.dumps(args, indent=2)
            body_escaped = body_json.replace("'", "'\\''")
            return f"curl -g -X {method} '{url}' \\\n  {headers_str} \\\n  -d '{body_escaped}'"
    else:
        # Native Client ERP mapping
        client_doctype = tool.client_doctype or ""
        op_type = tool.operation_type or "Read"
        
        if op_type == "Read":
            url = urljoin(server_url.rstrip("/") + "/", f"api/resource/{quote(client_doctype)}")
            
            # Filters
            filters = []
            for m in tool.match_filters:
                filters.append([m.client_field, "=", "<value>"])
            
            # Fields to retrieve
            fields = [m.client_field for m in tool.fields_to_read]
            if not fields:
                fields = ["*"]
                
            query_params = []
            if filters:
                query_params.append(f"--data-urlencode 'filters={json.dumps(filters)}'")
            if fields:
                query_params.append(f"--data-urlencode 'fields={json.dumps(fields)}'")
                
            params_str = " \\\n  ".join(query_params)
            if params_str:
                return f"curl -g -G '{url}' \\\n  {params_str} \\\n  {headers_str}"
            else:
                return f"curl -g -G '{url}' \\\n  {headers_str}"
            
        elif op_type == "Create":
            url = urljoin(server_url.rstrip("/") + "/", f"api/resource/{quote(client_doctype)}")
            
            # Body JSON from fields_to_write
            body_data = {}
            for m in tool.fields_to_write:
                if m.value_source == "Static Value":
                    body_data[m.client_field] = m.source_value
                elif m.value_source == "System Function":
                    body_data[m.client_field] = f"<{m.source_value}>"
                else: # From Tool Arguments
                    body_data[m.client_field] = f"<{m.source_value or 'value'}>"
                    
            body_json = json.dumps(body_data, indent=2)
            body_escaped = body_json.replace("'", "'\\''")
            return f"curl -g -X POST '{url}' \\\n  {headers_str} \\\n  -d '{body_escaped}'"
            
        elif op_type == "Update":
            url = urljoin(server_url.rstrip("/") + "/", f"api/resource/{quote(client_doctype)}/<name>")
            
            # Body JSON from fields_to_write
            body_data = {}
            for m in tool.fields_to_write:
                if m.value_source == "Static Value":
                    body_data[m.client_field] = m.source_value
                elif m.value_source == "System Function":
                    body_data[m.client_field] = f"<{m.source_value}>"
                else: # From Tool Arguments
                    body_data[m.client_field] = f"<{m.source_value or 'value'}>"
                    
            body_json = json.dumps(body_data, indent=2)
            body_escaped = body_json.replace("'", "'\\''")
            return f"curl -g -X PUT '{url}' \\\n  {headers_str} \\\n  -d '{body_escaped}'"


@frappe.whitelist()
def test_tool_call(tool_name: str, arguments: str) -> dict:
    if not tool_name:
        frappe.throw("Tool Name is required")
    if not frappe.db.exists("AI MCP Tool", tool_name):
        frappe.throw(f"Tool {tool_name} not found")
        
    tool = frappe.get_doc("AI MCP Tool", tool_name)
    args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments
    
    try:
        res = execute_mcp_tool(tool, args_dict, task_id=None)
        return {"status": "success", "result": res}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def test_server_connection(server_name: str | None = None, server_url: str | None = None, api_key: str | None = None, api_secret: str | None = None, bearer_token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    
    if server_name and frappe.db.exists("AI MCP Server", server_name):
        server = frappe.get_doc("AI MCP Server", server_name)
        url = server_url or server.server_url
        
        final_key = api_key if api_key is not None else server.api_key
        
        if api_secret and api_secret != "*****":
            final_secret = api_secret
        else:
            final_secret = server.get_password("api_secret", raise_exception=False)
            
        if final_key and final_secret:
            headers["Authorization"] = f"token {final_key}:{final_secret}"
        else:
            if bearer_token and bearer_token != "*****":
                final_bearer = bearer_token
            else:
                final_bearer = server.get_password("bearer_token", raise_exception=False)
                
            if final_bearer:
                headers["Authorization"] = final_bearer if final_bearer.startswith(("Bearer", "token")) else f"Bearer {final_bearer}"
    else:
        url = server_url
        if api_key and api_secret:
            headers["Authorization"] = f"token {api_key}:{api_secret}"
        elif bearer_token:
            headers["Authorization"] = bearer_token if bearer_token.startswith(("Bearer", "token")) else f"Bearer {bearer_token}"
            
    if not url:
        frappe.throw("Frappe URL is required")
        
    dest_url = urljoin(url.rstrip("/") + "/", "api/method/frappe.auth.get_logged_user")
    try:
        response = requests.get(dest_url, headers=headers, timeout=10)
        if response.ok:
            user = response.json().get("message", "User")
            return {"status": "success", "message": f"Successfully connected! Authenticated as {user}."}
        else:
            return {"status": "error", "message": f"Failed to authenticate (HTTP {response.status_code}): {response.text[:200]}"}
    except Exception as e:
        return {"status": "error", "message": f"Connection failed: {str(e)}"}


@frappe.whitelist()
def fetch_client_doctypes(server: str | None = None) -> list[str]:
    if not server:
        # Fallback to local doctypes
        data = frappe.get_all("DocType", filters={"istable": 0, "issingle": 0}, pluck="name")
        data.sort()
        return data
        
    if not frappe.db.exists("AI MCP Server", server):
        return []
        
    server_doc = frappe.get_doc("AI MCP Server", server)
    server_url = server_doc.server_url or ""
    headers = get_server_headers(server_doc)
    
    url = urljoin(server_url.rstrip("/") + "/", "api/resource/DocType")
    params = {
        "fields": json.dumps(["name"]),
        "filters": json.dumps([["istable", "=", 0], ["issingle", "=", 0]]),
        "limit_page_length": 3000
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if not response.ok:
            response = requests.get(url, headers=headers, params={"fields": json.dumps(["name"])}, timeout=15)
            
        if response.ok:
            data = response.json().get("data", [])
            doctypes = [d.get("name") for d in data if d.get("name")]
            doctypes.sort()
            return doctypes
        else:
            frappe.log_error(title=f"fetch_client_doctypes failed: HTTP {response.status_code}", message=response.text)
            return []
    except Exception as e:
        frappe.log_error(title="fetch_client_doctypes exception", message=frappe.get_traceback())
        return []
