"""Telegram channel: talk to JARVIS from your phone, get mission/action notifications, approve with a button.

Security model:
- Outbound polling from the worker (getUpdates); no webhook, no inbound port — the loopback-only rule holds.
- The bot answers exactly ONE chat: the one AJ paired with a one-time code from the authenticated UI. Everything else is
  ignored (and audited once per chat).
- Commands from the paired chat run through the normal command pipeline with origin 'channel', so auto-approve rules
  only apply if AJ opted them into channels.
- Approve buttons carry proposal id + version + a hash prefix; a revised plan voids them (same binding as the UI).
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
from jarvis.core.util import audit, iso, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

PAIR_TTL = timedelta(minutes=10)


class ChannelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def adapter(conn):
    if "telegram" in A._overrides:
        return A._overrides["telegram"]
    acct = A.get_account(conn, "telegram")
    if not acct:
        raise A.NotConnected("telegram")
    from jarvis.integrations.telegram import TelegramAdapter
    return TelegramAdapter(A.read_secret(acct))


def _acct(conn) -> dict | None:
    return A.get_account(conn, "telegram")


def _cfg_update(conn, acct_id: str, **kv) -> None:
    r = row(conn, "SELECT config FROM integration_accounts WHERE id=?", (acct_id,))
    cfg = jload(r["config"], {}) | kv
    conn.execute("UPDATE integration_accounts SET config=?, updated_at=? WHERE id=?", (json.dumps(cfg), now_iso(), acct_id))


# ------------------------------------------------------------------------------------------------ setup (UI only)
def connect(conn: sqlite3.Connection, token: str) -> dict:
    from jarvis.integrations.telegram import TelegramAdapter
    token = token.strip()
    if ":" not in token:
        raise ChannelError("bad_token", "That doesn't look like a bot token from @BotFather (digits:letters).")
    try:
        me = (A._overrides.get("telegram") or TelegramAdapter(token)).get_me()
    except ProviderError as e:
        raise ChannelError(e.kind, f"Telegram rejected the token ({e.kind}).") from None
    conn.execute("BEGIN IMMEDIATE")
    try:
        A.upsert_account(conn, "telegram", f"@{me.get('username')}", token, "bot_token",
                               external_account_id=str(me.get("id")), scopes=["bot"], config={"bot": me.get("username")})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return status(conn)


def start_pairing(conn: sqlite3.Connection) -> dict:
    acct = _acct(conn)
    if not acct:
        raise ChannelError("not_connected", "Connect the Telegram bot first.")
    code = f"{pysecrets.randbelow(10**6):06d}"
    conn.execute("BEGIN IMMEDIATE")
    _cfg_update(conn, acct["id"], pair_hash=hashlib.sha256(code.encode()).hexdigest(),
                pair_expires=iso(utcnow() + PAIR_TTL), pair_attempts=0)
    conn.execute("COMMIT")
    return {"code": code, "instructions": f"Send  /pair {code}  to @{acct['config'].get('bot')} within 10 minutes.",
            "expires_in_s": int(PAIR_TTL.total_seconds())}


def unpair(conn) -> dict:
    acct = _acct(conn)
    if acct:
        conn.execute("BEGIN IMMEDIATE")
        _cfg_update(conn, acct["id"], chat_id=None, chat_name=None)
        audit(conn, "user", "channel.unpaired", "Telegram chat unpaired", "integration_account", acct["id"])
        conn.execute("COMMIT")
    return status(conn)


def status(conn) -> dict:
    acct = _acct(conn)
    if not acct:
        return {"status": "not_connected"}
    cfg = acct["config"]
    return {"status": "paired" if cfg.get("chat_id") else "connected", "bot": cfg.get("bot"),
            "chat_name": cfg.get("chat_name"), "account_id": acct["id"], "notify": cfg.get("notify", True),
            "simulated": "telegram" in A._overrides}


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


def _paired_chat(conn) -> int | None:
    acct = _acct(conn)
    return (acct or {}).get("config", {}).get("chat_id")


def handle_update(conn, tg, u: dict) -> None:
    if "callback_query" in u:
        return _on_callback(conn, tg, u["callback_query"])
    msg = u.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    paired = _paired_chat(conn)
    if text.startswith("/pair"):
        return _try_pair(conn, tg, chat, msg, text)
    if chat is None or chat != paired:
        acct = _acct(conn)
        seen = set(acct["config"].get("ignored_chats", []))
        if chat not in seen:
            conn.execute("BEGIN IMMEDIATE")
            _cfg_update(conn, acct["id"], ignored_chats=sorted(seen | {chat})[-50:])
            audit(conn, "channel", "channel.ignored", "Ignored Telegram message from an unpaired chat", "integration_account",
                  acct["id"])
            conn.execute("COMMIT")
        return None
    if msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date"):
        return _save_forward(conn, tg, chat, msg)
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
    return _command(conn, tg, chat, text, msg)


def _try_pair(conn, tg, chat, msg, text) -> None:
    acct = _acct(conn)
    cfg = acct["config"]
    code = text.split(maxsplit=1)[1].strip() if " " in text else ""
    ok = (cfg.get("pair_hash") and cfg.get("pair_expires") and parse_iso(cfg["pair_expires"]) > utcnow()
          and int(cfg.get("pair_attempts") or 0) < 5
          and pysecrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(), cfg["pair_hash"]))
    conn.execute("BEGIN IMMEDIATE")
    if ok:
        name = (msg.get("from") or {}).get("username") or (msg.get("chat") or {}).get("first_name") or str(chat)
        _cfg_update(conn, acct["id"], chat_id=chat, chat_name=name, pair_hash=None, pair_expires=None, pair_attempts=0)
        audit(conn, "user", "channel.paired", f"Telegram chat paired ({name})", "integration_account", acct["id"])
    else:
        _cfg_update(conn, acct["id"], pair_attempts=int(cfg.get("pair_attempts") or 0) + 1)
    conn.execute("COMMIT")
    tg.send(chat, "Paired. I'll answer you here." if ok else "That code isn't valid. Get a new one in JARVIS → Settings.")


def _save_forward(conn, tg, chat, msg) -> None:
    from jarvis.knowledge.ingest import ingest_bytes
    body = (msg.get("text") or msg.get("caption") or "").strip()
    if not body:
        tg.send(chat, "I can only save forwarded text for now.")
        return
    when = datetime.fromtimestamp(msg.get("date") or 0, UTC).strftime("%Y-%m-%d %H%M")
    ingest_bytes(conn, body.encode(), f"Forwarded {when}.txt", "text/plain", None, origin="telegram",
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


def _conversation(conn) -> str:
    from jarvis.agents.chief import ensure_conversation
    acct = _acct(conn)
    cid = acct["config"].get("conversation_id")
    if not cid or not row(conn, "SELECT 1 FROM conversations WHERE id=?", (cid,)):
        cid = ensure_conversation(conn, None)
        conn.execute("BEGIN IMMEDIATE")
        _cfg_update(conn, acct["id"], conversation_id=cid)
        conn.execute("COMMIT")
    return cid


def _command(conn, tg, chat, text: str, msg: dict) -> None:
    from jarvis.agents import context as ctx
    from jarvis.agents.chief import CommandInput, handle
    sent = datetime.fromtimestamp(msg.get("date") or utcnow().timestamp(), UTC)
    resp = handle(conn, CommandInput(conversation_id=_conversation(conn), text=text, channel="telegram",
                                     captured_at=iso(sent), tz=ctx.user_tz(conn), origin="channel"))
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
    if chat is None or chat != _paired_chat(conn):
        tg.answer_callback(cq["id"], "Not authorized.")
        return
    try:
        op, pid, version, hprefix = (cq.get("data") or "").split(":")
        cur = P.current_version(conn, pid)
        if int(version) != cur["current_version"] or not cur["pv"]["hash"].startswith(hprefix):
            tg.answer_callback(cq["id"], "That plan changed; open the latest version.")
            return
        if op == "ap":
            acts = P.actions_for_version(conn, cur["pv"]["id"])
            P.approve(conn, pid, int(version), cur["pv"]["hash"], [a["item_index"] for a in acts], channel="telegram",
                      session_id=None, evidence="Telegram approve button")
            tg.answer_callback(cq["id"], "Approved — running it now.")
        elif op == "rj":
            P.reject(conn, pid)
            tg.answer_callback(cq["id"], "Rejected.")
        tg.clear_buttons(chat, (cq.get("message") or {}).get("message_id"))
    except P.ProposalError as e:
        tg.answer_callback(cq["id"], e.message)
    except ValueError:
        tg.answer_callback(cq["id"], "Unknown button.")


# ------------------------------------------------------------------------------------------------ notifications out
def maybe_forward_notification(conn, notification_id: str, kind: str) -> None:
    """Called by notify(): mirror mission/action/briefing notifications to the paired chat (async job)."""
    acct = _acct(conn)
    if acct and acct["config"].get("chat_id") and acct["config"].get("notify", True) and kind in ("mission", "action_result",
                                                                                                   "briefing"):
        jobs.enqueue(conn, "telegram_send", {"notification_id": notification_id}, dedupe_key=f"tg:{notification_id}")


def send_notification(conn, notification_id: str) -> dict:
    n = row(conn, "SELECT title, body FROM notifications WHERE id=?", (notification_id,))
    chat = _paired_chat(conn)
    if not n or not chat:
        return {"sent": False}
    adapter(conn).send(chat, f"{n['title']}\n{n['body']}".strip())
    return {"sent": True}
