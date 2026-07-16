import unittest
import json
import frappe

class TestAgentPrompts(unittest.TestCase):
    def setUp(self):
        # 1. Clean up any existing records
        existing_agent = frappe.db.get_value("AI Agent", {"agent_name": "MCP Prompter Agent"})
        if existing_agent:
            frappe.delete_doc("AI Agent", existing_agent, force=True)
        
        existing_tool = frappe.db.get_value("AI MCP Tool", {"tool_name": "test_prompter_tool"})
        if existing_tool:
            frappe.delete_doc("AI MCP Tool", existing_tool, force=True)
            
        frappe.db.commit()

        # 2. Create mock tool
        self.tool = frappe.new_doc("AI MCP Tool")
        self.tool.tool_name = "test_prompter_tool"
        self.tool.description = "Test Prompter Tool Description"
        self.tool.enabled = 1
        self.tool.client_doctype = "User"
        self.tool.operation_type = "Read"
        self.tool.append("input_parameters", {
            "parameter_name": "user_id",
            "type": "string",
            "required": 1,
            "description": "User ID"
        })
        self.tool.expected_json = json.dumps({"user_id": "string"}, indent=2)
        self.tool.insert(ignore_permissions=True)

        # 3. Create mock Agent
        self.agent = frappe.new_doc("AI Agent")
        self.agent.agent_name = "MCP Prompter Agent"
        self.agent.channel_type = "Voice"
        self.agent.system_prompt = "You are a helpful customer service representative."
        self.agent.primary_provider = "Gemini"
        
        # Add tool to grid
        self.agent.append("allowed_mcp_tools", {
            "tool": self.tool.name,
            "calling_condition": "when the user asks for their user ID or account status"
        })
        self.agent.insert(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        frappe.delete_doc("AI Agent", self.agent.name, force=True)
        frappe.delete_doc("AI MCP Tool", self.tool.name, force=True)
        frappe.db.commit()

    def test_get_system_prompt_compilation(self):
        prompt = self.agent.get_system_prompt()
        
        # Assert base prompt remains intact
        self.assertIn("You are a helpful customer service representative.", prompt)
        
        # Assert tool specifications are appended
        self.assertIn("You are allowed to call the following MCP tools:", prompt)
        self.assertIn("test_prompter_tool", prompt)
        self.assertIn("user_id", prompt)
        self.assertIn("when the user asks for their user ID or account status", prompt)
