from frappe.model.document import Document


class AICallLog(Document):
    def before_save(self):
        if self.flags.get("ignore_ai_disposition_auto_sync"):
            return
        if self.is_new() or not self.get("ai_disposition"):
            return
        if self.has_value_changed("ai_disposition"):
            self.erp_status_update_status = "Pending"
            self.flags.enqueue_ai_disposition_sync = True

    def on_update(self):
        if not self.flags.get("enqueue_ai_disposition_sync"):
            return
        from confluence_ai.services.call_disposition import enqueue_saved_disposition_sync

        enqueue_saved_disposition_sync(self.name)
