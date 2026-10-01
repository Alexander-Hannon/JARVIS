"""Contract fakes for the V4 app integrations. Every fake is `simulated = True` and never touches a real account.

They emulate the semantics the executor depends on (no native idempotency where the real API has none, read-back
after writes, hidden op markers) and support fault injection via `fail_next` (shared _FaultMixin).
"""
from __future__ import annotations

import itertools
from typing import Any

from .base import ProviderError
from .fakes import _FaultMixin


class FakeNotion(_FaultMixin):
    name = "notion"
    simulated = True

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.pages: dict[str, dict] = {}         # id -> {title, parent, text}
        self._init_faults()

    def add_page(self, title: str, text: str = "", parent: str | None = None) -> str:
        pid = f"page{next(self._ids)}"
        self.pages[pid] = {"title": title, "parent": parent, "text": text}
        return pid

    def whoami(self) -> dict:
        return {"id": "bot1", "name": "JARVIS (SIMULATED)", "workspace": "AJ's workspace"}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = query.lower()
        return self._run("search", (query,), lambda: [
            {"id": i, "title": p["title"], "url": f"https://notion.example/{i}", "last_edited": None}
            for i, p in self.pages.items() if q in (p["title"] + " " + p["text"]).lower()][:limit])

    def read_page(self, page_id: str) -> dict:
        def go():
            p = self.pages.get(page_id)
            if p is None:
                raise ProviderError("not_found", "page not found")
            return {"id": page_id, "title": p["title"], "url": f"https://notion.example/{page_id}", "text": p["text"]}
        return self._run("read_page", (page_id,), go)

    def find_child_page(self, parent_id: str, title: str) -> dict | None:
        return self._run("find_child_page", (parent_id, title), lambda: next(
            ({"id": i, "title": title} for i, p in self.pages.items() if p["parent"] == parent_id and p["title"] == title), None))

    def create_page(self, parent_id: str, title: str, text: str) -> dict:
        def go():
            if parent_id not in self.pages:
                raise ProviderError("not_found", "parent not shared with the integration")
            pid = self.add_page(title, text, parent_id)
            return {"id": pid, "url": f"https://notion.example/{pid}", "title": title}
        return self._run("create_page", (parent_id, title), go)

    def append(self, page_id: str, text: str) -> None:
        def go():
            if page_id not in self.pages:
                raise ProviderError("not_found", "page not found")
            self.pages[page_id]["text"] += "\n\n" + text
        self._run("append", (page_id,), go)


class FakeSlack(_FaultMixin):
    name = "slack"
    simulated = True

    def __init__(self, me: str = "U_AJ") -> None:
        self.me = me
        self.channels_: dict[str, dict] = {"D_SELF": {"id": "D_SELF", "name": "DM AJ (you)", "is_im": True, "is_private": True},
                                           "C_GEN": {"id": "C_GEN", "name": "general", "is_im": False, "is_private": False}}
        self.messages: dict[str, list[dict]] = {"D_SELF": [], "C_GEN": []}
        self._ts = itertools.count(1)
        self._init_faults()

    def receive(self, channel: str, user: str, text: str) -> None:
        self.messages.setdefault(channel, []).insert(0, {"user": user, "text": text, "ts": f"{next(self._ts)}.0001",
                                                         "metadata": None})

    def whoami(self) -> dict:
        return {"user_id": self.me, "user": "aj", "team": "UAH (SIMULATED)", "url": "https://uah.slack.example/"}

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = query.lower().replace("to:me", "").strip()
        return self._run("search", (query,), lambda: [
            {"channel_id": c, "channel": self.channels_.get(c, {}).get("name"), "user": m["user"], "text": m["text"],
             "ts": m["ts"], "permalink": f"https://slack.example/{c}/{m['ts']}"}
            for c, ms in self.messages.items() for m in ms if q in m["text"].lower()][:limit])

    def channels(self, limit: int = 200) -> list[dict]:
        return list(self.channels_.values())

    def history(self, channel: str, limit: int = 30, include_metadata: bool = False) -> list[dict]:
        return self._run("history", (channel,), lambda: [
            m if include_metadata else {**m, "metadata": None} for m in self.messages.get(channel, [])[:limit]])

    def find_post_by_op(self, channel: str, op_key: str) -> dict | None:
        return self._run("find_post_by_op", (channel, op_key), lambda: next(
            (m for m in self.messages.get(channel, []) if (m.get("metadata") or {}).get("event_payload", {}).get("op") == op_key),
            None))

    def post(self, channel: str, text: str, op_key: str) -> dict:
        def go():
            if channel not in self.channels_:
                raise ProviderError("not_found", "channel_not_found")
            ts = f"{next(self._ts)}.0001"
            self.messages.setdefault(channel, []).insert(0, {"user": "aj", "text": text, "ts": ts, "metadata": {
                "event_type": "jarvis_op", "event_payload": {"op": op_key}}})
            return {"channel": channel, "ts": ts}
        return self._run("post", (channel,), go)


