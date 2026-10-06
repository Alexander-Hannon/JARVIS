"""Weather specialist (read-only). Open-Meteo: free, no account, no API key.
Only the user's configured location coordinates are sent; nothing else about them is."""
from __future__ import annotations

import time
from datetime import date

import httpx

from jarvis.config import get_settings
from jarvis.core import settings as S

API = "https://api.open-meteo.com/v1/forecast"
DEFAULT_LOCATION = {"name": "Huntsville, AL", "lat": 34.7304, "lon": -86.5861}
_cache: dict[tuple, tuple[float, dict]] = {}
CACHE_S = 1800

WMO = {0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
       73: "snow", 75: "heavy snow", 77: "snow grains", 80: "passing showers", 81: "showers", 82: "heavy showers",
       85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
       99: "severe thunderstorms with hail"}


class WeatherUnavailable(Exception):
    pass


def location(conn, uid: str) -> dict | None:
    """The user's location; the personal edition falls back to AJ's home. Enterprise users set their own."""
    loc = S.user_data(conn, uid).get("location")
    return loc or (DEFAULT_LOCATION if get_settings().edition == "personal" else None)


def enabled(conn, uid: str) -> bool:
    return bool(S.user_data(conn, uid).get("weather", {}).get("enabled", True)) and location(conn, uid) is not None


def fetch(loc: dict, tz: str, client: httpx.Client | None = None) -> dict:
    key = (round(loc["lat"], 3), round(loc["lon"], 3), tz)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    params = {"latitude": loc["lat"], "longitude": loc["lon"], "timezone": tz, "forecast_days": 7,
              "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
              "current": "temperature_2m,apparent_temperature,weather_code",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"}
    own = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        r = client.get(API, params=params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise WeatherUnavailable(f"Weather service unreachable ({type(e).__name__}).") from e
    finally:
        if own:
            client.close()
    _cache[key] = (time.monotonic(), data)
    return data


def forecast_for(data: dict, day: date) -> dict | None:
    d = data.get("daily") or {}
    days = d.get("time") or []
    if day.isoformat() not in days:
        return None
    i = days.index(day.isoformat())

    def g(k):
        v = d.get(k) or []
        return v[i] if i < len(v) else None
    return {"date": day.isoformat(), "summary": WMO.get(g("weather_code"), "mixed conditions"), "high": g("temperature_2m_max"),
            "low": g("temperature_2m_min"), "rain_chance": g("precipitation_probability_max"), "wind": g("wind_speed_10m_max")}


def describe(f: dict, loc_name: str, label: str) -> tuple[str, str]:
    hi, lo, rain, wind = f["high"], f["low"], f["rain_chance"], f["wind"]
    text = (f"{label.capitalize()} in {loc_name}: {f['summary']}, high {round(hi)}°F / low {round(lo)}°F"
            + (f", {rain}% chance of rain" if rain is not None else "") + (f", wind up to {round(wind)} mph" if wind else "") + ".")
    advice = ""
    if rain is not None and rain >= 50:
        advice = " Bring an umbrella."
    elif hi is not None and hi >= 88:
        advice = " It'll be hot, so plan outdoor training early."
    elif lo is not None and lo <= 40:
        advice = " It'll be cold in the morning."
    spoken = f"{label.capitalize()}: {f['summary']}, a high of {round(hi)} and a low of {round(lo)}" + \
        (f", with a {rain} percent chance of rain" if rain else "") + "." + advice
    return text + advice, spoken
