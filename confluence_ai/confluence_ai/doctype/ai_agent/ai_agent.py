from __future__ import annotations

import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIAgent(Document):
    def validate(self) -> None:
        parse_json_object(self.model_config, "Model Config")
        parse_json_object(self.schedule_json, "Schedule JSON")
        parse_json_object(self.limits_json, "Limits JSON")

    def get_system_prompt(self) -> str:
        base_prompt = self.get("system_prompt") or ""
        
        tools = self.get("allowed_mcp_tools") or []
        if not tools:
            return base_prompt
            
        instructions = ["\n\nYou are allowed to call the following MCP tools:"]
        for t in tools:
            tool_doc = frappe.get_doc("AI MCP Tool", t.tool) if frappe.db.exists("AI MCP Tool", t.tool) else None
            if not tool_doc:
                continue
                
            expected_json = tool_doc.get("expected_json") or "{}"
            tool_name = tool_doc.get("tool_name")
            
            tool_info = f"\n- Tool Name: {tool_name}"
            tool_info += f"\n  Required JSON Payload structure: {expected_json}"
            if t.calling_condition:
                tool_info += f"\n  When to call: {t.calling_condition}"
                
            instructions.append(tool_info)
            
        return base_prompt + "\n".join(instructions)
