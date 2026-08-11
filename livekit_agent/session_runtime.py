from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any


EventEmitter = Callable[[str], Awaitable[None]]
RecoveryCallback = Callable[[str, str], Awaitable[None]]
DisconnectCallback = Callable[[], Awaitable[None]]

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(otp|one[- ]time password|password|passcode|cvv|card pin|payment pin|"
        r"upi pin|card number|credit card|debit card|bank account number)"
        r"(\s*(?:is|:|-)\s*)\S+"
    ),
    re.compile(r"(?i)\b(api[_ -]?key|api[_ -]?secret|access[_ -]?token)(\s*(?:is|:|=|-)\s*)\S+"),
)


def redact_sensitive_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return text


class VoiceSessionRuntime:
    """Track a single browser voice run without retaining audio."""

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        response_timeout_seconds: float = 15,
        playout_timeout_seconds: float = 30,
        recovery_timeout_seconds: float = 8,
        reconnect_grace_seconds: float = 20,
        false_interruption_timeout_seconds: float = 2.5,
        native_false_interruption_resume: bool = False,
    ) -> None:
        self.emit = emit
        self.response_timeout_seconds = response_timeout_seconds
        self.playout_timeout_seconds = playout_timeout_seconds
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.reconnect_grace_seconds = reconnect_grace_seconds
        self.false_interruption_timeout_seconds = false_interruption_timeout_seconds
        self.native_false_interruption_resume = native_false_interruption_resume
        self.started_monotonic = time.monotonic()
        self.started_at = time.time()
        self.turns: list[dict[str, Any]] = []
        self.seen_turn_ids: set[str] = set()
        self.tool_latencies_ms: list[int] = []
        self.tool_outcomes: list[dict[str, Any]] = []
        self.model_usage: list[dict[str, Any]] = []
        self.response_latencies_ms: list[int] = []
        self.reconnect_count = 0
        self.recovery_attempted = 0
        self.recovery_succeeded = 0
        self.interruption_count = 0
        self.completed_playout_count = 0
        self.false_interruption_recoveries = 0
        self.failure_code = ""
        self.close_reason = ""
        self._last_user_turn = 0
        self._last_agent_turn = 0
        self._last_latency_turn = 0
        self._last_user_text = ""
        self._last_user_monotonic: float | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._playout_watchdog_task: asyncio.Task | None = None
        self._interruption_recovery_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._recovery_callback: RecoveryCallback | None = None
        self._active_speech_id = ""
        self._speech_turns: dict[str, int] = {}
        self._speech_native_resume_eligible: dict[str, bool] = {}
        self._expected_realtime_tool_replies = 0
        self._finished = False

    def set_recovery_callback(self, callback: RecoveryCallback) -> None:
        self._recovery_callback = callback

    def _spawn(self, awaitable: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.create_task(awaitable)
        task.add_done_callback(self._log_task_exception)
        return task

    @staticmethod
    def _log_task_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return

    def add_user_turn(self, text: object, *, turn_id: str | None = None) -> bool:
        clean = redact_sensitive_text(text)
        if not clean:
            return False
        key = turn_id or f"user:{clean}"
        if key in self.seen_turn_ids:
            return False
        now_monotonic = time.monotonic()
        # Native realtime emits the same final customer utterance through both
        # user_input_transcribed and conversation_item_added with different IDs.
        # Keep one audit/runtime turn while still allowing a genuine later repeat.
        if (
            self._last_user_monotonic is not None
            and clean.casefold() == self._last_user_text.casefold()
            and now_monotonic - self._last_user_monotonic <= 3.0
        ):
            self.seen_turn_ids.add(key)
            return False
        self.seen_turn_ids.add(key)
        self._last_user_turn += 1
        self._last_user_text = clean
        self._last_user_monotonic = now_monotonic
        self._cancel_interruption_recovery()
        self.turns.append(
            {
                "role": "CUSTOMER",
                "text": clean,
                "created_at": time.time(),
                "turn": self._last_user_turn,
            }
        )
        self._schedule_watchdog(self._last_user_turn)
        self._spawn(self._emit_checkpoint("transcript_checkpoint"))
        return True

    def add_agent_turn(self, text: object, *, turn_id: str | None = None) -> bool:
        clean = redact_sensitive_text(text)
        if not clean:
            return False
        key = turn_id or f"agent:{clean}"
        if key in self.seen_turn_ids:
            return False
        self.seen_turn_ids.add(key)
        self._record_response_latency()
        self.turns.append(
            {
                "role": "AGENT",
                "text": clean,
                "created_at": time.time(),
                "turn": self._last_user_turn,
            }
        )
        self._spawn(self._emit_checkpoint("transcript_checkpoint"))
        return True

    @property
    def user_turn_count(self) -> int:
        """Monotonic customer-turn epoch used to reject stale async replies."""
        return self._last_user_turn

    def expect_realtime_tool_reply(self) -> None:
        """Authorize one server generation produced by a completed tool call."""
        self._expected_realtime_tool_replies += 1

    def consume_expected_realtime_tool_reply(self) -> bool:
        if self._expected_realtime_tool_replies <= 0:
            return False
        self._expected_realtime_tool_replies -= 1
        return True

    def clear_expected_realtime_tool_replies(self) -> None:
        """A new microphone turn invalidates any unconsumed prior tool reply."""
        self._expected_realtime_tool_replies = 0

    def mark_agent_speaking(self) -> None:
        self._record_response_latency()

    def track_agent_speech(
        self,
        speech_id: object,
        *,
        source: object = "generate_reply",
        native_resume_eligible: bool = False,
    ) -> None:
        clean_id = str(speech_id or "")
        if not clean_id:
            return
        self._active_speech_id = clean_id
        self._speech_turns[clean_id] = self._last_user_turn
        self._speech_native_resume_eligible[clean_id] = bool(native_resume_eligible)
        self._cancel_watchdog()
        self._cancel_playout_watchdog()

        async def watch_playout() -> None:
            try:
                await asyncio.sleep(self.playout_timeout_seconds)
            except asyncio.CancelledError:
                return
            if self._finished or self._active_speech_id != clean_id:
                return
            self.failure_code = "model_timeout"
            await self.emit(
                "agent_playout_stalled",
                speech_id=clean_id,
                source=str(source or "generate_reply"),
                metrics=self.metrics(),
            )
            await self._attempt_recovery("playout_stalled")

        self._playout_watchdog_task = self._spawn(watch_playout())
        self._spawn(
            self.emit(
                "agent_speech_created",
                speech_id=clean_id,
                source=str(source or "generate_reply"),
                metrics=self.metrics(),
            )
        )

    def complete_agent_playout(self, speech_id: object, *, interrupted: bool) -> None:
        clean_id = str(speech_id or "")
        if not clean_id:
            return
        speech_turn = self._speech_turns.pop(clean_id, self._last_user_turn)
        native_resume_eligible = self._speech_native_resume_eligible.pop(
            clean_id,
            False,
        )
        if clean_id == self._active_speech_id:
            self._active_speech_id = ""
            self._cancel_playout_watchdog()

        if interrupted:
            self.interruption_count += 1
            completed_playouts = self.completed_playout_count
            self._spawn(
                self.emit(
                    "agent_speech_interrupted",
                    speech_id=clean_id,
                    metrics=self.metrics(),
                )
            )
            if (
                not (
                    self.native_false_interruption_resume
                    and native_resume_eligible
                )
                and self._last_user_turn <= speech_turn
            ):
                self._schedule_interruption_recovery(
                    clean_id,
                    speech_turn,
                    completed_playouts,
                )
            return

        self.completed_playout_count += 1
        self._last_agent_turn = max(self._last_agent_turn, speech_turn)
        self._cancel_watchdog()
        self._cancel_interruption_recovery()
        self._mark_recovery_succeeded()
        self._spawn(
            self.emit(
                "agent_playout_completed",
                speech_id=clean_id,
                metrics=self.metrics(),
            )
        )

    def same_call_context(
        self,
        *,
        current_user_text: object = "",
        max_turns: int = 12,
        max_chars: int = 3500,
    ) -> str:
        selected = self.turns[-max(1, max_turns) :]
        lines = [f"{turn['role']}: {turn['text']}" for turn in selected]
        current = redact_sensitive_text(current_user_text)
        if current and (not lines or lines[-1] != f"CUSTOMER: {current}"):
            lines.append(f"CUSTOMER: {current}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[-max_chars:]
            first_break = text.find("\n")
            if first_break >= 0:
                text = text[first_break + 1 :]
        return text

    def record_memory_injection(self, context: str) -> None:
        if not context:
            return
        self._spawn(
            self.emit(
                "same_call_memory_applied",
                memory_turn_count=len(context.splitlines()),
                memory_chars=len(context),
                metrics=self.metrics(),
            )
        )

    def record_tool_latency(self, tool_name: str, elapsed_seconds: float, *, failed: bool = False) -> None:
        latency_ms = max(0, int(elapsed_seconds * 1000))
        self.tool_latencies_ms.append(latency_ms)
        if failed:
            self.failure_code = "tool_timeout" if elapsed_seconds >= 12 else "tool_error"
        self._spawn(
            self.emit(
                "tool_completed",
                tool_name=tool_name,
                tool_latency_ms=latency_ms,
                tool_failed=failed,
                metrics=self.metrics(),
            )
        )

    def record_tool_outcome(
        self,
        tool_name: str,
        *,
        status: object,
        summary: object = "",
    ) -> None:
        outcome = {
            "tool_name": str(tool_name or ""),
            "status": redact_sensitive_text(status)[:120],
            "summary": redact_sensitive_text(summary)[:500],
            "created_at": time.time(),
        }
        self.tool_outcomes.append(outcome)
        if len(self.tool_outcomes) > 40:
            self.tool_outcomes = self.tool_outcomes[-40:]
        self._spawn(
            self.emit(
                "tool_outcome",
                tool_outcome=outcome,
                tool_outcomes=list(self.tool_outcomes),
                metrics=self.metrics(),
            )
        )

    def record_session_usage(self, usage: object) -> None:
        raw_items = getattr(usage, "model_usage", None)
        if not isinstance(raw_items, list):
            return
        sanitized: list[dict[str, Any]] = []
        for item in raw_items:
            if is_dataclass(item):
                raw = asdict(item)
            elif isinstance(item, dict):
                raw = dict(item)
            else:
                raw = {
                    key: getattr(item, key)
                    for key in (
                        "type",
                        "provider",
                        "model",
                        "input_tokens",
                        "input_cached_tokens",
                        "input_audio_tokens",
                        "input_text_tokens",
                        "output_tokens",
                        "output_audio_tokens",
                        "output_text_tokens",
                        "audio_duration",
                        "characters_count",
                    )
                    if hasattr(item, key)
                }
            sanitized.append(
                {
                    str(key): value
                    for key, value in raw.items()
                    if isinstance(value, (str, int, float, bool, type(None)))
                }
            )
        self.model_usage = sanitized

    def participant_connected(self) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
            self.reconnect_count += 1
            self._spawn(self.emit("participant_reconnected", metrics=self.metrics()))
        self._disconnect_task = None

    def participant_disconnected(self, on_expired: DisconnectCallback) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            return

        async def wait_for_reconnect() -> None:
            await self.emit(
                "participant_reconnect_grace",
                reconnect_grace_seconds=self.reconnect_grace_seconds,
                metrics=self.metrics(),
            )
            try:
                await asyncio.sleep(self.reconnect_grace_seconds)
            except asyncio.CancelledError:
                return
            self.failure_code = self.failure_code or "participant_disconnect"
            await self.emit("participant_disconnect_timeout", metrics=self.metrics())
            await on_expired()

        self._disconnect_task = self._spawn(wait_for_reconnect())

    def record_error(self, error: object, *, code: str = "model_error") -> None:
        self.failure_code = code
        self._spawn(
            self.emit(
                "agent_error",
                failure_code=code,
                error=redact_sensitive_text(error)[:1000],
                metrics=self.metrics(),
            )
        )
        if (
            self._recovery_callback
            and self._last_user_text
            and self.recovery_attempted == 0
        ):
            self._cancel_watchdog()
            self.recovery_attempted += 1

            async def recover() -> None:
                await self.emit(
                    "agent_recovery_started",
                    failure_code=code,
                    customer_text=self._last_user_text,
                    metrics=self.metrics(),
                )
                try:
                    await self._recovery_callback(self._last_user_text, code)
                    await asyncio.sleep(self.recovery_timeout_seconds)
                except Exception as exc:
                    self.failure_code = "model_error"
                    await self.emit(
                        "agent_recovery_failed",
                        failure_code=self.failure_code,
                        error=redact_sensitive_text(exc)[:1000],
                        metrics=self.metrics(),
                    )
                    return
                if self.recovery_succeeded < self.recovery_attempted:
                    self.failure_code = "model_error"
                    await self.emit(
                        "agent_recovery_failed",
                        failure_code=self.failure_code,
                        metrics=self.metrics(),
                    )

            self._spawn(recover())

    def metrics(self) -> dict[str, Any]:
        return {
            "duration_seconds": max(0, int(time.monotonic() - self.started_monotonic)),
            "response_latencies_ms": list(self.response_latencies_ms),
            "tool_latencies_ms": list(self.tool_latencies_ms),
            "tool_outcome_count": len(self.tool_outcomes),
            "reconnect_count": self.reconnect_count,
            "recovery_attempted": self.recovery_attempted,
            "recovery_succeeded": self.recovery_succeeded,
            "interruption_count": self.interruption_count,
            "completed_playout_count": self.completed_playout_count,
            "false_interruption_recoveries": self.false_interruption_recoveries,
            "failure_code": self.failure_code,
            "close_reason": self.close_reason,
            "turn_count": len(self.turns),
            "model_usage": list(self.model_usage),
            "monetary_cost_status": "cost_unavailable",
        }

    def transcript(self) -> str:
        return "\n".join(f"{turn['role']}: {turn['text']}" for turn in self.turns)

    async def finish(self, reason: object) -> None:
        self._finished = True
        self.close_reason = str(reason or "unknown")
        self._cancel_watchdog()
        self._cancel_playout_watchdog()
        self._cancel_interruption_recovery()
        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
        intentional_disconnect = bool(
            self.failure_code == "participant_disconnect"
            and self.close_reason == "user_initiated"
        )
        await self.emit(
            "call_ended",
            status=(
                "completed"
                if intentional_disconnect or not self.failure_code
                else "failed"
            ),
            reason=self.close_reason,
            failure_code=self.failure_code,
            transcript=self.transcript(),
            tool_outcomes=list(self.tool_outcomes),
            metrics=self.metrics(),
            duration=self.metrics()["duration_seconds"],
        )

    async def _emit_checkpoint(self, event: str) -> None:
        await self.emit(
            event,
            transcript=self.transcript(),
            metrics=self.metrics(),
            duration=self.metrics()["duration_seconds"],
        )

    def _record_response_latency(self) -> None:
        if self._last_user_monotonic is None or self._last_latency_turn >= self._last_user_turn:
            return
        latency_ms = max(0, int((time.monotonic() - self._last_user_monotonic) * 1000))
        self.response_latencies_ms.append(latency_ms)
        self._last_latency_turn = self._last_user_turn

    def _schedule_watchdog(self, user_turn: int) -> None:
        self._cancel_watchdog()

        async def watch() -> None:
            try:
                await asyncio.sleep(self.response_timeout_seconds)
            except asyncio.CancelledError:
                return
            if self._last_agent_turn >= user_turn:
                return
            if self.recovery_attempted:
                self.failure_code = "model_timeout"
                await self.emit(
                    "agent_recovery_failed",
                    failure_code=self.failure_code,
                    metrics=self.metrics(),
                )
                return
            self.recovery_attempted += 1
            await self.emit(
                "agent_recovery_started",
                failure_code="response_timeout",
                customer_text=self._last_user_text,
                metrics=self.metrics(),
            )
            if not self._recovery_callback:
                self.failure_code = "model_timeout"
                await self.emit("agent_recovery_failed", failure_code=self.failure_code, metrics=self.metrics())
                return
            try:
                await self._recovery_callback(self._last_user_text, "response_timeout")
                await asyncio.sleep(self.recovery_timeout_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.failure_code = "model_error"
                await self.emit(
                    "agent_recovery_failed",
                    failure_code=self.failure_code,
                    error=redact_sensitive_text(exc)[:1000],
                    metrics=self.metrics(),
                )
                return
            if self._last_agent_turn < user_turn:
                self.failure_code = "model_timeout"
                await self.emit("agent_recovery_failed", failure_code=self.failure_code, metrics=self.metrics())
                return
            if self.recovery_succeeded < self.recovery_attempted:
                self.recovery_succeeded += 1
                await self.emit("agent_recovery_succeeded", metrics=self.metrics())

        self._watchdog_task = self._spawn(watch())

    def _cancel_watchdog(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _cancel_playout_watchdog(self) -> None:
        task = self._playout_watchdog_task
        self._playout_watchdog_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _cancel_interruption_recovery(self) -> None:
        task = self._interruption_recovery_task
        self._interruption_recovery_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _schedule_interruption_recovery(
        self,
        speech_id: str,
        interrupted_at_turn: int,
        completed_playouts: int,
    ) -> None:
        self._cancel_interruption_recovery()

        async def recover_if_false_interruption() -> None:
            try:
                await asyncio.sleep(self.false_interruption_timeout_seconds)
            except asyncio.CancelledError:
                return
            if (
                self._finished
                or self._last_user_turn != interrupted_at_turn
                or self.completed_playout_count > completed_playouts
            ):
                return
            self.false_interruption_recoveries += 1
            await self.emit(
                "agent_false_interruption_recovery",
                speech_id=speech_id,
                metrics=self.metrics(),
            )
            await self._attempt_recovery("false_interruption")

        self._interruption_recovery_task = self._spawn(recover_if_false_interruption())

    async def _attempt_recovery(self, reason: str) -> None:
        if self._finished or not self._recovery_callback:
            if not self._recovery_callback:
                self.failure_code = "model_timeout"
            return
        if self.recovery_attempted > self.recovery_succeeded:
            return
        self.recovery_attempted += 1
        await self.emit(
            "agent_recovery_started",
            failure_code=reason,
            customer_text=self._last_user_text,
            metrics=self.metrics(),
        )
        try:
            await self._recovery_callback(self._last_user_text, reason)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.failure_code = "model_error"
            await self.emit(
                "agent_recovery_failed",
                failure_code=self.failure_code,
                error=redact_sensitive_text(exc)[:1000],
                metrics=self.metrics(),
            )

    def _mark_recovery_succeeded(self) -> None:
        if self.recovery_attempted <= self.recovery_succeeded:
            return
        self.recovery_succeeded = self.recovery_attempted
        if self.failure_code in {"model_error", "model_timeout"}:
            self.failure_code = ""
        self._spawn(self.emit("agent_recovery_succeeded", metrics=self.metrics()))
