"""Gmail adapter (REST v1 over GoogleCredentials; no Google SDK).

Endpoints (https://developers.google.com/workspace/gmail/api/reference/rest):
* users.getProfile        GET  /users/me/profile
* users.messages.list     GET  /users/me/messages?q=&maxResults=&labelIds=
* users.messages.get      GET  /users/me/messages/{id}?format=metadata|full
* users.threads.get       GET  /users/me/threads/{id}?format=full
* users.drafts.create     POST /users/me/drafts  {"message": {"raw": <base64url RFC 2822>, "threadId": ...}}
* users.drafts.get/list/delete, users.drafts.send POST /users/me/drafts/send {"id": ...}

Scopes: `gmail.readonly` (read/triage) + `gmail.compose` (create/delete drafts; it also permits sending, which JARVIS
only does through the T2 `email.send` action AJ must enable). Both are Google "restricted" scopes: fine for a
single-user Testing-mode project, a security assessment applies only to apps distributing restricted-scope data.

Idempotency: Gmail has no request idempotency key, so every draft carries an `X-Jarvis-Op` MIME header (invisible in
the composed mail). Before any retry the executor lists recent drafts and matches that header.
"""
from __future__ import annotations

import base64
import html
import re
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from datetime import UTC, datetime

from .base import MailDraft, MailMessage, ProviderError
from .google_oauth import GoogleCredentials

API = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SCOPES_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
OP_HEADER = "X-Jarvis-Op"
META_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", OP_HEADER]
MAX_BODY = 20000


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", h)
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def _body(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html. Walks multipart trees."""
    plain, rich = [], []

    def walk(p: dict) -> None:
        mt = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64d(data).decode("utf-8", "replace"))
        elif data and mt == "text/html":
            rich.append(html_to_text(_b64d(data).decode("utf-8", "replace")))
        for c in p.get("parts") or []:
            walk(c)
    walk(payload)
    return ("\n".join(plain) if plain else "\n".join(rich))[:MAX_BODY]


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (payload.get("headers") or [])}


def _addrs(v: str | None) -> list[str]:
    return [a for _, a in getaddresses([v or ""]) if a]


def to_message(m: dict, *, with_body: bool) -> MailMessage:
    payload = m.get("payload") or {}
    h = _headers(payload)
    labels = m.get("labelIds") or []
    ts = m.get("internalDate")
    return MailMessage(
        id=m["id"], thread_id=m.get("threadId", m["id"]), subject=h.get("subject", "(no subject)"),
        sender=h.get("from", ""), sender_email=parseaddr(h.get("from", ""))[1].lower(), to=_addrs(h.get("to")),
        cc=_addrs(h.get("cc")),
        date=datetime.fromtimestamp(int(ts) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
        snippet=html.unescape(m.get("snippet", "")), body_text=_body(payload) if with_body else None,
        unread="UNREAD" in labels, important="IMPORTANT" in labels, labels=labels,
        message_id_header=h.get("message-id"), web_url=f"https://mail.google.com/mail/u/0/#all/{m.get('threadId', m['id'])}")


def build_mime(op_key: str, sender: str | None, to: list[str], subject: str, body: str, cc: list[str] | None,
               reply_to: MailMessage | None) -> EmailMessage:
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg[OP_HEADER] = op_key
    if reply_to and reply_to.message_id_header:
        msg["In-Reply-To"] = reply_to.message_id_header
        msg["References"] = reply_to.message_id_header
    msg.set_content(body)
    return msg


class GmailAdapter:
    name = "gmail"
    simulated = False

    def __init__(self, creds: GoogleCredentials):
        self.creds = creds
        self._me: str | None = None

    def _get(self, path: str, **params) -> dict:
        return self.creds.request("GET", API + path, params=params).json()

    def whoami(self) -> dict:
        p = self._get("/profile")
        self._me = p.get("emailAddress")
        return {"email": self._me, "name": self._me}

    def list_messages(self, query: str | None, max_results: int, unread_only: bool = False) -> list[MailMessage]:
        q = " ".join(x for x in [query or "", "is:unread" if unread_only else ""] if x).strip()
        params: dict = {"maxResults": max(1, min(max_results, 50))}
        if q:
            params["q"] = q
        else:
            params["labelIds"] = "INBOX"
        ids = self._get("/messages", **params).get("messages") or []
        out = []
        for i in ids:
            m = self.creds.request("GET", f"{API}/messages/{i['id']}",
                                   params=[("format", "metadata")] + [("metadataHeaders", h) for h in META_HEADERS]).json()
            out.append(to_message(m, with_body=False))
        return out

    def get_message(self, message_id: str) -> MailMessage | None:
        try:
            return to_message(self._get(f"/messages/{message_id}", format="full"), with_body=True)
        except ProviderError as e:
            if e.kind == "not_found":
                return None
            raise

    def get_thread(self, thread_id: str) -> list[MailMessage]:
        t = self._get(f"/threads/{thread_id}", format="full")
        return [to_message(m, with_body=True) for m in t.get("messages") or []]

    # ---------------------------------------------------------------- drafts
    def _draft(self, d: dict) -> MailDraft:
        m = d.get("message") or {}
        h = _headers(m.get("payload") or {})
        return MailDraft(id=d["id"], message_id=m.get("id", ""), thread_id=m.get("threadId"), to=_addrs(h.get("to")),
                         subject=h.get("subject", ""), body_text=_body(m.get("payload") or {}),
                         op_key=h.get(OP_HEADER.lower()), web_url="https://mail.google.com/mail/u/0/#drafts")

    def create_draft(self, op_key: str, to: list[str], subject: str, body: str, *, cc: list[str] | None = None,
                     reply_to: MailMessage | None = None) -> MailDraft:
        mime = build_mime(op_key, None, to, subject, body, cc, reply_to)
        msg: dict = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if reply_to:
            msg["threadId"] = reply_to.thread_id
        d = self.creds.request("POST", f"{API}/drafts", json={"message": msg}, write=True).json()
        return self.get_draft(d["id"]) or MailDraft(d["id"], d.get("message", {}).get("id", ""), msg.get("threadId"), to,
                                                    subject, body, op_key)

    def get_draft(self, draft_id: str) -> MailDraft | None:
        try:
            return self._draft(self._get(f"/drafts/{draft_id}", format="full"))
        except ProviderError as e:
            if e.kind == "not_found":
                return None
            raise

    def find_draft_by_op(self, op_key: str) -> MailDraft | None:
        for d in self._get("/drafts", maxResults=50).get("drafts") or []:
            full = self.get_draft(d["id"])
            if full and full.op_key == op_key:
                return full
        return None

    def delete_draft(self, draft_id: str) -> None:
        try:
            self.creds.request("DELETE", f"{API}/drafts/{draft_id}", write=True)
        except ProviderError as e:
            if e.kind != "not_found":   # already gone = desired end state
                raise

    def send_draft(self, draft_id: str) -> str:
        return self.creds.request("POST", f"{API}/drafts/send", json={"id": draft_id}, write=True).json()["id"]
