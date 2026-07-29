from __future__ import annotations

import asyncio
import unittest

from livekit_agent.session_runtime import VoiceSessionRuntime, redact_sensitive_text


class TestVoiceSessionRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, dict]] = []

        async def emit(event: str, **payload) -> None:
            self.events.append((event, payload))

        self.runtime = VoiceSessionRuntime(
            emit=emit,
            response_timeout_seconds=0.01,
            recovery_timeout_seconds=0.01,
            reconnect_grace_seconds=0.01,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.finish("test_finished")

    async def test_deduplicates_and_redacts_final_transcript_turns(self) -> None:
        self.assertTrue(self.runtime.add_user_turn("My OTP is 123456", turn_id="user-1"))
        self.assertFalse(self.runtime.add_user_turn("My OTP is 123456", turn_id="user-1"))
        self.runtime.add_agent_turn("Please keep it private.", turn_id="agent-1")
        await asyncio.sleep(0)

        self.assertEqual(
            self.runtime.transcript(),
            "CUSTOMER: My OTP is [REDACTED]\nAGENT: Please keep it private.",
        )
        self.assertEqual(self.runtime.metrics()["turn_count"], 2)

    async def test_recovers_once_after_response_timeout(self) -> None:
        async def recover(_customer_text: str) -> None:
            self.runtime.add_agent_turn("Sorry for the delay. How can I help?", turn_id="agent-recovery")

        self.runtime.set_recovery_callback(recover)
        self.runtime.add_user_turn("Are you there?", turn_id="user-timeout")
        await asyncio.sleep(0.05)

        metrics = self.runtime.metrics()
        self.assertEqual(metrics["recovery_attempted"], 1)
        self.assertEqual(metrics["recovery_succeeded"], 1)
        self.assertFalse(metrics["failure_code"])

    async def test_marks_persistent_silence_as_model_timeout(self) -> None:
        async def no_response(_customer_text: str) -> None:
            return None

        self.runtime.set_recovery_callback(no_response)
        self.runtime.add_user_turn("Hello?", turn_id="user-silent")
        await asyncio.sleep(0.05)

        self.assertEqual(self.runtime.metrics()["failure_code"], "model_timeout")
        self.assertIn("agent_recovery_failed", [event for event, _payload in self.events])

    async def test_allows_reconnect_before_grace_expires(self) -> None:
        expired = False

        async def on_expired() -> None:
            nonlocal expired
            expired = True

        self.runtime.participant_disconnected(on_expired)
        await asyncio.sleep(0)
        self.runtime.participant_connected()
        await asyncio.sleep(0.02)

        self.assertFalse(expired)
        self.assertEqual(self.runtime.metrics()["reconnect_count"], 1)

    async def test_five_consecutive_disconnect_rejoins_preserve_session(self) -> None:
        expiries = 0

        async def on_expired() -> None:
            nonlocal expiries
            expiries += 1

        for _attempt in range(5):
            self.runtime.participant_disconnected(on_expired)
            await asyncio.sleep(0)
            self.runtime.participant_connected()
            await asyncio.sleep(0)

        await asyncio.sleep(0.02)
        self.assertEqual(expiries, 0)
        self.assertEqual(self.runtime.metrics()["reconnect_count"], 5)

    async def test_model_error_attempts_one_recovery_and_clears_transient_failure(self) -> None:
        async def recover(_customer_text: str) -> None:
            self.runtime.add_agent_turn("I recovered and can continue.", turn_id="agent-model-recovery")

        self.runtime.set_recovery_callback(recover)
        self.runtime.add_user_turn("Please continue.", turn_id="user-before-error")
        self.runtime.record_error("temporary realtime model error")
        await asyncio.sleep(0.03)

        metrics = self.runtime.metrics()
        self.assertEqual(metrics["recovery_attempted"], 1)
        self.assertEqual(metrics["recovery_succeeded"], 1)
        self.assertFalse(metrics["failure_code"])

    async def test_disconnect_expiry_sets_failure(self) -> None:
        expired = False

        async def on_expired() -> None:
            nonlocal expired
            expired = True

        self.runtime.participant_disconnected(on_expired)
        await asyncio.sleep(0.03)

        self.assertTrue(expired)
        self.assertEqual(self.runtime.metrics()["failure_code"], "participant_disconnect")


class TestRedaction(unittest.TestCase):
    def test_redacts_common_spoken_secrets(self) -> None:
        self.assertEqual(redact_sensitive_text("password: hello123"), "password: [REDACTED]")
        self.assertEqual(redact_sensitive_text("card pin is 1111"), "card pin is [REDACTED]")
        self.assertEqual(redact_sensitive_text("card number is 4111111111111111"), "card number is [REDACTED]")
