from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AITask(Document):
    def validate(self) -> None:
        parse_json_object(self.context_json, "Context JSON")
        parse_json_object(self.result_json, "Result JSON")
