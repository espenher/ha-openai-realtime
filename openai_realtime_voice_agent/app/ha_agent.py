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

second_persona_agent_switch optionally names an input_boolean: the agent is then
used only while it is on, so the second persona can flip between the realtime
model and the agent from a dashboard.
"""

from __future__ import annotations

import logging
import os
import time

import aiohttp
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.persona import CONFIG as PERSONA, SECOND, STATE as PERSONA_STATE

logger = logging.getLogger(__name__)

HA_API = "http://supervisor/core/api"
# Home Assistant keeps an agent conversation for a few minutes of quiet; after
# that a turn starts a new one, which needs the avatar context again.
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

    async def ask(self, text: str, context: str, situation: str = "") -> str:
        """situation: what she's doing right now (also in context); repeated when it changes."""
        new = self._conversation_id is None or time.monotonic() - self._last > CONVERSATION_IDLE_S
        if new and context.strip():
            text = ("(Context for you, not said by the user; don't mention it.)\n" + context.strip()
                    + "\n(End of context.)\n\n" + text)
        elif situation and situation != self._told:
            text = f"(Right now, not said by the user: {situation})\n\n" + text
        self._told = situation
        body = {"text": text, "agent_id": PERSONA.second_agent}
        if not new:
            body["conversation_id"] = self._conversation_id
        t0 = time.monotonic()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            async with http.post(f"{HA_API}/conversation/process", json=body, headers=_headers()) as r:
                if r.status != 200:
                    raise RuntimeError(f"conversation/process → HTTP {r.status}: {(await r.text())[:200]}")
                data = await r.json()
        self._conversation_id = data.get("conversation_id") or self._conversation_id
        self._last = time.monotonic()
        speech = (((data.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech", "")
        logger.info(f"🧠 {PERSONA.second_agent} answered in {self._last - t0:.1f}s"
                    + (" (new conversation)" if new else ""))
        return speech.strip()


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
