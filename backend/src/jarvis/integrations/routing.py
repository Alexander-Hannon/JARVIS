"""Travel time via OpenRouteService (free API key; https://openrouteservice.org/dev/#/api-docs).

* GET  https://api.openrouteservice.org/geocode/search?text=&size=1&focus.point.lon=&focus.point.lat=
* POST https://api.openrouteservice.org/v2/directions/{profile}  {"coordinates": [[lon,lat],[lon,lat]]}
  profiles: driving-car, cycling-regular, foot-walking. Response routes[0].summary {distance (m), duration (s)}.
Read-only. ORS has no live traffic, so estimates carry a buffer when used for "leave by" alerts.
"""
from __future__ import annotations

import httpx

from .http import JsonClient

API = "https://api.openrouteservice.org"
PROFILES = {"drive": "driving-car", "bike": "cycling-regular", "walk": "foot-walking"}


class RoutingAdapter:
    name = "routing"
    simulated = False

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.http = JsonClient(API, {"Authorization": api_key, "Accept": "application/json, application/geo+json"},
                               "OpenRouteService", client=client)

    def geocode(self, text: str, near: tuple[float, float] | None = None) -> dict | None:
        params: dict = {"text": text, "size": 1}
        if near:
            params |= {"focus.point.lat": near[0], "focus.point.lon": near[1]}
        feats = self.http.get("/geocode/search", params=params).get("features") or []
        if not feats:
            return None
        f = feats[0]
        lon, lat = f["geometry"]["coordinates"][:2]
        return {"lat": lat, "lon": lon, "label": (f.get("properties") or {}).get("label") or text}

    def route(self, origin: tuple[float, float], dest: tuple[float, float], mode: str = "drive") -> dict:
        r = self.http.request("POST", f"/v2/directions/{PROFILES.get(mode, 'driving-car')}",
                              json={"coordinates": [[origin[1], origin[0]], [dest[1], dest[0]]]})
        s = (r.get("routes") or [{}])[0].get("summary") or {}
        return {"minutes": round(float(s.get("duration", 0)) / 60, 1), "miles": round(float(s.get("distance", 0)) / 1609.34, 1),
                "mode": mode}
