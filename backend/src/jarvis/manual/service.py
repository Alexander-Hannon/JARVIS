"""The Help manual: hand-written guides (manual/guides/*.md) + reference sections GENERATED from the live registries
(agent tools, action types with tiers, connectors), so the manual always matches what JARVIS can actually do.
`search_manual` lets JARVIS answer "how do I…" from the same text."""
from __future__ import annotations

import re
from pathlib import Path

GUIDES = Path(__file__).parent / "guides"
FAMILY_LABEL = {"chief": "Planning", "calendar": "Calendar", "tasks": "Tasks", "weather": "Weather", "memory": "Memory",
                "knowledge": "Documents", "school_gta": "School & GTA", "briefing": "Briefings", "github": "GitHub",
                "local_operator": "Workspace files", "email": "Email", "notes": "Notes", "messaging": "Slack",
                "life": "Music", "home": "Smart home", "travel": "Travel", "finance": "Money", "sandbox": "Sandbox",
                "skills": "Skills", "mcp": "MCP servers"}
RISK = {"read": "Reads only", "propose": "Asks before changing anything", "act": "Runs inside the sandbox"}


def guides() -> list[dict]:
    out = []
    for f in sorted(GUIDES.glob("*.md")):
        text = f.read_text()
        title = text.splitlines()[0].lstrip("# ").strip()
        out.append({"id": f.stem.split("-", 1)[1], "title": title, "markdown": text})
    return out


def reference(conn, uid: str) -> dict:
    from jarvis.actions import policy
    from jarvis.agents import tools as T
    from jarvis.integrations import connectors as C
    fams: dict[str, list] = {}
    for t in T.load_all().values():
        fams.setdefault(t.family, []).append({"name": t.name, "what": t.manual, "example": t.example, "risk": RISK[t.risk]})
    return {"capabilities": [{"family": f, "label": FAMILY_LABEL.get(f, f), "tools": sorted(v, key=lambda x: x["name"])}
                             for f, v in sorted(fams.items(), key=lambda kv: FAMILY_LABEL.get(kv[0], kv[0]))],
            "actions": policy.catalog(),
            "connectors": [{k: c[k] for k in ("id", "name", "summary", "reads", "writes", "status")} for c in C.catalog(conn, uid)]}


def _chunks() -> list[dict]:
    out = []
    for g in guides():
        for part in re.split(r"\n(?=#+ |\| |- \*\*)", g["markdown"]):
            if part.strip():
                out.append({"guide": g["title"], "id": g["id"], "text": part.strip()})
    return out


STOP = {"how", "the", "and", "can", "you", "what", "does", "for", "with", "jarvis", "this", "that", "into", "get", "use"}


def search(conn, uid: str, query: str, limit: int = 5) -> list[dict]:
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in STOP}
    if not terms:
        return []
    corpus = _chunks()
    for fam in reference(conn, uid)["capabilities"]:
        for t in fam["tools"]:
            corpus.append({"guide": f"Capabilities: {fam['label']}", "id": "reference",
                           "text": f"{t['name']}: {t['what']} Example: {t['example']}"})
    scored = []
    for c in corpus:
        words = set(re.findall(r"[a-z0-9]+", c["text"].lower()))
        title = c["guide"].lower()
        # stem-ish prefix match ("connect" ~ "connections"); a title hit means the whole guide is about it
        score = sum(1 for t in terms if any(w.startswith(t[:6]) for w in words)) \
            + sum(2 for t in terms if t[:6] in title)
        if score:
            scored.append((score, c))
    return [c for _, c in sorted(scored, key=lambda x: -x[0])[:limit]]
