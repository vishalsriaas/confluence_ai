"""Run the call-log regression boundary without placing calls or spending AI tokens."""
import io
import unittest
from unittest.mock import patch

import frappe


MODULES = (
    "test_call_registry", "test_call_identity", "test_call_log_reliability", "test_livekit",
    "test_vobiz", "test_recording_transcription", "test_inbound_startup", "test_executor",
    "test_call_disposition", "test_fresh_followup_deduplication", "test_fresh_followup",
    "test_repeat_followup",
    "test_bridge_disposition_flow",
)


def run():
    frappe.flags.in_test = True
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    for module in MODULES:
        suite.addTests(loader.loadTestsFromName("confluence_ai.tests." + module))
    stream = io.StringIO()
    with patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP disabled during regression tests")), \
         patch("aiohttp.ClientSession._request", side_effect=AssertionError("External async HTTP disabled during regression tests")), \
         patch("frappe.utils.background_jobs.enqueue", return_value=None):
        result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return {
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "passed": result.wasSuccessful(),
        "detail": stream.getvalue(),
    }


def configuration():
    from confluence_ai.services.recording_transcription import get_recording_transcription_config
    config = get_recording_transcription_config()
    return {"enabled": config.enabled, "grace_minutes": config.wait_minutes,
        "retry_minutes": config.retry_minutes, "maximum_checks": config.max_attempts,
        "call_identity_schema": bool(frappe.db.exists("DocType", "AI Call Identity"))}


def enable_local_recovery():
    if frappe.local.site != "localhost":
        raise RuntimeError("Local verification settings only")
    frappe.db.set_single_value("Confluence AI Settings", "enable_vobiz_transcript_recovery", 1)
    return configuration()
