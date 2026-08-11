from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from livekit_agent.session_runtime import VoiceSessionRuntime, redact_sensitive_text


class TestVoiceSessionRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, dict]] = []

        async def emit(event: str, **payload) -> None:
            self.events.append((event, payload))

        self.runtime = VoiceSessionRuntime(
            emit=emit,
            response_timeout_seconds=0.01,
            playout_timeout_seconds=0.01,
            recovery_timeout_seconds=0.01,
            reconnect_grace_seconds=0.01,
            false_interruption_timeout_seconds=0.01,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.finish("test_finished")

    async def test_deduplicates_and_redacts_final_transcript_turns(self) -> None:
        self.assertTrue(self.runtime.add_user_turn("My OTP is 123456", turn_id="user-1"))
        self.assertFalse(self.runtime.add_user_turn("My OTP is 123456", turn_id="user-1"))
        self.runtime.add_agent_turn("Please keep it private.", turn_id="agent-1")
        self.runtime.complete_agent_playout("speech-1", interrupted=False)
        await asyncio.sleep(0)

        self.assertEqual(
            self.runtime.transcript(),
            "CUSTOMER: My OTP is [REDACTED]\nAGENT: Please keep it private.",
        )
        self.assertEqual(self.runtime.metrics()["turn_count"], 2)

    async def test_deduplicates_native_realtime_events_with_different_ids(self) -> None:
        self.assertTrue(self.runtime.add_user_turn("Work Shop", turn_id="transcript-event"))
        self.assertFalse(self.runtime.add_user_turn("Work Shop", turn_id="conversation-item"))

        self.assertEqual(self.runtime.transcript(), "CUSTOMER: Work Shop")
        self.assertEqual(self.runtime.metrics()["turn_count"], 1)
        self.assertEqual(self.runtime.user_turn_count, 1)

    async def test_tool_reply_authorization_is_one_shot_and_clearable(self) -> None:
        self.assertFalse(self.runtime.consume_expected_realtime_tool_reply())
        self.runtime.expect_realtime_tool_reply()
        self.assertTrue(self.runtime.consume_expected_realtime_tool_reply())
        self.assertFalse(self.runtime.consume_expected_realtime_tool_reply())

        self.runtime.expect_realtime_tool_reply()
        self.runtime.clear_expected_realtime_tool_replies()
        self.assertFalse(self.runtime.consume_expected_realtime_tool_reply())

    async def test_recovers_once_after_response_timeout(self) -> None:
        async def recover(_customer_text: str, _reason: str) -> None:
            self.runtime.track_agent_speech("speech-recovery")
            self.runtime.add_agent_turn("Sorry for the delay. How can I help?", turn_id="agent-recovery")
            self.runtime.complete_agent_playout("speech-recovery", interrupted=False)

        self.runtime.set_recovery_callback(recover)
        self.runtime.add_user_turn("Are you there?", turn_id="user-timeout")
        await asyncio.sleep(0.05)

        metrics = self.runtime.metrics()
        self.assertEqual(metrics["recovery_attempted"], 1)
        self.assertEqual(metrics["recovery_succeeded"], 1)
        self.assertFalse(metrics["failure_code"])

    async def test_marks_persistent_silence_as_model_timeout(self) -> None:
        async def no_response(_customer_text: str, _reason: str) -> None:
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
        async def recover(_customer_text: str, _reason: str) -> None:
            self.runtime.add_agent_turn("I recovered and can continue.", turn_id="agent-model-recovery")
            self.runtime.complete_agent_playout("speech-model-recovery", interrupted=False)

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

    async def test_intentional_user_disconnect_finishes_as_completed(self) -> None:
        self.runtime.failure_code = "participant_disconnect"

        await self.runtime.finish("user_initiated")

        ended = [payload for event, payload in self.events if event == "call_ended"]
        self.assertTrue(ended)
        self.assertEqual(ended[-1]["status"], "completed")
        self.assertEqual(ended[-1]["failure_code"], "participant_disconnect")

    async def test_agent_transcript_does_not_complete_response_before_playout(self) -> None:
        recovery_reasons: list[str] = []

        async def recover(_customer_text: str, reason: str) -> None:
            recovery_reasons.append(reason)

        self.runtime.set_recovery_callback(recover)
        self.runtime.add_user_turn("Please tell me the rates.", turn_id="user-rates")
        self.runtime.add_agent_turn("Our rates start from", turn_id="agent-partial")
        await asyncio.sleep(0.03)

        self.assertIn("response_timeout", recovery_reasons)
        self.assertEqual(self.runtime.metrics()["completed_playout_count"], 0)

    async def test_false_interruption_recovers_once_without_new_customer_turn(self) -> None:
        recovery_reasons: list[str] = []

        async def recover(_customer_text: str, reason: str) -> None:
            recovery_reasons.append(reason)
            self.runtime.track_agent_speech("speech-continued")
            self.runtime.add_agent_turn("and you also get a dedicated manager.", turn_id="agent-continued")
            self.runtime.complete_agent_playout("speech-continued", interrupted=False)

        self.runtime.set_recovery_callback(recover)
        self.runtime.add_user_turn("What else do I get?", turn_id="user-benefits")
        self.runtime.track_agent_speech("speech-cut")
        self.runtime.complete_agent_playout("speech-cut", interrupted=True)
        await asyncio.sleep(0.04)

        self.assertEqual(recovery_reasons, ["false_interruption"])
        self.assertEqual(self.runtime.metrics()["false_interruption_recoveries"], 1)
        self.assertEqual(self.runtime.metrics()["recovery_succeeded"], 1)

    async def test_real_customer_barge_in_cancels_false_interruption_recovery(self) -> None:
        recovery_reasons: list[str] = []

        async def recover(_customer_text: str, reason: str) -> None:
            recovery_reasons.append(reason)

        runtime = VoiceSessionRuntime(
            emit=self.runtime.emit,
            response_timeout_seconds=0.1,
            playout_timeout_seconds=0.1,
            recovery_timeout_seconds=0.01,
            reconnect_grace_seconds=0.01,
            false_interruption_timeout_seconds=0.02,
        )
        runtime.set_recovery_callback(recover)
        runtime.add_user_turn("Explain the benefits.", turn_id="user-one")
        runtime.track_agent_speech(
            "speech-one",
            native_resume_eligible=True,
        )
        runtime.complete_agent_playout("speech-one", interrupted=True)
        runtime.add_user_turn("Wait, tell me the rates first.", turn_id="user-two")
        await asyncio.sleep(0.04)
        await runtime.finish("test_finished")

        self.assertEqual(recovery_reasons, [])
        self.assertEqual(runtime.metrics()["interruption_count"], 1)

    async def test_native_false_interruption_resume_does_not_also_regenerate(self) -> None:
        recovery_reasons: list[str] = []

        async def recover(_customer_text: str, reason: str) -> None:
            recovery_reasons.append(reason)

        runtime = VoiceSessionRuntime(
            emit=self.runtime.emit,
            response_timeout_seconds=0.1,
            playout_timeout_seconds=0.1,
            recovery_timeout_seconds=0.01,
            reconnect_grace_seconds=0.01,
            false_interruption_timeout_seconds=0.01,
            native_false_interruption_resume=True,
        )
        runtime.set_recovery_callback(recover)
        runtime.add_user_turn("Please continue.", turn_id="user-one")
        runtime.track_agent_speech(
            "speech-one",
            native_resume_eligible=True,
        )
        runtime.complete_agent_playout("speech-one", interrupted=True)
        await asyncio.sleep(0.03)
        await runtime.finish("test_finished")

        self.assertEqual(recovery_reasons, [])
        self.assertEqual(runtime.metrics()["interruption_count"], 1)
        self.assertEqual(runtime.metrics()["false_interruption_recoveries"], 0)

    async def test_explicit_reply_recovers_when_native_resume_is_enabled(self) -> None:
        recovery_reasons: list[str] = []

        async def recover(_customer_text: str, reason: str) -> None:
            recovery_reasons.append(reason)

        runtime = VoiceSessionRuntime(
            emit=self.runtime.emit,
            response_timeout_seconds=0.1,
            playout_timeout_seconds=0.1,
            recovery_timeout_seconds=0.01,
            reconnect_grace_seconds=0.01,
            false_interruption_timeout_seconds=0.01,
            native_false_interruption_resume=True,
        )
        runtime.set_recovery_callback(recover)
        runtime.add_user_turn(
            "Mere business ka naam Harsh Enterprises hai.",
            turn_id="business-name",
        )
        runtime.track_agent_speech(
            "worker-owned-speech",
            native_resume_eligible=False,
        )
        runtime.complete_agent_playout(
            "worker-owned-speech",
            interrupted=True,
        )
        await asyncio.sleep(0.03)
        await runtime.finish("test_finished")

        self.assertEqual(recovery_reasons, ["false_interruption"])
        self.assertEqual(runtime.metrics()["false_interruption_recoveries"], 1)

    async def test_same_call_context_is_bounded_and_redacted(self) -> None:
        self.runtime.add_user_turn("My OTP is 123456", turn_id="user-secret")
        self.runtime.add_agent_turn("Understood.", turn_id="agent-secret")
        self.runtime.add_user_turn("I need better rates.", turn_id="user-rates")

        memory = self.runtime.same_call_context(
            current_user_text="Also remember my OTP is 999999",
            max_turns=2,
            max_chars=500,
        )

        self.assertNotIn("123456", memory)
        self.assertNotIn("999999", memory)
        self.assertIn("I need better rates.", memory)
        self.assertIn("[REDACTED]", memory)

    async def test_same_call_memory_keeps_one_multi_detail_customer_answer(self) -> None:
        answer = (
            "My brand is North Star, we are a D2C business doing 600 shipments monthly, "
            "and high rates are our main challenge."
        )
        self.assertTrue(self.runtime.add_user_turn(answer, turn_id="qualification-details"))
        self.assertFalse(self.runtime.add_user_turn(answer, turn_id="qualification-details"))

        memory = self.runtime.same_call_context()

        self.assertEqual(memory.count("North Star"), 1)
        self.assertIn("600 shipments monthly", memory)
        self.assertIn("high rates are our main challenge", memory)

    async def test_tool_outcome_is_sanitized_and_emitted_once(self) -> None:
        self.runtime.record_tool_outcome(
            "calculate_shipkia_rate",
            status="blocked",
            summary="password: hidden-value",
        )
        await asyncio.sleep(0)

        self.assertEqual(self.runtime.metrics()["tool_outcome_count"], 1)
        self.assertIn("[REDACTED]", self.runtime.tool_outcomes[0]["summary"])
        self.assertEqual(
            len([event for event, _payload in self.events if event == "tool_outcome"]),
            1,
        )

    async def test_session_usage_is_persisted_without_inventing_cost(self) -> None:
        self.runtime.record_session_usage(
            SimpleNamespace(
                model_usage=[
                    {
                        "type": "llm_usage",
                        "provider": "google",
                        "model": "gemini-live",
                        "input_tokens": 120,
                        "input_audio_tokens": 80,
                        "output_tokens": 45,
                        "output_audio_tokens": 30,
                    }
                ]
            )
        )

        metrics = self.runtime.metrics()
        self.assertEqual(metrics["model_usage"][0]["input_tokens"], 120)
        self.assertEqual(metrics["model_usage"][0]["output_audio_tokens"], 30)
        self.assertEqual(metrics["monetary_cost_status"], "cost_unavailable")


class TestRedaction(unittest.TestCase):
    def test_redacts_common_spoken_secrets(self) -> None:
        self.assertEqual(redact_sensitive_text("password: hello123"), "password: [REDACTED]")
        self.assertEqual(redact_sensitive_text("card pin is 1111"), "card pin is [REDACTED]")
        self.assertEqual(redact_sensitive_text("card number is 4111111111111111"), "card number is [REDACTED]")
