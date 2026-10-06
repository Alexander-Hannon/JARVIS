"""Telegram channel: talk to JARVIS from your phone, get mission/action notifications, approve with a button.

Security model:
- Outbound polling from the worker (getUpdates); no webhook, no inbound port — the loopback-only rule holds.
- The bot is org-level (an admin connects it). Each user pairs *their own* chat with a one-time code from their
  authenticated session; a chat maps to exactly one user (`channel_links`). Unknown chats are ignored (audited once).
  Wrong pairing codes are capped per chat and per bot so 6-digit codes can't be guessed.
- Commands from a linked chat run as that user through the normal pipeline with origin 'channel', so auto-approve
  rules only apply if that user opted them into channels.
- Approve buttons carry proposal id + version + a hash prefix and only work for the proposal's owner's chat.
- Forwarded messages are someone else's words: they're saved as an untrusted document, never executed as a command.
"""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
import sqlite3
from datetime import UTC, datetime, timedelta

from jarvis.core import jobs
from jarvis.core.db import jload, row
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

PAIR_TTL = timedelta(minutes=10)
PAIR_FAILS_PER_CHAT = 5
PAIR_FAILS_PER_BOT = 20       # per pairing window, across all chats


class ChannelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def adapter(conn):
    if (o := A._override(None, "telegram")) is not None:
        return o
    acct = _acct(conn)
    if not acct:
        raise A.NotConnected("telegram")
    from jarvis.integrations.telegram import TelegramAdapter
    return TelegramAdapter(A.read_secret(acct))


def _acct(conn) -> dict | None:
    return A.org_account(conn, "telegram")


def _cfg_update(conn, acct_id: str, **kv) -> None:
    r = row(conn, "SELECT config FROM integration_accounts WHERE id=?", (acct_id,))
    cfg = jload(r["config"], {}) | kv
    conn.execute("UPDATE integration_accounts SET config=?, updated_at=? WHERE id=?", (json.dumps(cfg), now_iso(), acct_id))


def link_for_chat(conn, chat) -> dict | None:
    return row(conn, "SELECT l.* FROM channel_links l JOIN users u ON u.id=l.user_id "
                     "WHERE l.channel='telegram' AND l.chat_id=? AND u.status='active'", (str(chat),)) if chat is not None else None


def link_for_user(conn, uid: str) -> dict | None:
    return row(conn, "SELECT * FROM channel_links WHERE channel='telegram' AND user_id=?", (uid,))


# ------------------------------------------------------------------------------------------------ setup (UI only)
def connect(conn: sqlite3.Connection, token: str, by: str | None = None) -> dict:
    """Admin: connect the org's bot."""
    from jarvis.integrations.telegram import TelegramAdapter
    token = token.strip()
    if ":" not in token:
        raise ChannelError("bad_token", "That doesn't look like a bot token from @BotFather (digits:letters).")
    try:
        me = (A._override(None, "telegram") or TelegramAdapter(token)).get_me()
    except ProviderError as e:
        raise ChannelError(e.kind, f"Telegram rejected the token ({e.kind}).") from None
    conn.execute("BEGIN IMMEDIATE")
    try:
        A.upsert_account(conn, None, "telegram", f"@{me.get('username')}", token, "bot_token",
                         external_account_id=str(me.get("id")), scopes=["bot"], config={"bot": me.get("username")})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return status(conn, by) if by else {"status": "connected"}


def start_pairing(conn: sqlite3.Connection, uid: str) -> dict:
    acct = _acct(conn)
    if not acct:
        raise ChannelError("not_connected", "Your admin hasn't connected the Telegram bot yet.")
    code = f"{pysecrets.randbelow(10**6):06d}"
    conn.execute("BEGIN IMMEDIATE")
    pend = {h: p for h, p in (acct["config"].get("pairings") or {}).items()
            if p["user_id"] != uid and parse_iso(p["expires"]) > utcnow()}
    pend[hashlib.sha256(code.encode()).hexdigest()] = {"user_id": uid, "expires": iso(utcnow() + PAIR_TTL)}
    _cfg_update(conn, acct["id"], pairings=pend, pair_failures={}, pair_failures_total=0)
    conn.execute("COMMIT")
    return {"code": code, "instructions": f"Send  /pair {code}  to @{acct['config'].get('bot')} within 10 minutes.",
            "expires_in_s": int(PAIR_TTL.total_seconds())}


