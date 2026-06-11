from frappe.model.document import Document

from agent_army.services.utils import parse_json_object


class AIChannelAccount(Document):
    def validate(self) -> None:
        parse_json_object(self.endpoint_paths_json, "Endpoint Paths JSON")
        parse_json_object(self.rate_limits_json, "Rate Limits JSON")
