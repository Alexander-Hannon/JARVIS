"""Voice pipeline tests.

`slow` tests run the REAL local faster-whisper model on REAL recorded speech (Windows SAPI synthesized
WAV, transcoded to WebM/Opus and MP4/AAC with FFmpeg via PyAV). They are not fake transcript injection.
Decoder negative tests are separate from any transcript-level logic."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

AUDIO = Path(__file__).parent / "fixtures" / "audio"


def test_decode_all_browser_formats_real_audio(env):
    from jarvis.voice.stt import decode_audio
    durs = {}
    for name in ("scenario1.wav", "scenario1.webm", "scenario1.m4a"):
        d = decode_audio((AUDIO / name).read_bytes())
        assert d.samples.dtype.name == "float32" and d.samples.ndim == 1
        durs[name] = d.duration_s
        assert 8.0 < d.duration_s < 9.5, (name, d.duration_s)
        assert float(abs(d.samples).max()) > 0.05       # actual signal, not silence
    assert {n.split(".")[1] for n in durs} == {"wav", "webm", "m4a"}


@pytest.mark.parametrize("payload,code", [(b"", "empty_audio"), (b"not audio at all" * 50, "decode_failed")])
def test_decode_errors_are_explicit(env, payload, code):
    from jarvis.voice.stt import AudioError, decode_audio
    with pytest.raises(AudioError) as e:
        decode_audio(payload)
    assert e.value.code == code


def test_decode_limits(env):
    from jarvis.voice.stt import AudioError, decode_audio
    data = (AUDIO / "scenario1.wav").read_bytes()
    with pytest.raises(AudioError) as e:
        decode_audio(data, max_bytes=1000)
    assert e.value.code == "too_large"
    with pytest.raises(AudioError) as e:
        decode_audio(data, max_seconds=3)
    assert e.value.code == "too_long"


def test_unsupported_mime_rejected(env):
    from jarvis.voice.stt import AudioError, check_mime
    check_mime("audio/webm;codecs=opus")
    check_mime("audio/mp4")
    with pytest.raises(AudioError):
        check_mime("application/x-msdownload")


def test_stt_unavailable_is_not_a_reasoning_error(client):
    from jarvis.voice import stt

    class Broken(stt.WhisperEngine):
        def _ensure(self):
            raise stt.STTUnavailable("model download blocked (test)")
    stt.set_engine(Broken())
    try:
        r = client.c.post("/api/captures/audio", files={"file": ("a.wav", (AUDIO / "approve.wav").read_bytes(), "audio/wav")},
                          data={"captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"},
                          headers={"X-CSRF-Token": client.csrf})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "stt_unavailable"
        # typed commands still work
        ok = client.post("/api/commands", {"text": "buy milk tomorrow", "captured_at": "2026-09-28T20:00:00Z"})
        assert ok.status_code == 200
    finally:
        stt.set_engine(None)


@pytest.mark.slow
@pytest.mark.parametrize("name,mime", [("scenario1.wav", "audio/wav"), ("scenario1.webm", "audio/webm;codecs=opus"),
                                       ("scenario1.m4a", "audio/mp4")])
def test_real_transcription_each_format(client, name, mime):
    r = client.c.post("/api/captures/audio", files={"file": (name, (AUDIO / name).read_bytes(), mime)},
                      data={"captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"},
                      headers={"X-CSRF-Token": client.csrf})
    assert r.status_code == 200, r.text
    t = r.json()["transcript"]
    low = t["text"].lower()
    for w in ("grade", "640", "homework", "tomorrow", "follow up", "iflex", "thursday", "groceries", "tonight"):
        assert w in low, (w, t["text"])
    assert t["source"] == "stt" and t["confidence"] and t["latency_ms"] > 0
    cap = r.json()
    assert cap["audio"]["deleted_at"] and not cap["audio"]["retained"]     # raw audio not kept by default


@pytest.mark.slow
def test_s1_real_audio_end_to_end_with_voice_correction_and_approval(client, fakes):
    """Scenario 1+2+3 driven by actual recorded audio → editable transcript → proposal → spoken
    correction → spoken approval → execution (contract fakes) → verified receipts."""
    freeze(CHICAGO_AFTERNOON)

    def upload(name, conv=None):
        r = client.c.post("/api/captures/audio", files={"file": (name, (AUDIO / name).read_bytes(), "audio/wav")},
                          data={"captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago", "conversation_id": conv or ""},
                          headers={"X-CSRF-Token": client.csrf})
        assert r.status_code == 200, r.text
        return r.json()

    cap = upload("scenario1.wav")
    conv = cap["conversation_id"]
    # the transcript is editable: fix STT casing of the brand name, creating version 2
    fixed = cap["transcript"]["text"].replace("iFLEX", "iFlex").replace("Lead", "lead")
    e = client.post(f"/api/captures/{cap['capture_id']}/transcript", {"text": fixed}).json()
    assert e["transcript"]["version"] == 2 and e["transcript"]["source"] == "user_edit"
    r = client.post("/api/commands", {"conversation_id": conv, "capture_id": cap["capture_id"], "transcript_version": 2,
                                      "channel": "voice", "tz": "America/Chicago"}).json()
    p = r["proposal"]
    assert [i["args"]["content"] for i in p["items"]] == ["Grade IS 640 homework", "Follow up with the iFlex lead",
                                                           "Buy groceries"]
    assert [i["args"]["due_date"] for i in p["items"]] == ["2026-09-29", "2026-10-01", "2026-09-28"]
    corr = upload("correction.wav", conv)
    r2 = client.post("/api/commands", {"conversation_id": conv, "capture_id": corr["capture_id"], "channel": "voice",
                                       "tz": "America/Chicago"}).json()
    assert r2["proposal"]["version"] == 2
    assert r2["proposal"]["items"][1]["args"]["due_date"] == "2026-10-02"
    appr = upload("approve.wav", conv)
    assert appr["transcript"]["text"].lower().startswith("approve")
    r3 = client.post("/api/commands", {"conversation_id": conv, "capture_id": appr["capture_id"], "channel": "voice",
                                       "tz": "America/Chicago", "speech_playing": False}).json()
    assert "Approved 3 item(s) of version 2" in r3["message"]["text"]
    from jarvis.core.db import get_conn
    run_worker(get_conn())
    v = client.get(f"/api/proposals/{p['id']}").json()
    assert [i["state"] for i in v["items"]] == ["verified"] * 3
    assert {t.due["date"][:10] for t in fakes["todoist"].list_tasks()} == {"2026-09-29", "2026-10-02", "2026-09-28"}
    lat = client.get("/api/metrics/latency").json()
    assert lat["transcription"]["count"] == 3 and lat["execution"]["count"] == 3


@pytest.mark.slow
def test_local_jarvis_tts_is_intelligible(client):
    """Local Piper TTS: returns WAV and round-trips through local STT (the HUD effect must not hurt clarity)."""
    st = client.get("/api/voice/tts/status").json()
    assert st["local_only"] and any(p["key"] == "jarvis" for p in st["presets"])
    r = client.post("/api/voice/tts", {"text": "Two tasks are ready for Todoist. Shall I add them?", "preset": "jarvis"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    from jarvis.voice.stt import decode_audio, get_engine
    t = get_engine().transcribe(decode_audio(r.content))
    assert "tasks" in t.text.lower() and "ready" in t.text.lower() and "add them" in t.text.lower()


@pytest.mark.parametrize("raw,expected", [
    ("Yo Jarvis is Ahmad Hardy back at Mizzou? Subs by www.zeoranger.co.uk", "Yo Jarvis is Ahmad Hardy back at Mizzou?"),
    ("Thanks for watching!", ""),
    ("Subtitles by the Amara.org community", ""),
    ("Remind me to grade IS 640 tomorrow.", "Remind me to grade IS 640 tomorrow."),
    ("Check www.alabama.com for the score", "Check for the score"),
])
def test_whisper_hallucinated_credits_are_removed(raw, expected):
    from jarvis.voice.stt import clean_segment
    assert clean_segment(raw, 0.1, -0.3, 1.2) == expected


def test_low_confidence_silence_segment_dropped():
    from jarvis.voice.stt import clean_segment
    assert clean_segment("you", 0.85, -1.2, 1.0) == ""
    assert clean_segment("you you you you you you you you", 0.1, -0.2, 3.1) == ""
