import unittest
import json
import frappe
from unittest.mock import patch

from confluence_ai.api.mcp import gateway


class TestNativeMCP(unittest.TestCase):
    def setUp(self):
        # Clean up any conflicting records first to prevent validation / integrity errors
        existing_tmpl_key = frappe.db.get_value("AI Task Template", {"template_key": "test_mcp_template_key"})
        if existing_tmpl_key:
            frappe.delete_doc("AI Task Template", existing_tmpl_key, force=True)

        existing_agent = frappe.db.get_value("AI Agent", {"agent_name": "MCP Test Agent"})
        if existing_agent:
            frappe.delete_doc("AI Agent", existing_agent, force=True)

        # 1. Create a dummy Agent
        agent = frappe.new_doc("AI Agent")
        agent.agent_name = "MCP Test Agent"
        agent.channel_type = "Voice"
        agent.system_prompt = "Hello"
        agent_doc = agent.insert(ignore_permissions=True)
        self.agent_name = agent_doc.name

        # 2. Create a dummy Template
        tmpl = frappe.new_doc("AI Task Template")
        tmpl.template_name = "MCP Test Template"
        tmpl.template_key = "test_mcp_template_key"
        tmpl.objective_prompt = "Test Objective Prompt"
        tmpl_doc = tmpl.insert(ignore_permissions=True)
        self.template_name = tmpl_doc.name

        # 3. Create a test AI MCP Tool (Local DB target: AI Agent)
        self.tool_name = "test_agent_search"
        if frappe.db.exists("AI MCP Tool", {"tool_name": self.tool_name}):
            frappe.delete_doc("AI MCP Tool", frappe.db.get_value("AI MCP Tool", {"tool_name": self.tool_name}), force=True)
            
        self.tool = frappe.new_doc("AI MCP Tool")
        self.tool.tool_name = self.tool_name
        self.tool.description = "Search for agents by name"
        self.tool.enabled = 1
        self.tool.client_doctype = "AI Agent"
        self.tool.operation_type = "Read"
        
        # Schema Parameters
        self.tool.append("input_parameters", {
            "parameter_name": "agent_id",
            "type": "string",
            "required": 1,
            "description": "The target agent ID to search"
        })
        
        # Mappings
        self.tool.append("match_filters", {
            "client_field": "name",
            "value_source": "From Tool Arguments",
            "source_value": "agent_id",
            "is_filter": 1
        })
        self.tool.append("fields_to_read", {
            "client_field": "agent_name",
            "value_source": "From Tool Arguments",
            "source_value": "agent_id",
            "is_filter": 0
        })
        self.tool.insert(ignore_permissions=True)

        # 4. Create an AI Event Route (Route-Level allowed tools)
        self.route_name = "Test MCP Scoped Route"
        if frappe.db.exists("AI Event Route", {"route_name": self.route_name}):
            frappe.delete_doc("AI Event Route", frappe.db.get_value("AI Event Route", {"route_name": self.route_name}), force=True)

        self.route = frappe.new_doc("AI Event Route")
        self.route.route_name = self.route_name
        self.route.enabled = 1
        self.route.event_key_field = "event"
        self.route.event_value = "mcp_test_event"
        self.route.task_template = self.template_name
        self.route.target_agent = self.agent_name
        self.route.dispatch_mode = "Batch"
        self.route.batch_records_field = "records"
        self.route.batch_label = "MCP Test Campaign"
        self.route.append("allowed_tools", {
            "tool": self.tool.name
        })
        self.route.insert(ignore_permissions=True)

        # 5. Create AI Task Batch & AI Task
        self.batch = frappe.new_doc("AI Task Batch")
        self.batch.status = "Queued"
        self.batch.batch_label = "MCP Test Campaign"
        self.batch.source_system = self.route_name
        self.batch.task_template = self.template_name
        self.batch.insert(ignore_permissions=True)

        self.task = frappe.new_doc("AI Task")
        self.task.status = "Running"
        self.task.task_batch = self.batch.name
        self.task.task_template = self.template_name
        self.task.channel = "Voice"
        self.task.context_json = json.dumps({"agent_id": self.agent_name})
        self.task.insert(ignore_permissions=True)

        # 6. Create AI Tool Permission
        permission = frappe.new_doc("AI Tool Permission")
        permission.tool = self.tool.name
        permission.agent = self.agent_name
        permission.enabled = 1
        permission.insert(ignore_permissions=True)
        self.perm_name = permission.name

        frappe.db.commit()

    def tearDown(self):
        frappe.db.delete("AI Event Route", {"route_name": self.route_name})
        frappe.db.delete("AI Task Batch", {"name": self.batch.name})
        frappe.db.delete("AI Task", {"name": self.task.name})
        frappe.db.delete("AI MCP Tool", {"name": self.tool.name})
        frappe.db.delete("AI Tool Permission", {"name": self.perm_name})
        frappe.db.delete("AI Agent", {"name": self.agent_name})
        frappe.db.delete("AI Task Template", {"name": self.template_name})
        frappe.db.commit()

    @patch("confluence_ai.api.mcp.require_access")
    def test_gateway_tools_list_scoped(self, mock_auth):
        # Mock API auth validation
        mock_auth.return_value = True

        from unittest import mock
        frappe.local.request = mock.MagicMock()
        frappe.local.request.headers = {"X-Confluence-Task-ID": self.task.name}
        
        try:
            with patch("confluence_ai.api.mcp.get_request_json") as mock_json:
                mock_json.return_value = {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/list"
                }
                
                resp = gateway()
                self.assertEqual(resp.get("jsonrpc"), "2.0")
                self.assertEqual(resp.get("id"), 42)
                tools = resp.get("result", {}).get("tools", [])
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0]["name"], self.tool_name)
                
                # Check Schema Conversion
                schema = tools[0]["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIn("agent_id", schema["properties"])
                self.assertEqual(schema["properties"]["agent_id"]["type"], "string")
                self.assertIn("agent_id", schema["required"])
        finally:
            if hasattr(frappe.local, "request"):
                del frappe.local.request

    @patch("confluence_ai.api.mcp.require_access")
    def test_gateway_tools_call_read(self, mock_auth):
        mock_auth.return_value = True

        from unittest import mock
        frappe.local.request = mock.MagicMock()
        frappe.local.request.headers = {"X-Confluence-Task-ID": self.task.name}
        
        try:
            with patch("confluence_ai.api.mcp.get_request_json") as mock_json:
                mock_json.return_value = {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": self.tool_name,
                        "arguments": {
                            "agent_id": self.agent_name
                        }
                    }
                }
                
                resp = gateway()
                self.assertEqual(resp.get("jsonrpc"), "2.0")
                self.assertEqual(resp.get("id"), 99)
                self.assertIn("result", resp)
                result = resp["result"]
                self.assertEqual(result.get("status"), "success")
                data = result.get("data", [])
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["agent_name"], "MCP Test Agent")
        finally:
            if hasattr(frappe.local, "request"):
                del frappe.local.request

    @patch("confluence_ai.api.mcp.requests.get")
    @patch("confluence_ai.api.mcp.require_access")
    def test_fetch_client_schema(self, mock_auth, mock_get):
        from unittest import mock
        mock_auth.return_value = True

        # 1. Create a dummy MCP Server
        server_name = "Test ERP Server"
        if frappe.db.exists("AI MCP Server", {"server_name": server_name}):
            frappe.delete_doc("AI MCP Server", frappe.db.get_value("AI MCP Server", {"server_name": server_name}), force=True)
            
        server = frappe.new_doc("AI MCP Server")
        server.server_name = server_name
        server.server_key = "test_key"
        server.server_url = "http://mock-erp.local"
        server.bearer_token = "mock_token"
        server.insert(ignore_permissions=True)
        
        # 2. Mock requests.get response for metadata
        mock_response = mock.MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "docs": [
                {
                    "fields": [
                        {"fieldname": "patient_name", "fieldtype": "Data", "label": "Patient Name"},
                        {"fieldname": "phone", "fieldtype": "Phone", "label": "Phone Number"},
                        {"fieldname": "sb", "fieldtype": "Section Break", "label": "Details"}
                    ]
                }
            ]
        }
        mock_get.return_value = mock_response
        
        from confluence_ai.api.mcp import fetch_client_schema
        
        try:
            fields = fetch_client_schema(server.name, "Patient")
            
            # Assert requests.get details
            mock_get.assert_called_once_with(
                "http://mock-erp.local/api/method/frappe.desk.form.load.getdoctype",
                headers={"Content-Type": "application/json", "Authorization": "Bearer mock_token"},
                params={"doctype": "Patient"},
                timeout=15
            )
            
            # Assert result filtering (Section Break should be excluded, and 7 standard fields are prepended)
            self.assertEqual(len(fields), 9)
            self.assertEqual(fields[7]["fieldname"], "patient_name")
            self.assertEqual(fields[7]["fieldtype"], "Data")
            self.assertEqual(fields[8]["fieldname"], "phone")
            self.assertEqual(fields[8]["fieldtype"], "Phone")
            
        finally:
            frappe.delete_doc("AI MCP Server", server.name, force=True)
            frappe.db.commit()

    def test_execute_mcp_tool_missing_filter_fails(self):
        from confluence_ai.api.mcp import execute_local_db_tool
        with self.assertRaises(frappe.ValidationError):
            execute_local_db_tool(self.tool, {})
            
        with self.assertRaises(frappe.ValidationError):
            execute_local_db_tool(self.tool, {"agent_id": ""})

    def test_execute_mcp_tool_no_filters_update_fails(self):
        from confluence_ai.api.mcp import execute_local_db_tool
        update_tool = frappe.new_doc("AI MCP Tool")
        update_tool.tool_name = "test_unsafe_update"
        update_tool.enabled = 1
        update_tool.client_doctype = "AI Agent"
        update_tool.operation_type = "Update"
        update_tool.insert(ignore_permissions=True)
        
        try:
            with self.assertRaises(frappe.ValidationError):
                execute_local_db_tool(update_tool, {})
        finally:
            frappe.delete_doc("AI MCP Tool", update_tool.name, force=True)
            frappe.db.commit()
