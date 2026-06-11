from __future__ import annotations

import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIAgent(Document):
    def validate(self) -> None:
        pass
        parse_json_object(self.model_config, "Model Config")
        parse_json_object(self.schedule_json, "Schedule JSON")
        parse_json_object(self.limits_json, "Limits JSON")
