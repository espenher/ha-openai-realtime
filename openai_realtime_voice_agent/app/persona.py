"""Two personas on one device: the main one, and an optional second one voiced by ElevenLabs.

The main persona is the add-on as it always was: the realtime model speaks with
its own voice. The second persona (configured with its own instructions and an
ElevenLabs voice) runs the same realtime session in TEXT mode, and an
ElevenLabs TTS service in the pipeline speaks that text. Which one is active
follows a Home Assistant input_select, so it can be switched from a dashboard,
an automation or by voice.

While the main persona is active, TtsGate marks every text frame skip_tts so
the ElevenLabs service passes them through untouched — the main persona's audio
path is exactly what it was before this module existed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

MAIN = "main"
SECOND = "second"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return default if value in (None, "", "null") else value.strip()


def _float_env(name: str) -> float | None:
    try:
        return float(_env(name)) if _env(name) else None
    except ValueError:
        return None


@dataclass
class PersonaConfig:
    main_name: str
    second_name: str
    second_instructions: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    entity_id: str
    second_wake_word: str
    # ElevenLabs voice settings; None = the voice's own defaults.
    stability: float | None = None
    style: float | None = None
    speed: float | None = None
    # Optional Home Assistant conversation agent that answers for the second
    # persona (see ha_agent.py), and an optional input_boolean that turns it on/off.
    second_agent: str = ""
    second_agent_switch: str = ""
    # Her avatar model's file name; empty = the .vrm whose name contains her name.
    second_model_file: str = ""

    @property
    def second_available(self) -> bool:
        return bool((self.second_instructions or self.second_agent)
                    and self.elevenlabs_api_key and self.elevenlabs_voice_id)

    @classmethod
    def from_env(cls) -> "PersonaConfig":
        return cls(
            main_name=_env("MAIN_PERSONA_NAME", "Jarvis"),
            second_name=_env("SECOND_PERSONA_NAME", "Lola"),
            second_instructions=_env("SECOND_PERSONA_INSTRUCTIONS"),
            elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_env("ELEVENLABS_VOICE_ID"),
            elevenlabs_model=_env("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            entity_id=_env("PERSONA_ENTITY", "input_select.voice_assistant"),
            second_wake_word=_env("SECOND_PERSONA_WAKE_WORD", "Okay Nabu"),
            stability=_float_env("ELEVENLABS_STABILITY"),
            style=_float_env("ELEVENLABS_STYLE"),
            speed=_float_env("ELEVENLABS_SPEED"),
            second_agent=_env("SECOND_PERSONA_AGENT"),
            second_agent_switch=_env("SECOND_PERSONA_AGENT_SWITCH"),
            second_model_file=_env("SECOND_PERSONA_MODEL"),
        )

    def persona_for_option(self, option: str | None) -> str:
        """Map the input_select's option to MAIN/SECOND (unknown → MAIN)."""
        if option and self.second_available and option.strip().lower() == self.second_name.lower():
            return SECOND
        return MAIN

    def persona_for_wake_word(self, wake_word: str) -> str:
        """Which persona a wake word calls: the configured one → SECOND, any other → MAIN."""
        def norm(text: str) -> str:
            return "".join(ch for ch in text.lower() if ch.isalnum())
        if self.second_available and self.second_wake_word and norm(wake_word) == norm(self.second_wake_word):
            return SECOND
        return MAIN


CONFIG = PersonaConfig.from_env()


class PersonaState:
    """Which persona is speaking right now (module-level singleton)."""

    def __init__(self) -> None:
        self.active = MAIN

    @property
    def speaks_with_tts(self) -> bool:
        return self.active == SECOND


STATE = PersonaState()


class TtsGate(FrameProcessor):
    """Sits right before the ElevenLabs TTS service.

    Main persona: mark text and response brackets skip_tts so the TTS service
    forwards them unchanged (the realtime model's own audio carries the reply).
    Second persona: leave them alone so the TTS service speaks the text.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and not STATE.speaks_with_tts
            and isinstance(frame, (TextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame))
        ):
            frame.skip_tts = True
        await self.push_frame(frame, direction)