class FakeCanvas(_FaultMixin):
    name = "canvas"
    simulated = True

    def __init__(self) -> None:
        self.courses_ = [{"id": 640, "name": "IS 640 Business Analytics", "code": "IS-640"},
                         {"id": 701, "name": "IS 701 Research Methods", "code": "IS-701"}]
        self.assignments_: dict[int, list[dict]] = {640: [], 701: []}
        self.todo_: list[dict] = []
        self.announcements_: list[dict] = []
        self._init_faults()

    def whoami(self) -> dict:
        return {"id": 1, "name": "AJ (SIMULATED)", "email": "aj@uah.example"}

    def courses(self) -> list[dict]:
        return self._run("courses", (), lambda: list(self.courses_))

    def todo(self) -> list[dict]:
        return self._run("todo", (), lambda: list(self.todo_))

    def assignments(self, course_id, bucket: str = "upcoming") -> list[dict]:
        return self._run("assignments", (course_id,), lambda: list(self.assignments_.get(int(course_id), [])))

    def announcements(self, course_ids: list, limit: int = 10) -> list[dict]:
        return self._run("announcements", (), lambda: list(self.announcements_)[:limit])


class FakeSpotify(_FaultMixin):
    name = "spotify"
    simulated = True

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"active": True, "is_playing": False, "device": "AJ's laptop",
                                      "track": {"uri": "spotify:track:1", "name": "Focus", "artists": "Lo-Fi Crew", "album": "Study"},
                                      "context": None, "progress_ms": 0}
        self.queue_: list[str] = []
        self.catalog = [{"uri": "spotify:track:2", "name": "Eye of the Tiger", "artists": "Survivor", "album": "Rocky"},
                        {"uri": "spotify:playlist:deep", "name": "Deep Focus", "owner": "Spotify"}]
        self._init_faults()

    def whoami(self) -> dict:
        return {"id": "aj", "name": "AJ (SIMULATED)", "product": "premium"}

    def now_playing(self) -> dict:
        return self._run("now_playing", (), lambda: dict(self.state))

    def search(self, query: str, kind: str = "track", limit: int = 5) -> list[dict]:
        q = query.lower()
        return self._run("search", (query,), lambda: [c for c in self.catalog if f":{kind}:" in c["uri"]
                                                       and q in (c["name"] + " " + c.get("artists", "")).lower()][:limit])

    def _need_device(self):
        if not self.state["active"]:
            raise ProviderError("not_found", "NO_ACTIVE_DEVICE")

    def play(self, uri: str | None = None) -> None:
        def go():
            self._need_device()
            if uri:
                hit = next((c for c in self.catalog if c["uri"] == uri), None)
                if ":track:" in uri and hit:
                    self.state["track"] = hit
                else:
                    self.state["context"] = uri
            self.state["is_playing"] = True
        self._run("play", (uri,), go)

    def pause(self) -> None:
        def go():
            self._need_device()
            self.state["is_playing"] = False
        self._run("pause", (), go)

    def next(self) -> None:
        self._run("next", (), lambda: self.queue_.pop(0) if self.queue_ else None)

    def queue(self, uri: str) -> None:
        self._run("queue", (uri,), lambda: self.queue_.append(uri))


