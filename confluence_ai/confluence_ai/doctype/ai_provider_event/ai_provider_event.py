import frappe
from frappe.model.document import Document


class AIProviderEvent(Document):
    def validate(self):
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
