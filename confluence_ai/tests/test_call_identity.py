import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from unittest.mock import patch

import frappe

from confluence_ai.api import webhook
from confluence_ai.services import call_identity, inbound_sales, livekit, vobiz


class TestCallIdentity(unittest.TestCase):
    """Isolated database tests. No provider, phone or model requests."""

    def setUp(self):
        self.company = "unit-identity-" + frappe.generate_hash(length=10).lower()
        frappe.get_doc({"doctype": "AI Company", "company_key": self.company, "company_name": self.company}).insert(ignore_permissions=True)
        self.task = frappe.get_doc({"doctype": "AI Task", "company": self.company, "context_json": "{}"})
        self.task.flags.ignore_mandatory = True
        self.task.insert(ignore_permissions=True)
        self.attempt = frappe.get_doc({"doctype": "AI Task Attempt", "company": self.company, "task": self.task.name}).insert(ignore_permissions=True)
        self.channel_patch = patch.object(vobiz, "_candidate_channel_accounts", return_value=[])
        self.channel_patch.start()
        self.addCleanup(self.channel_patch.stop)
        frappe.db.commit()

    def tearDown(self):
        frappe.db.rollback()
        frappe.db.delete("AI Webhook Event", {"task": self.task.name})
        for doctype in ("AI Call Log", "AI Task Attempt", "AI Task", "AI Company"):
            frappe.db.delete(doctype, {"name" if doctype == "AI Company" else "company": self.company})
        frappe.db.commit()

    def payload(self, event, key="one"):
        return {
            "event": event, "company": self.company, "CallUUID": self.company + "-" + key,
            "SIPCallID": self.company + "-sip-" + key,
            "From": "+919999999998", "To": "+919999999999", "Direction": "Outbound",
            **({"CallStatus": "completed", "Duration": "42"} if event == "hangup" else {}),
            **({"recording_url": "https://media.example.invalid/one.wav"} if event == "recording" else {}),
            **({"transcript": "Customer asked to call tomorrow."} if event == "transcript" else {}),
        }

    def bind(self, key="one"):
        return call_identity.bind_provider_identity(self.task, {
            "identity_source": "sip.callIDFull", "sip_call_id": self.company + "-sip-" + key,
            "direction": "Outbound",
        })

    def test_all_24_event_orders_and_duplicates(self):
        for index, order in enumerate(permutations(("initiated", "hangup", "recording", "transcript"))):
            with self.subTest(order=order):
                names = set()
                for event in order:
                    payload = self.payload(event, str(index))
                    names.add(vobiz.upsert_call_log(payload))
                    names.add(vobiz.upsert_call_log(payload))
                self.assertEqual(len(names), 1)
                doc = frappe.get_doc("AI Call Log", names.pop())
                self.assertEqual(doc.status, "Completed")
                for field, event in (("initiated_payload_json", "initiated"), ("status_payload_json", "hangup"), ("recording_payload_json", "recording"), ("transcript_payload_json", "transcript")):
                    self.assertEqual(json.loads(doc.get(field)), self.payload(event, str(index)))
                self.assertTrue(doc.recording_url)
                self.assertEqual(doc.transcript, "Customer asked to call tomorrow.")
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 24)

    def test_identity_before_callbacks_uses_one_log(self):
        target = self.bind()
        for event in ("recording", "hangup", "transcript", "initiated"):
            self.assertEqual(vobiz.upsert_call_log(self.payload(event)), target)
        self.assertEqual(self.bind(), target)
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)
        doc = frappe.get_doc("AI Call Log", target)
        self.assertEqual(doc.task, self.task.name)
        self.assertEqual(doc.attempt, self.attempt.name)
        self.assertEqual(doc.call_uuid, self.payload("recording")["CallUUID"])
        self.assertTrue(doc.transcript)

    def test_orphan_merge_keeps_media_and_task_transcript(self):
        canonical = frappe.get_doc({"doctype": "AI Call Log", "task": self.task.name, "company": self.company,
            "call_uuid": "agent-army-" + self.task.name, "sip_call_id": "agent-army-" + self.task.name,
            "status_payload_json": '{"event":"call_ended","room_name":"unit"}'}).insert(ignore_permissions=True)
        orphan = vobiz.upsert_call_log(self.payload("recording"))
        vobiz.upsert_call_log(self.payload("transcript"))
        vobiz.upsert_call_log(self.payload("hangup"))
        self.assertNotEqual(canonical.name, orphan)
        self.assertEqual(self.bind(), canonical.name)
        self.assertFalse(frappe.db.exists("AI Call Log", orphan))
        canonical.reload()
        self.assertEqual(canonical.call_uuid, self.payload("hangup")["CallUUID"])
        self.assertEqual(canonical.status, "Completed")
        self.assertTrue(canonical.recording_payload_json)
        self.assertTrue(canonical.transcript_payload_json)
        self.assertEqual(json.loads(canonical.status_payload_json)["event"], "hangup")
        self.task.reload()
        self.attempt.reload()
        self.assertEqual(self.task.transcript, canonical.transcript)
        self.assertEqual(self.attempt.transcript, canonical.transcript)

    def test_orphan_uuid_equal_to_sip_id_merges_without_unique_conflict(self):
        canonical = frappe.get_doc({"doctype": "AI Call Log", "task": self.task.name, "company": self.company,
            "call_uuid": "agent-army-" + self.task.name}).insert(ignore_permissions=True)
        payload = self.payload("recording")
        payload["CallUUID"] = payload["SIPCallID"]
        vobiz.upsert_call_log(payload)
        self.assertEqual(self.bind(), canonical.name)
        canonical.reload()
        self.assertEqual(canonical.call_uuid, payload["CallUUID"])
        self.assertTrue(canonical.recording_url)

    def test_identity_notification_does_not_run_call_completion(self):
        payload = {"task": self.task.name, "event": "call_identity", "sip_call_id": self.company + "-sip-one",
            "identity_source": "sip.callIDFull", "direction": "Outbound"}
        result = livekit.handle_callback(payload)
        self.assertEqual(result["status"], "success")
        self.task.reload()
        self.attempt.reload()
        self.assertEqual(self.task.status, "Queued")
        self.assertEqual(self.attempt.status, "Started")

    def test_same_phone_different_calls_not_merged(self):
        first = vobiz.upsert_call_log(self.payload("recording"))
        other = vobiz.upsert_call_log(self.payload("recording", "other"))
        self.assertEqual(self.bind(), first)
        self.assertNotEqual(first, other)
        self.assertFalse(frappe.db.get_value("AI Call Log", other, "task"))

    def test_missing_full_identity_does_nothing(self):
        self.assertIsNone(call_identity.bind_provider_identity(self.task, {"sip_call_id": "SCL_internal"}))
        self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 0)

    def test_inbound_and_outbound_replace_internal_id_and_keep_one_log(self):
        for direction in ("inbound", "outbound"):
            with self.subTest(direction=direction):
                frappe.db.delete("AI Call Log", {"company": self.company})
                frappe.db.set_value("AI Task", self.task.name, "call_uuid", "SCL_internal")
                self.task.reload()
                canonical = frappe.get_doc({"doctype": "AI Call Log", "company": self.company, "task": self.task.name,
                    "sip_call_id": "SCL_internal", "call_uuid": "SCL_internal", "customer_phone": "9999999999"}).insert(ignore_permissions=True)
                payload = self.payload("initiated")
                payload["Direction"] = direction
                payload["From" if direction == "inbound" else "To"] = "00919999999999"
                vobiz._bind_vobiz_identity(self.task, payload)
                self.assertEqual(vobiz.upsert_call_log(payload, self.task, self.attempt), canonical.name)
                for event in ("recording", "transcript", "hangup"):
                    media = {**payload, **self.payload(event), "Direction": direction}
                    self.assertEqual(vobiz.upsert_call_log(media), canonical.name)
                canonical.reload()
                livekit._apply_livekit_call_log_payload(canonical,
                    {"sip_call_id": "SCL_internal", "event": "call_ended"}, self.task, self.attempt,
                    diagnostics_enabled=False, context={"customer_phone": "9999999999"}, livekit_event="call_ended",
                    event_type_lower="call_ended", call_uuid="SCL_internal")
                self.assertEqual(canonical.sip_call_id, payload["SIPCallID"])
                self.assertEqual(canonical.call_uuid, payload["CallUUID"])
                self.assertTrue(canonical.transcript)
                self.assertEqual(frappe.db.count("AI Call Log", {"company": self.company}), 1)

    def test_vobiz_binding_merges_recording_that_arrived_first(self):
        canonical = frappe.get_doc({"doctype": "AI Call Log", "company": self.company, "task": self.task.name,
            "call_uuid": "SCL_internal", "sip_call_id": "SCL_internal"}).insert(ignore_permissions=True)
        orphan = vobiz.upsert_call_log(self.payload("recording"))
        vobiz.upsert_call_log(self.payload("transcript"))
        vobiz._bind_vobiz_identity(self.task, self.payload("hangup"))
        self.assertFalse(frappe.db.exists("AI Call Log", orphan))
        canonical.reload()
        self.assertEqual(canonical.call_uuid, self.payload("recording")["CallUUID"])
        self.assertEqual(self.task.transcript, canonical.transcript)

    def test_number_format_does_not_guess_country(self):
        self.assertEqual(call_identity.call_phone("00919873090386"), "+919873090386")
        self.assertEqual(call_identity.call_phone("9873090386", "+919873090386"), "+919873090386")
        self.assertEqual(call_identity.call_phone("00442071234567"), "+442071234567")
        self.assertEqual(call_identity.call_phone("2025550123"), "2025550123")

    def test_inbound_does_not_reuse_old_or_different_call(self):
        self.task.channel = "Voice"
        self.task.external_record_type = "Vobiz Inbound Call"
        self.task.status = "Running"
        self.task.call_uuid = "SCL_old"
        self.task.context_json = json.dumps({"customer_phone": "9999999999", "called_number": "9999999998"})
        self.task.flags.ignore_mandatory = True
        self.task.save(ignore_permissions=True)
        old_time = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-3)
        frappe.db.set_value("AI Task", self.task.name, "creation", old_time)
        payload = {"CallUUID": "new-provider-id", "From": "00919999999999", "To": "00919999999998"}
        self.assertIsNone(inbound_sales._find_latest_inbound_task(payload))
        frappe.db.set_value("AI Task", self.task.name, {"creation": frappe.utils.now_datetime(), "call_uuid": "old-provider-id"})
        self.assertIsNone(inbound_sales._find_latest_inbound_task(payload))
        frappe.db.set_value("AI Task", self.task.name, "call_uuid", "SCL_current")
        self.assertEqual(inbound_sales._find_latest_inbound_task(payload).name, self.task.name)
        payload["From"] = "00918888888888"
        self.assertIsNone(inbound_sales._find_latest_inbound_task(payload))

    def test_conflicting_task_is_rejected(self):
        other = frappe.get_doc({"doctype": "AI Task", "company": self.company})
        other.flags.ignore_mandatory = True
        other.insert(ignore_permissions=True)
        name = vobiz.upsert_call_log(self.payload("recording"))
        frappe.db.set_value("AI Call Log", name, "task", other.name)
        with self.assertRaises(frappe.ValidationError):
            self.bind()
        self.assertEqual(frappe.db.get_value("AI Call Log", name, "task"), other.name)

    def test_other_company_identity_is_not_claimed(self):
        name = vobiz.upsert_call_log(self.payload("recording"))
        frappe.db.set_value("AI Call Log", name, "company", None)
        self.assertNotEqual(self.bind(), name)
        self.assertFalse(frappe.db.get_value("AI Call Log", name, "task"))
        frappe.db.set_value("AI Call Log", name, "company", self.company)

    def test_livekit_end_preserves_provider_payload(self):
        name = vobiz.upsert_call_log(self.payload("hangup"))
        doc = frappe.get_doc("AI Call Log", name)
        original = doc.status_payload_json
        for event in ("call_ended", "participant_joined"):
            livekit._apply_livekit_call_log_payload(doc, {"event": event, "direction": "Outbound"}, self.task, self.attempt,
                diagnostics_enabled=False, context={}, livekit_event=event, event_type_lower=event, call_uuid=doc.call_uuid)
            self.assertEqual(doc.status, "Completed")
            self.assertEqual(doc.status_payload_json, original)
            self.assertEqual(doc.direction, "Outbound")

    def test_failed_handler_receipt_survives_rollback(self):
        payload = {**self.payload("transcript"), "task": self.task.name}
        def fail(value):
            frappe.db.set_value("AI Task", self.task.name, "transcript", "must roll back")
            raise RuntimeError("unit handler failure")
        with self.assertRaisesRegex(RuntimeError, "unit handler failure"):
            webhook._process_telephony_receipt("vobiz", payload, fail)
        rows = frappe.get_all("AI Webhook Event", filters={"task": self.task.name}, fields=["status", "payload_json", "error_message"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "Failed")
        self.assertEqual(json.loads(rows[0].payload_json), payload)
        self.assertFalse(frappe.db.get_value("AI Task", self.task.name, "transcript"))

    def test_concurrent_callbacks_create_one_row(self):
        self._concurrent_events(("recording", "transcript"))

    def test_concurrent_identity_and_media_keep_one_linked_row(self):
        self._concurrent_events(("identity", "recording"))
        rows = frappe.get_all("AI Call Log", filters={"company": self.company}, fields=["task", "recording_url"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].task, self.task.name)
        self.assertTrue(rows[0].recording_url)

    def _concurrent_events(self, events):
        site, sites_path = frappe.local.site, frappe.local.sites_path
        barrier = threading.Barrier(2)
        def callback(event):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            try:
                barrier.wait(timeout=10)
                name = self.bind() if event == "identity" else vobiz.upsert_call_log(self.payload(event))
                frappe.db.commit()
                return name
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(callback, event) for event in events]
            names = [job.result(timeout=30) for job in jobs]
        self.assertEqual(names[0], names[1])
        doc = frappe.get_doc("AI Call Log", names[0])
        if "transcript" in events:
            self.assertTrue(doc.transcript)
        self.assertTrue(doc.recording_url)
