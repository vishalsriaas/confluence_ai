from __future__ import annotations

import json

import frappe
from frappe.model.document import Document


class AIWhatsAppTemplateMap(Document):
    def validate(self) -> None:
        if not self.priority:
            self.priority = 50
        if not self.language_code:
            self.language_code = "en"
        if not self.body_values_json:
            self.body_values_json = '["{message}"]'
        if not self.header_values_json:
            self.header_values_json = "[]"
        if not self.button_values_json:
            self.button_values_json = "{}"
        if not self.extra_payload_json:
            self.extra_payload_json = "{}"

        for fieldname in ("body_values_json", "header_values_json", "button_values_json", "extra_payload_json"):
            _validate_json_field(fieldname, self.get(fieldname))


def _validate_json_field(fieldname: str, value: str | None) -> None:
    if not value:
        return
    try:
        json.loads(value)
    except Exception as exc:
        frappe.throw(f"{fieldname} must contain valid JSON: {exc}")
