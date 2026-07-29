from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatMessage, function_tool
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import google, silero

from session_runtime import VoiceSessionRuntime


load_dotenv(os.getenv("SHIPKIA_ENV_FILE", ".env.local"), override=False)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("shipkia-livekit-agent")

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "shipkia-voice-sales")
MCP_GATEWAY = os.getenv("MCP_SERVER_URL", "").strip()
CONFLUENCE_CALLBACK = os.getenv("CONFLUENCE_LIVEKIT_WEBHOOK_URL", "").strip()
CONSOLE_MCP_SCOPE = "livekit-console-sandbox"

ALLOWED_TOOLS = {
    "lookup_shipkia_crm_lead",
    "create_or_update_shipkia_lead",
    "record_shipkia_call_progress",
    "create_shipkia_followup",
    "finalize_shipkia_call_outcome",
    "lookup_pincode_serviceability",
    "calculate_shipkia_rate",
}

_TOOL_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}


def load_local_console_prompt() -> tuple[str, str]:
    """Load the current versioned prompt for direct LiveKit Console sessions."""
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "confluence_ai"
        / "prompts"
        / "shipkia_voice.py"
    )
    spec = importlib.util.spec_from_file_location("shipkia_voice_console_prompt", prompt_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load the local ShipKia prompt from {prompt_path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompt_version = str(module.SHIPKIA_VOICE_PROMPT_VERSION)
    return str(module.get_shipkia_voice_prompt(prompt_version)), prompt_version


def build_headers(task_id: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if token:
        headers[os.getenv("MCP_AUTH_HEADER", "X-MCP-Token")] = token
    if task_id:
        headers["X-Confluence-Task-ID"] = task_id
    return headers


def unwrap_frappe_response(payload: dict) -> dict:
    message = payload.get("message")
    return message if isinstance(message, dict) else payload


def parse_dispatch_metadata(ctx: JobContext) -> dict[str, Any]:
    candidates = (
        getattr(ctx.room, "metadata", None),
        getattr(getattr(ctx.job, "room", None), "metadata", None),
        getattr(ctx.job, "metadata", None),
    )
    for raw in candidates:
        if isinstance(raw, dict):
            return raw
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def compact_json(value: Any, max_chars: int = 3500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "...TRUNCATED"


def make_mcp_forwarder(tool_name: str, task_id: str, runtime: VoiceSessionRuntime | None = None):
    async def forwarder(raw_arguments: dict[str, object]) -> str:
        arguments = dict(raw_arguments or {})
        key = (task_id, tool_name, json.dumps(arguments, sort_keys=True, default=str))
        now = time.monotonic()
        cached = _TOOL_CACHE.get(key)
        if cached and now - cached[0] < 15:
            return cached[1]

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        failed = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MCP_GATEWAY,
                    json=payload,
                    headers=build_headers(task_id),
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        failed = True
                        logger.error("MCP %s failed HTTP %s: %s", tool_name, response.status, body[:500])
                        return compact_json(
                            {
                                "status": "error",
                                "message": "The ShipKia system could not save or retrieve this information.",
                            }
                        )
                    parsed = unwrap_frappe_response(json.loads(body))
                    if parsed.get("error"):
                        failed = True
                        result = {
                            "status": "error",
                            "message": parsed["error"].get("message", "ShipKia tool failed."),
                        }
                    else:
                        result = parsed.get("result", {})
                    text = compact_json(result)
                    _TOOL_CACHE[key] = (now, text)
                    return text
        except Exception as exc:
            failed = True
            logger.exception("MCP %s connection failed: %s", tool_name, exc)
            return compact_json(
                {
                    "status": "error",
                    "message": "The ShipKia system is temporarily unavailable. Do not repeat the question solely because saving failed.",
                }
            )
        finally:
            if runtime:
                runtime.record_tool_latency(tool_name, time.monotonic() - now, failed=failed)

    return forwarder


async def fetch_tools(
    task_id: str | None,
    system_prompt: str,
    runtime: VoiceSessionRuntime | None = None,
) -> list:
    if not task_id or not MCP_GATEWAY:
        logger.warning("MCP tools unavailable: task=%s gateway_configured=%s", task_id, bool(MCP_GATEWAY))
        return []

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"task_id": task_id},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                MCP_GATEWAY,
                json=payload,
                headers=build_headers(task_id),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.error("MCP tools/list failed HTTP %s: %s", response.status, body[:500])
                    return []
                parsed = unwrap_frappe_response(json.loads(body))
    except Exception as exc:
        logger.exception("MCP tools/list connection failed: %s", exc)
        return []

    tools = []
    for definition in parsed.get("result", {}).get("tools", []):
        tool_name = str(definition.get("name") or "")
        if tool_name not in ALLOWED_TOOLS:
            logger.warning("Ignoring non-ShipKia tool returned by Confluence: %s", tool_name)
            continue
        if tool_name not in system_prompt:
            logger.warning("Ignoring ShipKia tool not present in the active prompt: %s", tool_name)
            continue
        raw_schema = {
            "name": tool_name,
            "description": definition.get("description", ""),
            "parameters": definition.get(
                "inputSchema",
                {"type": "object", "properties": {}, "required": []},
            ),
        }
        tools.append(function_tool(make_mcp_forwarder(tool_name, task_id, runtime), raw_schema=raw_schema))
        logger.info("Registered ShipKia tool: %s", tool_name)
    return tools


class ShipKiaAssistant(Agent):
    def __init__(
        self,
        *,
        system_prompt: str,
        personality: str,
        context: dict,
        tools: list,
        runtime: VoiceSessionRuntime,
    ) -> None:
        if not system_prompt.strip():
            raise RuntimeError("Confluence did not provide the ShipKia system prompt.")

        instructions = system_prompt.strip()
        if personality.strip():
            instructions += f"\n\n## Voice personality\n{personality.strip()}"

        context_lines = []
        for key, value in context.items():
            if value in (None, "", [], {}):
                continue
            rendered = compact_json(value, max_chars=1000) if isinstance(value, (dict, list)) else str(value)
            context_lines.append(f"- {key}: {rendered}")
        if context_lines:
            instructions += "\n\n## Call context\n" + "\n".join(context_lines)

        instructions += """

## Voice runtime rules
- Follow the Confluence ShipKia prompt and known context exactly.
- Never speak tool names, field names, JSON, metadata, record IDs, or implementation details.
- Speak naturally in short turns and ask only one useful question at a time.
- Finish each spoken sentence and complete the current thought before going silent.
- Remember every clear answer in this call and never ask it again unless the customer corrects it.
- Use tools only for confirmed information. Never guess a CRM value, serviceability result, zone, or rate.
- If saving fails, acknowledge internally and continue naturally; do not repeatedly ask the customer for the same answer.
- Never send a message or invoke a messaging channel from this voice worker.
"""
        self._runtime = runtime
        super().__init__(instructions=instructions, tools=tools)

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        memory = self._runtime.same_call_context(
            current_user_text=new_message.text_content or "",
        )
        if not memory:
            return
        turn_ctx.add_message(
            role="system",
            content=(
                "Same-call memory from the current room follows. Treat confirmed customer answers "
                "as already known, do not ask them again, and use corrections from later turns. "
                "This memory is only for the current call:\n"
                f"{memory}"
            ),
        )
        self._runtime.record_memory_injection(memory)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.2")),
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.35")),
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.55")),
    )


async def post_confluence_event(task_id: str | None, room_name: str, event: str, **extra: Any) -> None:
    if not task_id or not CONFLUENCE_CALLBACK:
        return
    payload = {
        "task": task_id,
        "room_name": room_name,
        "event": event,
        "status": extra.pop("status", event),
        **extra,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CONFLUENCE_CALLBACK,
                json=payload,
                headers=build_headers(task_id),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    logger.warning("Confluence callback %s failed HTTP %s", event, response.status)
    except Exception as exc:
        logger.warning("Confluence callback %s failed: %s", event, exc)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    metadata = parse_dispatch_metadata(ctx)
    task_id = metadata.get("task")
    system_prompt = str(metadata.get("system_prompt") or "")
    personality = str(metadata.get("personality") or "")
    context = metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
    prompt_version = str(metadata.get("prompt_version") or context.get("prompt_version") or "legacy")
    if not system_prompt.strip() and not task_id:
        system_prompt, prompt_version = load_local_console_prompt()
        context = {
            **context,
            "local_livekit_console": 1,
            "prompt_version": prompt_version,
        }
        logger.warning(
            "Confluence dispatch metadata is absent; using local console prompt version=%s "
            "with the read-only Console MCP scope.",
            prompt_version,
        )

    async def emit_runtime_event(event: str, **extra: Any) -> None:
        if not task_id:
            return
        await post_confluence_event(task_id, ctx.room.name, event, prompt_version=prompt_version, **extra)

    runtime = VoiceSessionRuntime(
        emit=emit_runtime_event,
        response_timeout_seconds=float(os.getenv("LIVEKIT_RESPONSE_TIMEOUT_SECONDS", "15")),
        playout_timeout_seconds=float(os.getenv("LIVEKIT_PLAYOUT_TIMEOUT_SECONDS", "30")),
        recovery_timeout_seconds=float(os.getenv("LIVEKIT_RECOVERY_TIMEOUT_SECONDS", "8")),
        reconnect_grace_seconds=float(os.getenv("LIVEKIT_RECONNECT_GRACE_SECONDS", "20")),
        false_interruption_timeout_seconds=float(
            os.getenv("LIVEKIT_FALSE_INTERRUPTION_RECOVERY_SECONDS", "2.5")
        ),
    )
    tools = await fetch_tools(task_id or CONSOLE_MCP_SCOPE, system_prompt, runtime)
    if not tools:
        failure_message = (
            "ShipKia MCP tools are unavailable. Ensure the Frappe bench is responding on port "
            "8000 before starting a LiveKit call."
        )
        logger.error("%s room=%s task=%s", failure_message, ctx.room.name, task_id)
        await emit_runtime_event(
            "agent_startup_failed",
            status="failed",
            failure_code="mcp_unavailable",
            error=failure_message,
        )
        raise RuntimeError(failure_message)

    model_name = os.getenv(
        "GEMINI_LIVE_MODEL",
        "gemini-live-2.5-flash-native-audio",
    )
    voice = os.getenv("GEMINI_LIVE_VOICE", metadata.get("audio_name") or "Puck")
    realtime_input_config = genai_types.RealtimeInputConfig(
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=int(os.getenv("GEMINI_VAD_PREFIX_PADDING_MS", "300")),
            silence_duration_ms=int(os.getenv("GEMINI_VAD_SILENCE_DURATION_MS", "700")),
        )
    )
    model = google.realtime.RealtimeModel(
        model=model_name,
        voice=voice,
        vertexai=True,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.35")),
        realtime_input_config=realtime_input_config,
    )
    logger.info(
        "Starting ShipKia session room=%s task=%s model=%s voice=%s tools=%s",
        ctx.room.name,
        task_id,
        model_name,
        voice,
        len(tools),
    )

    session = AgentSession(
        llm=model,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            turn_detection="realtime_llm",
            interruption={
                "enabled": True,
                "min_duration": float(
                    os.getenv("LIVEKIT_INTERRUPTION_MIN_DURATION_SECONDS", "0.7")
                ),
                "resume_false_interruption": True,
                "false_interruption_timeout": float(
                    os.getenv("LIVEKIT_FALSE_INTERRUPTION_TIMEOUT_SECONDS", "2.0")
                ),
                "discard_audio_if_uninterruptible": True,
            },
        ),
    )
    assistant = ShipKiaAssistant(
        system_prompt=system_prompt,
        personality=personality,
        context=context,
        tools=tools,
        runtime=runtime,
    )

    call_done = asyncio.Event()
    participant_seen = bool(ctx.room.remote_participants)
    close_reason = "maximum_call_duration"

    @ctx.room.on("participant_connected")
    def _participant_connected(participant):
        nonlocal participant_seen
        participant_seen = True
        runtime.participant_connected()
        logger.info("Customer joined room: %s", participant.identity)

    @ctx.room.on("participant_disconnected")
    def _participant_disconnected(participant):
        logger.info("Customer left room: %s", participant.identity)
        if participant_seen and not ctx.room.remote_participants:
            async def end_after_grace() -> None:
                nonlocal close_reason
                close_reason = "participant_disconnect_timeout"
                call_done.set()

            runtime.participant_disconnected(end_after_grace)

    @session.on("close")
    def _session_closed(event):
        nonlocal close_reason
        reason = getattr(event, "reason", "unknown")
        close_reason = str(getattr(reason, "value", reason))
        logger.info("Session closed: %s", close_reason)
        call_done.set()

    @session.on("user_input_transcribed")
    def _user_input_transcribed(event):
        if getattr(event, "is_final", False):
            runtime.add_user_turn(
                getattr(event, "transcript", ""),
                turn_id=f"user:{getattr(event, 'created_at', time.time())}",
            )

    @session.on("conversation_item_added")
    def _conversation_item_added(event):
        item = getattr(event, "item", None)
        if not isinstance(item, ChatMessage) or str(item.role) != "assistant":
            return
        runtime.add_agent_turn(item.text_content or "", turn_id=getattr(item, "id", None))

    @session.on("speech_created")
    def _speech_created(event):
        handle = getattr(event, "speech_handle", None)
        if handle is None:
            return
        speech_id = str(getattr(handle, "id", ""))
        source = str(getattr(event, "source", "generate_reply"))
        runtime.track_agent_speech(speech_id, source=source)
        logger.info("Agent speech created id=%s source=%s", speech_id, source)

        def speech_done(completed_handle) -> None:
            interrupted = bool(getattr(completed_handle, "interrupted", False))
            runtime.complete_agent_playout(
                getattr(completed_handle, "id", speech_id),
                interrupted=interrupted,
            )
            logger.info(
                "Agent speech finished id=%s interrupted=%s",
                getattr(completed_handle, "id", speech_id),
                interrupted,
            )

        handle.add_done_callback(speech_done)

    @session.on("agent_state_changed")
    def _agent_state_changed(event):
        if getattr(event, "new_state", "") == "speaking":
            runtime.mark_agent_speaking()
        asyncio.create_task(
            emit_runtime_event(
                "agent_state",
                old_state=str(getattr(event, "old_state", "")),
                new_state=str(getattr(event, "new_state", "")),
                metrics=runtime.metrics(),
            )
        )

    @session.on("error")
    def _session_error(event):
        runtime.record_error(getattr(event, "error", event))

    async def recover_from_silence(customer_text: str, reason: str) -> None:
        memory = runtime.same_call_context(current_user_text=customer_text)
        if reason == "false_interruption":
            recovery_direction = (
                "The previous speech was cut off by brief microphone activity, but the customer did "
                "not begin a real new turn. Continue and finish the interrupted thought naturally. "
                "Do not greet again, restart the whole answer, or apologize."
            )
        else:
            recovery_direction = (
                "The customer is still connected and the previous response stalled. Apologize once "
                "for the short delay, answer their latest request in one short natural turn, and "
                "continue the ShipKia conversation."
            )
        reply = session.generate_reply(
            instructions=(
                f"{recovery_direction}\n"
                f"Latest customer words: {customer_text or '[no new customer words]'}\n"
                f"Same-call memory:\n{memory or '[no earlier turns]'}"
            )
        )
        if hasattr(reply, "wait_for_playout"):
            await reply.wait_for_playout()
        else:
            await reply

    runtime.set_recovery_callback(recover_from_silence)

    async def shutdown_callback() -> None:
        await runtime.finish(close_reason)

    ctx.add_shutdown_callback(shutdown_callback)
    await session.start(
        agent=assistant,
        room=ctx.room,
        room_options=RoomOptions(
            audio_input=True,
            video_input=False,
            audio_output=True,
            text_output=True,
            close_on_disconnect=False,
        ),
        record=False,
    )
    await post_confluence_event(task_id, ctx.room.name, "agent_started", status="running")

    if not call_done.is_set():
        try:
            reply = session.generate_reply(
                instructions=(
                    "The ShipKia call has connected. Produce exactly one short greeting, then one "
                    "relevant first question. Follow the configured adaptive-language rules: reply "
                    "in English to English speech and natural Hinglish to Hindi or Hinglish speech. "
                    "Never repeat the greeting or answer in a second language. Use known customer "
                    "context and do not mention tools or internal systems."
                )
            )
            if hasattr(reply, "wait_for_playout"):
                await reply.wait_for_playout()
            else:
                await reply
        except RuntimeError:
            # A short-lived browser probe can leave while the session is
            # starting. Treat that as a normal disconnected call.
            if not call_done.is_set():
                raise

    try:
        await asyncio.wait_for(
            call_done.wait(),
            timeout=int(os.getenv("LIVEKIT_AGENT_MAX_CALL_SECONDS", "900")),
        )
    except asyncio.TimeoutError:
        logger.info("Ending ShipKia session after maximum call duration.")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,
            # Python's forkserver cannot reliably inherit its Unix socket
            # descriptors when the worker is launched through wsl.exe.
            multiprocessing_context="spawn",
        )
    )
