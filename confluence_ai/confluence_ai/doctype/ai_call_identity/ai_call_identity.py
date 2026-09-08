from frappe.model.document import Document


class AICallIdentity(Document):
    def autoname(self):
        from confluence_ai.services.call_registry import identity_key
        self.name = identity_key(self.company, self.identity_kind, self.identity_value)
