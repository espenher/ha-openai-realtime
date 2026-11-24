"""Speech to text for one utterance, with OpenAI's transcription API.

Active mode uses this instead of the realtime session: it knows itself where an
utterance begins and ends, and only Espen's own speech is sent.
"""

from __future__ import annotations

import io
import logging
import os
import wave

import aiohttp
import numpy as np

logger = logging.getLogger(__name__)

URL = "https://api.openai.com/v1/audio/transcriptions"
TIMEOUT_S = 15
# The realtime session's own transcription model isn't offered here.
NOT_HERE = {"gpt-realtime-whisper": "gpt-4o-transcribe"}


def _model() -> str:
    chosen = os.environ.get("TRANSCRIPTION_MODEL", "").strip() or "gpt-4o-transcribe"
    if chosen == "custom":
        chosen = os.environ.get("TRANSCRIPTION_MODEL_CUSTOM", "").strip() or "gpt-4o-transcribe"
    return NOT_HERE.get(chosen, chosen)


def wav_bytes(audio: np.ndarray, rate: int) -> bytes:
    file = io.BytesIO()
    with wave.open(file, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return file.getvalue()


async def transcribe(audio: np.ndarray, rate: int = 16000) -> str:
    """What's said in it; "" if nothing could be made out (or OpenAI couldn't be reached)."""
    form = aiohttp.FormData()
    form.add_field("file", wav_bytes(audio, rate), filename="speech.wav", content_type="audio/wav")
    form.add_field("model", _model())
    form.add_field("response_format", "json")
    language = os.environ.get("TRANSCRIPTION_LANGUAGE", "").strip()
    if language:
        form.add_field("language", language)
    headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as http:
            async with http.post(URL, data=form, headers=headers) as r:
                if r.status != 200:
                    logger.warning(f"🎧 transcription failed: HTTP {r.status} {(await r.text())[:200]}")
                    return ""
                return str((await r.json()).get("text", "")).strip()
    except Exception as e:
        logger.warning(f"🎧 transcription failed: {e!r}")
        return ""
