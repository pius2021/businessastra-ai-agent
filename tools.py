

import asyncio
import logging
import os
import time
from typing import Optional

from livekit.agents import llm

from db import (
    log_call, log_error,
    
    add_contact_memory, get_contact_memory, compress_contact_memory,
)

logger = logging.getLogger("LLM-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class AppointmentTools:
    """All function tools available to the appointment-booking agent."""

    def __init__(self, phone_number: Optional[str] = None, lead_name: Optional[str] = None,
                 room=None, room_disconnect_fn=None):
        self.phone_number = phone_number
        self.lead_name = lead_name
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self._room = room
        self._room_disconnect_fn = room_disconnect_fn

    def build_tool_list(self, enabled: list) -> list:
        """Return low-latency tool functions filtered by the enabled list."""
        all_methods = [
           
            self.transfer_to_human, 
            self.remember_details,
           
        ]
        if not enabled:
            return [self.transfer_to_human]
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

  
  
    # @llm.function_tool
    # async def end_call(self, outcome: str, reason: str = "") -> str:
    #     """
    #     End the call and log the outcome. Use this when the conversation needs to end.
    #     outcome: 'completed' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
    #     reason: brief description
    #     """
    #     duration = int(time.time() - self._call_start_time)
    #     try:
    #         await log_call(
    #             phone_number=self.phone_number or "unknown",
    #             lead_name=self.lead_name, outcome=outcome, reason=reason,
    #             duration_seconds=duration, recording_url=self.recording_url,
    #             room_name=self._room.name if self._room else None,
    #         )
    #     except Exception as exc:
    #         logger.error("Failed to log call: %s", exc, exc_info=True)
        
    #     # Disconnect the call - try multiple methods
    #     disconnected = False
        
    #     # Method 1: Remove SIP participant
    #     try:
    #         if self._room and self.phone_number:
    #             sip_participant_id = f"sip_{self.phone_number}"
    #             from livekit import api as lk_api
    #             from config import LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
                
    #             lk = lk_api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    #             try:
    #                 await lk.room.remove_participant(
    #                     lk_api.RoomParticipantIdentity(room=self._room.name, identity=sip_participant_id)
    #                 )
    #                 logger.info("SIP participant removed (BYE sent) for %s", self.phone_number)
    #                 disconnected = True
    #             finally:
    #                 await lk.aclose()
    #     except Exception as exc:
    #         logger.warning("Failed to disconnect SIP participant: %s", exc)
        
    #     # Method 2: Disconnect room if SIP participant removal failed
    #     if not disconnected and self._room:
    #         try:
    #             await self._room.disconnect()
    #             logger.info("Room disconnected as fallback")
    #             disconnected = True
    #         except Exception as exc:
    #             logger.warning("Failed to disconnect room: %s", exc)
        
    #     # Method 3: Use room disconnect function if available
    #     if not disconnected and self._room_disconnect_fn:
    #         try:
    #             await self._room_disconnect_fn()
    #             logger.info("Room disconnect function called")
    #             disconnected = True
    #         except Exception as exc:
    #             logger.warning("Failed to call room disconnect function: %s", exc)
        
    #     return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Call when lead requests a human, is angry, or has a complex issue.
        reason: why you're transferring
        """
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        return f"Transferring to human agent. Reason: {reason}"

   

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        insight: the detail to remember
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        """Compress old memories using LLM summarisation (best-effort)."""
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            # Simple compression: keep last 3 and summarise the rest
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            compressed = f"Summary of {len(memories)} notes:\n{bullet_list}"
            await compress_contact_memory(self.phone_number, compressed)
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

