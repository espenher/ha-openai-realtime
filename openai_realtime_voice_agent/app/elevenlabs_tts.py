"""ElevenLabs TTS for the second persona, made to survive ElevenLabs dropping the connection.

ElevenLabs closes the streaming websocket now and then (seen exactly an hour
after it opened). pipecat 0.0.97 notices on the next reply and opens a new
socket, but its receive and keepalive tasks have already ended and are never
restarted: nothing reads the new socket, so the reply came out as a 3 s pause
and then less than a second of speech (2026-10-18 16:22), and every reply
after that stayed broken until the add-on restarted.
"""

from __future__ import annotations

import logging

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from websockets.protocol import State

logger = logging.getLogger(__name__)


class SafeElevenLabsTTSService(ElevenLabsTTSService):
    async def _connect(self):
        # Forget tasks that ended with the old socket so super() starts fresh ones.
        if self._receive_task is not None and self._receive_task.done():
            self._receive_task = None
        if self._keepalive_task is not None and self._keepalive_task.done():
            self._keepalive_task = None
        await super()._connect()

    async def ensure_connected(self) -> None:
        """Reconnect now if the socket dropped, so the next reply doesn't wait for it.

        Called when the device wakes: the user is still talking, which leaves
        ample time to get the connection back before the reply is spoken.
        """
        if self._websocket is None or self._websocket.state is State.CLOSED:
            logger.info("🔊 ElevenLabs connection was closed → reconnecting at wake")
            await self._connect()
