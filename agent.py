# agent.py — Voice Agent (Odia + Outbound appointment booking)
from __future__ import annotations

import asyncio
import json
import logging
import os
import aiohttp
from datetime import datetime

from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import openai, sarvam, silero

from config import (
    GROQ_API_KEY, GROQ_MODEL, LIVEKIT_AGENT_NAME,
    LLM_PROVIDER, SARVAM_LLM_MODEL, ensure_env,
    VAD_MIN_SILENCE_DURATION, VAD_MIN_ENDPOINTING_DELAY,
    LIVEKIT_URL,
    SARVAM_STT_MODEL, SARVAM_TTS_MODEL, SARVAM_LANGUAGE_CODE,
)
from prompts import build_prompt

ensure_env("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "SARVAM_API_KEY")

from logger import setup_logging

setup_logging()
logger = logging.getLogger("voice-agent")

# ---------------------------------------------------------------------------
# Module-level Odia digit map (DRY — used in multiple functions)
# ---------------------------------------------------------------------------
ODIA_DIGITS = {
    "0": "୦", "1": "୧", "2": "୨", "3": "୩", "4": "୪",
    "5": "୫", "6": "୬", "7": "୭", "8": "୮", "9": "୯",
}

MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March",
    "04": "April",   "05": "May",      "06": "June",
    "07": "July",    "08": "August",   "09": "September",
    "10": "October", "11": "November", "12": "December",
}


def convert_to_odia_date(date_str) -> str:
    if not date_str:
        return "N/A"
    value = str(date_str).strip()
    if value.isdigit():
        return to_odia_number(value)
    parts = None
    if "-" in value:
        parts = value.split("-")
    elif "/" in value:
        parts = value.split("/")
    if not parts or len(parts) != 3:
        logger.debug("Non-date value passed to convert_to_odia_date: %r", value)
        return value
    day, month, year = parts[0], parts[1], parts[2]
    odia_day  = "".join(ODIA_DIGITS.get(d, d) for d in day)
    odia_year = "".join(ODIA_DIGITS.get(d, d) for d in year)
    month_name = MONTH_NAMES.get(month, month)
    return f"{odia_day} {month_name} {odia_year}"


def to_odia_number(value) -> str:
    return "".join(ODIA_DIGITS.get(ch, ch) for ch in str(value))


# ---------------------------------------------------------------------------
# LLM builder with groq → sarvam fallback
# ---------------------------------------------------------------------------

def _build_llm_config(provider: str, model: str, api_key: str = None, base_url: str = None) -> dict:
    """Build LLM configuration dictionary."""
    config = {"model": model, "temperature": 0.2}
    if api_key:
        config["api_key"] = api_key
    if base_url:
        config["base_url"] = base_url
    return config

def _build_groq_llm():
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
    return openai.LLM(
        **_build_llm_config(
            "groq", GROQ_MODEL, GROQ_API_KEY, "https://api.groq.com/openai/v1"
        ),
        parallel_tool_calls=True,
    )

def _build_sarvam_llm():
    return sarvam.LLM(model=SARVAM_LLM_MODEL, temperature=0.7)

def build_llm():
    """Build LLM with fallback support."""
    if LLM_PROVIDER == "groq":
        try:
            primary = _build_groq_llm()
            try:
                from livekit.agents.llm import FallbackAdapter
                fallback = _build_sarvam_llm()
                logger.info("LLM: groq (primary) → sarvam (fallback)")
                return FallbackAdapter([primary, fallback])
            except ImportError:
                logger.info("LLM: groq (primary), sarvam fallback not available")
                return primary
        except RuntimeError:
            logger.warning("Groq unavailable, falling back to Sarvam LLM")
            return _build_sarvam_llm()
    elif LLM_PROVIDER == "sarvam":
        logger.info("LLM: sarvam (primary)")
        return _build_sarvam_llm()
    elif LLM_PROVIDER == "custom":
        from config import CUSTOM_LLM_BASE_URL, CUSTOM_LLM_API_KEY, CUSTOM_LLM_MODEL
        logger.info("LLM: custom provider (model=%s, base_url=%s)", CUSTOM_LLM_MODEL, CUSTOM_LLM_BASE_URL)
        return openai.LLM(
            **_build_llm_config("custom", CUSTOM_LLM_MODEL, CUSTOM_LLM_API_KEY, CUSTOM_LLM_BASE_URL)
        )
    else:
        raise RuntimeError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER!r}")


