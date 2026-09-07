import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe

from confluence_ai.services import inbound_sales as flow, vobiz


class TestInboundStartup(unittest.TestCase):
    """Real task/attempt/log creation; only external context tools are stubbed."""

    def setUp(self):
        self.company = "unit-startup-" + frappe.generate_hash(length=10).lower()
        frappe.get_doc({"doctype": "AI Company", "company_key": self.company, "company_name": self.company}).insert(ignore_permissions=True)
        self.agent = frappe.get_doc({"doctype": "AI Agent", "company": self.company,
            "agent_name": self.company, "agent_type": "Single-Stage", "system_prompt": "Unit test", "enabled": 1}).insert(ignore_permissions=True)
        self.template = frappe.get_doc({"doctype": "AI Task Template", "template_key": self.company,
            "template_name": self.company, "objective_prompt": "Unit test", "default_channel": "Voice"}).insert(ignore_permissions=True)
        selection = {"company": self.company, "target_agent": self.agent.name}
        self.patches = [
            patch.object(flow, "resolve_inbound_sales_route", return_value=selection),
            patch.object(flow, "_resolve_task_template", return_value=self.template.name),
            patch.object(flow, "enrich_start_context_tools", side_effect=lambda context, **kw: context),
            patch.object(flow, "enrich_sales_context", side_effect=lambda context, **kw: context),
            patch.object(flow, "_maybe_start_fresh_followup_from_task", return_value=None),
            patch.object(flow, "refresh_batch_counts"),
            patch.object(flow, "build_voice_metadata", side_effect=lambda task, context: {"task": task, "context": context}),
            patch.object(vobiz, "_candidate_channel_accounts", return_value=[]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        frappe.db.commit()

    def tearDown(self):
        frappe.db.rollback()
        for dt in ("AI Call Log", "AI Task Attempt", "AI Task", "AI Task Batch", "AI Agent"):
            frappe.db.delete(dt, {"company": self.company})
        frappe.db.delete("AI Task Template", {"name": self.template.name})
        frappe.db.delete("AI Company", {"name": self.company})
        frappe.db.commit()

    def provider(self, key="one"):
        return {"Event": "CallInitiated", "Status": "initiated", "Direction": "inbound",
            "CallUUID": self.company + key, "SIPCallID": self.company + key,
            "From": "00919873090386", "To": "00919262175574", "TrunkID": "provider-trunk"}

    def resolver(self, key="one"):
        return {"room_name": "sip-unit-" + key, "caller_phone": "+00919873090386",
            "called_number": "+919262175574", "call_uuid": "SCL_internal_" + key, "trunk_id": "ST_unit",
            "participants": [{"attributes": {"sip.callID": "SCL_internal_" + key, "sip.callIDFull": self.company + key}}]}

    def assert_single_call(self, first, second):
        self.assertEqual(first["task"], second["task"])
        for dt in ("AI Task", "AI Task Attempt", "AI Call Log", "AI Task Batch"):
            self.assertEqual(frappe.db.count(dt, {"company": self.company}), 1, dt)
        task = frappe.get_doc("AI Task", first["task"])
        self.assertEqual(task.call_uuid, self.company + "one")
        self.assertFalse(task.idempotency_key.endswith("SCL_internal_one"))

    def test_vobiz_first_then_old_worker_resolver(self):
        self.assert_single_call(flow.handle_vobiz_inbound_call(self.provider()), flow.resolve_latest_inbound_metadata(self.resolver()))

    def test_resolver_first_then_vobiz(self):
        self.assert_single_call(flow.resolve_latest_inbound_metadata(self.resolver()), flow.handle_vobiz_inbound_call(self.provider()))

    def test_simultaneous_startup_requests(self):
        site, sites_path = frappe.local.site, frappe.local.sites_path
        barrier = threading.Barrier(2)
        def run(resolver):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            try:
                barrier.wait(timeout=10)
                return flow.resolve_latest_inbound_metadata(self.resolver()) if resolver else flow.handle_vobiz_inbound_call(self.provider())
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(run, value) for value in (False, True)]
            first, second = [job.result(timeout=40) for job in jobs]
        self.assert_single_call(first, second)

    def test_repeated_resolver_and_webhook_keep_one_attempt(self):
        first = flow.resolve_latest_inbound_metadata(self.resolver())
        for _ in range(3):
            self.assert_single_call(first, flow.handle_vobiz_inbound_call(self.provider()))
            self.assert_single_call(first, flow.resolve_latest_inbound_metadata(self.resolver()))

    def test_same_phone_two_real_calls_stay_separate(self):
        first = flow.handle_vobiz_inbound_call(self.provider())
        second = flow.resolve_latest_inbound_metadata(self.resolver("two"))
        self.assertNotEqual(first["task"], second["task"])
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 2)

    def test_missing_full_id_never_creates_internal_id_task(self):
        payload = self.resolver()
        payload["participants"][0]["attributes"].pop("sip.callIDFull")
        result = flow.resolve_latest_inbound_metadata(payload)
        self.assertEqual(result["status"], "pending_identity")
        self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 0)

    def test_ambiguous_full_ids_are_not_guessed(self):
        payload = self.resolver()
        payload["participants"].append({"attributes": {"sip.callIDFull": "other-call"}})
        self.assertEqual(flow.resolve_latest_inbound_metadata(payload)["status"], "pending_identity")
        self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 0)

    def test_new_worker_top_level_provider_id(self):
        payload = self.resolver()
        payload["call_uuid"] = self.company + "one"
        payload.pop("participants")
        self.assert_single_call(flow.handle_vobiz_inbound_call(self.provider()), flow.resolve_latest_inbound_metadata(payload))

    def test_malformed_participant_attributes_do_not_create_task(self):
        payload = self.resolver()
        payload["participants"] = [{"attributes": "invalid"}, None]
        self.assertEqual(flow.resolve_latest_inbound_metadata(payload)["status"], "pending_identity")
        self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 0)

    def test_call_identity_cannot_cross_companies(self):
        first = flow.handle_vobiz_inbound_call(self.provider())
        selection = {"company": "different-company", "target_agent": self.agent.name}
        with patch.object(flow, "resolve_inbound_sales_route", return_value=selection):
            with self.assertRaises(frappe.ValidationError):
                flow.handle_vobiz_inbound_call(self.provider())
        frappe.db.set_value("AI Task", first["task"], "idempotency_key", "legacy-" + self.company)
        frappe.db.commit()
        with patch.object(flow, "resolve_inbound_sales_route", return_value=selection):
            with self.assertRaises(frappe.ValidationError):
                flow.handle_vobiz_inbound_call(self.provider())
        self.assertEqual(frappe.db.count("AI Task", {"company": self.company}), 1)

    def test_lifecycle_after_both_starts_updates_same_log(self):
        first = flow.handle_vobiz_inbound_call(self.provider())
        second = flow.resolve_latest_inbound_metadata(self.resolver())
        self.assert_single_call(first, second)
        task = frappe.get_doc("AI Task", first["task"])
        attempt = frappe.get_last_doc("AI Task Attempt", filters={"task": task.name})
        log = frappe.db.get_value("AI Call Log", {"task": task.name}, "name")
        for event in ("hangup", "recording", "transcript"):
            payload = {**self.provider(), "event": event, "Status": "completed", "CallUUID": "bridge-" + self.company}
            if event == "recording":
                payload["recording_url"] = "https://media.example.invalid/audio.wav"
            if event == "transcript":
                payload["transcript"] = "Unit customer reply"
            self.assertEqual(vobiz.upsert_call_log(payload, task, attempt), log)
        doc = frappe.get_doc("AI Call Log", log)
        self.assertTrue(doc.recording_url)
        self.assertEqual(doc.transcript, "Unit customer reply")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
