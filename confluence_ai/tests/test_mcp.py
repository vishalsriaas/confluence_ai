import unittest
from unittest.mock import patch
from types import SimpleNamespace

import frappe

from confluence_ai.api.mcp import (
    LIVEKIT_CONSOLE_SANDBOX_TASK,
    LIVEKIT_CONSOLE_TOOLS,
    get_allowed_tools,
)
from confluence_ai.services.mcp import assert_tool_allowed


class TestMCPPermissions(unittest.TestCase):
    @patch("confluence_ai.services.mcp.frappe")
    def test_assert_tool_allowed_blocks_missing_permission(self, fake_frappe):
        fake_frappe.db = SimpleNamespace(exists=lambda *args, **kwargs: False)
        fake_frappe.PermissionError = frappe.PermissionError
        with self.assertRaises(frappe.PermissionError):
            assert_tool_allowed("create_patient_note", agent="agent-1")

    @patch("confluence_ai.api.mcp.frappe.get_doc")
    @patch("confluence_ai.api.mcp.frappe.get_all")
    def test_livekit_console_scope_exposes_only_safe_read_tools(self, get_all, get_doc):
        tool_ids = {
            f"MCP-TOOL-{index}": tool_name
            for index, tool_name in enumerate(LIVEKIT_CONSOLE_TOOLS)
        }
        get_all.return_value = list(tool_ids)
        get_doc.side_effect = lambda _doctype, name: SimpleNamespace(tool_name=tool_ids[name])

        tools = get_allowed_tools(LIVEKIT_CONSOLE_SANDBOX_TASK)

        self.assertEqual([tool.tool_name for tool in tools], list(LIVEKIT_CONSOLE_TOOLS))
        self.assertEqual(
            get_all.call_args.kwargs["filters"]["tool_name"],
            ["in", list(LIVEKIT_CONSOLE_TOOLS)],
        )
