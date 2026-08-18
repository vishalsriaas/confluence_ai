from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIFreshFollowUpWorkflow(Document):
    def validate(self) -> None:
        parse_json_object(self.source_payload_json, "Source Payload JSON")
        parse_json_object(self.context_json, "Context JSON")
        if self.result_json:
            parse_json_object(self.result_json, "Result JSON")
        if self.extra_json:
            parse_json_object(self.extra_json, "Extra JSON")
        if self.task_history_json:
            try:
                value = json.loads(self.task_history_json)
            except Exception as exc:
                raise frappe.ValidationError("Task History JSON must be valid JSON") from exc
            if not isinstance(value, list):
                raise frappe.ValidationError("Task History JSON must be a JSON array")