class OutboundAssistant(Agent):
    def __init__(
        self,
        instructions: str,
        lead_name: str = "there",
        tool_ctx=None,
        room_name: str | None = None,
        voice: str | None = None,
        language_code: str | None = None,
        tts_model: str | None = None,
    ) -> None:
        self._lead_name = lead_name
        self._tool_ctx = tool_ctx
        self._max_function_calls_reached = False
        self._room_name = room_name
        super().__init__(
            instructions=instructions,
            stt=sarvam.STT(
                model=SARVAM_STT_MODEL,
                language=language_code or SARVAM_LANGUAGE_CODE,
                mode="transcribe",
                flush_signal=True,
            ),
            llm=build_llm(),
            tts=sarvam.TTS(
                model=tts_model or SARVAM_TTS_MODEL,
                target_language_code=language_code or SARVAM_LANGUAGE_CODE,
                speaker=voice or "ritu",
            ),
            vad=silero.VAD.load(min_silence_duration=VAD_MIN_SILENCE_DURATION),
            tools=[],
        )
        
        # Make tools available as methods on this agent instance
        if tool_ctx:
            for tool_method in tool_ctx.build_tool_list([]):
                # Bind the tool method to this agent instance
                setattr(self, tool_method.__name__, tool_method)

    async def on_enter(self) -> None:
        """Speak first greeting when call connects."""
        # Store room name for monitoring
        session_room = getattr(getattr(self, "session", None), "room", None)
        if not self._room_name and session_room:
            self._room_name = session_room.name
            logger.info("Room name set for monitoring: %s", self._room_name)
        elif not self._room_name:
            logger.warning("Could not set room name - session or room not available")
        
        greeting = f"ନମସ୍କାର, ମୁଁ {self._lead_name}ଙ୍କ ସହ କଥା ହେଉଛି କି?"
        await self.session.say(greeting, add_to_chat_ctx=False)
        
        # Send greeting to monitoring
        if self._room_name:
            self._emit_conversation_event("AI", greeting, "Call Started")
        else:
            logger.warning("Cannot send conversation event - room name not set")

        
    async def on_error(self, error: Exception) -> None:
        """Handle errors including max function calls reached."""
        if "maximum number of function calls steps reached" in str(error):
            logger.warning("Max function calls reached, ending call gracefully")
            self._max_function_calls_reached = True
            end_call = getattr(self._tool_ctx, "end_call", None) if self._tool_ctx else None
            if end_call:
                await end_call("completed", "Maximum function steps reached")
        else:
            logger.exception("Outbound assistant error: %s", error)

    def _emit_conversation_event(self, speaker: str, text: str, status: str = None) -> None:
        """Publish monitoring data without delaying the speech pipeline."""
        if not self._room_name:
            return
        task = asyncio.create_task(send_conversation_event(self._room_name, speaker, text, status))
        task.add_done_callback(_log_background_task_error)


def _log_background_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception as exc:
        logger.warning("Conversation event task failed: %s", exc)


async def _dial_sip_number(ctx: JobContext, phone_number: str, trunk_id: str, timeout: int = 30) -> None:
    """Dial a phone number via SIP trunk."""
    from livekit import api as lk_api
    logger.info("Dialing %s via SIP trunk %s", phone_number, trunk_id)
    try:
        await ctx.api.sip.create_sip_participant(
            lk_api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                participant_identity=f"sip_{phone_number}",
                wait_until_answered=True,
            )
        )
        logger.info("Call ANSWERED — %s picked up", phone_number)
    except Exception as exc:
        logger.error(
            "SIP dial FAILED for %s: %s | "
            "Check: (1) trunk '%s' exists in LiveKit dashboard, "
            "(2) Vobiz SIP credentials are correct, "
            "(3) the outbound number has permission to dial.",
            phone_number, exc, trunk_id, exc_info=True,
        )
        ctx.shutdown()
        raise


async def _wait_for_participant_safely(ctx: JobContext) -> None:
    """Wait for participant with error handling."""
    try:
        await ctx.wait_for_participant()
    except RuntimeError:
        return


# ---------------------------------------------------------------------------
# Call Monitoring Helper Functions
# ---------------------------------------------------------------------------