def unpair(conn, uid: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    n = conn.execute("DELETE FROM channel_links WHERE channel='telegram' AND user_id=?", (uid,)).rowcount
    if n:
        audit(conn, "user", "channel.unpaired", "Telegram chat unpaired", uid=uid)
    conn.execute("COMMIT")
    return status(conn, uid)


def set_notify(conn, uid: str, enabled: bool) -> dict:
    """Per-user: mirror notifications to their linked chat (stored on their link via settings)."""
    from jarvis.core import settings as S
    conn.execute("BEGIN IMMEDIATE")
    S.update_user_data(conn, uid, lambda d: d.setdefault("telegram", {}).__setitem__("notify", bool(enabled)))
    conn.execute("COMMIT")
    return status(conn, uid)


def _notify_on(conn, uid: str) -> bool:
    from jarvis.core import settings as S
    return (S.user_data(conn, uid).get("telegram") or {}).get("notify", True)


def status(conn, uid: str) -> dict:
    acct = _acct(conn)
    if not acct:
        return {"status": "not_connected"}
    link = link_for_user(conn, uid)
    return {"status": "paired" if link else "connected", "bot": acct["config"].get("bot"),
            "chat_name": link["chat_name"] if link else None, "account_id": acct["id"],
            "notify": _notify_on(conn, uid), "simulated": A._override(None, "telegram") is not None}


# ------------------------------------------------------------------------------------------------ polling (worker)
def poll(conn: sqlite3.Connection) -> int:
    acct = _acct(conn)
    if not acct or acct["status"] not in ("connected", "error"):
        return 0
    tg = adapter(conn)
    try:
        updates = tg.get_updates(acct["config"].get("offset"))
    except ProviderError as e:
        if e.kind == "auth":
            A.set_status(conn, acct["id"], "error", "Telegram bot token revoked; reconnect.")
        return 0
    for u in updates:
        conn.execute("BEGIN IMMEDIATE")
        _cfg_update(conn, acct["id"], offset=u["update_id"] + 1)     # ack first: a poisoned update is never re-processed
        conn.execute("COMMIT")
        try:
            handle_update(conn, tg, u)
        except Exception:   # noqa: BLE001 — one bad update must not stall the channel
            import logging
            logging.getLogger("jarvis.telegram").exception("telegram update failed")
    return len(updates)


def handle_update(conn, tg, u: dict) -> None:
    if "callback_query" in u:
        return _on_callback(conn, tg, u["callback_query"])
    msg = u.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if text.startswith("/pair"):
        return _try_pair(conn, tg, chat, msg, text)
    link = link_for_chat(conn, chat)
    if link is None:
        acct = _acct(conn)
        seen = set(acct["config"].get("ignored_chats", []))
        if chat not in seen:
            conn.execute("BEGIN IMMEDIATE")
            _cfg_update(conn, acct["id"], ignored_chats=sorted(seen | {chat})[-50:])
            audit(conn, "channel", "channel.ignored", "Ignored Telegram message from an unpaired chat", "integration_account",
                  acct["id"])
            conn.execute("COMMIT")
        return None
    uid = link["user_id"]
    if msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date"):
        return _save_forward(conn, tg, uid, chat, msg)
    if msg.get("voice") or msg.get("audio"):
        text = _transcribe(conn, tg, chat, msg)
        if not text:
            return None
    if not text:
        tg.send(chat, "I can read text and voice notes here.")
        return None
    if text in ("/start", "/help"):
        tg.send(chat, "Talk to me like you would in the app. Anything that changes something comes back with Approve / "
                      "Reject buttons. Forwarded messages are saved as notes, not treated as instructions.")
        return None
    return _command(conn, tg, uid, link, chat, text, msg)


def _try_pair(conn, tg, chat, msg, text) -> None:
    acct = _acct(conn)
    if acct is None or chat is None:
        return
    cfg = acct["config"]
    code = text.split(maxsplit=1)[1].strip() if " " in text else ""
    fails = dict(cfg.get("pair_failures") or {})
    total = int(cfg.get("pair_failures_total") or 0)
    hit = None
    if int(fails.get(str(chat), 0)) < PAIR_FAILS_PER_CHAT and total < PAIR_FAILS_PER_BOT:
        h = hashlib.sha256(code.encode()).hexdigest()
        for ph, pend in (cfg.get("pairings") or {}).items():
            if pysecrets.compare_digest(h, ph) and parse_iso(pend["expires"]) > utcnow():
                hit = pend
    conn.execute("BEGIN IMMEDIATE")
    taken = link_for_chat(conn, chat)
    if hit and (taken is None or taken["user_id"] == hit["user_id"]):
        name = (msg.get("from") or {}).get("username") or (msg.get("chat") or {}).get("first_name") or str(chat)
        conn.execute("DELETE FROM channel_links WHERE channel='telegram' AND (user_id=? OR chat_id=?)",
                     (hit["user_id"], str(chat)))
        conn.execute("INSERT INTO channel_links (id, user_id, channel, chat_id, chat_name, paired_at) "
                     "VALUES (?,?,'telegram',?,?,?)", (new_id("chl"), hit["user_id"], str(chat), name, now_iso()))
        pend = {k: v for k, v in (cfg.get("pairings") or {}).items() if v["user_id"] != hit["user_id"]}
        _cfg_update(conn, acct["id"], pairings=pend)
        audit(conn, "user", "channel.paired", f"Telegram chat paired ({name})", "integration_account", acct["id"],
              uid=hit["user_id"])
        ok = True
    else:
        fails[str(chat)] = int(fails.get(str(chat), 0)) + 1
        _cfg_update(conn, acct["id"], pair_failures=fails, pair_failures_total=total + 1)
        ok = False
    conn.execute("COMMIT")
    tg.send(chat, "Paired. I'll answer you here." if ok else "That code isn't valid. Get a new one in JARVIS → Settings.")


def _save_forward(conn, tg, uid: str, chat, msg) -> None:
    from jarvis.knowledge.ingest import ingest_bytes
    body = (msg.get("text") or msg.get("caption") or "").strip()
    if not body:
        tg.send(chat, "I can only save forwarded text for now.")
        return
    when = datetime.fromtimestamp(msg.get("date") or 0, UTC).strftime("%Y-%m-%d %H%M")
    ingest_bytes(conn, uid, body.encode(), f"Forwarded {when}.txt", "text/plain", None, origin="telegram",
                 origin_ref=f"tg:{chat}:{msg.get('message_id')}")
    tg.send(chat, "Saved that forwarded message as a note (I won't act on instructions inside it). "
                  "Ask me about it or tell me what to do with it.")


def _transcribe(conn, tg, chat, msg) -> str | None:
    from jarvis.voice import stt
    v = msg.get("voice") or msg.get("audio")
    try:
        audio = stt.decode_audio(tg.download_file(v["file_id"]), max_seconds=300)
        t = stt.get_engine().transcribe(audio)
    except (ProviderError, stt.AudioError) as e:
        tg.send(chat, f"I couldn't process that voice note ({getattr(e, 'kind', None) or getattr(e, 'code', 'error')}).")
        return None
    if not t.text.strip():
        tg.send(chat, "I didn't catch any speech in that voice note.")
        return None
    tg.send(chat, f"🎙 “{t.text.strip()}”")
    return t.text.strip()


def _conversation(conn, uid: str, link: dict) -> str:
    from jarvis.agents.chief import ensure_conversation
    cid = link.get("conversation_id")
    if not cid or not row(conn, "SELECT 1 FROM conversations WHERE id=? AND user_id=?", (cid, uid)):
        cid = ensure_conversation(conn, uid, None)
        conn.execute("UPDATE channel_links SET conversation_id=? WHERE id=?", (cid, link["id"]))
    return cid


def _command(conn, tg, uid: str, link: dict, chat, text: str, msg: dict) -> None:
    from jarvis.agents import context as ctx
    from jarvis.agents.chief import CommandInput, handle
    sent = datetime.fromtimestamp(msg.get("date") or utcnow().timestamp(), UTC)
    resp = handle(conn, CommandInput(user_id=uid, conversation_id=_conversation(conn, uid, link), text=text,
                                     channel="telegram", captured_at=iso(sent), tz=ctx.user_tz(conn, uid), origin="channel"))
    reply = resp["message"]["text"] or "Done."
    p = resp.get("proposal")
    buttons = None
    if p and p["status"] == "proposed":
        reply += "\n\n" + "\n".join(f"{i['index'] + 1}. {i['expected_effect']}" for i in p["items"])
        buttons = [[{"text": "✅ Approve", "callback_data": f"ap:{p['id']}:{p['version']}:{p['hash'][:16]}"},
                    {"text": "✖ Reject", "callback_data": f"rj:{p['id']}:{p['version']}:{p['hash'][:16]}"}]]
    tg.send(chat, reply, buttons)


def _on_callback(conn, tg, cq: dict) -> None:
    from jarvis.actions import proposals as P
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    link = link_for_chat(conn, chat)
    if link is None:
        tg.answer_callback(cq["id"], "Not authorized.")
        return
    uid = link["user_id"]
    try:
        op, pid, version, hprefix = (cq.get("data") or "").split(":")
        cur = P.current_version(conn, uid, pid)          # someone else's proposal is "not found"
        if int(version) != cur["current_version"] or not cur["pv"]["hash"].startswith(hprefix):
            tg.answer_callback(cq["id"], "That plan changed; open the latest version.")
            return
        if op == "ap":
            acts = P.actions_for_version(conn, cur["pv"]["id"])
            P.approve(conn, uid, pid, int(version), cur["pv"]["hash"], [a["item_index"] for a in acts], channel="telegram",
                      session_id=None, evidence="Telegram approve button")
            tg.answer_callback(cq["id"], "Approved — running it now.")
        elif op == "rj":
            P.reject(conn, uid, pid)
            tg.answer_callback(cq["id"], "Rejected.")
        tg.clear_buttons(chat, (cq.get("message") or {}).get("message_id"))
    except P.ProposalError as e:
        tg.answer_callback(cq["id"], e.message)
    except ValueError:
        tg.answer_callback(cq["id"], "Unknown button.")


# ------------------------------------------------------------------------------------------------ notifications out
def maybe_forward_notification(conn, notification_id: str, kind: str) -> None:
    """Called by notify(): mirror a user's mission/action/briefing notifications to *their* linked chat (async job)."""
    n = row(conn, "SELECT user_id FROM notifications WHERE id=?", (notification_id,))
    if (n and _acct(conn) and link_for_user(conn, n["user_id"]) and _notify_on(conn, n["user_id"])
            and kind in ("mission", "action_result", "briefing")):
        jobs.enqueue(conn, "telegram_send", {"notification_id": notification_id}, dedupe_key=f"tg:{notification_id}")


def send_notification(conn, notification_id: str) -> dict:
    n = row(conn, "SELECT user_id, title, body FROM notifications WHERE id=?", (notification_id,))
    link = link_for_user(conn, n["user_id"]) if n else None
    if not n or not link:
        return {"sent": False}
    adapter(conn).send(int(link["chat_id"]), f"{n['title']}\n{n['body']}".strip())
    return {"sent": True}
