"""Avatar page: a 3D character on a screen that moves and lip-syncs with the assistant.

A small aiohttp server (its own port) serves a web page, the user's model and
animation files from Home Assistant's media folder, and a websocket that
streams what the page needs to animate:

  {"type": "state", "phase": ..., "persona": ...}   on connect
  {"type": "phase", "value": "listening" | "thinking" | "replying" | "idle"}
  {"type": "persona", "value": "<name>"}
  {"type": "wake"} / {"type": "interrupt"}
  {"type": "env", "dt": 0.02, "values": [...]}       loudness of the reply audio,
                                                      one value per 20 ms, for lip-sync

Nothing here touches the device path: the tap copies loudness off audio frames
that are on their way to the Voice PE anyway, and every send is fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
import math
import pathlib
from array import array

from aiohttp import WSMsgType, web
from pipecat.frames.frames import Frame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).parent / "static"
MEDIA_DIRS = (pathlib.Path("/media/avatar"), pathlib.Path("/media"))
ALLOWED_SUFFIXES = {".vrm", ".fbx", ".vrma", ".glb"}
ENVELOPE_STEP_S = 0.02


class AvatarHub:
    """Fans phase, persona and loudness out to every open avatar page."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._state = {"phase": "idle", "persona": None}

    # ---------- server ----------
    async def start(self, port: int) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/files", self._list_files)
        app.router.add_get("/files/{name}", self._file)
        app.router.add_get("/ws", self._ws)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"🧍 Avatar page on port {port} (models from {self._media_dir()})")

    @staticmethod
    def _media_dir() -> pathlib.Path:
        for directory in MEDIA_DIRS:
            if directory.is_dir():
                return directory
        return MEDIA_DIRS[-1]

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "avatar.html", headers={"Cache-Control": "no-cache"})

    async def _list_files(self, request: web.Request) -> web.Response:
        directory = self._media_dir()
        names = sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES) \
            if directory.is_dir() else []
        return web.json_response(names)

    async def _file(self, request: web.Request) -> web.StreamResponse:
        directory = self._media_dir()
        path = directory / request.match_info["name"]
        if path.parent != directory or path.suffix.lower() not in ALLOWED_SUFFIXES or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        await ws.send_json({"type": "state", **self._state})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
        return ws

    # ---------- sending ----------
    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    def send(self, obj: dict) -> None:
        if obj.get("type") in ("phase", "persona"):
            self._state[obj["type"]] = obj.get("value")
        for ws in list(self._clients):
            if not ws.closed:
                asyncio.create_task(self._safe_send(ws, obj))

    @staticmethod
    async def _safe_send(ws: web.WebSocketResponse, obj: dict) -> None:
        try:
            await ws.send_json(obj)
        except Exception:
            pass

    def send_envelope(self, pcm: bytes, sample_rate: int) -> None:
        """RMS loudness (0–1) per 20 ms of 16-bit mono PCM."""
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        step = max(1, int(sample_rate * ENVELOPE_STEP_S))
        values = []
        for start in range(0, len(samples), step):
            chunk = samples[start : start + step]
            if chunk:
                values.append(round(math.sqrt(sum(x * x for x in chunk) / len(chunk)) / 32768.0, 4))
        if values:
            self.send({"type": "env", "dt": ENVELOPE_STEP_S, "values": values})


HUB = AvatarHub()


class AvatarTap(FrameProcessor):
    """Copies the loudness of outgoing reply audio to the avatar pages."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame) and HUB.has_clients:
            try:
                HUB.send_envelope(frame.audio, frame.sample_rate)
            except Exception as e:  # never let the avatar disturb the audio path
                logger.debug(f"avatar envelope skipped: {e!r}")
        await self.push_frame(frame, direction)