async def send_conversation_event(room_name: str, speaker: str, text: str, status: str = None) -> None:
    """Send conversation event to WebSocket monitoring system."""
    try:
        backend_url = os.getenv("CALL_MONITOR_URL", "http://localhost:8000")
        event_url = f"{backend_url}/api/call-monitor/{room_name}/event"
        
        payload = {
            "speaker": speaker,
            "text": text,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if status:
            payload["status"] = status
            
        logger.debug("Sending conversation event: speaker=%s room=%s text_len=%d", speaker, room_name, len(text))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(event_url, json=payload, timeout=aiohttp.ClientTimeout(total=1.5)) as resp:
                if resp.status == 200:
                    logger.info("Conversation event sent successfully: speaker=%s room=%s", speaker, room_name)
                else:
                    logger.warning("Conversation event POST returned %d for room=%s", resp.status, room_name)
                    response_text = await resp.text()
                    logger.warning("Response: %s", response_text)
    except Exception as e:
        logger.error("Failed to send conversation event: %s", e, exc_info=True)


async def _notify_participant_disconnect(room_name: str, phone_number: str = None) -> None:
    """Notify monitoring system that a participant has disconnected."""
    try:
        backend_url = os.getenv("CALL_MONITOR_URL", "http://localhost:8000")
        disconnect_url = f"{backend_url}/api/call-monitor/{room_name}/participant-disconnect"
        
        payload = {
            "room_name": room_name,
            "phone_number": phone_number,
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        logger.info("Notifying participant disconnect: room=%s phone=%s", room_name, phone_number)
        
        async with aiohttp.ClientSession() as session:
            async with session.post(disconnect_url, json=payload, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    logger.info("Participant disconnect notified successfully: room=%s", room_name)
                else:
                    logger.warning("Participant disconnect notify returned %d for room=%s", resp.status, room_name)
    except Exception as e:
        logger.debug("Failed to notify participant disconnect (non-critical): %s", e)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

async def _safe_post_call_log(customer_name: str, room_name: str, outcome: str, duration_seconds: int) -> None:
    """Safely post call log with error handling."""
    backend_url = os.getenv("CALL_LOG_URL", "http://localhost:8000/api/call-log")
    payload = {
        "room_name": room_name,
        "customer_name": customer_name,
        "outcome": outcome,
        "duration_seconds": duration_seconds,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(backend_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    logger.info("Call log posted: customer=%s outcome=%s", customer_name, outcome)
                else:
                    logger.warning("Call log POST returned %d", resp.status)
    except Exception as e:
        logger.warning("Failed to post call log (non-critical): %s", e)


def _create_agent_session(tools=None) -> AgentSession:
    """Create a standardized agent session configuration."""
    return AgentSession(
        turn_handling={
            "endpointing": {"min_delay": VAD_MIN_ENDPOINTING_DELAY},
            "preemptive_generation": {
                "enabled": True,
                "preemptive_tts": True,
            },
        },
        tools=tools or [],
    )


async def _wait_for_disconnect(ctx: JobContext, phone_number: str = None, timeout: int = 3600) -> None:
    """Wait for participant or room disconnect with timeout."""
    done = asyncio.Event()
    
    if phone_number:
        sip_identity = f"sip_{phone_number}"
        
        @ctx.room.on("participant_disconnected")
        def on_participant_disconnected(participant):
            if participant.identity == sip_identity:
                logger.info("Participant disconnected: %s from room %s", sip_identity, ctx.room.name)
                # Notify monitoring system that participant has disconnected
                asyncio.create_task(_notify_participant_disconnect(ctx.room.name, phone_number))
                done.set()
    
    @ctx.room.on("disconnected")
    def on_disconnected():
        done.set()
    
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Call reached %d second safety timeout", timeout)


# ---------------------------------------------------------------------------
# Call log helper (deprecated - use _safe_post_call_log)
# ---------------------------------------------------------------------------

async def _post_call_log(
    customer_name: str, room_name: str, outcome: str, duration_seconds: int,
) -> None:
    """Legacy function - use _safe_post_call_log instead."""
    await _safe_post_call_log(customer_name, room_name, outcome, duration_seconds)


# ---------------------------------------------------------------------------
# Entrypoint — routes to Odia or Outbound agent based on metadata
# ---------------------------------------------------------------------------

async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("Connected to room: %s", ctx.room.name)

    raw_metadata = ctx.room.metadata or ""
    job_metadata = ctx.job.metadata if hasattr(ctx.job, "metadata") and ctx.job.metadata else ""



    outbound_data = None
    if job_metadata:
        try:
            data = json.loads(job_metadata)
            if "phone_number" in data:
                outbound_data = data
        except (json.JSONDecodeError, AttributeError):
            pass

    if outbound_data:
        from db import init_pool
        try:
            await init_pool()
        except Exception as e:
            logger.warning("Could not initialize DB pool in agent: %s", e)
        await _handle_outbound_call(ctx, outbound_data)
    else:
        await _handle_odia_call(ctx, raw_metadata)


async def _handle_odia_call(ctx: JobContext, raw_metadata: str) -> None:
    """Handle Odia loan collection call (existing flow)."""
    try:
        participant = await ctx.wait_for_participant()
    except RuntimeError as e:
        logger.warning("Could not wait for participant: %s", e)
        return

    logger.info("Participant joined: %s", participant.identity)

    if not raw_metadata:
        logger.error("No room metadata — cannot start personalised session")
        return

    try:
        customer = json.loads(raw_metadata)
    except json.JSONDecodeError:
        logger.error("Room metadata is not valid JSON: %s", raw_metadata)
        return

    logger.info("Loaded customer: %s", customer.get("Customer Name"))

    # TODO: Implement OdiaVoiceAgent class or use OutboundAssistant with different config
    logger.error("OdiaVoiceAgent not implemented - this functionality is broken")
    return
    
    # agent = OdiaVoiceAgent(customer=customer)  # This class doesn't exist
    # session = _create_agent_session()
    # 
    # try:
    #     await session.start(agent=agent, room=ctx.room)
    # except Exception as exc:
    #     logger.exception("Session failed: %s", exc)
    #     await _safe_post_call_log(
    #         customer_name=customer.get("Customer Name", "unknown"),
    #         room_name=ctx.room.name,
    #         outcome="failed",
    #         duration_seconds=agent.get_call_duration(),
    #     )
    #     return
    # 
    # logger.info(
    #     "Session ended: customer=%s duration=%ds",
    #     customer.get("Customer Name"),
    #     agent.get_call_duration(),
    # )


async def _handle_outbound_call(ctx: JobContext, data: dict) -> None:
    """Handle outbound appointment-booking call (new spec flow)."""
    from tools import AppointmentTools
    from db import get_enabled_tools

    phone_number = data.get("phone_number")
    lead_name = data.get("lead_name", "there")
    customer_context = data.get("customer_context", "")
    custom_prompt = data.get("system_prompt")

    logger.info("Outbound call: phone=%s lead=%s context_len=%d", phone_number, lead_name, len(customer_context))

    system_prompt = build_prompt(
        lead_name=lead_name,
        customer_context=customer_context,
        custom_prompt=custom_prompt,
    )

    tool_ctx = AppointmentTools(
        phone_number=phone_number, lead_name=lead_name,
        room=ctx.room,
    )

    # Resolve enabled tools
    tools_override = data.get("tools_override")
    if tools_override:
        try:
            enabled_tools = json.loads(tools_override) if isinstance(tools_override, str) else tools_override
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    active_tools = tool_ctx.build_tool_list(enabled_tools)
    logger.info("Tools loaded: %s", [t.__name__ for t in active_tools])

    # ── Dial via SIP (if trunk configured) ─────────────────────────────
    if phone_number:
        from db import get_setting
        trunk_id = await get_setting("OUTBOUND_TRUNK_ID", os.getenv("OUTBOUND_TRUNK_ID", ""))
        call_timeout = int(os.getenv("SIP_CALL_TIMEOUT", "30"))

        # Validate trunk ID format — LiveKit SIP trunk IDs must start with "ST_"
        if trunk_id and not trunk_id.startswith("ST_"):
            logger.error(
                "OUTBOUND_TRUNK_ID '%s' is INVALID. LiveKit SIP trunk IDs must start with 'ST_'. "
                "Go to Settings → Vobiz SIP Telephony → click 'Create SIP Trunk' to generate a valid one, "
                "or copy the correct ID from your LiveKit Cloud dashboard under SIP → Outbound Trunks.",
                trunk_id,
            )
            trunk_id = ""  # treat as unconfigured so we fall through gracefully

        if trunk_id:
            await _dial_sip_number(ctx, phone_number, trunk_id, call_timeout)
        else:
            logger.warning(
                "No valid OUTBOUND_TRUNK_ID configured — skipping SIP dial, waiting for participant. "
                "Fix: Go to Settings → Vobiz SIP Telephony → click 'Create SIP Trunk'."
            )
            await _wait_for_participant_safely(ctx)

    agent = OutboundAssistant(
        instructions=system_prompt,
        lead_name=lead_name,
        tool_ctx=tool_ctx,
        room_name=ctx.room.name,
        voice=data.get("voice_override"),
        language_code=data.get("language_code"),
        tts_model=data.get("voice_model_override"),
    )

    session = _create_agent_session(active_tools)

    @session.on("conversation_item_added")
    def on_conversation_item_added(ev):
        if not hasattr(ev, "item"): return
        item = ev.item
        if hasattr(item, "role") and hasattr(item, "content"):
            content = item.content
            # Note: in livekit 1.5.7, content might be a string or a list of elements
            # We'll just extract the string representation if it's text
            text = content if isinstance(content, str) else str(content)
            if text and text.strip() and agent._room_name:
                if item.role == "user":
                    agent._emit_conversation_event("User", text.strip())
                elif item.role == "assistant":
                    agent._emit_conversation_event("AI", text.strip())
    await session.start(agent=agent, room=ctx.room)
    logger.info("Outbound agent session started for %s", phone_number)

    # ── Keep session alive until participant disconnects ───────────────────
    await _wait_for_disconnect(ctx, phone_number)
    logger.info("Session ended for %s", phone_number)
    await session.aclose()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=LIVEKIT_AGENT_NAME,
        )
    )
