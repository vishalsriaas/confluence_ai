import unittest
from unittest.mock import patch
from types import SimpleNamespace

import frappe

from agent_army.services.mcp import assert_tool_allowed


class TestMCPPermissions(unittest.TestCase):
    @patch("agent_army.services.mcp.frappe")
    def test_assert_tool_allowed_blocks_missing_permission(self, fake_frappe):
        fake_frappe.db = SimpleNamespace(exists=lambda *args, **kwargs: False)
        fake_frappe.PermissionError = frappe.PermissionError
        with self.assertRaises(frappe.PermissionError):
            assert_tool_allowed("create_patient_note", agent="agent-1")
