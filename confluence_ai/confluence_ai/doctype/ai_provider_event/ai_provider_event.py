import frappe
from frappe.model.document import Document

from confluence_ai.services.utils import _extract_provider_chat_summary, parse_json_object


class AIProviderEvent(Document):
    def validate(self):
        if not self.get("chat_summary") and self.response_json:
            try:
                response = parse_json_object(self.response_json, "AI Provider Event Response JSON")
            except Exception:
                response = {}
            self.chat_summary = _extract_provider_chat_summary(response)

        if self.company:
            return

        if self.task:
            self.company = frappe.db.get_value("AI Task", self.task, "company")
            if self.company:
                return

            task_batch = frappe.db.get_value("AI Task", self.task, "task_batch")
            if task_batch:
                self.company = frappe.db.get_value("AI Task Batch", task_batch, "company")
                if self.company:
                    return

        if self.agent:
            self.company = frappe.db.get_value("AI Agent", self.agent, "company")
