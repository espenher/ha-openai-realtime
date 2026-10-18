"""Avatar page: a 3D character on a screen that moves and lip-syncs with the assistant.

A small aiohttp server (its own port) serves a web page, the user's model and
animation files from Home Assistant's media folder, and a websocket that
streams what the page needs to animate:

  {"type": "state", "phase": ..., "persona": ...}   on connect
  {"type": "phase", "value": "listening" | "thinking" | "replying" | "idle"}
  {"type": "persona", "value": "<name>"}
  {"type": "wake"} / {"type": "interrupt"}
  {"type": "tag", "value": "<tag>"}                 a [tag] from the second persona's reply (mood or gesture)
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

from app.persona import CONFIG as PERSONA

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).parent / "static"
UPLOAD_DIR = pathlib.Path("/media/avatar")
OLD_UPLOAD_DIR = pathlib.Path("/data/avatar")
MEDIA_DIRS = (UPLOAD_DIR, pathlib.Path("/media"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {".vrm", ".fbx", ".vrma", ".glb", ".png", ".jpg", ".jpeg"}
ENVELOPE_STEP_S = 0.02

# What the second persona's avatar can do: moods (a facial expression that stays)
# and gestures (an animation played once). The room editor saves an edited copy
# in layout.json ("actions"); until then these defaults apply. The same list
# drives the page and her instructions (avatar_body.py), so the model only
# ever hears about things the avatar can actually show.
#   mood:    "face" = [[expression, weight], ...] from the model's own expressions
#   gesture: "animation" = an animation file's name without its extension
DEFAULT_ACTIONS = [
    {"tag": "neutral", "kind": "mood", "face": [], "note": "calm, matter-of-fact"},
    {"tag": "happy", "kind": "mood", "face": [["happy", 0.55], ["relaxed", 0.4]], "note": "glad, pleased, cheerful"},
    {"tag": "smug", "kind": "mood", "face": [["Confident", 0.9]], "note": "pleased with yourself, superior, told-you-so"},
    {"tag": "teasing", "kind": "mood", "face": [["Confident", 0.6], ["relaxed", 0.4]], "note": "playfully mocking"},
    {"tag": "annoyed", "kind": "mood", "face": [["Sulk", 1]], "note": "irritated, unimpressed"},
    {"tag": "angry", "kind": "mood", "face": [["angry", 1]], "note": "really cross"},
    {"tag": "bored", "kind": "mood", "face": [["Meh", 0.9]], "note": "uninterested, whatever"},
    {"tag": "surprised", "kind": "mood", "face": [["eyeWideLeft", 1], ["eyeWideRight", 1], ["browInnerUp", 1],
                                                  ["mouthFunnel", 0.4], ["jawOpen", 0.1]], "note": "caught off guard"},
    {"tag": "shocked", "kind": "mood", "face": [["Shock", 0.8]], "note": "genuinely stunned"},
    {"tag": "sad", "kind": "mood", "face": [["Sorrow", 0.9]], "note": "sad or sympathetic"},
    {"tag": "shy", "kind": "mood", "face": [["Shy", 1], ["relaxed", 0.3]], "note": "flustered, embarrassed, complimented"},
    {"tag": "excited", "kind": "mood", "face": [["Sparkly_Eyes", 1], ["relaxed", 0.5]], "note": "thrilled, enthusiastic"},
    {"tag": "tired", "kind": "mood", "face": [["Exhausted", 0.6]], "note": "sleepy, worn out"},
    {"tag": "serious", "kind": "mood", "face": [["Serious", 0.5]], "note": "focused, explaining something important"},
    {"tag": "confused", "kind": "mood", "face": [["Guruguru_Eyes", 0.7]], "note": "puzzled, it makes no sense"},
    {"tag": "scared", "kind": "mood", "face": [["Fear", 1]], "note": "nervous, worried"},
    {"tag": "wave", "kind": "gesture", "animation": "Greeting_female", "note": "saying hi or bye"},
    {"tag": "bow", "kind": "gesture", "animation": "Quick Formal Bow", "note": "a (mock) formal bow, at your service"},
    {"tag": "peace sign", "kind": "gesture", "animation": "Peace sign", "note": "cute victory pose, nailed it"},
    {"tag": "shrug", "kind": "gesture", "animation": "Shrugging", "note": "no idea, whatever, not my problem"},
    {"tag": "thanks", "kind": "gesture", "animation": "Thankful_female", "note": "thanking someone"},
    {"tag": "loser", "kind": "gesture", "animation": "L_on_forehead", "note": "mocking someone who failed"},
    {"tag": "dejected", "kind": "gesture", "animation": "Rejected_female", "note": "turned down, disappointed"},
    {"tag": "finger guns", "kind": "gesture", "animation": "Shoot", "note": "playful bang, gotcha"},
    {"tag": "spin", "kind": "gesture", "animation": "Spin", "note": "a happy twirl"},
    {"tag": "pose", "kind": "gesture", "animation": "Model pose", "note": "posing, showing off"},
    {"tag": "show outfit", "kind": "gesture", "animation": "Show full body", "note": "showing off your outfit"},
]
TAG = re.compile(r"\[([A-Za-z][A-Za-z _-]{0,23})\]\s*")
UNFINISHED_TAG = re.compile(r"\[[A-Za-z _-]{0,24}$")


def load_layout() -> dict:
    """The room editor's saved layout (room, wardrobe, colours, moods/gestures, character text)."""
    try:
        return json.loads((UPLOAD_DIR / "layout.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_layout(layout: dict) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOAD_DIR / "layout.json.part"
    tmp.write_text(json.dumps(layout, indent=1), encoding="utf-8")
    tmp.replace(UPLOAD_DIR / "layout.json")


def avatar_actions() -> list[dict]:
    """The avatar's moods and gestures: the room editor's saved list, or the defaults."""
    actions = load_layout().get("actions")
    return actions if isinstance(actions, list) else DEFAULT_ACTIONS


class AvatarHub:
    """Fans phase, persona and loudness out to every open avatar page."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._state = {"phase": "idle", "persona": None}
        # Set by the websocket handler: refreshes the live session's instructions
        # when the editor saves new moods/gestures.
        self.on_layout_saved = None

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
        app.router.add_get("/actions", self._get_actions)
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
        # Files get replaced under the same name (a re-exported room, a new
        # texture), so browsers must check before reusing a cached copy; an
        # unchanged file still comes back as a cheap 304.
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    # The room layout (placed furniture, edited room objects, spots, camera),
    # written by the page's edit mode. Kept next to the uploaded files.
    async def _get_layout(self, request: web.Request) -> web.Response:
        path = UPLOAD_DIR / "layout.json"
        if not path.is_file():
            return web.json_response({})
        return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json",
                            headers={"Cache-Control": "no-cache"})

    async def _get_actions(self, request: web.Request) -> web.Response:
        # "persona": whose replies carry the tags (the page only applies them to that character);
        # "model": her avatar file when set in the options (else the page matches it by name).
        return web.json_response({"actions": avatar_actions(), "defaults": DEFAULT_ACTIONS,
                                  "persona": PERSONA.second_name, "model": PERSONA.second_model_file or None})

    async def _put_layout(self, request: web.Request) -> web.Response:
        try:
            layout = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Layout must be JSON.")
        if not isinstance(layout, dict):
            raise web.HTTPBadRequest(text="Layout must be a JSON object.")
        save_layout(layout)
        logger.info("🧍 Avatar room layout saved")
        self.send({"type": "layout"})
        if self.on_layout_saved:
            await self.on_layout_saved()
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


class AvatarTags(FrameProcessor):
    """Takes "[tag]" moods and gestures out of text-mode replies and sends them to the avatar pages.

    Sits right after the realtime service, so the transcript, the follow-up
    question check and the ElevenLabs voice all get the text without tags. A tag
    can be split over several text chunks, so an unfinished "[..." is held back
    until it closes (or the reply ends).
    """

    def __init__(self, body_tag_action=None) -> None:
        super().__init__()
        self._held = ""
        # Optional: given a tag's text, the outfit/colour change it asks for (avatar_body.body_tag_action).
        self._body_tag_action = body_tag_action

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
            for tag in TAG.finditer(text):
                value = tag.group(1).strip().lower()
                logger.info(f"🧍 tag: {value}")
                change = self._body_tag_action(value) if self._body_tag_action else None
                if change:
                    asyncio.create_task(change())
                else:
                    HUB.send({"type": "tag", "value": value})
            text = TAG.sub("", text)
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
