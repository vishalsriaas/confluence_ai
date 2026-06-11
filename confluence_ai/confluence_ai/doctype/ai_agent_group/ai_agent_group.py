from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIAgentGroup(Document):
    def validate(self) -> None:
        parse_json_object(self.routing_rules_json, "Routing Rules JSON")
        parse_json_object(self.limits_json, "Limits JSON")
