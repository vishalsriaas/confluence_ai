from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_object


class AITaskBatch(Document):
    def validate(self) -> None:
        parse_json_object(self.source_payload_json, "Source Payload JSON")
        parse_json_object(self.result_summary_json, "Result Summary JSON")
