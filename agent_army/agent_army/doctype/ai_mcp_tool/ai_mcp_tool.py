from frappe.model.document import Document

from agent_army.services.utils import parse_json_object


class AIMCPTool(Document):
    def validate(self) -> None:
        parse_json_object(self.parameters_schema_json, "Parameters Schema JSON")
