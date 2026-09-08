"""Legacy repair checks; normal callback permutations live in test_call_registry."""
import json
import unittest
from unittest.mock import patch

import frappe

from confluence_ai.services import call_identity, call_registry, livekit
from confluence_ai.tests import test_call_registry


class TestCallIdentity(unittest.TestCase):
    setUp = test_call_registry.TestCallRegistry.setUp
    cleanup = test_call_registry.TestCallRegistry.cleanup
    new_attempt = test_call_registry.TestCallRegistry.new_attempt
    reserve = test_call_registry.TestCallRegistry.reserve
    payload = test_call_registry.TestCallRegistry.payload
    identity = test_call_registry.TestCallRegistry.identity

    def legacy_rows(self):
        canonical = frappe.get_doc({"doctype": "AI Call Log", "company": self.company,
            "task": self.task.name, "attempt": self.attempt.name,
            "call_uuid": "agent-army-" + self.task.name, "sip_call_id": "agent-army-" + self.task.name}).insert(ignore_permissions=True)
        source = frappe.get_doc({"doctype": "AI Call Log", "company": self.company, "provider": "Vobiz",
            "call_uuid": self.payload("hangup")["CallUUID"], "sip_call_id": self.payload("hangup")["SIPCallID"],
            "recording_url": "https://media.invalid/record.wav", "transcript": "Verified transcript",
            "from_number": "+919999999998", "to_number": "+919999999999", "status": "Completed",
            "status_payload_json": json.dumps(self.payload("hangup"))}).insert(ignore_permissions=True)
        return canonical, source

    def repair(self):
        return call_identity.merge_legacy_provider_identity(self.task, {
            "sip_call_id": self.payload("hangup")["SIPCallID"], "identity_source": "sip.callIDFull",
            "direction": "Outbound", "attempt": self.attempt.name})

    def test_explicit_legacy_merge_preserves_media_and_audit(self):
        canonical, source = self.legacy_rows()
        self.assertEqual(self.repair(), canonical.name)
        self.assertFalse(frappe.db.exists("AI Call Log", source.name))
        canonical.reload()
        self.assertEqual(canonical.call_uuid, self.payload("hangup")["CallUUID"])
        self.assertEqual(canonical.transcript, "Verified transcript")
        self.assertTrue(canonical.recording_url)
        self.assertEqual(canonical.from_number, "+919999999998")
        self.assertEqual(frappe.db.count("AI Webhook Event", {"task": self.task.name, "event_type": "call_log_merged"}), 1)

    def test_conflicting_task_is_not_merged(self):
        canonical, source = self.legacy_rows()
        other = frappe.get_doc({"doctype": "AI Task", "company": self.company,
            "task_template": self.task.task_template, "task_batch": self.task.task_batch}).insert(ignore_permissions=True)
        frappe.db.set_value("AI Call Log", source.name, "task", other.name)
        with self.assertRaises(frappe.ValidationError):
            self.repair()
        self.assertTrue(frappe.db.exists("AI Call Log", source.name))

    def test_legacy_single_attempt_without_link_is_reused(self):
        canonical = frappe.get_doc({"doctype": "AI Call Log", "company": self.company,
            "task": self.task.name, "call_uuid": "agent-army-" + self.task.name}).insert(ignore_permissions=True)
        result = self.identity()
        self.assertEqual(result["call_log"], canonical.name)
        canonical.reload()
        self.assertEqual(canonical.attempt, self.attempt.name)
        self.assertFalse(canonical.call_uuid)

    def test_other_company_identity_cannot_be_claimed(self):
        self.reserve()
        self.identity()
        self.assertIsNone(call_registry.find_call({"sip_call_id": self.payload("hangup")["SIPCallID"]}, "another-company"))

    def test_missing_full_identity_does_not_allocate(self):
        self.assertIsNone(call_identity.bind_provider_identity(self.task, {"sip_call_id": "SCL_internal"}))
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 0)

    def test_number_format_does_not_guess_country(self):
        self.assertEqual(call_identity.call_phone("00919873090386"), "+919873090386")
        self.assertEqual(call_identity.call_phone("9873090386", "+919873090386"), "+919873090386")
        self.assertEqual(call_identity.call_phone("00442071234567"), "+442071234567")
        self.assertEqual(call_identity.call_phone("2025550123"), "2025550123")

    def test_livekit_end_preserves_provider_payload(self):
        canonical, source = self.legacy_rows()
        name = self.repair()
        doc = frappe.get_doc("AI Call Log", name)
        original = doc.status_payload_json
        for event in ("call_ended", "participant_joined"):
            livekit._apply_livekit_call_log_payload(doc, {"event": event, "direction": "Outbound"}, self.task, self.attempt,
                diagnostics_enabled=False, context={}, livekit_event=event, event_type_lower=event, call_uuid=doc.call_uuid)
            self.assertEqual(doc.status, "Completed")
            self.assertEqual(doc.status_payload_json, original)
