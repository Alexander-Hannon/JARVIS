"""Local speech-to-text.

Decoding: PyAV (bundled FFmpeg libs) handles WebM/Opus (Chromium/Firefox), MP4/AAC (iOS Safari),
Ogg, WAV, MP3. We resample to 16 kHz mono float32 and enforce byte + duration limits BEFORE
running the model. Missing STT is surfaced as an explicit `stt_unavailable` error, never as a
reasoning error or an empty transcript.
"""
from __future__ import annotations

import io
import math
import re
import os
import platform
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from jarvis.config import get_settings

SUPPORTED_MIME_PREFIXES = ("audio/webm", "audio/mp4", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave",
                           "audio/mpeg", "audio/aac", "audio/x-m4a", "video/webm", "video/mp4", "audio/m4a")

# Approximate download sizes (CTranslate2 converted weights) to show before first use.
MODEL_SIZES_MB = {"tiny.en": 75, "base.en": 142, "small.en": 484, "medium.en": 1530, "large-v3": 3100}


# Biases local transcription toward AJ's vocabulary (names, courses, apps). Plain text; never sent anywhere.
VOCAB_PROMPT = ("Jarvis, Todoist, Google Calendar, IS 640, MSC 550, IS 401, IS 501, MKT 601, GTA, iFlex, Rumble, "
                "Yi Tan, DJ Hovermale, CCM, Monk Mode, AI GURU.")


# Whisper sometimes "hears" subtitle credits/outros on silence or noise (artifacts of its training data).
HALLUCINATIONS = re.compile(
    r"(?:\bsub(?:title)?s?\s+(?:by|from)\b.*|\bsubtitles?\s+(?:by|from|provided)\b.*|\bcaptions?\s+by\b.*|"
    r"\bamara\.org\b.*|\bwww\.[\w.-]+\.(?:co\.uk|com|org|net)\b|\bthanks? (?:you )?for watching\b.*|"
    r"\bplease (?:like and )?subscribe\b.*|\bdon'?t forget to (?:like|subscribe)\b.*|\btranscri(?:bed|ption) by\b.*|"
    r"\bcopyright\b.*\d{4}.*)",
    re.IGNORECASE)


def clean_segment(text: str, no_speech_prob: float, avg_logprob: float, compression_ratio: float) -> str:
    """Drop hallucinated segments: the model's own 'probably not speech' signal, repetition loops, credit lines."""
    if no_speech_prob > 0.6 and avg_logprob < -0.8:
        return ""
    if compression_ratio > 2.6:            # repetitive loop ("you you you …")
        return ""
    t = HALLUCINATIONS.sub("", text)
    if t == text:
        return text
    t = re.sub(r"\s{2,}", " ", t).strip(" ,-")
    return t if re.search(r"[A-Za-z0-9]", t) else ""


class AudioError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class STTUnavailable(AudioError):
    def __init__(self, message: str):
        super().__init__("stt_unavailable", message)


@dataclass
class DecodedAudio:
    samples: np.ndarray
    duration_s: float
    source_format: str


@dataclass
class Transcript:
    text: str
    confidence: float | None
    language: str | None
    duration_s: float
    latency_ms: int
    model: str
    segments: list[dict] = field(default_factory=list)


def check_mime(mime: str | None) -> None:
    base = (mime or "").split(";")[0].strip().lower()
    if base and not base.startswith(SUPPORTED_MIME_PREFIXES):
        raise AudioError("unsupported_format", f"Unsupported audio type '{base}'.")


