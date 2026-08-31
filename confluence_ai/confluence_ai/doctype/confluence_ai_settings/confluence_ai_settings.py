import frappe
from frappe.model.document import Document


class ConfluenceAISettings(Document):
    def validate(self) -> None:
        if self.embedding_provider and self.embedding_provider not in {"OpenAI", "OpenAI Compatible", "Gemini"}:
            frappe.throw("Embedding Provider must be OpenAI, OpenAI Compatible, or Gemini.")
        if self.whatsapp_summary_provider and self.whatsapp_summary_provider not in {"OpenAI", "OpenAI Compatible", "Gemini"}:
            frappe.throw("WhatsApp Summary Provider must be OpenAI, OpenAI Compatible, or Gemini.")
        if self.embedding_timeout_seconds and int(self.embedding_timeout_seconds) < 5:
            frappe.throw("Embedding Timeout Seconds must be at least 5.")
        if self.whatsapp_summary_timeout_seconds and int(self.whatsapp_summary_timeout_seconds) < 5:
            frappe.throw("WhatsApp Summary Timeout Seconds must be at least 5.")
        if self.embedding_provider == "OpenAI Compatible" and not self.embedding_base_url:
            frappe.throw("Embedding Base URL is required for OpenAI Compatible provider.")
        if self.whatsapp_summary_provider == "OpenAI Compatible" and not self.whatsapp_summary_base_url:
            frappe.throw("WhatsApp Summary Base URL is required for OpenAI Compatible provider.")
