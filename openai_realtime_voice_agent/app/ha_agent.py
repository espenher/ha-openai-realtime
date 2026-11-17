"""Let a Home Assistant conversation agent (e.g. a Claude one) answer for the second persona.

Optional (second_persona_agent). While it is in use the realtime session only
listens: OpenAI's VAD decides when the user has finished and its transcription
gives the words, but it creates no replies (create_response off). Each
transcript goes to the agent through Home Assistant's conversation API, so the
agent's own prompt, model settings and device tools apply exactly as they do in
Assist; the reply is then spoken like a realtime text reply would be: [tags] to
the avatar, ElevenLabs voice, phases to the device.

The conversation API takes no extra system prompt, so the avatar context (body,
room, moods and gestures) goes in front of the first message of each agent
conversation, marked as not coming from the user. If what she's doing changes
during a conversation (she sat down by herself, say), the next turn starts with a
line saying so.

Her memory (memory.py) rides along: each new conversation also gets the time,
her notes and her recent diary; every turn goes in the chat log; and when a
conversation has been quiet for CONVERSATION_IDLE_S she's asked, in that same
conversation, for a diary entry before it's over.

second_persona_agent_switch optionally names an input_boolean: the agent is then
used only while it is on, so the second persona can flip between the realtime
model and the agent from a dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app import memory
from app.persona import CONFIG as PERSONA, SECOND, STATE as PERSONA_STATE

logger = logging.getLogger(__name__)

HA_API = "http://supervisor/core/api"
# Home Assistant keeps an agent conversation for 5 minutes of quiet; after this
# long a turn starts a new one (which needs the avatar context again), and the
# old one gets its diary request while Home Assistant still has it.
CONVERSATION_IDLE_S = 240


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}


class AgentMode:
    """Whether the second persona's replies currently come from the HA agent."""

    def __init__(self) -> None:
        self.enabled = False   # decided at each wake (switch state), see refresh()

    @property
    def configured(self) -> bool:
        return bool(PERSONA.second_agent)

    @property
    def active(self) -> bool:
        return self.configured and self.enabled and PERSONA_STATE.active == SECOND

    async def refresh(self) -> bool:
        """Re-read the optional switch; returns whether the agent is to be used."""
        if not self.configured:
            self.enabled = False
        elif not PERSONA.second_agent_switch:
            self.enabled = True
        else:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as http:
                    async with http.get(f"{HA_API}/states/{PERSONA.second_agent_switch}", headers=_headers()) as r:
                        self.enabled = r.status == 200 and (await r.json()).get("state") == "on"
            except Exception as e:
                logger.warning(f"🧠 couldn't read {PERSONA.second_agent_switch}: {e!r} (keeping {self.enabled})")
        return self.enabled


MODE = AgentMode()


