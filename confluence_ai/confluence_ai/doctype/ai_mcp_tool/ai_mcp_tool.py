from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AIMCPTool(Document):
    def validate(self) -> None:
        pass
