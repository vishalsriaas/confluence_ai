from frappe.model.document import Document

from agent_army.services.utils import parse_json_object


class AITaskTemplate(Document):
    def validate(self) -> None:
        parse_json_object(self.input_schema_json, "Input Schema JSON")
        parse_json_object(self.default_context_json, "Default Context JSON")
