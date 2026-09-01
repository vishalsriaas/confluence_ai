from __future__ import annotations

import unittest

from confluence_ai.services import call_disposition


class TestCallDisposition(unittest.TestCase):
    def test_normalizes_allowed_and_alias_dispositions(self):
        self.assertEqual(call_disposition._normalize_disposition("Order Placed"), "Order Placed")
        self.assertEqual(call_disposition._normalize_disposition("follow-up"), "Follow up")
        self.assertEqual(call_disposition._normalize_disposition("price issue"), "Financial Issue")
        self.assertEqual(call_disposition._normalize_disposition("unknown value"), "Fresh")

    def test_phone_variants_include_common_erp_forms(self):
        variants = call_disposition._phone_variants("+91 98730 90386")

        self.assertIn("+919873090386", variants)
        self.assertIn("9873090386", variants)
        self.assertIn("919873090386", variants)

    def test_lead_id_prefers_crm_lead_reference(self):
        class FakeDoc:
            def get(self, fieldname):
                return None

        lead_id = call_disposition._lead_id_from_context(
            FakeDoc(),
            {
                "source_reference_type": "CRM Lead",
                "source_reference_name": "CRM-LEAD-2026-00119",
                "external_record_id": "OTHER",
                "external_record_type": "Other",
            },
        )

        self.assertEqual(lead_id, "CRM-LEAD-2026-00119")

