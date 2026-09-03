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

    def test_phone_argument_values_use_crm_mobile_format(self):
        values = call_disposition._phone_argument_values("+91 98730 90386")

        self.assertEqual(values["phone_e164"], "+919873090386")
        self.assertEqual(values["mobile_no"], "919873090386")
        self.assertEqual(values["phone_last10"], "9873090386")

    def test_fallback_vobiz_disposition_uses_specific_label(self):
        label = call_disposition._fallback_vobiz_disposition_label(
            {
                "ai_disposition": "Follow up",
                "ai_disposition_reason": "Customer needs family discussion.",
                "ai_disposition_summary": "Caller will ask ghar par and confirm later.",
            }
        )

        self.assertEqual(label, "Family Discussion")

    def test_missing_transcript_fallback_marks_not_reachable(self):
        class FakeDoc:
            def get(self, fieldname):
                return {
                    "status": "Completed",
                    "event_type": "Hangup",
                }.get(fieldname)

        decision = call_disposition._missing_transcript_fallback_decision(FakeDoc())

        self.assertEqual(decision["ai_disposition"], "Not Answered")
        self.assertEqual(decision["custom_vobiz_disposition"], "Not Reachable")
        self.assertTrue(call_disposition._is_final_call_for_missing_transcript(FakeDoc()))
        self.assertTrue(
            call_disposition._is_waiting_for_transcript_response('{"reason":"waiting_for_transcript"}')
        )

    def test_saved_disposition_decision_uses_call_log_fields(self):
        class FakeDoc:
            def get(self, fieldname):
                return {
                    "ai_disposition": "Not Interested",
                    "ai_disposition_reason": "Customer refused online service.",
                    "ai_disposition_confidence": 0.87,
                    "ai_disposition_summary": "Customer did not want to proceed.",
                }.get(fieldname)

        decision = call_disposition._decision_from_saved_call_log(FakeDoc())

        self.assertEqual(decision["ai_disposition"], "Not Interested")
        self.assertEqual(decision["ai_disposition_confidence"], 0.87)

    def test_missing_transcript_fallback_doc_detects_only_fallback_reason(self):
        class FakeDoc:
            def __init__(self, reason):
                self.reason = reason

            def get(self, fieldname):
                return {
                    "ai_disposition": "Not Answered",
                    "ai_disposition_reason": self.reason,
                }.get(fieldname)

        self.assertTrue(call_disposition._is_missing_transcript_fallback_doc(FakeDoc("No transcript was received.")))
        self.assertFalse(call_disposition._is_missing_transcript_fallback_doc(FakeDoc("Customer did not answer.")))

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

    def test_company_prompt_is_added_to_disposition_instructions(self):
        instructions = call_disposition._build_disposition_instructions(
            "For Globifit, sperm fertility enquiry should be Fresh unless caller refuses."
        )

        self.assertIn("Company-specific disposition rules", instructions)
        self.assertIn("sperm fertility enquiry should be Fresh", instructions)
        self.assertIn("follow the company-specific rules", instructions)

    def test_empty_company_prompt_uses_default_disposition_instructions(self):
        instructions = call_disposition._build_disposition_instructions("")

        self.assertEqual(instructions, call_disposition.DEFAULT_DISPOSITION_INSTRUCTIONS)
