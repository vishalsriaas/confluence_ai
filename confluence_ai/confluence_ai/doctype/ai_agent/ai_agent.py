from __future__ import annotations

import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIAgent(Document):
    def validate(self) -> None:
        parse_json_object(self.model_config, "Model Config")
        parse_json_object(self.schedule_json, "Schedule JSON")
        parse_json_object(self.limits_json, "Limits JSON")

    def get_system_prompt(self, include_tool_catalog: bool = True) -> str:
        base_prompt = self.get("system_prompt") or ""
        if self.get("enable_sales_context"):
            base_prompt += """

## Sales Context Rules
- Use the Sales Brief from Context & Metadata as your primary source of truth for this call.
- If customer_type is repeat, refer to previous context respectfully and confirm what they need now.
- If customer_type is new, explain the company, relevant treatment/product category, diet/pricing/offer basics from the brief, then qualify the lead.
- You may explain approved information, pricing ranges, discounts, and next steps from the brief, but you must not diagnose, prescribe, guarantee cure, or claim medical certainty.
- If the customer asks for detail missing from the Sales Brief and a knowledge search tool is available, call it before answering.
- If the customer asks for a doctor/madam/human or a medical suitability question you cannot answer safely, do not promise a live transfer. Note the request, collect the reason/preferred time if needed, and say the team will call back.
- Before creating a follow-up or lead update, ask the needed details and confirm the next action with the customer.
- At the end of the call, use the available sales MCP tool to log outcome and create/update lead or follow-up when appropriate.
"""
        
        tools = self.get("allowed_mcp_tools") or []
        if not tools or not include_tool_catalog:
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
