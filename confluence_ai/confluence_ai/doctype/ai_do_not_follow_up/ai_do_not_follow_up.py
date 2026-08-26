from __future__ import annotations

import re

import frappe
from frappe.model.document import Document


class AIDoNotFollowUp(Document):
    def before_validate(self) -> None:
        self.normalized_phone = normalize_phone(self.phone)

    def validate(self) -> None:
        if not self.normalized_phone:
            frappe.throw("Valid phone number is required.")
        if not self.enabled:
            return
        existing = frappe.db.get_value(
            self.doctype,
            {
                "company": self.company,
                "normalized_phone": self.normalized_phone,
                "enabled": 1,
                "name": ["!=", self.name],
            },
            "name",
        )
        if existing:
            frappe.throw(f"This number is already in Do Not Follow Up list: {existing}")


def normalize_phone(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[-10:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if text.startswith("+") and digits:
        return f"+{digits}"
    return text
