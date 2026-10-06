"""How JARVIS addresses each user, and what it knows about where they are. Replaces the V1–V4 hardcoded "AJ" / "sir" /
Huntsville: AJ's own settings carry those values, so his prompts read exactly as before."""
from __future__ import annotations

from dataclasses import dataclass

from jarvis.core import settings as S


PRONOUNS = {"he": ("him", "his"), "she": ("her", "her"), "they": ("them", "their")}


@dataclass(frozen=True)
class Persona:
    assistant: str            # the brand name (org setting), e.g. "Jarvis"
    name: str                 # how to address the user, e.g. "AJ"
    honorific: str | None     # optional, e.g. "sir" (used sparingly)
    full_name: str            # the account's display name
    location: str | None      # e.g. "Huntsville, AL"
    tz: str
    obj: str = "them"         # pronoun forms used in prompts ("him", "his" for AJ)
    poss: str = "their"


def persona(conn, uid: str) -> Persona:
    data = S.user_data(conn, uid)
    addr = data.get("address_as") or {}
    display = S.display_name(conn, uid)
    brand = (S.org_data(conn).get("brand") or {}).get("assistant_name") or "Jarvis"
    obj, poss = PRONOUNS.get(addr.get("pronoun") or "they", PRONOUNS["they"])
    return Persona(assistant=brand, name=addr.get("name") or display, honorific=addr.get("honorific"),
                   full_name=display, location=(data.get("location") or {}).get("name"), tz=S.user_tz(conn, uid),
                   obj=obj, poss=poss)
