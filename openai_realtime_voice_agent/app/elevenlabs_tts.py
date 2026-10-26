"""ElevenLabs TTS for the second persona, made to survive ElevenLabs dropping the connection.

ElevenLabs closes the streaming websocket now and then (seen exactly an hour
after it opened). pipecat 0.0.97 notices on the next reply and opens a new
socket, but its receive and keepalive tasks have already ended and are never
restarted: nothing reads the new socket, so the reply came out as a 3 s pause
and then less than a second of speech (2026-10-18 16:22), and every reply
after that stayed broken until the add-on restarted.

When ElevenLabs closes the socket cleanly instead (after the keepalive sends
an empty text with no context, which it reads as "end of stream": that
happens when the user interrupts), the receive loop ends without an error and
pipecat's `while True` calls it again at once, forever, never yielding: the
whole add-on froze until the watchdog restarted it (2026-10-26 21:23).

It also reads units out as words: ElevenLabs says "5°C" as "five degs".
"""

from __future__ import annotations

import logging
import re

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from websockets.exceptions import ConnectionClosedOK
from websockets.protocol import State

logger = logging.getLogger(__name__)

_NUMBER = r"(-|−)?(\d+(?:[.,]\d+)?)"
_SPOKEN = [
    # Temperatures: Celsius is what's meant here, so just "degrees".
    (re.compile(_NUMBER + r"\s*°\s*C\b"), lambda m: f"{'minus ' if m[1] else ''}{m[2]} degrees"),
    (re.compile(_NUMBER + r"\s*°\s*F\b"), lambda m: f"{'minus ' if m[1] else ''}{m[2]} degrees Fahrenheit"),
    (re.compile(_NUMBER + r"\s*°"), lambda m: f"{'minus ' if m[1] else ''}{m[2]} degrees"),
    (re.compile(r"(\d)\s*km/h\b"), lambda m: f"{m[1]} kilometres per hour"),
    (re.compile(r"(\d)\s*m/s\b"), lambda m: f"{m[1]} metres per second"),
]


def spoken(text: str) -> str:
    """Text as it should be said: units the voice reads badly written out in words."""
    for pattern, words in _SPOKEN:
        text = pattern.sub(words, text)
    return text


class SafeElevenLabsTTSService(ElevenLabsTTSService):
    async def _connect(self):
        # Forget tasks that ended with the old socket so super() starts fresh ones.
        if self._receive_task is not None and self._receive_task.done():
            self._receive_task = None
        if self._keepalive_task is not None and self._keepalive_task.done():
            self._keepalive_task = None
        await super()._connect()

    async def _receive_messages(self):
        await super()._receive_messages()
        # Only gets here when the socket closed cleanly. Raising ends pipecat's
        # receive task instead of letting it spin; the next wake or reply reconnects.
        logger.info("🔊 ElevenLabs closed the connection cleanly → reconnecting at the next wake")
        raise ConnectionClosedOK(None, None)

    async def run_tts(self, text: str):
        async for frame in super().run_tts(spoken(text)):
            yield frame

    async def ensure_connected(self) -> None:
        """Reconnect now if the socket dropped, so the next reply doesn't wait for it.

        Called when the device wakes: the user is still talking, which leaves
        ample time to get the connection back before the reply is spoken.
        """
        if self._websocket is None or self._websocket.state is State.CLOSED:
            logger.info("🔊 ElevenLabs connection was closed → reconnecting at wake")
            await self._connect()
