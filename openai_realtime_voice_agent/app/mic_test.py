"""Mic test: sends the Voice PE's mic to the PC for a few minutes.

For finding out whether the Voice PE hears Espen well enough for a listening mode
without a wake word (the PC side is LolaMicTest/record.py). Temporary: take it out
again once the test is done.

How it goes:
1. The PC asks for the recording (GET /mictest on the avatar port). Nothing is
   recorded yet: the test is only armed, for ARM_S seconds.
2. Espen says the wake word. Only that starts it, so nobody can open the mic from
   the network alone. The add-on tells the device "listening", which keeps its mic
   open (the ring shows it).
3. The mic goes straight to the PC that asked: nothing is stored here, and nothing
   reaches OpenAI or her agent.
4. It ends after the asked number of seconds, when the PC hangs up, on "stop" or the
   device's button, or when no audio arrives for QUIET_S. The device goes back to idle.

What the PC receives is a row of records: RECORD (kind, time, length) and then that
many bytes. Kind "A" is audio (16-bit mono PCM at RATE), "E" is a JSON event. The time
is this machine's clock, in seconds, when the audio arrived.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time

from aiohttp import web

logger = logging.getLogger(__name__)

RATE = 16000
RECORD = struct.Struct("<cdI")
ARM_S = 60.0            # how long the wake word is waited for
MAX_S = 900.0           # longest recording
QUIET_S = 5.0           # no audio for this long ends it
HOLD_EVERY_S = 3.0      # "listening" is repeated, in case the device missed one
HEADER = "X-Mic-Test"   # asked for so that no web page can start this from a browser


class MicTest:
    def __init__(self) -> None:
        self.armed = False
        self.recording = False
        self._queue: asyncio.Queue | None = None
        self._send_phase = None     # async callable(value): a phase to the device
        self._ready = None          # callable: is a device connected and idle?

    def connect(self, send_phase, ready) -> None:
        self._send_phase, self._ready = send_phase, ready

    # ---------- from the device (raw_audio_serializer.py) ----------

    def on_wake(self) -> bool:
        """The device woke. True if that was for the test (so nothing else handles it)."""
        if self.recording:
            self._put("wake")
            return True
        if not self.armed:
            return False
        self.armed, self.recording = False, True
        self._put("started")
        return True

    def on_interrupt(self) -> bool:
        """The "stop" word or the button. True if it ended the test."""
        if not self.recording:
            return False
        self._put("stop")
        return True

    def on_audio(self, pcm: bytes) -> bool:
        """Mic audio. True if the test took it."""
        if not self.recording:
            return False
        self._put("audio", pcm)
        return True

    def _put(self, kind: str, pcm: bytes = b"") -> None:
        self._queue.put_nowait((kind, time.monotonic(), pcm))

    # ---------- to the PC (avatar.py's web server) ----------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if HEADER not in request.headers:
            raise web.HTTPForbidden(text=f"Needs the {HEADER} header.")
        try:
            seconds = min(MAX_S, max(1.0, float(request.query.get("seconds", "300"))))
        except ValueError:
            raise web.HTTPBadRequest(text="seconds must be a number.")
        if self.armed or self.recording:
            raise web.HTTPConflict(text="A mic test is already running.")
        if self._ready is None or not self._ready():
            raise web.HTTPConflict(text="The Voice PE isn't connected, or Lola is busy. Try again in a moment.")
        self._queue = asyncio.Queue()
        self.armed = True
        logger.info(f"🎙️ mic test: waiting for the wake word ({seconds:.0f}s, for {request.remote})")
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})
        why = "the PC hung up"
        try:
            await response.prepare(request)
            why = await self._run(response, seconds)
            await self._event(response, "ended", why=why)
            await response.write_eof()
        except ConnectionError:
            pass
        finally:
            was_recording, self.armed, self.recording = self.recording, False, False
            logger.info(f"🎙️ mic test over: {why}")
            if was_recording:
                await self._send_phase("idle")
        return response

    async def _run(self, response: web.StreamResponse, seconds: float) -> str:
        """Runs until it's over; returns why it ended."""
        await self._event(response, "armed", seconds=ARM_S)
        waiting_until = time.monotonic() + ARM_S
        while True:   # for the wake word (writing now and then notices a PC that hung up)
            try:
                kind, at, _ = await asyncio.wait_for(self._queue.get(), 1.0)
            except asyncio.TimeoutError:
                if time.monotonic() > waiting_until:
                    return "no wake word"
                await self._event(response, "waiting")
                continue
            if kind == "started":
                break
        ends, last_audio, held = at + seconds, at, 0.0
        await self._event(response, "started", at=at, rate=RATE)
        logger.info("🎙️ mic test: recording")
        while True:
            now = time.monotonic()
            if now >= ends:
                return "done"
            if now - last_audio > QUIET_S:
                return "no more audio from the Voice PE"
            if now - held >= HOLD_EVERY_S:
                held = now
                await self._send_phase("listening")
            try:
                kind, at, pcm = await asyncio.wait_for(self._queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            if kind == "stop":
                return "stopped at the device"
            if kind == "wake":   # woken again: its no-speech timer is running, so hold it open now
                held = 0.0
            elif kind == "audio":
                last_audio = at
                await response.write(RECORD.pack(b"A", at, len(pcm)) + pcm)

    @staticmethod
    async def _event(response: web.StreamResponse, name: str, **more) -> None:
        body = json.dumps({"event": name, **more}).encode()
        await response.write(RECORD.pack(b"E", time.monotonic(), len(body)) + body)


MIC_TEST = MicTest()
