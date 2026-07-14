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

    def test_multi_stage_metadata_compilation(self):
        from confluence_ai.services.livekit import build_voice_metadata

        # 1. Update agent type to Multi-Stage
        self.agent.agent_type = "Multi-Stage State Machine"
        self.agent.stage_prompts = []
        
        # Append stage prompts
        self.agent.append("stage_prompts", {
            "stage_id": "greeting",
            "stage_name": "Greeting Stage",
            "is_orchestrator": 0,
            "system_prompt": "Hello {patient_name}, welcome!"
        })
        self.agent.append("stage_prompts", {
            "stage_id": "orchestrator",
            "stage_name": "Orchestrator Stage",
            "is_orchestrator": 1,
            "system_prompt": "State contract details"
        })
        self.agent.save(ignore_permissions=True)
        frappe.db.commit()

        # 2. Create a mock AI Task and Batch
        template_name = frappe.db.get_value("AI Task Template", {"template_key": "test_agent_prompts_template_key"}, "name")
        if not template_name:
            tmpl = frappe.new_doc("AI Task Template")
            tmpl.template_name = "Test Template"
            tmpl.template_key = "test_agent_prompts_template_key"
            tmpl.objective_prompt = "Test Objective Prompt"
            tmpl.insert(ignore_permissions=True)
            template_name = tmpl.name

        batch = frappe.new_doc("AI Task Batch")
        batch.status = "Running"
        batch.source_system = "Test System"
        batch.task_template = template_name
        batch.save(ignore_permissions=True)

        task = frappe.new_doc("AI Task")
        task.task_batch = batch.name
        task.task_template = template_name
        task.channel = "Voice"
        task.target_agent = self.agent.name
        task.context_json = json.dumps({"patient_name": "Rahul"})
        task.save(ignore_permissions=True)
        frappe.db.commit()

        # 3. Call build_voice_metadata
        metadata = build_voice_metadata(task.name, {"patient_name": "Rahul"})

        # 4. Clean up mock task/batch first
        frappe.delete_doc("AI Task", task.name, force=True)
        frappe.delete_doc("AI Task Batch", batch.name, force=True)
        frappe.delete_doc("AI Task Template", template_name, force=True)
        frappe.db.commit()

        # 5. Assertions
        self.assertIn("stage_prompts", metadata)
        stages = metadata["stage_prompts"]
        self.assertEqual(len(stages), 2)
        
        # Verify orchestrator stage
        orchestrator_stage = next((s for s in stages if s["is_orchestrator"]), None)
        self.assertIsNotNone(orchestrator_stage)
        self.assertEqual(orchestrator_stage["system_prompt"], "State contract details")
        
        # Verify greeting stage and that templating resolved {patient_name} to Rahul
        greeting_stage = next((s for s in stages if s["stage_id"] == "greeting"), None)
        self.assertIsNotNone(greeting_stage)
        self.assertEqual(greeting_stage["system_prompt"], "Hello Rahul, welcome!")
