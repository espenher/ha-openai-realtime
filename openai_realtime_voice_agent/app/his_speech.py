"""Finds what Espen says in an open mic, and leaves out everybody and everything else.

feed() gets the mic as it comes and returns what it found: "started" when he begins to
talk, then the whole Utterance when he's done (or Unknown, for speech that isn't his).
Two things make it out as his:

- While there's speech (anyone's), the last VOICE_S of it are compared with his voice
  print every VOICE_EVERY_S. His utterance is where that fits.
- It ends at a pause: PAUSE_S after the speech stops, Smart Turn says whether he sounded
  finished. If not, he gets up to LONG_PAUSE_S to go on. When something else keeps talking
  (a video), there is no pause, so it ends once his voice hasn't been heard for GONE_S.

It's told (lenient) when he's likely to be answering her, since a short "yeah" has too
little in it to be sure about.

No asyncio in here: active_mode.py runs it in a thread, since the models take a while.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.voice_models import RATE, SpeechDetector, TurnEnd, VoicePrint

LIMIT = 0.30            # how much a voice must sound like his
GOING_ON_LIMIT = 0.20   # ... while he's talking already
LENIENT_LIMIT = 0.20    # for a short answer to her
SHORT_S = 1.0           # shorter than this is "short"
SHORTEST_S = 0.3        # shorter than this is nothing
VOICE_S = 1.5
VOICE_EVERY_S = 0.5
PAUSE_S = 0.4
LONG_PAUSE_S = 2.0
GONE_S = 1.5
LONGEST_S = 30.0
FINISHED = 0.5          # Smart Turn's chance from which he's finished
BACK_S = 1.0            # his speech may have begun this long before his voice was made out
BEFORE_S = 0.3          # kept from before his speech, and after it
SPEECH_ON, SPEECH_OFF = 0.5, 0.35
KEEP_S = LONGEST_S + 10


@dataclass
class Utterance:
    audio: np.ndarray    # 16 kHz, from -1 to 1
    at: int              # where it began, in samples since the start (or reset)
    seconds: float       # of speech, from its beginning to its end
    score: float         # how much it sounds like him, at best
    ended: str           # "finished", "long pause", "other voice" or "too long"


@dataclass
class Unknown:
    """A stretch of speech that wasn't taken to be his."""
    seconds: float
    score: float         # how much it sounds like him, at best


class HisSpeech:
    def __init__(self, detector: SpeechDetector, voice: VoicePrint, turn_end: TurnEnd) -> None:
        self._detector, self._voice, self._turn_end = detector, voice, turn_end
        self.lenient = False
        self.reset()

    def reset(self) -> None:
        """Forget what was heard so far."""
        self._detector.reset()
        self._kept = np.zeros(0, np.float32)
        self._kept_from = 0        # which sample _kept starts at
        self._now = 0              # samples heard
        self._waiting = np.zeros(0, np.float32)
        self._speech = False
        self._from = 0             # where the current stretch of speech began
        self._until = 0            # where the last speech was
        self._next_voice = 0       # when to compare the voice next
        self._asked = False        # Smart Turn was asked about this pause
        self._ended = True         # the last stretch has been dealt with
        self._begin: int | None = None   # where his utterance began, while there is one
        self._his_until = 0        # where his voice was last heard
        self._best = 0.0
        self._closest = 0.0        # the same, for a stretch that isn't his (so far)

    @property
    def talking(self) -> bool:
        return self._begin is not None

    def feed(self, audio: np.ndarray) -> list:
        found: list = []
        self._waiting = np.concatenate([self._waiting, audio.astype(np.float32)])
        size = SpeechDetector.SIZE
        while len(self._waiting) >= size:
            piece, self._waiting = self._waiting[:size], self._waiting[size:]
            self._step(piece, found)
        return found

    # ---------- one piece ----------

    def _step(self, piece: np.ndarray, found: list) -> None:
        self._kept = np.concatenate([self._kept, piece])
        self._now += len(piece)
        if len(self._kept) > (KEEP_S + 5) * RATE:   # (trimmed now and then, not every time)
            drop = len(self._kept) - int(KEEP_S * RATE)
            self._kept, self._kept_from = self._kept[drop:], self._kept_from + drop

        chance = self._detector(piece)
        if chance >= (SPEECH_OFF if self._speech else SPEECH_ON):
            if not self._speech:
                self._speech, self._asked = True, False
                if self._ended or self._now - self._until > PAUSE_S * RATE:   # a new stretch
                    self._from, self._ended, self._closest = self._now - len(piece), False, 0.0
                    self._next_voice = self._from + int(SHORT_S * RATE)
            self._until = self._now
        else:
            self._speech = False

        if not self._ended and self._speech and self._now >= self._next_voice:
            self._next_voice = self._now + int(VOICE_EVERY_S * RATE)
            limit = LIMIT if self._begin is None else GOING_ON_LIMIT
            self._compare(max(self._from, self._now - int(VOICE_S * RATE)), self._now, limit, found)
        if not self._ended and self._now - self._until >= PAUSE_S * RATE:
            self._pause(found)
        if self._begin is not None:
            if self._speech and self._now - max(self._his_until, self._from) > GONE_S * RATE:
                self._finish("other voice", self._his_until, found)
            elif self._now - self._begin > LONGEST_S * RATE:
                self._finish("too long", self._now, found)

    def _compare(self, start: int, end: int, limit: float, found: list) -> None:
        score = self._voice.score(self._piece(start, end))
        self._closest = max(self._closest, score)
        if score < limit:
            return
        self._his_until, self._best = end, max(self._best, score)
        if self._begin is None:
            # From where the speech began, unless something else was talking before him.
            self._begin = max(self._from, start - int(BACK_S * RATE))
            found.append("started")

    def _pause(self, found: list) -> None:
        """No speech for PAUSE_S (or longer)."""
        if not self._asked:
            self._asked = True
            seconds = (self._until - self._from) / RATE
            if self._begin is None or self._his_until < self._until:   # the last bit, or a short stretch
                if seconds >= SHORTEST_S:
                    limit = (GOING_ON_LIMIT if self._begin is not None
                             else LENIENT_LIMIT if self.lenient and seconds < SHORT_S else LIMIT)
                    self._compare(max(self._from, self._until - int(VOICE_S * RATE)), self._until, limit, found)
            if self._begin is None:   # nobody he knows
                self._ended = True
                if seconds >= SHORTEST_S:
                    found.append(Unknown(seconds, self._closest))
                return
            stretch = self._piece(max(self._from, self._begin), self._now)
            if self._turn_end(stretch) >= FINISHED:
                self._finish("finished", self._until, found)
                return
        if self._begin is None:
            self._ended = True
        elif self._now - self._until >= LONG_PAUSE_S * RATE:
            self._finish("long pause", self._until, found)

    def _finish(self, how: str, end: int, found: list) -> None:
        spare = int(BEFORE_S * RATE)
        audio = self._piece(self._begin - spare, min(end + spare, self._now))
        found.append(Utterance(audio, self._begin, (end - self._begin) / RATE, self._best, how))
        self._begin, self._best, self._ended = None, 0.0, True
        if self._speech:   # somebody is still talking: a stretch of its own
            self._from, self._ended, self._asked, self._closest = end, False, False, 0.0
            self._next_voice = self._now

    def _piece(self, start: int, end: int) -> np.ndarray:
        return self._kept[max(0, start - self._kept_from):max(0, end - self._kept_from)]
