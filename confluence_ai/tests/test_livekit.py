from __future__ import annotations

import unittest

import frappe

from confluence_ai.services.livekit import _outbound_sip_trunk_id


class TestLiveKit(unittest.TestCase):
    def test_outbound_sip_trunk_prefers_explicit_outbound_id(self):
        account = frappe._dict({"trunk_id": "ST_INBOUND"})
        endpoints = {"outbound_sip_trunk_id": "ST_OUTBOUND", "sip_trunk_id": "ST_GENERIC"}

        self.assertEqual(_outbound_sip_trunk_id(account, endpoints), "ST_OUTBOUND")

    def test_outbound_sip_trunk_falls_back_to_legacy_fields(self):
        account = frappe._dict({"trunk_id": "ST_ACCOUNT"})

        self.assertEqual(_outbound_sip_trunk_id(account, {"sip_trunk_id": "ST_GENERIC"}), "ST_GENERIC")
        self.assertEqual(_outbound_sip_trunk_id(account, {}), "ST_ACCOUNT")
