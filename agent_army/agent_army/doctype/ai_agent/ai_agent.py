from __future__ import annotations

import frappe
from frappe.model.document import Document

from agent_army.services.utils import parse_json_object


class AIAgent(Document):
    def validate(self) -> None:
        self.agent_key = (self.agent_key or "").strip().lower()
        if not self.agent_key:
            frappe.throw("Agent Key is required")
        parse_json_object(self.model_config, "Model Config")
        parse_json_object(self.schedule_json, "Schedule JSON")
        parse_json_object(self.limits_json, "Limits JSON")
