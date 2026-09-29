"""Local neural text-to-speech (Piper). Nothing leaves the machine.

Presets give Jarvis a calm, refined British "AI butler" delivery. They are generic synthetic voices from the
open Piper voice set — deliberately NOT a clone of any actor's voice.

The optional "hud" effect adds a very short, quiet slap-back echo and a gentle presence lift so the voice reads
as a synthetic assistant without hurting intelligibility.
"""
from __future__ import annotations

import io
import re
import threading
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from jarvis.config import get_settings

VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices"


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    model: str
    speaker: int | None = None
    length_scale: float = 1.0      # >1 = slower, more measured
    hud: bool = True


PRESETS: dict[str, Preset] = {
    "jarvis": Preset("jarvis", "Jarvis — refined British (default)", "en_GB-alan-medium", None, 1.06, True),
    "jarvis-dry": Preset("jarvis-dry", "Jarvis — refined British, no effect", "en_GB-alan-medium", None, 1.06, False),
    "obadiah": Preset("obadiah", "Obadiah — deep British", "en_GB-semaine-medium", 2, 1.04, True),
    "northern": Preset("northern", "Northern English male", "en_GB-northern_english_male-medium", None, 1.0, True),
}
MAX_CHARS = 700
# Pronunciation fixes for names the voice mangles (display text is unaffected).
LEXICON = [(r"\bTodoist\b", "To-do-ist"), (r"\bIS[ -]?(\d{3})\b", r"I.S. \1"), (r"\bMSC[ -]?(\d{3})\b", r"M.S.C. \1"),
           (r"\bMKT[ -]?(\d{3})\b", r"marketing \1"), (r"\bGTA\b", "G.T.A."), (r"\bCCM\b", "C.C.M."),
           (r"\biFlex\b", "eye-flex"), (r"\bJARVIS\b", "Jarvis")]


class TTSUnavailable(Exception):
    pass


def _voices_dir():
    return get_settings().models_dir / "piper"


def model_available(model: str) -> bool:
    d = _voices_dir()
    return (d / f"{model}.onnx").exists() and (d / f"{model}.onnx.json").exists()


def download(model: str) -> None:
    from piper.download_voices import download_voice
    d = _voices_dir()
    d.mkdir(parents=True, exist_ok=True)
    download_voice(model, d)


def _hud(audio: np.ndarray, sr: int) -> np.ndarray:
    x = audio.astype(np.float32)
    # presence lift: add a little of the first difference (a gentle high-shelf)
    diff = np.concatenate([[0.0], np.diff(x)]).astype(np.float32)
    y = x + 0.35 * diff
    # 22 ms slap-back at -20 dB
    d = int(0.022 * sr)
    echo = np.zeros_like(y)
    echo[d:] = y[:-d] * 0.1
    y = y + echo
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / peak * 0.92).astype(np.float32)


class TTSEngine:
    def __init__(self) -> None:
        self._voices: dict[str, object] = {}
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.last_ms: int | None = None

    def status(self) -> dict:
        return {"engine": "piper", "local_only": True, "last_synthesis_ms": self.last_ms,
                "presets": [{"key": p.key, "label": p.label, "model": p.model, "downloaded": model_available(p.model)}
                            for p in PRESETS.values()],
                "note": "Generic open Piper voices (not a clone of any real person's voice)."}

    def _voice(self, model: str):
        with self._lock:
            if model not in self._voices:
                if not model_available(model):
                    try:
                        download(model)
                    except Exception as e:  # noqa: BLE001
                        raise TTSUnavailable(f"Voice '{model}' isn't downloaded and download failed ({type(e).__name__}). "
                                             f"Run `uv run jarvis tts-download` with internet access.") from e
                from piper import PiperVoice
                d = _voices_dir()
                self._voices[model] = PiperVoice.load(d / f"{model}.onnx", config_path=d / f"{model}.onnx.json")
            return self._voices[model]

    def synthesize(self, text: str, preset: str = "jarvis") -> bytes:
        text = " ".join(text.split())[:MAX_CHARS]
        for pat, rep in LEXICON:
            text = re.sub(pat, rep, text, flags=re.IGNORECASE)
        if not text:
            raise ValueError("empty text")
        p = PRESETS.get(preset, PRESETS["jarvis"])
        key = (p.key, text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        from piper import SynthesisConfig
        voice = self._voice(p.model)
        t = time.perf_counter()
        cfg = SynthesisConfig(speaker_id=p.speaker, length_scale=p.length_scale, noise_scale=0.6, noise_w_scale=0.7)
        chunks = list(voice.synthesize(text, syn_config=cfg))
        sr = chunks[0].sample_rate if chunks else 22050
        audio = np.concatenate([c.audio_float_array for c in chunks]) if chunks else np.zeros(1, np.float32)
        if p.hud:
            audio = _hud(audio, sr)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
        out = buf.getvalue()
        self.last_ms = int((time.perf_counter() - t) * 1000)
        self._cache[key] = out
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return out


_engine: TTSEngine | None = None


def get_tts() -> TTSEngine:
    global _engine
    if _engine is None:
        _engine = TTSEngine()
    return _engine
