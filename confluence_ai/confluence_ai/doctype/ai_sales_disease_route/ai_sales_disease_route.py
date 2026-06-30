from __future__ import annotations

import json

import frappe
from frappe.model.document import Document


class AISalesDiseaseRoute(Document):
    def validate(self) -> None:
        self.disease_key = (self.disease_key or "").strip().lower()
        self.inbound_vobiz_trunk_id = (self.inbound_vobiz_trunk_id or "").strip()
        self.inbound_phone_number = (self.inbound_phone_number or "").strip()
        self.inbound_domain = (self.inbound_domain or "").strip()
        if self.aliases_json:
            try:
                aliases = json.loads(self.aliases_json)
            except Exception as exc:
                frappe.throw(f"Aliases JSON must be valid JSON: {exc}")
            if not isinstance(aliases, list):
                frappe.throw("Aliases JSON must be a JSON array, for example: [\"kidney\", \"ckd\"]")
        if self.is_default and self.enabled:
            existing = frappe.get_all(
                "AI Sales Disease Route",
                filters={"enabled": 1, "is_default": 1, "name": ["!=", self.name]},
                pluck="name",
            )
            for name in existing:
                frappe.db.set_value("AI Sales Disease Route", name, "is_default", 0, update_modified=False)
