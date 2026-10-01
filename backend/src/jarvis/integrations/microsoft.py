"""Microsoft identity platform (OAuth 2.0 auth-code + PKCE) and the Outlook mail adapter over Microsoft Graph v1.0.

Endpoints:
* authorize https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
* token     https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token   (refresh needs `scope` again)
* Graph     https://graph.microsoft.com/v1.0/me, /me/messages, /me/mailFolders/{inbox|drafts}/messages,
            POST /me/messages (draft), POST /me/messages/{id}/createReply, DELETE /me/messages/{id},
            POST /me/messages/{id}/send
Tenant `common` accepts both personal (outlook.com) and work/school accounts.

Scopes (least privilege for read + drafts): `openid email offline_access User.Read Mail.ReadWrite`.
`Mail.Send` is NOT requested unless AJ enables sending (Settings → Email), which re-consents with it added.

Idempotency: drafts carry a single-value extended property `String {GUID} Name JarvisOp` (not visible to recipients);
reconciliation filters the Drafts folder on it before any retry.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import MailDraft, MailMessage, ProviderError
from .gmail import html_to_text
from .google_oauth import GoogleCredentials, transport_error

LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
MAIL_SCOPES = ["openid", "email", "offline_access", "User.Read", "Mail.ReadWrite"]
SEND_SCOPE = "Mail.Send"
OP_PROP = "String {6c1b2f43-5f0e-4f6b-9b7e-2b1d8a3e4c51} Name JarvisOp"
SELECT = ("id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,importance,"
          "internetMessageId,webLink")


def graph_api_error(resp: httpx.Response, *, write: bool) -> ProviderError:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": resp.text[:300]}}
    err = body.get("error") if isinstance(body, dict) else {}
    message = (err or {}).get("message") or f"HTTP {resp.status_code}"
    code = resp.status_code
    retry_after = float(resp.headers["Retry-After"]) if (resp.headers.get("Retry-After") or "").isdigit() else None
    kw: dict[str, Any] = {"status": code, "detail": body}
    if code == 401:
        return ProviderError("auth", f"Microsoft auth failed: {message}", **kw)
    if code == 403:
        return ProviderError("forbidden", f"Microsoft forbidden: {message}", **kw)
    if code == 404:
        return ProviderError("not_found", f"Microsoft not found: {message}", **kw)
    if code == 409:
        return ProviderError("conflict", f"Microsoft conflict: {message}", **kw)
    if code == 412:
        return ProviderError("precondition_failed", f"Microsoft precondition failed: {message}", **kw)
    if code == 429:
        return ProviderError("rate_limit", f"Microsoft throttled: {message}", retry_after=retry_after, **kw)
    if code >= 500:
        return ProviderError("server", f"Microsoft server error {code}", outcome_known=not write, retry_after=retry_after,
                             **kw)
    return ProviderError("validation", f"Microsoft rejected request: {message}", **kw)


# --------------------------------------------------------------------------- OAuth
def build_auth_url(tenant: str, client_id: str, redirect_uri: str, scopes: list[str], state: str, challenge: str) -> str:
    if len(state) < 16:
        raise ProviderError("validation", "state must be an unguessable value (>=16 chars)")
    return LOGIN.format(tenant=tenant) + "/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "response_mode": "query",
        "scope": " ".join(scopes), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "select_account"})


def _token_post(tenant: str, data: dict, client: httpx.Client | None) -> dict:
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.post(LOGIN.format(tenant=tenant) + "/token", data=data, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=False) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code != 200:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("error") == "invalid_grant":
            raise ProviderError("auth", "Microsoft grant invalid/revoked; reconnect required",
                                status=resp.status_code, detail={"reconnect_required": True})
        if body.get("error") in ("invalid_client", "unauthorized_client"):
            raise ProviderError("config", f"Microsoft OAuth client misconfigured: {body.get('error')}", status=resp.status_code)
        raise ProviderError("server" if resp.status_code >= 500 else "validation",
                            f"Microsoft token request failed: {body.get('error')}", status=resp.status_code)
    return resp.json()


def _norm(body: dict, now: float, previous_refresh: str | None = None) -> dict:
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or previous_refresh,
            "expires_at": now + float(body.get("expires_in", 3600)), "scope": body.get("scope", ""),
            "token_type": body.get("token_type", "Bearer"), "id_token": body.get("id_token")}


def exchange_code(tenant: str, client_id: str, client_secret: str, redirect_uri: str, code: str, verifier: str,
                  scopes: list[str], *, client: httpx.Client | None = None) -> dict:
    body = _token_post(tenant, {"client_id": client_id, "client_secret": client_secret, "code": code,
                                "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
                                "scope": " ".join(scopes)}, client)
    return _norm(body, time.time())


class MicrosoftCredentials(GoogleCredentials):
    """Same refresh-on-expiry / retry-once-on-401 behavior as GoogleCredentials, against Microsoft endpoints."""

    def __init__(self, tokens: dict, client_id: str, client_secret: str, tenant: str, *,
                 token_updater: Callable[[dict], None] | None = None, http_client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(tokens, client_id, client_secret, token_updater=token_updater, http_client=http_client,
                         clock=clock)
        self.tenant = tenant

    def refresh(self) -> None:
        with self._lock:
            rt = self._tokens.get("refresh_token")
            if not rt:
                raise ProviderError("auth", "no refresh token stored; reconnect required")
            body = _token_post(self.tenant, {"client_id": self._client_id, "client_secret": self._client_secret,
                                             "refresh_token": rt, "grant_type": "refresh_token",
                                             "scope": self._tokens.get("scope") or " ".join(MAIL_SCOPES)}, self.client)
            self._tokens = _norm(body, self._clock(), previous_refresh=rt)
            new = dict(self._tokens)
        if self._updater:
            self._updater(new)

    def request(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code == 401:
            self.refresh()
            resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code >= 400:
            raise graph_api_error(resp, write=write)
        return resp


def fetch_me(access_token: str, *, client: httpx.Client | None = None) -> dict:
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.get(GRAPH + "/me", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=False) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code != 200:
        raise graph_api_error(resp, write=False)
    me = resp.json()
    return {"email": (me.get("mail") or me.get("userPrincipalName") or "").lower(), "name": me.get("displayName")}


# --------------------------------------------------------------------------- Outlook mail
def _addr(r: dict | None) -> str:
    return ((r or {}).get("emailAddress") or {}).get("address", "").lower()


def to_message(m: dict, *, with_body: bool) -> MailMessage:
    frm = (m.get("from") or {}).get("emailAddress") or {}
    body = (m.get("body") or {})
    text = body.get("content", "") if with_body else None
    if text and body.get("contentType", "").lower() == "html":
        text = html_to_text(text)
    return MailMessage(
        id=m["id"], thread_id=m.get("conversationId") or m["id"], subject=m.get("subject") or "(no subject)",
        sender=f"{frm.get('name', '')} <{frm.get('address', '')}>".strip(), sender_email=(frm.get("address") or "").lower(),
        to=[_addr(r) for r in m.get("toRecipients") or []], cc=[_addr(r) for r in m.get("ccRecipients") or []],
        date=(m.get("receivedDateTime") or "").replace(".0000000", "") or None, snippet=m.get("bodyPreview", ""),
        body_text=text[:20000] if text else text, unread=not m.get("isRead", True), important=m.get("importance") == "high",
        labels=[], message_id_header=m.get("internetMessageId"), web_url=m.get("webLink"))


class OutlookAdapter:
    name = "outlook"
    simulated = False

    def __init__(self, creds: MicrosoftCredentials):
        self.creds = creds

    def _get(self, path: str, params: dict | None = None, text_body: bool = False) -> dict:
        headers = {"Prefer": 'outlook.body-content-type="text"'} if text_body else {}
        return self.creds.request("GET", GRAPH + path, params=params or {}, headers=headers).json()

    def whoami(self) -> dict:
        return fetch_me(self.creds.access_token(), client=self.creds.client)

    def list_messages(self, query: str | None, max_results: int, unread_only: bool = False) -> list[MailMessage]:
        top = max(1, min(max_results, 50))
        if query:
            # $search can't be combined with $orderby; Graph returns relevance order.
            data = self._get("/me/messages", {"$search": f'"{query.replace(chr(34), "")}"', "$top": top, "$select": SELECT})
        else:
            params = {"$top": top, "$select": SELECT, "$orderby": "receivedDateTime desc"}
            if unread_only:
                params["$filter"] = "isRead eq false"
            data = self._get("/me/mailFolders/inbox/messages", params)
        msgs = [to_message(m, with_body=False) for m in data.get("value") or []]
        return [m for m in msgs if m.unread] if unread_only and query else msgs

    def get_message(self, message_id: str) -> MailMessage | None:
        try:
            return to_message(self._get(f"/me/messages/{message_id}", {"$select": SELECT + ",body"}, text_body=True),
                              with_body=True)
        except ProviderError as e:
            if e.kind == "not_found":
                return None
            raise

    def get_thread(self, thread_id: str) -> list[MailMessage]:
        safe = thread_id.replace("'", "''")
        data = self._get("/me/messages", {"$filter": f"conversationId eq '{safe}'", "$select": SELECT + ",body",
                                          "$top": 25}, text_body=True)
        return sorted((to_message(m, with_body=True) for m in data.get("value") or []), key=lambda m: m.date or "")

    # ---------------------------------------------------------------- drafts
    def _draft(self, m: dict) -> MailDraft:
        props = {p.get("id"): p.get("value") for p in m.get("singleValueExtendedProperties") or []}
        body = (m.get("body") or {})
        text = body.get("content", "")
        if body.get("contentType", "").lower() == "html":
            text = html_to_text(text)
        return MailDraft(id=m["id"], message_id=m["id"], thread_id=m.get("conversationId"),
                         to=[_addr(r) for r in m.get("toRecipients") or []], subject=m.get("subject") or "",
                         body_text=text, op_key=props.get(OP_PROP) or next(iter(props.values()), None),
                         web_url=m.get("webLink"))

    def create_draft(self, op_key: str, to: list[str], subject: str, body: str, *, cc: list[str] | None = None,
                     reply_to: MailMessage | None = None) -> MailDraft:
        fields = {"toRecipients": [{"emailAddress": {"address": a}} for a in to],
                  "ccRecipients": [{"emailAddress": {"address": a}} for a in cc or []],
                  "singleValueExtendedProperties": [{"id": OP_PROP, "value": op_key}]}
        if reply_to:
            m = self.creds.request("POST", f"{GRAPH}/me/messages/{reply_to.id}/createReply",
                                   json={"message": fields, "comment": body}, write=True).json()
        else:
            m = self.creds.request("POST", f"{GRAPH}/me/messages", write=True,
                                   json=fields | {"subject": subject, "body": {"contentType": "Text", "content": body}}).json()
        return self.get_draft(m["id"]) or self._draft(m)

    def get_draft(self, draft_id: str) -> MailDraft | None:
        try:
            m = self._get(f"/me/messages/{draft_id}", {"$select": "id,conversationId,subject,toRecipients,body,webLink,isDraft",
                                                       "$expand": f"singleValueExtendedProperties($filter=id eq '{OP_PROP}')"},
                          text_body=True)
        except ProviderError as e:
            if e.kind == "not_found":
                return None
            raise
        return self._draft(m) if m.get("isDraft", True) else None

    def find_draft_by_op(self, op_key: str) -> MailDraft | None:
        safe = op_key.replace("'", "''")
        data = self._get("/me/mailFolders/drafts/messages", {
            "$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{OP_PROP}' and ep/value eq '{safe}')",
            "$expand": f"singleValueExtendedProperties($filter=id eq '{OP_PROP}')",
            "$select": "id,conversationId,subject,toRecipients,body,webLink"}, text_body=True)
        hits = data.get("value") or []
        return self._draft(hits[0]) if hits else None

    def delete_draft(self, draft_id: str) -> None:
        try:
            self.creds.request("DELETE", f"{GRAPH}/me/messages/{draft_id}", write=True)
        except ProviderError as e:
            if e.kind != "not_found":
                raise

    def send_draft(self, draft_id: str) -> str:
        self.creds.request("POST", f"{GRAPH}/me/messages/{draft_id}/send", write=True)
        return draft_id
