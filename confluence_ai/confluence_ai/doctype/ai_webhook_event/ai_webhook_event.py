import frappe
from frappe.model.document import Document


class AIWebhookEvent(Document):
    def autoname(self):
        # Callback logging must not hold the shared naming-series row.
        self.name = "webhook-" + frappe.generate_hash(length=20)
