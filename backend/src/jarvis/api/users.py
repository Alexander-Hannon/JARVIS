"""User management (admins): invite people, set roles, disable accounts, re-issue invite links.

Rules enforced here (application code):
- Only owners grant or remove the owner/admin roles; admins manage everyone else (approver, member, auditor).
- The last active owner can't be demoted or disabled, and nobody changes their own role or status here.
- Disabling ends the person's sessions immediately and pauses their missions and schedules; their data is kept.
- Admins never see anyone's content through this API — only account metadata.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from jarvis.api.app import db, require
from jarvis.config import get_settings
from jarvis.core.db import row, rows, tx
from jarvis.core.util import audit, new_id, now_iso
from jarvis.security import auth
from jarvis.security.principal import ROLES, Principal

router = APIRouter(prefix="/api")
OWNER_ONLY_ROLES = {"owner", "admin"}


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    email: str | None = Field(None, max_length=200)
    role: str = "member"


class UserPatch(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=80)
    role: str | None = None
    status: str | None = Field(None, pattern="^(active|disabled)$")


def _view(conn, u: dict) -> dict:
    mfa = conn.execute("SELECT COUNT(*) FROM user_mfa WHERE user_id=? AND (kind='webauthn' OR confirmed_at IS NOT NULL)",
                       (u["id"],)).fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at>?",
                          (u["id"], now_iso())).fetchone()[0]
    pending = row(conn, "SELECT expires_at FROM invites WHERE user_id=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1",
                  (u["id"],))
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "email": u["email"],
            "role": u["role"], "status": u["status"], "created_at": u["created_at"], "last_login_at": u["last_login_at"],
            "mfa_methods": mfa, "active_sessions": active, "has_password": bool(u["password_hash"]),
            "invite_pending_until": pending["expires_at"] if pending else None}


def _invite_url(request: Request, token: str) -> str:
    base = get_settings().public_origin or str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/invite/{token}"


def _check_role_change(me: Principal, current: str | None, new: str) -> None:
    if new not in ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}.")
    if (new in OWNER_ONLY_ROLES or current in OWNER_ONLY_ROLES) and not me.can("users.owner"):
        raise HTTPException(403, "Only an owner can grant or remove the owner and admin roles.")


def _last_owner(conn, uid: str) -> bool:
    n = conn.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND status='active' AND id<>?", (uid,)).fetchone()[0]
    return n == 0


@router.get("/users")
def list_users(me: Principal = Depends(require("users.manage")), conn=Depends(db)):
    return [_view(conn, u) for u in rows(conn, "SELECT * FROM users ORDER BY created_at")]


@router.post("/users")
def create_user(body: UserIn, request: Request, me: Principal = Depends(require("users.manage")), conn=Depends(db)):
    """Creates the account and returns a single-use invite link (72 h) where the person sets their own password."""
    from jarvis.core.seed import seed_user
    uname = auth.normalize_username(body.username)
    _check_role_change(me, None, body.role)
    with tx(conn):
        if row(conn, "SELECT id FROM users WHERE username=? COLLATE NOCASE", (uname,)):
            raise HTTPException(409, "That username is taken.")
        uid = new_id("usr")
        conn.execute("INSERT INTO users (id, username, display_name, email, role, created_at) VALUES (?,?,?,?,?,?)",
                     (uid, uname, body.display_name.strip(), (body.email or "").strip() or None, body.role, now_iso()))
        seed_user(conn, uid, body.display_name.strip())
        token = auth.create_invite(conn, uid, me.user_id)
        audit(conn, "user", "user.invited", f"Invited {uname} as {body.role}", "user", uid, uid=me.user_id)
    return {"user": _view(conn, row(conn, "SELECT * FROM users WHERE id=?", (uid,))),
            "invite_url": _invite_url(request, token), "invite_expires_hours": int(auth.INVITE_TTL.total_seconds() // 3600)}


@router.patch("/users/{uid}")
def update_user(uid: str, body: UserPatch, me: Principal = Depends(require("users.manage")), conn=Depends(db)):
    with tx(conn):
        u = row(conn, "SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "Not found.")
        if u["role"] in OWNER_ONLY_ROLES and uid != me.user_id and not me.can("users.owner"):
            raise HTTPException(403, "Only an owner can change an owner's or admin's account.")
        changes = []
        if body.role and body.role != u["role"]:
            if uid == me.user_id:
                raise HTTPException(400, "You can't change your own role.")
            _check_role_change(me, u["role"], body.role)
            if u["role"] == "owner" and _last_owner(conn, uid):
                raise HTTPException(400, "This is the last owner; make someone else an owner first.")
            conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
            changes.append(f"role {u['role']} → {body.role}")
        if body.status and body.status != u["status"]:
            if uid == me.user_id:
                raise HTTPException(400, "You can't disable your own account.")
            if u["role"] in OWNER_ONLY_ROLES and not me.can("users.owner"):
                raise HTTPException(403, "Only an owner can disable an owner or admin.")
            if body.status == "disabled":
                if u["role"] == "owner" and _last_owner(conn, uid):
                    raise HTTPException(400, "This is the last owner; it can't be disabled.")
                conn.execute("UPDATE users SET status='disabled', disabled_at=? WHERE id=?", (now_iso(), uid))
                conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), uid))
                conn.execute("UPDATE invites SET used_at=? WHERE user_id=? AND used_at IS NULL", (now_iso(), uid))
                conn.execute("UPDATE missions SET enabled=0, next_run_at=NULL WHERE user_id=?", (uid,))
                conn.execute("UPDATE schedules SET enabled=0, next_run_at=NULL WHERE user_id=?", (uid,))
                changes.append("disabled (sessions ended, missions and schedules paused)")
            else:
                conn.execute("UPDATE users SET status='active', disabled_at=NULL, last_login_at=? WHERE id=?",
                             (now_iso(), uid))     # re-enabling resets the dormancy clock
                changes.append("re-enabled")
        if body.display_name and body.display_name.strip() != u["display_name"]:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), uid))
            changes.append("display name")
        if changes:
            audit(conn, "user", "user.updated", f"{u['username']}: " + "; ".join(changes), "user", uid, uid=me.user_id)
    return _view(conn, row(conn, "SELECT * FROM users WHERE id=?", (uid,)))


@router.post("/users/{uid}/invite")
def reinvite(uid: str, request: Request, me: Principal = Depends(require("users.manage")), conn=Depends(db)):
    """A fresh invite link (also how an admin resets someone's forgotten password; the old one stops working)."""
    with tx(conn):
        u = row(conn, "SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "Not found.")
        if u["role"] in OWNER_ONLY_ROLES and not me.can("users.owner"):
            raise HTTPException(403, "Only an owner can reset an owner's or admin's password.")
        if u["status"] != "active":
            raise HTTPException(400, "Re-enable the account first.")
        token = auth.create_invite(conn, uid, me.user_id)
        audit(conn, "user", "user.reinvited", f"New invite link for {u['username']}", "user", uid, uid=me.user_id)
    return {"invite_url": _invite_url(request, token), "invite_expires_hours": int(auth.INVITE_TTL.total_seconds() // 3600)}


@router.post("/users/{uid}/reset-mfa")
def reset_mfa(uid: str, me: Principal = Depends(require("users.manage")), conn=Depends(db)):
    """Lost phone: wipe someone's second factors; their sessions end and they enroll again at next sign-in."""
    from jarvis.security import mfa
    with tx(conn):
        u = row(conn, "SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "Not found.")
        if uid == me.user_id:
            raise HTTPException(400, "Manage your own second factors in Settings → Account.")
        if u["role"] in OWNER_ONLY_ROLES and not me.can("users.owner"):
            raise HTTPException(403, "Only an owner can reset an owner's or admin's second factors.")
        mfa.reset_user(conn, uid, me.user_id)
    return _view(conn, row(conn, "SELECT * FROM users WHERE id=?", (uid,)))
