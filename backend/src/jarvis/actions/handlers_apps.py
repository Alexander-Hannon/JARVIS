"""Executor handlers for V4 app actions (Notion, Slack, Spotify, Home Assistant). Called only by actions/executor.py
after every guard passed (approval binding, registry, account identity, resource write grant)."""
from __future__ import annotations

from jarvis.actions.executor import Blocked
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError


def _sim(p) -> bool:
    return A.is_simulated(p)


# ------------------------------------------------------------------------------------------------ Notion
def h_notion_create(conn, a: dict, reconcile_first: bool) -> dict:
    n = A.app_provider(conn, a["user_id"], "notion")
    parent, title, text = a["target_resource"], a["args"]["title"], a["args"]["text"]
    existing = n.find_child_page(parent, title)
    if existing:
        # Reconcile by exact title under the parent. On a first attempt an existing page means AJ already has one with
        # that title: don't silently add a duplicate.
        if reconcile_first:
            return {"ok": True, "method": "reconcile_title", "external_id": existing["id"], "simulated": _sim(n),
                    "observed": {"title": title}, "message": f"Found the Notion page already created: {title}"}
        raise Blocked("conflict", f"A page titled “{title}” already exists there; rename it or append instead.")
    p = n.create_page(parent, title, text)
    back = n.read_page(p["id"])
    ok = back["title"] == title and text.strip()[:60] in back["text"]
    return {"ok": ok, "method": "read_after_write", "external_id": p["id"], "url": p.get("url"), "simulated": _sim(n),
            "provider_ids": {"page_id": p["id"]}, "observed": {"title": back["title"]},
            "message": f"Notion page created: {title}"}


def h_notion_append(conn, a: dict, reconcile_first: bool) -> dict:
    n = A.app_provider(conn, a["user_id"], "notion")
    page, text = a["target_resource"], a["args"]["text"]
    if reconcile_first:
        if text.strip()[:80] in n.read_page(page)["text"]:
            return {"ok": True, "method": "reconcile_content", "external_id": page, "simulated": _sim(n),
                    "observed": {"appended": True}, "message": "The note was already appended"}
        raise Blocked("failed", "Unclear whether the earlier append landed; check the page and approve again.")
    n.append(page, text)
    ok = text.strip()[:80] in n.read_page(page)["text"]
    return {"ok": ok, "method": "read_after_write", "external_id": page, "simulated": _sim(n),
            "observed": {"appended": ok}, "message": "Appended to the Notion page"}


# ------------------------------------------------------------------------------------------------ Slack
def h_slack_post(conn, a: dict, reconcile_first: bool) -> dict:
    s = A.app_provider(conn, a["user_id"], "slack")
    channel, text = a["args"]["channel"], a["args"]["text"]
    found = s.find_post_by_op(channel, a["op_key"])          # hidden message metadata, never visible text
    if found:
        return {"ok": True, "method": "reconcile_marker", "external_id": found["ts"], "simulated": _sim(s),
                "observed": {"text": found["text"][:200]}, "message": "Slack message was already posted (no duplicate)"}
    r = s.post(channel, text, a["op_key"])
    back = s.find_post_by_op(channel, a["op_key"])
    return {"ok": bool(back), "method": "read_after_write", "external_id": r["ts"], "simulated": _sim(s),
            "provider_ids": {"channel": r["channel"], "ts": r["ts"]}, "observed": {"posted": bool(back)},
            "message": f"Posted to Slack ({a['args'].get('channel_name') or channel})"}


# ------------------------------------------------------------------------------------------------ Spotify
def h_music(conn, a: dict, reconcile_first: bool) -> dict:
    sp = A.app_provider(conn, a["user_id"], "spotify")
    op = a["action_type"].split(".", 1)[1]
    uri = a["args"].get("uri")
    try:
        if op == "play":
            sp.play(uri)
        elif op == "pause":
            sp.pause()
        elif op == "next":
            if reconcile_first:
                raise Blocked("failed", "Unclear whether the skip happened; not skipping again.")
            sp.next()
        elif op == "queue":
            if reconcile_first:
                raise Blocked("failed", "Unclear whether the track was queued; not queueing it twice.")
            sp.queue(uri)
    except ProviderError as e:
        if e.kind == "not_found" and "NO_ACTIVE_DEVICE" in e.message.upper().replace(" ", "_"):
            raise Blocked("failed", "No active Spotify device — open Spotify on a phone or computer first.") from None
        raise
    st = sp.now_playing()
    ok = {"play": st.get("is_playing") is True, "pause": st.get("is_playing") is False}.get(op, True)
    return {"ok": ok, "method": "read_after_write", "simulated": _sim(sp), "observed": {
        "is_playing": st.get("is_playing"), "track": (st.get("track") or {}).get("name")},
        "message": {"play": "Playing", "pause": "Paused", "next": "Skipped", "queue": "Queued"}[op] + " on Spotify"}


# ------------------------------------------------------------------------------------------------ Home Assistant
def h_home(conn, a: dict, reconcile_first: bool) -> dict:
    from jarvis.integrations.homeassistant import EXPECT, SERVICES, domain_of, is_secure
    ha = A.app_provider(conn, a["user_id"], "homeassistant")
    entity, service = a["target_resource"], a["args"]["service"]
    domain = domain_of(entity)
    # Re-derive the tier from the entity in code: a proposal can't launder a lock through home.control.
    if is_secure(entity) and a["action_type"] != "home.secure":
        raise Blocked("failed", f"{entity} is a security device; it needs explicit approval.")
    if service not in SERVICES.get(domain, set()):
        raise Blocked("failed", f"Service {domain}.{service} isn't allowed.")
    want = EXPECT.get(service)
    cur = ha.state(entity)
    if cur is None:
        raise Blocked("conflict", f"{entity} no longer exists in Home Assistant.")
    if not (want and cur["state"] == want):     # already in the desired state = done (natural idempotency)
        ha.call(domain, service, entity, a["args"].get("data") or {})
    after = ha.state(entity) or {}
    ok = (after.get("state") == want) if want else True
    return {"ok": ok, "method": "read_after_write", "external_id": entity, "simulated": _sim(ha),
            "observed": {"state": after.get("state")}, "message": f"{after.get('name') or entity}: {after.get('state')}"}
