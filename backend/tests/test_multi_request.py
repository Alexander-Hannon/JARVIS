"""One utterance, several different requests → each routed to its own specialist (reported bug: everything
became Todoist tasks). Weather is mocked with respx (no network); calendar/Todoist are contract fakes."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from conftest import freeze

AJ = ("Yo Jarvis, go ahead and make a to-do-is task for me to complete my Grading for my two classes tomorrow Go ahead "
      "and give me a debrief on what my Google Calendar looks like and then tell me What the weather will look like tomorrow")
NOW = datetime(2026, 9, 29, 22, 26, tzinfo=UTC)   # Tue 5:26 PM CDT
AUDIO = Path(__file__).parent / "fixtures" / "audio"

FORECAST = {"daily": {"time": ["2026-09-29", "2026-09-30", "2026-10-01"], "weather_code": [0, 61, 3],
                      "temperature_2m_max": [84.2, 78.9, 75.0], "temperature_2m_min": [63.1, 60.4, 58.0],
                      "precipitation_probability_max": [5, 70, 20], "wind_speed_10m_max": [8.0, 12.3, 6.0]}}


@pytest.fixture
def weather_api():
    from jarvis.agents import weather
    weather._cache.clear()
    with respx.mock(assert_all_called=False) as m:
        route = m.get("https://api.open-meteo.com/v1/forecast").mock(return_value=httpx.Response(200, json=FORECAST))
        yield route


def _seed_calendar(fakes):
    gc = fakes["calendar"]
    gc.add_event("primary", {"id": "e1", "summary": "IS 640 GTA shift", "start": {"dateTime": "2026-09-29T18:00:00-05:00"},
                             "end": {"dateTime": "2026-09-29T20:00:00-05:00"}})
    gc.add_event("primary", {"id": "e2", "summary": "CCM leadership", "start": {"dateTime": "2026-09-29T20:30:00-05:00"},
                             "end": {"dateTime": "2026-09-29T21:30:00-05:00"}})


def test_segmentation_of_reported_utterance():
    from jarvis.agents.interpret import classify, segment_utterance
    segs = segment_utterance(AJ)
    kinds = [classify(s, has_active_proposal=False, has_attachments=False, captured_at=NOW, tz="America/Chicago").intent
             for s in segs]
    assert kinds == ["capture", "agenda", "weather"]


@pytest.mark.parametrize("text", ["ask Yi Tan what is due Friday", "grade IS 640 homework tomorrow and buy groceries tonight",
                                  "call mom and email Yi Tan about the rubric"])
def test_task_lists_are_not_split_into_questions(text):
    from jarvis.agents.interpret import segment_utterance
    assert len(segment_utterance(text)) == 1


def test_multi_request_routes_each_part(client, fakes, weather_api):
    freeze(NOW)
    _seed_calendar(fakes)
    r = client.post("/api/commands", {"text": AJ, "captured_at": "2026-09-29T22:26:00Z", "tz": "America/Chicago"}).json()
    assert r["intent"] == "multi"
    assert set(r["specialists"]) >= {"tasks", "calendar", "weather"}
    t = r["message"]["text"]
    # 1) exactly ONE Todoist task, with the right content and date — no "Go ahead", calendar or weather tasks
    items = r["proposal"]["items"]
    assert [(i["action_type"], i["args"]["content"], i["args"]["due_date"]) for i in items] == [
        ("task.create", "Complete my Grading for my two classes", "2026-09-30")]
    # 2) calendar rundown from the (fake) connected calendar
    assert "▸ Calendar" in t and "IS 640 GTA shift" in t and "CCM leadership" in t
    # 3) weather for tomorrow (Wed Sep 30 → 70% rain)
    assert "▸ Weather" in t and "high 79°F / low 60°F" in t and "70% chance of rain" in t and "umbrella" in t
    q = weather_api.calls[0].request.url.params
    assert q["latitude"] == "34.7304" and q["timezone"] == "America/Chicago"
    spoken = r["message"]["spoken_text"]
    assert "task" in spoken and "event" in spoken and "rain" in spoken


def test_single_request_behavior_unchanged(client, fakes, weather_api):
    freeze(NOW)
    r = client.post("/api/commands", {"text": "what's the weather tomorrow?", "captured_at": "2026-09-29T22:26:00Z"}).json()
    assert r["intent"] == "weather" and r["proposal"] is None and "light rain" in r["message"]["text"]
    r2 = client.post("/api/commands", {"text": "what does my calendar look like today", "captured_at": "2026-09-29T22:26:00Z"}).json()
    assert r2["intent"] == "agenda" and r2["proposal"] is None


def test_weather_outage_is_explicit(client, fakes):
    from jarvis.agents import weather
    weather._cache.clear()
    freeze(NOW)
    with respx.mock() as m:
        m.get("https://api.open-meteo.com/v1/forecast").mock(side_effect=httpx.ConnectTimeout("down"))
        r = client.post("/api/commands", {"text": "what's the weather tomorrow", "captured_at": "2026-09-29T22:26:00Z"}).json()
    assert "unreachable" in r["message"]["text"] and "°F" not in r["message"]["text"]


def test_calendar_not_connected_is_truthful(client, weather_api):
    freeze(NOW)
    r = client.post("/api/commands", {"text": AJ, "captured_at": "2026-09-29T22:26:00Z"}).json()
    assert "can't read your calendar" in r["message"]["text"]
    assert "nothing on your selected calendars" not in r["message"]["text"]


@pytest.mark.slow
def test_multi_request_from_real_speech(client, fakes, weather_api):
    freeze(NOW)
    _seed_calendar(fakes)
    cap = client.c.post("/api/captures/audio", files={"file": ("m.wav", (AUDIO / "multi_request.wav").read_bytes(), "audio/wav")},
                        data={"captured_at": "2026-09-29T22:26:00Z", "tz": "America/Chicago"},
                        headers={"X-CSRF-Token": client.csrf}).json()
    assert "transcript" in cap, cap
    assert "todoist" in cap["transcript"]["text"].lower()
    r = client.post("/api/commands", {"capture_id": cap["capture_id"], "channel": "voice", "tz": "America/Chicago"}).json()
    assert [i["args"]["content"].lower() for i in r["proposal"]["items"]] == ["complete my grading for my two classes"]
    assert "▸ Calendar" in r["message"]["text"] and "▸ Weather" in r["message"]["text"]


def test_spoken_text_never_contains_markdown_symbols():
    from jarvis.agents.chief import speechify
    out = speechify("**Tomorrow**: *light rain* — high 79°F / low 60°F.\n▸ Calendar\n  • 6:00 PM — IS 640\n## Note\n"
                    "See [Syllabus · page 2] and https://example.com/x")
    for bad in ("*", "#", "▸", "•", "—", "http", "·", "/"):
        assert bad not in out, (bad, out)
    assert "degrees" in out and "light rain" in out


def test_model_answer_markdown_is_not_spoken(client, fakes):
    from jarvis.llm.provider import ScriptedLLM, set_provider
    set_provider(ScriptedLLM(["**Focus:** finish the **IS 640** grading first.\n- then *MKT 601*"]))
    r = client.post("/api/commands", {"text": "what should I prioritize tonight?", "captured_at": "2026-09-29T22:26:00Z"}).json()
    assert "*" not in r["message"]["spoken_text"] and "finish the IS 640 grading first" in r["message"]["spoken_text"]


def test_replies_carry_visual_cards(client, fakes, weather_api):
    freeze(NOW)
    _seed_calendar(fakes)
    r = client.post("/api/commands", {"text": AJ, "captured_at": "2026-09-29T22:26:00Z", "tz": "America/Chicago"}).json()
    kinds = {v["type"] for v in r["visuals"]}
    assert {"calendar", "weather"} <= kinds
    cal = next(v for v in r["visuals"] if v["type"] == "calendar")
    assert [e["summary"] for e in cal["days"][0]["events"]] == ["IS 640 GTA shift", "CCM leadership"]
    assert cal["days"][0]["events"][0]["start"].startswith("2026-09-29T18:00:00-05:00")
    wx = next(v for v in r["visuals"] if v["type"] == "weather")
    assert wx["focus"] == "2026-09-30" and len(wx["days"]) == 3
    today = client.get("/api/today").json()
    assert today["calendar_visual"]["type"] == "calendar"


def test_tts_rate_changes_speed_not_pitch(client):
    from jarvis.voice.tts import get_tts
    slow = get_tts().synthesize("Good evening, sir.", "jarvis", 0.8)
    fast = get_tts().synthesize("Good evening, sir.", "jarvis", 1.5)
    assert len(fast) < len(slow) * 0.75          # shorter audio = faster speech, synthesized (natural pitch)