class FakeHomeAssistant(_FaultMixin):
    name = "homeassistant"
    simulated = True
    _EFFECT = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open",
               "close_cover": "closed", "media_play": "playing", "media_pause": "paused"}

    def __init__(self) -> None:
        self.entities: dict[str, dict] = {
            "light.office": {"state": "off", "name": "Office light"},
            "light.living_room": {"state": "on", "name": "Living room"},
            "lock.front_door": {"state": "locked", "name": "Front door"},
            "cover.garage": {"state": "closed", "name": "Garage door"},
            "climate.thermostat": {"state": "cool", "name": "Thermostat"},
        }
        self.calls_: list[tuple] = []
        self._init_faults()

    def ping(self) -> dict:
        return {"message": "API running. (SIMULATED)"}

    def states(self) -> list[dict]:
        return self._run("states", (), lambda: [{"entity_id": k, "state": v["state"], "name": v["name"], "attributes": {}}
                                                for k, v in self.entities.items()])

    def state(self, entity_id: str) -> dict | None:
        e = self.entities.get(entity_id)
        return {"entity_id": entity_id, "state": e["state"], "name": e["name"]} if e else None

    def call(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> list:
        def go():
            if entity_id not in self.entities:
                raise ProviderError("not_found", "entity not found")
            self.calls_.append((domain, service, entity_id, data or {}))
            if service == "toggle":
                cur = self.entities[entity_id]["state"]
                self.entities[entity_id]["state"] = "off" if cur == "on" else "on"
            elif service in self._EFFECT:
                self.entities[entity_id]["state"] = self._EFFECT[service]
            return [{"entity_id": entity_id, "state": self.entities[entity_id]["state"]}]
        return self._run("call", (domain, service, entity_id), go)


class FakeRouting(_FaultMixin):
    name = "routing"
    simulated = True

    def __init__(self, minutes: float = 18.0) -> None:
        self.minutes = minutes
        self.places = {"uah": {"lat": 34.7254, "lon": -86.6405, "label": "University of Alabama in Huntsville"}}
        self._init_faults()

    def geocode(self, text: str, near=None) -> dict | None:
        return self._run("geocode", (text,), lambda: next((v for k, v in self.places.items() if k in text.lower()),
                                                           {"lat": 34.73, "lon": -86.59, "label": text}))

    def route(self, origin, dest, mode: str = "drive") -> dict:
        return self._run("route", (mode,), lambda: {"minutes": self.minutes, "miles": 6.2, "mode": mode})


class FakeSimpleFin(_FaultMixin):
    name = "simplefin"
    simulated = True

    def __init__(self) -> None:
        self.data = {"accounts": [
            {"id": "chk", "name": "Checking", "org": "Redstone FCU", "currency": "USD", "balance": 1840.22, "available": 1790.22,
             "as_of": 1790000000, "transactions": [
                 {"id": "t1", "posted": 1789900000, "amount": -12.99, "description": "Spotify", "pending": False},
                 {"id": "t2", "posted": 1789800000, "amount": -64.10, "description": "Publix", "pending": False},
                 {"id": "t3", "posted": 1789700000, "amount": -412.00, "description": "Unknown Merchant LLC", "pending": True},
                 {"id": "t4", "posted": 1789600000, "amount": 1450.00, "description": "UAH Payroll", "pending": False}]},
            {"id": "cc", "name": "Credit Card", "org": "Chase", "currency": "USD", "balance": -523.45, "available": None,
             "as_of": 1790000000, "transactions": []}], "errors": []}
        self._init_faults()

    def accounts(self, start_unix=None, end_unix=None) -> dict:
        return self._run("accounts", (), lambda: self.data)


class FakeTelegram(_FaultMixin):
    """Telegram Bot API fake: tests push updates in; sent messages/callback answers are recorded."""
    name = "telegram"
    simulated = True

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.answers: list[str] = []
        self.files: dict[str, bytes] = {}
        self._uid = itertools.count(1)
        self._mid = itertools.count(100)
        self._init_faults()

    def push_message(self, chat: int, text: str = "", date: int | None = None, **extra) -> None:
        from jarvis.core.util import utcnow
        self.updates.append({"update_id": next(self._uid), "message": {
            "message_id": next(self._mid), "date": date or int(utcnow().timestamp()), "chat": {"id": chat, "first_name": "AJ"},
            "from": {"username": "aj"}, "text": text, **extra}})

    def push_callback(self, chat: int, data: str) -> None:
        self.updates.append({"update_id": next(self._uid), "callback_query": {
            "id": f"cb{next(self._uid)}", "data": data, "message": {"message_id": 1, "chat": {"id": chat}}}})

    def get_me(self) -> dict:
        return {"id": 42, "username": "jarvis_test_bot"}

    def get_updates(self, offset, limit: int = 50) -> list[dict]:
        return [u for u in self.updates if offset is None or u["update_id"] >= offset][:limit]

    def send(self, chat_id: int, text: str, buttons=None) -> dict:
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons})
        return {"message_id": next(self._mid)}

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append(text)

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        pass

    def download_file(self, file_id: str, max_bytes: int = 0) -> bytes:
        return self.files[file_id]
