"""Skills: reusable know-how packages in the Agent Skills / OpenClaw style — a folder with SKILL.md (YAML-ish
frontmatter `name`, `description`, then instructions) plus optional helper files.

Trust model: skills are installed only by AJ (from a local folder he points at, or a draft JARVIS proposed and he
approved as a T2 action). There is no public-registry auto-install — public skill marketplaces have shipped malicious
skills. Loading is progressive: the model sees only name + description (deferred tool) and pulls the body when relevant.
Helper scripts never run on the host; they are copied into the sandbox when the skill is loaded.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from jarvis.config import get_settings
from jarvis.core.util import audit

NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
MAX_FILES = 50
MAX_BYTES = 2 * 1024 * 1024
TEXT_EXT = {".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".csv", ".js", ".ts", ".html", ".css", ".sql"}


class SkillError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def root() -> Path:
    p = get_settings().data_dir / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def parse(text: str) -> tuple[dict, str]:
    meta: dict[str, str] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        body = m.group(2)
    return meta, body.strip()


# Skills are installed org-wide by admins; each user can switch individual skills off for themselves.
def _save_disabled(conn, uid: str, off: set[str]) -> None:
    from jarvis.core import settings as S
    S.update_user_data(conn, uid, lambda d: d.setdefault("skills", {}).__setitem__("disabled", sorted(off)))


def disabled(conn, uid: str) -> set[str]:
    from jarvis.core import settings as S
    return set((S.user_data(conn, uid).get("skills") or {}).get("disabled", []))


def list_skills(conn, uid: str | None = None) -> list[dict]:
    """With uid: `enabled` reflects that user's choice. Without (admin catalog): everything installed."""
    off = disabled(conn, uid) if uid else set()
    out = []
    for d in sorted(p for p in root().iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.exists():
            continue
        meta, body = parse(f.read_text("utf-8", "replace"))
        out.append({"name": d.name, "title": meta.get("name") or d.name, "description": meta.get("description", "")[:500],
                    "enabled": d.name not in off, "files": sorted(p.name for p in d.iterdir() if p.is_file()),
                    "chars": len(body)})
    return out


def _validate_tree(src: Path) -> list[Path]:
    if not (src / "SKILL.md").is_file():
        raise SkillError("no_skill_md", "That folder has no SKILL.md.")
    files, total = [], 0
    for p in src.rglob("*"):
        if p.is_symlink():
            raise SkillError("symlink", "Skills can't contain symlinks.")
        if p.is_file():
            if p.suffix.lower() not in TEXT_EXT:
                raise SkillError("file_type", f"Only text files are allowed in skills ({p.name}).")
            total += p.stat().st_size
            files.append(p)
    if len(files) > MAX_FILES or total > MAX_BYTES:
        raise SkillError("too_big", "Skills are limited to 50 text files / 2 MB.")
    return files


def install_from_folder(conn: sqlite3.Connection, folder: str, by: str | None = None) -> dict:
    """An admin points at a folder on the JARVIS host (authenticated UI). It's copied, so later edits there don't change
    it."""
    src = Path(folder).expanduser().resolve()
    if not src.is_dir():
        raise SkillError("not_found", "Folder not found on the JARVIS host.")
    _validate_tree(src)
    meta, body = parse((src / "SKILL.md").read_text("utf-8", "replace"))
    name = re.sub(r"[^a-z0-9-]+", "-", (meta.get("name") or src.name).lower()).strip("-")[:40]
    if not NAME.match(name) or not meta.get("description"):
        raise SkillError("bad_frontmatter", "SKILL.md needs frontmatter with `name` and `description`.")
    dest = root() / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)
    audit(conn, "user", "skill.installed", f"Skill installed: {name}", "skill", name, uid=by)
    return next(s for s in list_skills(conn) if s["name"] == name)


def write_skill(conn: sqlite3.Connection, name: str, description: str, body: str, by: str | None = None) -> dict:
    """Used only by the executor after an approved `skill.install` proposal (org-wide, so it needs an admin approver)."""
    if not NAME.match(name):
        raise SkillError("bad_name", "Skill names: lowercase letters, digits, dashes (2–40).")
    d = root() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description.strip()[:500]}\n---\n\n{body.strip()}\n")
    audit(conn, "user", "skill.installed", f"Skill installed from an approved draft: {name}", "skill", name, uid=by)
    return next(s for s in list_skills(conn) if s["name"] == name)


def set_enabled(conn, uid: str, name: str, enabled: bool) -> list[dict]:
    off = disabled(conn, uid)
    off = off - {name} if enabled else off | {name}
    _save_disabled(conn, uid, off)
    return list_skills(conn, uid)


def remove(conn, name: str, by: str | None = None) -> None:
    if not NAME.match(name) or not (root() / name).is_dir():
        raise SkillError("not_found", "Unknown skill.")
    shutil.rmtree(root() / name, ignore_errors=True)
    audit(conn, "user", "skill.removed", f"Skill removed: {name}", "skill", name, uid=by)


def load(name: str) -> dict:
    d = root() / name
    meta, body = parse((d / "SKILL.md").read_text("utf-8", "replace"))
    files = {p.relative_to(d).as_posix(): p.read_text("utf-8", "replace") for p in d.rglob("*")
             if p.is_file() and p.name != "SKILL.md"}
    return {"name": name, "description": meta.get("description", ""), "body": body, "files": files}
