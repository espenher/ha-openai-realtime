"""Active mode: she keeps listening without a wake word, and may stay silent.

Turned on by her own [stay active] tag (when Espen asks her to stay) and off by
[stop listening], the button on the Voice PE pressed twice while she's listening (or the
"stop" word said twice: once could be anything a game shouts), or when he hasn't said
anything for ACTIVE_MODE_MINUTES.

While it's on:
- The device is told "listening" whenever nothing else is going on, which keeps its mic
  open. (It's closed by itself while she talks.)
- The mic doesn't go to OpenAI's realtime session. his_speech.py, on this machine, finds
  what Espen says by his voice print, and leaves everybody and everything else out. Only
  that is transcribed (transcribe.py).
- What he says is for her if they've talked in the last ACTIVE_MODE_WINDOW_SECONDS, if it
  has her name in it, or if it's a question. She gets it like any other turn, and may
  answer just [quiet] if it wasn't for her after all.
- The rest she isn't asked about. She gets the last few lines of it along with the next
  turn (hidden, "not necessarily to you"); it doesn't count as having talked.

Only with a Home Assistant agent answering for her (ha_agent.py). Needs his voice print,
in VOICEPRINT (PUT /voiceprint on the avatar port; LolaMicTest/make_voiceprint.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

import numpy as np

from app.his_speech import HisSpeech, Utterance
from app.transcribe import transcribe
from app.voice_models import RATE, SpeechDetector, TurnEnd, VoicePrint

logger = logging.getLogger(__name__)

VOICEPRINT = Path(os.environ.get("VOICEPRINT", "/data/voiceprint.json"))
LENIENT_S = 10          # a short answer is let through more easily this long after she's talked
HOLD_EVERY_S = 10       # the device is reminded to keep listening, in case it reconnected
OVERHEARD_LINES = 8
OVERHEARD_S = 600       # older lines aren't passed on
TWICE_S = 6             # two stops within this end it
BUSY_PIECES = 250       # this much mic audio waiting (4 s) means the models can't keep up

TAG = re.compile(r"\[[^\]\n]{0,300}\]\s*")


def is_quiet(reply: str) -> bool:
    """Did she choose to say nothing? (Only tags, with [quiet] among them.)"""
    return "[quiet]" in reply.lower() and not TAG.sub("", reply).strip()


def _seconds(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


class ActiveMode:
    def __init__(self) -> None:
        self.on = False
        self._ever_on = False
        self._his: HisSpeech | None = None
        self._voice: VoicePrint | None = None
        self._tasks: list[asyncio.Task] = []
        self._audio: asyncio.Queue = asyncio.Queue()        # mic audio; None: forget what was heard
        self._utterances: asyncio.Queue = asyncio.Queue()
        self._deaf = False            # while she talks
        self._said: list[str] = []    # transcribed, while he goes on talking
        self._overheard: deque = deque(maxlen=OVERHEARD_LINES)   # (when, text)
        self._window_until = 0.0      # until when what he says is taken to be for her
        self._window_before = 0.0     # what that was before the last turn
        self._quiet = False           # her last answer was [quiet]
        self._lenient_until = 0.0
        self._heard_at = 0.0          # when he last said something
        self._woken_at = 0.0
        self._stopped_at = 0.0        # the last stop while listening
        self._looking = False         # the page shows her listening to him

    # ---------- setup ----------

    async def load(self) -> None:
        """Load the models (at startup). Without them, active mode just isn't offered."""
        try:
            await asyncio.to_thread(self._load)
        except Exception as e:
            logger.warning(f"🎧 active mode isn't available: {e!r}")

    def _load(self) -> None:
        voice, turn_end = VoicePrint(), TurnEnd()
        noise = np.random.default_rng(0).normal(0, 0.01, int(1.5 * RATE)).astype(np.float32)
        took = []
        for model in (voice.of, turn_end):
            model(noise)   # the first time is slower
            start = time.perf_counter()
            model(noise)
            took.append((time.perf_counter() - start) * 1000)
        voice.load(VOICEPRINT)
        self._voice, self._his = voice, HisSpeech(SpeechDetector(), voice, turn_end)
        logger.info(f"🎧 active mode's models loaded (voice check {took[0]:.0f} ms, end of turn {took[1]:.0f} ms); "
                    + (f"{len(voice.known)} voice prints" if voice.known is not None else "no voice print yet"))

    def connect(self, *, phase, send_listening, send_idle, take_turn, woken, page) -> None:
        """phase(): her current phase. send_listening(), send_idle(): async, to the device.
        take_turn(text): async, she gets what he said; False if she can't now.
        woken(): async, the wake word was said. page(message): to the avatar pages."""
        self._phase, self._send_listening, self._send_idle = phase, send_listening, send_idle
        self._take_turn, self._on_woken, self._page = take_turn, woken, page
        self._window_s = _seconds("ACTIVE_MODE_WINDOW_SECONDS", 45)
        self._minutes = _seconds("ACTIVE_MODE_MINUTES", 30)

    @property
    def available(self) -> bool:
        return self._his is not None and self._voice.known is not None

    def save_voiceprint(self, sent: dict) -> int:
        """Store his voice prints; how many there are."""
        prints = np.array(sent["prints"], np.float32)
        if prints.ndim != 2 or not 1 <= len(prints) <= 50:
            raise ValueError("prints must be a list of rows of numbers")
        VOICEPRINT.parent.mkdir(parents=True, exist_ok=True)
        VOICEPRINT.write_text(json.dumps({"prints": prints.tolist()}), encoding="utf-8")
        if self._voice is not None:
            self._voice.load(VOICEPRINT)
        return len(prints)

    # ---------- on and off ----------

    async def start(self) -> None:
        if self.on:
            return
        if not self.available:
            logger.warning("🎧 active mode can't start: no models or no voice print")
            return
        now = time.monotonic()
        self.on = self._ever_on = True
        self._audio, self._utterances = asyncio.Queue(), asyncio.Queue()
        self._audio.put_nowait(None)
        self._deaf = self._phase() == "replying"
        self._said, self._quiet, self._looking = [], False, False
        self._overheard.clear()
        self._heard_at, self._window_until = now, now + self._window_s
        self._tasks = [asyncio.create_task(job()) for job in (self._listen, self._answer, self._watch)]
        self._page({"type": "active", "value": True})
        logger.info("🎧 active mode on")
        if self._phase() in (None, "idle"):
            await self._send_listening()

    async def stop(self, why: str) -> None:
        if not self.on:
            return
        self.on = False
        me = asyncio.current_task()
        for task in self._tasks:
            if task is not me:
                task.cancel()
        self._page({"type": "active", "value": False})
        logger.info(f"🎧 active mode off ({why})")
        if self._phase() in (None, "idle"):
            await self._send_idle()

    def tag_action(self, value: str):
        """What one of her tags asks for here (an async function), if anything."""
        if value == "stay active":
            return self.start
        if value == "stop listening":
            return lambda: self.stop("she ended it")
        if value == "quiet":
            return lambda: asyncio.sleep(0)   # ha_agent.py and note_reply deal with it
        return None

    # ---------- the device ----------

    def device_phase(self, value: str) -> str:
        """Her phase changed; what the device is told."""
        if not self.on:
            return value
        if value == "replying":
            self._deaf = True
            self._audio.put_nowait(None)
        elif value == "idle":   # her turn is over: back to listening
            now = time.monotonic()
            self._deaf = False
            self._audio.put_nowait(None)
            if self._quiet:
                self._window_until = min(self._window_until, self._window_before)
            else:
                self._window_until, self._lenient_until = now + self._window_s, now + LENIENT_S
            self._quiet = self._looking = False
            return "listening"
        return value

    def on_audio(self, pcm: bytes) -> bool:
        """Mic audio. True if active mode took it."""
        if not self.on:
            return False
        if not self._deaf:
            self._audio.put_nowait(pcm)
        return True

    def on_wake(self) -> bool:
        """The wake word was said while active. True if that's dealt with."""
        if not self.on:
            return False
        self._woken_at = time.monotonic()
        self._heard_at = self._woken_at
        self._window_until = self._woken_at + self._window_s
        self._audio.put_nowait(None)
        self._page({"type": "wake"})
        asyncio.create_task(self._after_wake())
        return True

    async def _after_wake(self) -> None:
        await self._on_woken()
        # Its no-speech timer runs from the wake; "listening" ends it. Said twice, since the
        # first one can arrive before the timer has started.
        for pause in (0.0, 0.7):
            await asyncio.sleep(pause)
            if self.on and self._phase() in (None, "idle", "listening"):
                await self._send_listening()

    def on_interrupt(self) -> bool:
        """"Stop", or the button. While she talks that's about her reply (False: the usual);
        while she listens, the second time ends active mode."""
        if not self.on or self._phase() not in (None, "idle"):
            return False
        twice = time.monotonic() - self._stopped_at < TWICE_S
        self._stopped_at = time.monotonic()
        asyncio.create_task(self._after_stop(twice))
        return True

    async def _after_stop(self, twice: bool) -> None:
        await asyncio.sleep(0.5)   # the device sends an interrupt before some wakes, too
        if not self.on or self._woken_at > self._stopped_at:
            return
        if twice:
            await self.stop("stopped at the Voice PE")
        else:   # the device has closed its mic
            logger.info("🎧 stop while listening: once more ends active mode")
            await self._send_listening()

    async def _watch(self) -> None:
        stuck = False
        while True:
            await asyncio.sleep(HOLD_EVERY_S)
            if time.monotonic() - self._heard_at > self._minutes * 60:
                await self.stop(f"he hasn't said anything for {self._minutes:.0f} minutes")
                return
            if self._phase() in (None, "idle"):
                if self._deaf and stuck:   # a reply that never came
                    self._deaf = False
                stuck = self._deaf
                if not stuck:
                    await self._send_listening()

    # ---------- what he says ----------

    async def _listen(self) -> None:
        late = False
        while True:
            pieces = [await self._audio.get()]
            while not self._audio.empty() and len(pieces) < 8:
                pieces.append(self._audio.get_nowait())
            if None in pieces:
                pieces = pieces[len(pieces) - pieces[::-1].index(None):]   # what came after the last one
                self._his.reset()
            if not pieces:
                continue
            if late != (self._audio.qsize() > BUSY_PIECES):
                late = not late
                if late:
                    logger.warning("🎧 the models can't keep up with the mic")
            self._his.lenient = time.monotonic() < self._lenient_until
            audio = np.frombuffer(b"".join(pieces), "<i2").astype(np.float32) / 32768
            try:
                found = await asyncio.to_thread(self._his.feed, audio)
            except Exception as e:
                logger.warning(f"🎧 listening failed: {e!r}")
                self._his.reset()
                continue
            for event in found:
                if isinstance(event, Utterance):
                    logger.info(f"🎧 Espen, {event.seconds:.1f}s (voice {event.score:.2f}, {event.ended})")
                    self._utterances.put_nowait(event)
                elif time.monotonic() < self._window_until and self._phase() in (None, "idle"):
                    self._look(True)

    async def _answer(self) -> None:
        while True:
            utterance = await self._utterances.get()
            try:
                await self._take(utterance)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"🎧 couldn't handle what he said: {e!r}")

    async def _take(self, utterance: Utterance) -> None:
        self._heard_at = time.monotonic()
        started = time.perf_counter()
        text = await transcribe(utterance.audio)
        took = time.perf_counter() - started
        if any(c.isalpha() for c in text):
            self._said.append(text)
        if self._his.talking or not self._utterances.empty():
            return   # he's going on: all of it then
        text, self._said = " ".join(self._said), []
        if not text:
            self._look(False)
            return
        if not self._for_her(text):
            logger.info(f"🎧 not for her ({len(text.split())} words, transcribed in {took:.1f}s)")
            self._overheard.append((time.monotonic(), text))
            self._look(False)
            return
        logger.info(f"🎧 for her ({len(text.split())} words, transcribed in {took:.1f}s)")
        self._window_before = self._window_until
        self._window_until = time.monotonic() + self._window_s
        if not await self._take_turn(text):
            self._overheard.append((time.monotonic(), text))
            self._look(False)

    def _for_her(self, text: str) -> bool:
        return (time.monotonic() < self._window_until or text.rstrip(" \"'”’)").endswith("?")
                or bool(self._name().search(text)))

    @staticmethod
    def _name() -> re.Pattern:
        """Her name, also as speech-to-text may spell it ("Lolla")."""
        from app.persona import CONFIG   # late import: persona imports pipecat
        letters = [re.escape(c) + "+" for c in CONFIG.second_name.strip() or "Lola"]
        return re.compile(r"\b" + "".join(letters) + r"\b", re.I)

    def _look(self, at_him: bool) -> None:
        """The page shows her listening while he talks to her (the device always listens)."""
        if at_him != self._looking:
            self._looking = at_him
            self._page({"type": "phase", "value": "listening" if at_him else "idle"})

    # ---------- what she's told ----------

    def note_reply(self, reply: str) -> None:
        """Her answer to a turn, before it's spoken."""
        self._quiet = is_quiet(reply)
        if self._quiet:
            logger.info("🎧 she stays quiet")

    def take_overheard(self) -> str:
        """What she overheard since last time, as a hidden note with the next turn."""
        now = time.monotonic()
        lines = [text for at, text in self._overheard if now - at < OVERHEARD_S]
        self._overheard.clear()
        if not lines:
            return ""
        return ("(Overheard from Espen before this, not necessarily said to you: "
                + " / ".join(lines) + ")\n\n")

    def instructions(self) -> str:
        """For her notes, at the start of a conversation."""
        if not self.available:
            return ""
        return ("\n\n# Listening\n"
                "Usually you only hear Espen after he wakes you. When he asks you to stay active, stay with him "
                "or keep listening, use [stay active]: then you keep hearing him without a wake word. When he's "
                "done with that or asks you to stop, use [stop listening]."
                + ("\n" + self.situation() if self.on else ""))

    def situation(self) -> str:
        """What holds right now (she's told when it changes)."""
        if self.on:
            return ("You're in active mode: you hear what Espen says without a wake word, also when he's talking "
                    "to somebody else or to himself. Answer what's for you, or when you really have something to "
                    "add. Otherwise your whole reply is just [quiet] (the one tag that may stand alone): nobody "
                    "hears anything then, and you needn't explain it.")
        return "Active mode is over: you only hear Espen after he wakes you." if self._ever_on else ""


ACTIVE = ActiveMode()
