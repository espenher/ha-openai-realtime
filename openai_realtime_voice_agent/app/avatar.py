"""Avatar page: a 3D character on a screen that moves and lip-syncs with the assistant.

A small aiohttp server (its own port) serves a web page, the user's model and
animation files from Home Assistant's media folder, and a websocket that
streams what the page needs to animate:

  {"type": "state", "phase": ..., "persona": ...}   on connect
  {"type": "phase", "value": "listening" | "thinking" | "replying" | "idle"}
  {"type": "persona", "value": "<name>"}
  {"type": "wake"} / {"type": "interrupt"}
  {"type": "mood", "value": "<mood>"}               from the second persona's [mood] tags
  {"type": "env", "dt": 0.02, "values": [...]}       loudness of the reply audio,
                                                      one value per 20 ms, for lip-sync

Files live in Home Assistant's media folder under avatar/ (uploaded with
PUT /files/<name>, or copied in over Samba). Files from older versions, which
kept them in the add-on's own storage, are moved there on start.

Nothing here touches the device path: the tap copies loudness off audio frames
that are on their way to the Voice PE anyway, and every send is fire-and-forget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import pathlib
import re
import shutil
from array import array

from aiohttp import WSMsgType, web
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).parent / "static"
UPLOAD_DIR = pathlib.Path("/media/avatar")
OLD_UPLOAD_DIR = pathlib.Path("/data/avatar")
MEDIA_DIRS = (UPLOAD_DIR, pathlib.Path("/media"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {".vrm", ".fbx", ".vrma", ".glb", ".png", ".jpg", ".jpeg"}
ENVELOPE_STEP_S = 0.02

# Added to the second persona's instructions: a mood tag the avatar shows as a
# facial expression. MoodTags takes the tags out before the voice reads the text.
MOOD_INSTRUCTIONS = (
    "\n\n# Facial expression\n"
    "You have an animated avatar whose face shows your mood. Start every reply with one mood tag in square "
    "brackets, for example \"[smug] Oh, you need my help again?\". If your mood clearly changes during a reply, "
    "put a new tag there. The tags are never spoken, so never mention them. Use only these: [neutral] [happy] "
    "[smug] [teasing] [annoyed] [angry] [bored] [surprised] [shocked] [sad] [shy] [excited] [tired] [serious] "
    "[confused] [scared]."
)
MOOD_TAG = re.compile(r"\[([A-Za-z][A-Za-z _-]{0,19})\]\s*")
UNFINISHED_TAG = re.compile(r"\[[A-Za-z _-]{0,20}$")


class AvatarHub:
    """Fans phase, persona and loudness out to every open avatar page."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._state = {"phase": "idle", "persona": None}

    # ---------- server ----------
    async def start(self, port: int) -> None:
        await asyncio.to_thread(self._move_old_uploads)
        app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
        app.router.add_get("/", self._index)
        app.router.add_get("/files", self._list_files)
        app.router.add_get("/files/{name}", self._file)
        app.router.add_put("/files/{name}", self._upload)
        app.router.add_delete("/files/{name}", self._delete)
        app.router.add_get("/layout", self._get_layout)
        app.router.add_put("/layout", self._put_layout)
        app.router.add_get("/ws", self._ws)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"🧍 Avatar page on port {port} (models from {self._media_dir()})")

    @staticmethod
    def _move_old_uploads() -> None:
        """Move files from the add-on's own storage (used before 0.6.0-q22) into media/avatar."""
        if not OLD_UPLOAD_DIR.is_dir():
            return
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for old in OLD_UPLOAD_DIR.iterdir():
            new = UPLOAD_DIR / old.name
            if not old.is_file() or old.name.endswith(".part"):
                continue
            if new.exists():
                logger.warning(f"🧍 Not moving {old.name}: media/avatar already has a file with that name")
                continue
            tmp = new.with_name(new.name + ".part")
            try:
                shutil.copyfile(old, tmp)
                tmp.replace(new)
                old.unlink()
                logger.info(f"🧍 Moved avatar file {old.name} to media/avatar")
            except OSError as e:
                tmp.unlink(missing_ok=True)
                logger.warning(f"🧍 Could not move {old.name} to media/avatar: {e!r}")

    @staticmethod
    def _media_dir() -> pathlib.Path:
        """The first folder that actually holds model files."""
        for directory in MEDIA_DIRS:
            if directory.is_dir() and any(p.suffix.lower() in ALLOWED_SUFFIXES for p in directory.iterdir()):
                return directory
        return UPLOAD_DIR

    @staticmethod
    def _upload_path(name: str) -> pathlib.Path:
        path = UPLOAD_DIR / name
        if path.parent != UPLOAD_DIR or path.suffix.lower() not in ALLOWED_SUFFIXES or name.startswith("."):
            raise web.HTTPBadRequest(text="Only .vrm, .fbx, .vrma, .glb and image files can be uploaded.")
        return path

    async def _upload(self, request: web.Request) -> web.Response:
        path = self._upload_path(request.match_info["name"])
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        size = 0
        with tmp.open("wb") as out:
            async for chunk in request.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=size)
                out.write(chunk)
        tmp.replace(path)
        logger.info(f"🧍 Avatar file uploaded: {path.name} ({size // 1024} KB)")
        return web.json_response({"name": path.name, "bytes": size})

    async def _delete(self, request: web.Request) -> web.Response:
        path = self._upload_path(request.match_info["name"])
        path.unlink(missing_ok=True)
        return web.json_response({"deleted": path.name})

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

    # The room layout (placed furniture, edited room objects, spots, camera),
    # written by the page's edit mode. Kept next to the uploaded files.
    async def _get_layout(self, request: web.Request) -> web.Response:
        path = UPLOAD_DIR / "layout.json"
        if not path.is_file():
            return web.json_response({})
        return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json",
                            headers={"Cache-Control": "no-cache"})

    async def _put_layout(self, request: web.Request) -> web.Response:
        try:
            layout = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Layout must be JSON.")
        if not isinstance(layout, dict):
            raise web.HTTPBadRequest(text="Layout must be a JSON object.")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPLOAD_DIR / "layout.json.part"
        tmp.write_text(json.dumps(layout, indent=1), encoding="utf-8")
        tmp.replace(UPLOAD_DIR / "layout.json")
        logger.info("🧍 Avatar room layout saved")
        self.send({"type": "layout"})
        return web.json_response({"saved": True})

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


class MoodTags(FrameProcessor):
    """Takes "[mood]" tags out of text-mode replies and sends them to the avatar pages.

    Sits right after the realtime service, so the transcript, the follow-up
    question check and the ElevenLabs voice all get the text without tags. A tag
    can be split over several text chunks, so an unfinished "[..." is held back
    until it closes (or the reply ends).
    """

    def __init__(self) -> None:
        super().__init__()
        self._held = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMTextFrame):
            text, self._held = self._held + frame.text, ""
            unfinished = UNFINISHED_TAG.search(text)
            if unfinished:
                text, self._held = text[: unfinished.start()], text[unfinished.start():]
            for tag in MOOD_TAG.finditer(text):
                mood = tag.group(1).strip().lower()
                logger.info(f"🧍 mood: {mood}")
                HUB.send({"type": "mood", "value": mood})
            text = MOOD_TAG.sub("", text)
            if text:
                frame.text = text
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._held = ""
        elif isinstance(frame, LLMFullResponseEndFrame) and self._held:
            held, self._held = self._held, ""
            await self.push_frame(LLMTextFrame(held), direction)
        await self.push_frame(frame, direction)


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
