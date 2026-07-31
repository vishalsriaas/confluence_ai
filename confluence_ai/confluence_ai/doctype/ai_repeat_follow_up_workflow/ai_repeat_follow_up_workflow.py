from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIRepeatFollowUpWorkflow(Document):
    def validate(self) -> None:
        parse_json_object(self.source_payload_json, "Source Payload JSON")
        parse_json_object(self.encounter_json, "Encounter JSON")
        parse_json_object(self.context_json, "Context JSON")
        parse_json_object(self.shipkia_result_json, "Shipkia Result JSON")
        parse_json_object(self.structured_details_json, "Structured Details JSON")
        if self.mcp_tools_used_json:
            try:
                value = json.loads(self.mcp_tools_used_json)
            except Exception as exc:
                raise frappe.ValidationError("MCP Tools Used JSON must be valid JSON") from exc
            if not isinstance(value, (dict, list)):
                raise frappe.ValidationError("MCP Tools Used JSON must be a JSON object or array")
