from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import frappe
from frappe.exceptions import TimestampMismatchError

from confluence_ai.services.livekit import _outbound_sip_trunk_id, _upsert_livekit_call_log, _voice_metadata_context
from confluence_ai.services import livekit


class TestBackendProviderIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.participant = SimpleNamespace(identity="sip_unit", sid="PA_unit", attributes={
            "sip.callID": "SCL_unit", "sip.callIDFull": "provider-unit",
        })
        self.lookup = AsyncMock(return_value=self.participant)
        self.client = SimpleNamespace(room=SimpleNamespace(get_participant=self.lookup))
        self.diagnostics = {}
        self.sleep = AsyncMock()
        sleeper = patch.object(livekit.asyncio, "sleep", self.sleep)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    async def resolve(self):
        return await livekit._outbound_provider_call_id(
            self.client, "room-unit", "sip_unit", participant_sid="PA_unit",
            sip_call_sid="SCL_unit", diagnostics=self.diagnostics,
        )

    async def test_immediate_identity_needs_no_retry(self):
        self.assertEqual(await self.resolve(), "provider-unit")
        self.assertEqual(self.diagnostics, {"checks": 1, "reason": "captured"})
        request = self.lookup.call_args.args[0]
        self.assertEqual((request.room, request.identity), ("room-unit", "sip_unit"))
        self.sleep.assert_not_awaited()

    async def test_missing_attributes_are_retried(self):
        missing = SimpleNamespace(identity="sip_unit", sid="PA_unit", attributes={"sip.callID": "SCL_unit"})
        self.lookup.side_effect = [missing, self.participant]
        self.assertEqual(await self.resolve(), "provider-unit")
        self.assertEqual(self.lookup.await_count, 2)

    async def test_transient_api_error_is_retried(self):
        from livekit.api.twirp_client import TwirpError
        self.lookup.side_effect = [TwirpError("not_found", "not visible yet", status=404), self.participant]
        self.assertEqual(await self.resolve(), "provider-unit")
        self.assertEqual(self.diagnostics["reason"], "captured")

    async def test_lookup_timeout_is_retried(self):
        self.lookup.side_effect = [TimeoutError(), self.participant]
        self.assertEqual(await self.resolve(), "provider-unit")

    async def test_retries_stop_and_internal_id_is_never_bound(self):
        self.participant.attributes["sip.callIDFull"] = "SCL_internal"
        self.assertIsNone(await self.resolve())
        self.assertEqual(self.lookup.await_count, 5)
        self.assertEqual(self.sleep.await_count, 4)
        self.assertEqual(self.diagnostics["reason"], "provider_call_id_missing")

    async def test_auth_failure_is_visible_and_not_retried(self):
        from livekit.api.twirp_client import TwirpError
        self.lookup.side_effect = TwirpError("unauthenticated", "sensitive SDK detail", status=401)
        self.assertIsNone(await self.resolve())
        self.assertEqual(self.diagnostics, {"checks": 1, "reason": "lookup_unauthenticated"})
        self.sleep.assert_not_awaited()

    async def test_replaced_participant_is_not_bound(self):
        self.participant.sid = "PA_other_call"
        self.assertIsNone(await self.resolve())
        self.assertEqual(self.diagnostics["reason"], "participant_identity_mismatch")
        self.sleep.assert_not_awaited()

    async def test_other_sip_leg_is_not_bound(self):
        self.participant.attributes["sip.callID"] = "SCL_other"
        self.assertIsNone(await self.resolve())
        self.assertEqual(self.diagnostics["reason"], "participant_identity_mismatch")

    async def test_dispatch_precedes_lookup_and_missing_id_does_not_fail_call(self):
        task = frappe._dict(name="task-unit", assigned_agent="agent-unit", channel="Voice")
        agent = frappe._dict(name="agent-unit", allowed_channel_account="channel-unit")
        account = frappe._dict(base_url="wss://unit.invalid", endpoint_paths_json='{"outbound_sip_trunk_id":"ST_unit"}')
        account.get_password = Mock(return_value="unit-credential")
        client = SimpleNamespace(
            room=SimpleNamespace(create_room=AsyncMock(return_value=SimpleNamespace(sid="RM_unit", name="room-unit", metadata="{}"))),
            sip=SimpleNamespace(create_sip_participant=AsyncMock(return_value=SimpleNamespace(
                sip_call_id="SCL_unit", participant_identity="sip_unit", participant_id="PA_unit"))),
            agent_dispatch=SimpleNamespace(create_dispatch=AsyncMock(return_value=SimpleNamespace(id="AD_unit"))),
            aclose=AsyncMock(),
        )
        async def missing(*args, **kwargs):
            client.agent_dispatch.create_dispatch.assert_awaited_once()
            kwargs["diagnostics"].update(checks=5, reason="provider_call_id_missing")
            return None
        with patch.object(livekit.frappe, "get_doc", side_effect=[task, agent, account]), \
             patch.object(livekit, "build_voice_metadata", return_value={"context": {}}), \
             patch.object(livekit, "_livekit_dispatch_name", return_value="unit-agent"), \
             patch.object(livekit.api, "LiveKitAPI", return_value=client), \
             patch.object(livekit, "_outbound_provider_call_id", side_effect=missing), \
             patch.object(livekit, "record_provider_event"), \
             patch.object(livekit, "create_error") as error:
            result = await livekit._start_voice_task_async(task.name, {"phone": "+919999999999", "attempt": "attempt-unit"})
        self.assertEqual(result["dispatch_id"], "AD_unit")
        self.assertEqual(result["call_identity_status"], "unavailable")
        self.assertNotIn("sip_call_id", result)
        error.assert_called_once()
        client.sip.create_sip_participant.assert_awaited_once()
        client.aclose.assert_awaited_once()


