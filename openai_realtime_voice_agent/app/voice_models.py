"""The small models active mode listens with (all on this machine, on the CPU):

- SpeechDetector (Silero): is this 32 ms speech, anyone's?
- VoicePrint (3D-Speaker CAM++): whose voice is it? Compared with Espen's print.
- TurnEnd (Smart Turn): was that the end of what he's saying, or just a pause?

They only need numpy and onnxruntime: the sound features each model wants are worked out
here (checked against kaldi's and Whisper's own). Silero and Smart Turn come with
pipecat; the voice model is fetched when the add-on is built (Dockerfile).
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16000
VOICE_MODEL = Path(os.environ.get("VOICE_MODEL", "/models/speaker.onnx"))
TURN_LOOK_S = 8   # Smart Turn hears the last 8 seconds


def _session(path, threads: int = 1):
    import onnxruntime
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return onnxruntime.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def _pipecat_file(package: str, name: str) -> Path:
    return Path(str(resources.files(package).joinpath(name)))


class SpeechDetector:
    """The chance that a piece of 512 samples (32 ms) is speech."""

    SIZE = 512
    CONTEXT = 64

    def __init__(self, path=None) -> None:
        self._model = _session(path or _pipecat_file("pipecat.audio.vad.data", "silero_vad.onnx"))
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), np.float32)
        self._before = np.zeros((1, self.CONTEXT), np.float32)

    def __call__(self, piece: np.ndarray) -> float:
        x = np.concatenate([self._before, piece[None].astype(np.float32)], axis=1)
        chance, self._state = self._model.run(None, {"input": x, "state": self._state, "sr": np.array(RATE, np.int64)})
        self._before = x[:, -self.CONTEXT:]
        return float(chance[0][0])


# ---------- whose voice ----------

@lru_cache
def _kaldi_mels(size: int, bins: int, low: float, high: float) -> np.ndarray:
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
    edges = np.linspace(mel(low), mel(high), bins + 2)
    at = mel(np.arange(size // 2) * RATE / size)
    up = (at[None] - edges[:-2, None]) / (edges[1:-1] - edges[:-2])[:, None]
    down = (edges[2:, None] - at[None]) / (edges[2:] - edges[1:-1])[:, None]
    return np.maximum(0.0, np.minimum(up, down)).astype(np.float32)


def kaldi_features(audio: np.ndarray, bins: int = 80) -> np.ndarray:
    """Kaldi's filter banks (25 ms windows every 10 ms), one row per window."""
    length, step, size = RATE // 40, RATE // 100, 512
    count = (len(audio) + step // 2) // step
    # Windows are centred on their step; the ends are mirrored.
    at = np.arange(count)[:, None] * step + step // 2 - length // 2 + np.arange(length)[None]
    at = np.where(at < 0, -at - 1, at)
    at = np.where(at >= len(audio), 2 * len(audio) - 1 - at, at)
    frames = audio[np.clip(at, 0, len(audio) - 1)].astype(np.float32)
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(length) / (length - 1))) ** 0.85
    power = np.abs(np.fft.rfft(frames * window.astype(np.float32), size)) ** 2
    energy = power[:, : size // 2] @ _kaldi_mels(size, bins, 20.0, RATE / 2 - 400.0).T
    return np.log(np.maximum(energy, np.finfo(np.float32).eps)).astype(np.float32)


class VoicePrint:
    """Turns speech into a print of the voice (a row of numbers), and compares it with Espen's."""

    def __init__(self, path=None) -> None:
        self._model = _session(path or VOICE_MODEL)
        self._input = self._model.get_inputs()[0].name
        self._scale = 1.0 if self._model.get_modelmeta().custom_metadata_map.get("normalize_samples", "1") == "1" else 32768.0
        self.known: np.ndarray | None = None   # one row per print of his

    def of(self, audio: np.ndarray) -> np.ndarray:
        features = kaldi_features(audio * self._scale)
        features -= features.mean(axis=0, keepdims=True)
        print_ = self._model.run(None, {self._input: features[None]})[0][0]
        return print_ / (np.linalg.norm(print_) + 1e-9)

    def load(self, path: Path) -> bool:
        """His prints, from a file {"prints": [[...], ...]}; False if there's none (yet)."""
        try:
            prints = np.array(json.loads(path.read_text(encoding="utf-8"))["prints"], np.float32)
            self.known = prints / np.linalg.norm(prints, axis=1, keepdims=True)
        except (OSError, ValueError, KeyError, TypeError) as e:
            if path.exists():
                logger.warning(f"🎧 voice print not readable: {e!r}")
            self.known = None
        return self.known is not None

    def score(self, audio: np.ndarray) -> float:
        """How much it sounds like him: 1 is identical, around 0 is somebody else."""
        return float((self.known @ self.of(audio)).max())


# ---------- end of a turn ----------

@lru_cache
def _whisper_mels(bins: int = 80, size: int = 400) -> np.ndarray:
    """Slaney's mel filters, as Whisper uses them."""
    def mel(f):
        f = np.asarray(f, np.float64)
        return np.where(f >= 1000, 15 + np.log(np.maximum(f, 1e-9) / 1000) * 27 / np.log(6.4), f * 3 / 200)

    def hertz(m):
        return np.where(m >= 15, 1000 * np.exp((m - 15) * np.log(6.4) / 27), m * 200 / 3)

    edges = hertz(np.linspace(mel(0), mel(RATE / 2), bins + 2))
    at = np.linspace(0, RATE / 2, size // 2 + 1)
    up = (at[None] - edges[:-2, None]) / (edges[1:-1] - edges[:-2])[:, None]
    down = (edges[2:, None] - at[None]) / (edges[2:] - edges[1:-1])[:, None]
    return np.maximum(0, np.minimum(up, down)) * (2 / (edges[2:] - edges[:-2]))[:, None]


def whisper_features(audio: np.ndarray) -> np.ndarray:
    """Whisper's log-mel features of TURN_LOOK_S seconds: (80, 800)."""
    audio = audio.astype(np.float64)
    audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    size, step = 400, 160
    padded = np.pad(audio, size // 2, mode="reflect")
    count = 1 + (len(padded) - size) // step
    frames = padded[np.arange(count)[:, None] * step + np.arange(size)[None]]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(size) / size)
    power = np.abs(np.fft.rfft(frames * window, size)) ** 2
    log = np.log10(np.maximum(_whisper_mels() @ power.T, 1e-10))[:, :-1]
    return ((np.maximum(log, log.max() - 8) + 4) / 4).astype(np.float32)


class TurnEnd:
    """The chance that he's finished, given the stretch of speech up to now and a little silence."""

    def __init__(self, path=None) -> None:
        self._model = _session(path or _pipecat_file("pipecat.audio.turn.smart_turn.data", "smart-turn-v3.1-cpu.onnx"))

    def __call__(self, audio: np.ndarray) -> float:
        size = TURN_LOOK_S * RATE
        audio = audio[-size:]
        audio = np.pad(audio, (size - len(audio), 0))
        return float(self._model.run(None, {"input_features": whisper_features(audio)[None]})[0][0][0])
