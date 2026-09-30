"""Structured visual payloads returned alongside a reply (rendered as cards in the UI: calendar timeline, weather,
tasks, free slots). They carry only data Jarvis actually read — no decorative/fake content."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.agents import context as ctx


def calendar(conn, start: date, days: int, tz: str, cal: dict | None = None) -> dict:
    days = max(1, min(days, 7))
    s, _ = ctx.day_bounds(start, tz)
    _, e = ctx.day_bounds(start + timedelta(days=days - 1), tz)
    cal = cal or ctx.events_between(conn, s, e)
    zi = ZoneInfo(tz)
    out_days = []
    for i in range(days):
        d = start + timedelta(days=i)
        evs = []
        for ev in cal.get("events", []):
            if ev["all_day"]:
                if ev["start"][:10] <= d.isoformat() < (ev["end"] or ev["start"])[:10] or ev["start"][:10] == d.isoformat():
                    evs.append({"summary": ev["summary"], "all_day": True, "calendar": ev.get("calendar")})
                continue
            st = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).astimezone(zi)
            en = datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).astimezone(zi)
            if st.date() == d or (st.date() < d <= en.date()):
                evs.append({"summary": ev["summary"], "start": st.isoformat(), "end": en.isoformat(), "all_day": False,
                            "busy": ev["busy"], "calendar": ev.get("calendar")})
        wins = ctx.work_windows([x for x in cal.get("events", [])], d, tz) if cal.get("status") in ("ok", "partial") else []
        out_days.append({"date": d.isoformat(), "label": d.strftime("%a %b %-d"), "events": evs,
                         "windows": [{"start": datetime.fromisoformat(w["start"].replace("Z", "+00:00")).astimezone(zi).isoformat(),
                                      "end": datetime.fromisoformat(w["end"].replace("Z", "+00:00")).astimezone(zi).isoformat()}
                                     for w in wins]})
    return {"type": "calendar", "tz": tz, "status": cal.get("status"), "detail": cal.get("detail"), "days": out_days}


def weather(data: dict, loc: dict, focus: date | None) -> dict:
    from jarvis.agents.weather import forecast_for
    d = data.get("daily") or {}
    days = [forecast_for(data, date.fromisoformat(x)) for x in (d.get("time") or [])]
    cur = data.get("current") or {}
    return {"type": "weather", "location": loc["name"], "focus": focus.isoformat() if focus else None,
            "current": {"temp": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
                        "code": cur.get("weather_code")} if cur else None,
            "days": [x | {"code": (d.get("weather_code") or [None] * 9)[i]} for i, x in enumerate(days) if x]}


def tasks(items: list[dict], title: str) -> dict:
    return {"type": "tasks", "title": title,
            "tasks": [{"content": t["content"], "due": (t.get("due") or {}).get("date") if isinstance(t.get("due"), dict)
                       else t.get("due"), "priority": t.get("priority")} for t in items[:12]]}


def slots(day: date, minutes: int, items: list[dict]) -> dict:
    return {"type": "slots", "date": day.isoformat(), "label": day.strftime("%a %b %-d"), "minutes": minutes, "slots": items[:6]}