class TestLiveKit(unittest.TestCase):
    def test_outbound_sip_trunk_prefers_explicit_outbound_id(self):
        account = frappe._dict({"trunk_id": "ST_INBOUND"})
        endpoints = {"outbound_sip_trunk_id": "ST_OUTBOUND", "sip_trunk_id": "ST_GENERIC"}

        self.assertEqual(_outbound_sip_trunk_id(account, endpoints), "ST_OUTBOUND")

    def test_outbound_sip_trunk_falls_back_to_legacy_fields(self):
        account = frappe._dict({"trunk_id": "ST_ACCOUNT"})

        self.assertEqual(_outbound_sip_trunk_id(account, {"sip_trunk_id": "ST_GENERIC"}), "ST_GENERIC")
        self.assertEqual(_outbound_sip_trunk_id(account, {}), "ST_ACCOUNT")

    def test_call_log_upsert_retries_after_timestamp_mismatch(self):
        class FakeMeta:
            def has_field(self, fieldname):
                return False

        class FakeCallLog:
            def __init__(self, fail_once=False):
                self.meta = FakeMeta()
                self.fail_once = fail_once

            def __getattr__(self, fieldname):
                return None

            def save(self, ignore_permissions=False):
                if self.fail_once:
                    self.fail_once = False
                    raise TimestampMismatchError("stale")

        task = SimpleNamespace(
            name="task-unit-livekit",
            context_json=frappe.as_json({"phone": "+919999999999"}),
            call_uuid="call-unit-livekit",
            external_record_id=None,
            assigned_agent="agent-unit",
            target_agent=None,
            company="globifit",
            trunk_id=None,
        )
        first_doc = FakeCallLog(fail_once=True)
        second_doc = FakeCallLog()
        fake_db = SimpleNamespace(exists=Mock(return_value=True), get_value=Mock(return_value=None))
        get_doc = Mock(side_effect=[first_doc, second_doc])

        with patch("confluence_ai.services.livekit.frappe.db", fake_db), \
            patch("confluence_ai.services.call_registry.resolve_call", get_doc), \
            patch("confluence_ai.services.call_registry.register_call"), \
            patch("confluence_ai.services.call_registry.apply_event_state"), \
            patch("confluence_ai.services.livekit.create_error") as create_error, \
            patch("confluence_ai.services.livekit.frappe.clear_messages", Mock()):
            _upsert_livekit_call_log(
                {
                    "event": "call_ended",
                    "status": "completed",
                    "duration_ms": 31000,
                    "ended_at": "2026-08-31 10:00:00",
                },
                task,
            )

        self.assertEqual(get_doc.call_count, 2)
        self.assertEqual(first_doc.status, "Completed")
        self.assertEqual(second_doc.status, "Completed")
        self.assertEqual(second_doc.duration_sec, 31)
        create_error.assert_not_called()

    def test_voice_metadata_promotes_start_context_whatsapp_summary(self):
        context = {
            "event": "inbound-sales-call",
            "customer_phone": "9582005503",
            "selected_sales_route": {"route": "sales-route"},
            "start_context_tools": {
                "GLOBIFIT_whatsapp_conversation_summary": {
                    "status": "success",
                    "found": True,
                    "summary": "1. Chat Summary: fallback should not be preferred.",
                    "records": [
                        {
                            "channel_account": "GLOBIFIT_MI",
                            "ai_summary": "Customer discussed erection concern, shared age 32, and asked to confirm order.",
                            "chat_summary": "Old/noisy summary should not be preferred.",
                        }
                    ],
                }
            },
        }

        metadata = _voice_metadata_context(context)

        self.assertTrue(metadata["whatsapp_conversation_found"])
        self.assertEqual(
            metadata["whatsapp_conversation_summary"],
            "Customer discussed erection concern, shared age 32, and asked to confirm order.",
        )
        self.assertNotIn("start_context_tools", metadata)