def decode_audio(data: bytes, max_bytes: int | None = None, max_seconds: float | None = None) -> DecodedAudio:
    import av  # local import keeps module import cheap

    s = get_settings()
    max_bytes = max_bytes or s.max_audio_bytes
    max_seconds = max_seconds or s.max_audio_seconds
    if not data:
        raise AudioError("empty_audio", "No audio data was received.")
    if len(data) > max_bytes:
        raise AudioError("too_large", f"Audio exceeds {max_bytes // (1024 * 1024)} MB limit.")
    try:
        container = av.open(io.BytesIO(data), mode="r")
    except Exception as e:  # av.error.InvalidDataError etc.
        raise AudioError("decode_failed", f"Audio could not be decoded ({type(e).__name__}).") from e
    try:
        streams = [st for st in container.streams if st.type == "audio"]
        if not streams:
            raise AudioError("no_audio_stream", "File contains no audio stream.")
        stream = streams[0]
        fmt = f"{container.format.name}/{stream.codec_context.name}"
        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        chunks: list[np.ndarray] = []
        total = 0
        limit = int(max_seconds * 16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                arr = out.to_ndarray().reshape(-1)
                chunks.append(arr)
                total += arr.shape[0]
                if total > limit:
                    raise AudioError("too_long", f"Audio exceeds {max_seconds}s limit.")
        for out in resampler.resample(None):
            arr = out.to_ndarray().reshape(-1)
            chunks.append(arr)
            total += arr.shape[0]
    except AudioError:
        raise
    except Exception as e:
        raise AudioError("decode_failed", f"Audio stream could not be decoded ({type(e).__name__}).") from e
    finally:
        container.close()
    if total == 0:
        raise AudioError("empty_audio", "Audio contained no samples.")
    samples = np.concatenate(chunks).astype(np.float32)
    return DecodedAudio(samples=samples, duration_s=samples.shape[0] / 16000.0, source_format=fmt)


def hardware_profile() -> dict:
    info = {"cpu_count": os.cpu_count(), "machine": platform.machine(), "system": platform.system(), "cuda": False}
    try:
        import ctranslate2

        info["cuda"] = ctranslate2.get_cuda_device_count() > 0
        info["ct2_version"] = ctranslate2.__version__
    except Exception:
        pass
    # Practical defaults: base.en on CPU int8; small.en offered for >=8 cores; GPU profile when CUDA present.
    info["recommended_model"] = "small.en" if info["cuda"] else "base.en"
    return info


class WhisperEngine:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self.state = "not_loaded"   # not_loaded | downloading | loading | ready | error
        self.error: str | None = None
        self.load_ms: int | None = None
        self.warmup_ms: int | None = None
        self.model_name = get_settings().stt_model

    def model_downloaded(self) -> bool:
        root = get_settings().models_dir
        needle = self.model_name.replace("/", "--")
        return any(needle in p.name for p in root.glob("models--*")) if root.exists() else False

    def status(self) -> dict:
        s = get_settings()
        return {
            "engine": s.stt_engine, "model": self.model_name, "device": s.stt_device,
            "compute_type": s.stt_compute_type, "state": self.state, "error": self.error,
            "downloaded": self.model_downloaded(),
            "download_size_mb": MODEL_SIZES_MB.get(self.model_name), "load_ms": self.load_ms,
            "warmup_ms": self.warmup_ms, "local_only": True,
        }

    def _ensure(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            s = get_settings()
            try:
                from faster_whisper import WhisperModel
            except Exception as e:  # pragma: no cover - dependency missing
                self.state, self.error = "error", f"faster-whisper not installed: {e}"
                raise STTUnavailable(self.error) from e
            self.state = "loading" if self.model_downloaded() else "downloading"
            t = time.perf_counter()
            try:
                # Already downloaded → load strictly offline (no Hugging Face call on every start).
                self._model = WhisperModel(self.model_name, device=s.stt_device, compute_type=s.stt_compute_type,
                                           download_root=str(s.models_dir), local_files_only=self.model_downloaded())
            except Exception as e:
                self.state = "error"
                self.error = (f"Could not load/download model '{self.model_name}': {type(e).__name__}: {e}. "
                              f"Remedy: ensure network access to huggingface.co, or place the CTranslate2 model in "
                              f"{s.models_dir} and retry via Settings → Voice → Warm up.")
                raise STTUnavailable(self.error) from e
            self.load_ms = int((time.perf_counter() - t) * 1000)
            self.state, self.error = "ready", None
            return self._model

    def warm_up(self) -> dict:
        model = self._ensure()
        t = time.perf_counter()
        silence = np.zeros(16000, dtype=np.float32)
        list(model.transcribe(silence, language="en")[0])
        self.warmup_ms = int((time.perf_counter() - t) * 1000)
        return self.status()

    def transcribe(self, audio: DecodedAudio) -> Transcript:
        model = self._ensure()
        t = time.perf_counter()
        segments, info = model.transcribe(audio.samples, language="en", vad_filter=True, beam_size=5,
                                          condition_on_previous_text=False, word_timestamps=True,
                                          hotwords=VOCAB_PROMPT if audio.duration_s >= 3.0 else None)
        segs = []
        word_probs: list[float] = []
        logprobs = []
        dropped: list[str] = []
        for sg in segments:
            kept = clean_segment(sg.text.strip(), sg.no_speech_prob, sg.avg_logprob, getattr(sg, "compression_ratio", 1.0))
            if not kept:
                dropped.append(sg.text.strip())
                continue
            if kept != sg.text.strip():
                dropped.append(sg.text.strip())
            segs.append({"start": round(sg.start, 2), "end": round(sg.end, 2), "text": kept,
                         "avg_logprob": sg.avg_logprob, "no_speech_prob": sg.no_speech_prob})
            logprobs.append(sg.avg_logprob)
            word_probs += [w.probability for w in (sg.words or [])]
        text = " ".join(s["text"] for s in segs).strip()
        # Confidence = mean word probability (more meaningful than exp(avg_logprob) for 1–2 word utterances).
        conf = (sum(word_probs) / len(word_probs)) if word_probs else (
            math.exp(sum(logprobs) / len(logprobs)) if logprobs else None)
        return Transcript(text=text, confidence=round(conf, 3) if conf is not None else None,
                          language=info.language, duration_s=round(audio.duration_s, 2),
                          latency_ms=int((time.perf_counter() - t) * 1000), model=self.model_name, segments=segs)


_engine: WhisperEngine | None = None


def get_engine() -> WhisperEngine:
    global _engine
    if _engine is None:
        _engine = WhisperEngine()
    return _engine


def set_engine(engine) -> None:
    """Tests may install an alternative engine; it must be labeled as such in its status()."""
    global _engine
    _engine = engine