class HAConversationAgent:
    """Asks the configured Home Assistant conversation agent, keeping one conversation going."""

    def __init__(self) -> None:
        self._conversation_id: str | None = None
        self._last = 0.0
        self._told = ""   # the situation she was last told about
        self._espen_spoke = False   # this conversation had a turn from him (not only announcements)
        self._ending: asyncio.Task | None = None
        self._lock = asyncio.Lock()   # a turn and the diary request never overlap
        self._zone_known = False

    async def ask(self, text: str, context: str, situation: str = "", kind: str = "speech") -> str:
        """situation: what she's doing right now (also in context); repeated when it changes.
        kind: "speech" (what speech-to-text heard from Espen) or "unprompted" (an
        announcement or a touch she's told to speak up about), for the chat log."""
        await self._learn_time_zone()
        async with self._lock:
            new = self._conversation_id is None or time.monotonic() - self._last > CONVERSATION_IDLE_S
            hidden = ""
            if new:
                full = "\n\n".join(filter(None, [context.strip(), memory.context()]))
                hidden = "(Context for you, not said by the user; don't mention it.)\n" + full + "\n(End of context.)\n\n"
                self._espen_spoke = False
            elif situation and situation != self._told:
                hidden = f"(Right now, not said by the user: {situation})\n\n"
            self._told = situation
            t0 = time.monotonic()
            data = await self._send(hidden + text, None if new else self._conversation_id)
            self._conversation_id = data.get("conversation_id") or self._conversation_id
            self._last = time.monotonic()
            speech = _speech(data)
            logger.info(f"🧠 {PERSONA.second_agent} answered in {self._last - t0:.1f}s"
                        + (" (new conversation)" if new else ""))
            memory.log_turn(self._conversation_id, kind, text, hidden, speech)
            memory.touch_talked()
            self._espen_spoke = self._espen_spoke or kind == "speech"
        if self._ending:
            self._ending.cancel()
        self._ending = asyncio.create_task(self._end_after_quiet(self._conversation_id))
        return speech

    async def _end_after_quiet(self, conversation: str | None) -> None:
        """After CONVERSATION_IDLE_S without a turn, the conversation is over: ask her for
        her diary entry in it (only if Espen took part, not for announcements alone)."""
        await asyncio.sleep(CONVERSATION_IDLE_S)
        async with self._lock:
            if conversation is None or conversation != self._conversation_id:
                return
            self._conversation_id = None   # the next turn starts a new conversation
            if not self._espen_spoke:
                return
            try:
                data = await self._send(memory.DIARY_REQUEST, conversation)
            except Exception as e:
                logger.warning(f"🧠 diary request failed: {e!r}")
                return
            reply = _speech(data)
            memory.log_turn(conversation, "diary", "", memory.DIARY_REQUEST, reply)
            memory.add_diary(reply)

    async def _send(self, text: str, conversation: str | None) -> dict:
        body = {"text": text, "agent_id": PERSONA.second_agent}
        if conversation:
            body["conversation_id"] = conversation
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            async with http.post(f"{HA_API}/conversation/process", json=body, headers=_headers()) as r:
                if r.status != 200:
                    raise RuntimeError(f"conversation/process → HTTP {r.status}: {(await r.text())[:200]}")
                return await r.json()

    async def _learn_time_zone(self) -> None:
        """Her clock follows Home Assistant's time zone (the add-on container runs on UTC)."""
        if self._zone_known:
            return
        self._zone_known = True
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async with http.get(f"{HA_API}/config", headers=_headers()) as r:
                    zone = (await r.json()).get("time_zone") if r.status == 200 else None
            if zone:
                memory.set_time_zone(zone)
        except Exception as e:
            logger.warning(f"🧠 couldn't read Home Assistant's time zone: {e!r}")


def _speech(data: dict) -> str:
    return ((((data.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech", "")).strip()


class AgentReplies(FrameProcessor):
    """Speaks the agent's replies as if the realtime model had written them.

    Sits right after the realtime service. speak() pushes the reply downstream
    as an LLM response (start, text, end), so the [tag] stripper, transcript,
    follow-up check and ElevenLabs voice handle it as usual. While the agent is
    answering, any text reply the realtime model makes anyway is dropped.
    """

    def __init__(self) -> None:
        super().__init__()
        self._let_realtime_reply = False

    def let_realtime_reply(self) -> None:
        """The next realtime reply is wanted (an announcement or the switch-on greeting)."""
        self._let_realtime_reply = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Only frames arriving from the realtime service pass through here;
        # speak() pushes straight downstream.
        if (direction == FrameDirection.DOWNSTREAM and MODE.active
                and isinstance(frame, (LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame))):
            if not self._let_realtime_reply:
                return
            if isinstance(frame, LLMFullResponseEndFrame):
                self._let_realtime_reply = False
        await self.push_frame(frame, direction)

    async def speak(self, text: str) -> None:
        if text:
            for frame in (LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame()):
                await self.push_frame(frame, FrameDirection.DOWNSTREAM)
