"""Who is acting, and what they may do.

Every authenticated request resolves to a `Principal` (dependencies `principal` / `require(perm)` live in
`api/app.py` next to the session check). Services receive `p.user_id` explicitly (no globals) and scope
every per-user query by it. Private by default: a user's conversations, memories, documents, proposals, rules,
integrations and sandbox are theirs alone; admins manage people and org configuration and see audit *metadata*, not
content. Foreign or unknown object ids are answered with 404, never 403, so existence doesn't leak.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

ROLES = ("owner", "admin", "approver", "member", "auditor")
PRIVILEGED = frozenset({"owner", "admin", "approver"})

# Permission → roles. Kept as data so the role matrix test and the Help manual can read it.
PERMS: dict[str, frozenset[str]] = {
    # use JARVIS for yourself: commands, own data, own integrations, own rules, own sandbox
    "use": frozenset({"owner", "admin", "approver", "member"}),
    # second approver on `dual` action types
    "approve.dual": frozenset({"owner", "admin", "approver"}),
    "users.manage": frozenset({"owner", "admin"}),
    "users.owner": frozenset({"owner"}),           # create/demote owners
    "org.settings": frozenset({"owner", "admin"}),
    # org-wide integration surface: MCP servers, skills, Telegram bot, host workspace roots, sandbox image
    "org.integrations": frozenset({"owner", "admin"}),
    "org.backups": frozenset({"owner", "admin"}),
    "org.doctor": frozenset({"owner", "admin"}),
    "org.pause": frozenset({"owner", "admin"}),
    "audit.read": frozenset({"owner", "admin", "auditor"}),
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str
    session_id: str
    auth_level: str = "password"

    def can(self, perm: str) -> bool:
        return self.role in PERMS[perm]

    @property
    def privileged(self) -> bool:
        return self.role in PRIVILEGED


def from_session(s: dict) -> Principal:
    return Principal(user_id=s["user_id"], username=s["username"], display_name=s["display_name"], role=s["role"],
                     session_id=s["id"], auth_level=s.get("auth_level") or "password")


def forbid(message: str = "You don't have permission to do that.") -> HTTPException:
    return HTTPException(403, message)


def not_found() -> HTTPException:
    return HTTPException(404, "Not found.")
