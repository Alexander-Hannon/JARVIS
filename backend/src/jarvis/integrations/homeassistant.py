"""Home Assistant adapter (REST API with a long-lived access token).

REST (https://developers.home-assistant.io/docs/api/rest/): GET /api/ (validate), GET /api/states,
GET /api/states/<entity_id>, POST /api/services/<domain>/<service> {"entity_id": ..., ...}.

Home Assistant usually lives on AJ's LAN (http://homeassistant.local:8123), so unlike other user-supplied URLs a
private address is allowed here — AJ types it himself in an authenticated settings page. Cloud-metadata and
link-local addresses are still refused. JARVIS can only touch entities AJ allowlisted, and the domain decides the tier:
locks, alarms, garage doors/covers, scripts and automations are always explicit-approval (`home.secure`).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .http import JsonClient

# Comfort devices: T1-eligible (a rule may auto-approve them). Everything else is "secure" (always ask).
COMFORT_DOMAINS = {"light", "switch", "fan", "scene", "media_player", "climate", "humidifier", "input_boolean"}
SECURE_DOMAINS = {"lock", "alarm_control_panel", "cover", "garage_door", "script", "automation", "camera", "valve",
                  "siren", "button"}
SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"}, "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage"}, "scene": {"turn_on"},
    "media_player": {"media_play", "media_pause", "volume_set", "turn_on", "turn_off"},
    "climate": {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"}, "humidifier": {"turn_on", "turn_off"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "lock": {"lock", "unlock"}, "alarm_control_panel": {"alarm_arm_home", "alarm_arm_away", "alarm_disarm"},
    "cover": {"open_cover", "close_cover", "stop_cover"}, "script": {"turn_on"}, "automation": {"trigger", "turn_on", "turn_off"},
    "valve": {"open_valve", "close_valve"}, "siren": {"turn_on", "turn_off"}, "button": {"press"},
}
# Expected state after a service, for read-back verification (None = no simple expectation).
EXPECT = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open",
          "close_cover": "closed", "media_play": "playing", "media_pause": "paused", "alarm_disarm": "disarmed",
          "alarm_arm_home": "armed_home", "alarm_arm_away": "armed_away"}
ATTR_ALLOW = {"brightness_pct", "color_temp_kelvin", "temperature", "hvac_mode", "percentage", "volume_level"}


class UnsafeHAURL(ValueError):
    pass


def validate_base_url(url: str, resolver=socket.getaddrinfo) -> str:
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise UnsafeHAURL("Use http(s)://host:port with no embedded credentials.")
    try:
        infos = resolver(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeHAURL(f"Host could not be resolved: {p.hostname}") from e
    for info in infos:
        a = ipaddress.ip_address(info[4][0])
        if a.is_link_local or a.is_multicast or a.is_unspecified or str(a) == "169.254.169.254":
            raise UnsafeHAURL("That address is link-local/metadata; refusing.")
    return f"{p.scheme}://{p.netloc}"


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def is_secure(entity_id: str) -> bool:
    return domain_of(entity_id) not in COMFORT_DOMAINS


class HomeAssistantAdapter:
    name = "homeassistant"
    simulated = False

    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self.http = JsonClient(base_url.rstrip("/") + "/api", {"Authorization": f"Bearer {token}"}, "Home Assistant",
                               client=client, timeout=10.0)

    def ping(self) -> dict:
        return self.http.get("/")

    def states(self) -> list[dict]:
        return [{"entity_id": s["entity_id"], "state": s.get("state"),
                 "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"],
                 "attributes": {k: v for k, v in (s.get("attributes") or {}).items() if k in ATTR_ALLOW | {"unit_of_measurement",
                                                                                                    "current_temperature"}}}
                for s in self.http.get("/states") or []]

    def state(self, entity_id: str) -> dict | None:
        from .base import ProviderError
        try:
            s = self.http.get(f"/states/{entity_id}")
        except ProviderError as e:
            if e.kind == "not_found":
                return None
            raise
        return {"entity_id": s["entity_id"], "state": s.get("state"),
                "name": (s.get("attributes") or {}).get("friendly_name") or entity_id}

    def call(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> list:
        return self.http.request("POST", f"/services/{domain}/{service}", write=True,
                                 json={"entity_id": entity_id, **(data or {})})
