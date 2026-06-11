from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIACPEvent(Document):
    def validate(self) -> None:
        parse_json_object(self.context_json, "Context JSON")
