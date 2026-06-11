from frappe.model.document import Document

from agent_army.services.utils import parse_json_object


class AITaskAttempt(Document):
    def validate(self) -> None:
        parse_json_object(self.request_json, "Request JSON")
        parse_json_object(self.response_json, "Response JSON")
