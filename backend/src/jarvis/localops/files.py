"""Typed file tools confined to user-designated workspace roots.

Defenses: realpath containment (defeats ../ traversal and symlink escapes), refusal of hidden
components and secret-looking names, refusal of roots that are the home directory / system dirs /
credential stores, byte limits, no overwrite without an approved diff + expected hash, recoverable
backups before overwrite, atomic replace. No shell, no arbitrary execution.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from jarvis.config import get_settings
from jarvis.core.db import row, rows
from jarvis.core.util import audit, new_id, now_iso

MAX_READ = 1_000_000
MAX_WRITE = 2_000_000
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx", "credentials*",
                   "*secret*", "*.kdbx", "token*.json", "*.sqlite", "*.db", "cookies*", "login data"]
FORBIDDEN_ROOT_PARTS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", ".mozilla", ".password-store",
                        "AppData", "Library", "keychains", ".local"}
TEXT_EXT = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".html", ".css", ".ipynb", ".tex",
            ".rst", ".org", ".xml", ".ini", ".toml", ".log"}


class FileOpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def validate_root(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise FileOpError("bad_root", "Root must be an absolute path.")
    rp = p.resolve()
    if not rp.is_dir():
        raise FileOpError("bad_root", "Root must be an existing directory.")
    home = _home()
    if rp == Path("/") or rp == home or home.is_relative_to(rp):
        raise FileOpError("bad_root", "Choose a specific workspace folder, not your home or system root.")
    if any(part in FORBIDDEN_ROOT_PARTS or (part.startswith(".") and part not in (".",)) for part in rp.parts):
        raise FileOpError("bad_root", "Hidden, config or credential directories cannot be workspace roots.")
    for sys_dir in ("/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/var", "/usr", "/bin", "/sbin"):
        if rp == Path(sys_dir) or rp.is_relative_to(sys_dir):
            raise FileOpError("bad_root", "System directories cannot be workspace roots.")
    data_dir = get_settings().data_dir.resolve()
    if rp == data_dir or rp.is_relative_to(data_dir) or data_dir.is_relative_to(rp):
        raise FileOpError("bad_root", "Jarvis's own data directory cannot be a workspace root.")
    return rp


# Workspace roots are folders on the machine running Jarvis. Only admins designate them (org.integrations), and each
# one is assigned to exactly one user: only that user's agent can read it or propose writes into it.
def _view(r: dict) -> dict:
    return {"id": r["id"], "path": r["resource_id"], "label": r["resource_name"], "writable": r["access"] == "write",
            "user_id": r["user_id"]}


def add_root(conn, uid: str, path: str, label: str, writable: bool, *, by: str | None = None) -> dict:
    rp = validate_root(path)
    gid = new_id("root")
    conn.execute("INSERT INTO scope_grants (id, account_id, resource_type, resource_id, resource_name, access, created_at, "
                 "user_id) VALUES (?,NULL,'local_root',?,?,?,?,?)",
                 (gid, str(rp), label, "write" if writable else "read", now_iso(), uid))
    audit(conn, "user", "localops.root_added", f"Workspace root {label} ({'read/write' if writable else 'read-only'})",
          "scope_grant", gid, {"assigned_to": uid}, uid=by or uid)
    return {"id": gid, "path": str(rp), "label": label, "writable": writable, "user_id": uid}


def list_roots(conn, uid: str) -> list[dict]:
    return [_view(r) for r in rows(conn, "SELECT * FROM scope_grants WHERE resource_type='local_root' AND user_id=? "
                                         "ORDER BY created_at", (uid,))]


def list_all_roots(conn) -> list[dict]:
    """Admin view: every designated root and who it's assigned to."""
    return [_view(r) for r in rows(conn, "SELECT * FROM scope_grants WHERE resource_type='local_root' ORDER BY created_at")]


def get_root(conn, uid: str, root_id: str) -> dict:
    r = row(conn, "SELECT * FROM scope_grants WHERE id=? AND resource_type='local_root' AND user_id=?", (root_id, uid))
    if not r:
        raise FileOpError("no_root", "Unknown workspace root.")
    return _view(r)


def remove_root(conn, root_id: str) -> bool:
    return conn.execute("DELETE FROM scope_grants WHERE id=? AND resource_type='local_root'", (root_id,)).rowcount > 0


def _is_secretish(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_PATTERNS)


def resolve_in_root(root: dict, rel: str, must_exist: bool = True) -> Path:
    if rel is None:
        raise FileOpError("bad_path", "Path required.")
    rel = rel.replace("\\", "/").strip()
    if rel.startswith("/") or rel.startswith("~") or ":" in rel.split("/")[0]:
        raise FileOpError("bad_path", "Use a path relative to the workspace root.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileOpError("traversal", "Path traversal ('..') is not allowed.")
    if any(p.startswith(".") for p in parts):
        raise FileOpError("hidden", "Hidden files/folders are off-limits.")
    if parts and _is_secretish(parts[-1]):
        raise FileOpError("secret", "That looks like a secret/credential file; Jarvis won't touch it.")
    base = Path(root["path"]).resolve()
    target = base.joinpath(*parts)
    real = Path(os.path.realpath(target))
    if not (real == base or real.is_relative_to(base)):
        raise FileOpError("escape", "Path resolves outside the workspace root (symlink or traversal).")
    # also check each existing ancestor (a symlinked directory inside the root pointing outside)
    cur = base
    for p in parts[:-1]:
        cur = cur / p
        if cur.exists() and not Path(os.path.realpath(cur)).is_relative_to(base):
            raise FileOpError("escape", "A folder in that path links outside the workspace root.")
    if must_exist and not real.exists():
        raise FileOpError("not_found", f"'{rel}' does not exist in {root['label']}.")
    return real


def list_dir(root: dict, rel: str = "") -> list[dict]:
    p = resolve_in_root(root, rel or ".", must_exist=True) if rel else Path(root["path"]).resolve()
    if not p.is_dir():
        raise FileOpError("not_dir", "Not a folder.")
    out = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:500]:
        if child.name.startswith(".") or _is_secretish(child.name):
            continue
        real = Path(os.path.realpath(child))
        if not real.is_relative_to(Path(root["path"]).resolve()):
            continue  # symlink pointing outside: hidden
        st = child.stat()
        out.append({"name": child.name, "path": str(child.relative_to(Path(root["path"]).resolve())), "dir": child.is_dir(),
                    "bytes": st.st_size if child.is_file() else None, "modified": st.st_mtime})
    return out


def read_file(root: dict, rel: str) -> dict:
    p = resolve_in_root(root, rel)
    if p.is_dir():
        raise FileOpError("is_dir", "That's a folder.")
    if p.stat().st_size > MAX_READ:
        raise FileOpError("too_large", "File exceeds 1 MB read limit.")
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FileOpError("binary", "Binary file; upload it to Knowledge for document processing instead.") from e
    return {"path": rel, "text": text, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def search(root: dict, q: str, limit: int = 30) -> list[dict]:
    base = Path(root["path"]).resolve()
    ql = q.lower()
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for f in filenames:
            if f.startswith(".") or _is_secretish(f):
                continue
            fp = Path(dirpath) / f
            rel = str(fp.relative_to(base))
            hit = ql in f.lower()
            snippet = None
            if not hit and fp.suffix.lower() in TEXT_EXT and fp.stat().st_size < 300_000 and not fp.is_symlink():
                try:
                    txt = fp.read_text(errors="ignore")
                    i = txt.lower().find(ql)
                    if i >= 0:
                        hit, snippet = True, txt[max(0, i - 60): i + 80].replace("\n", " ")
                except OSError:
                    pass
            if hit:
                out.append({"path": rel, "snippet": snippet})
                if len(out) >= limit:
                    return out
    return out


def diff_preview(old: str | None, new: str, rel: str) -> str:
    if old is None:
        return f"(new file {rel}, {len(new.encode())} bytes)\n" + new[:4000]
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))[:8000]


def plan_write(conn, uid: str, root_id: str, rel: str, content: str, allow_overwrite: bool = False) -> dict:
    """Build args/before/after for a file.write proposal (no side effects)."""
    root = get_root(conn, uid, root_id)
    if not root["writable"]:
        raise FileOpError("read_only", f"Workspace '{root['label']}' is read-only.")
    if len(content.encode()) > MAX_WRITE:
        raise FileOpError("too_large", "Content exceeds 2 MB.")
    p = resolve_in_root(root, rel, must_exist=False)
    old = None
    expected = None
    if p.exists():
        if not allow_overwrite:
            raise FileOpError("exists", f"'{rel}' already exists; say 'overwrite' to see a diff proposal.")
        cur = read_file(root, rel)
        old, expected = cur["text"], cur["sha256"]
    return {"args": {"root_id": root_id, "path": rel, "content": content, "mode": "overwrite" if old is not None else "create",
                     "expected_sha256": expected},
            "before": {"exists": old is not None, "sha256": expected},
            "after": {"sha256": hashlib.sha256(content.encode()).hexdigest(), "bytes": len(content.encode())},
            "diff": diff_preview(old, content, rel), "root": root}


def plan_move(conn, uid: str, root_id: str, src: str, dst: str) -> dict:
    root = get_root(conn, uid, root_id)
    if not root["writable"]:
        raise FileOpError("read_only", f"Workspace '{root['label']}' is read-only.")
    s = resolve_in_root(root, src)
    d = resolve_in_root(root, dst, must_exist=False)
    if d.exists():
        raise FileOpError("exists", f"Destination '{dst}' already exists; Jarvis won't overwrite on move.")
    if s.is_dir():
        raise FileOpError("is_dir", "Moving folders is not supported; move files individually.")
    return {"args": {"root_id": root_id, "src": src, "dst": dst,
                     "expected_sha256": hashlib.sha256(s.read_bytes()).hexdigest()},
            "before": {"path": src}, "after": {"path": dst}, "root": root}


def _backup(conn, root: dict, rel: str, p: Path, action_id: str) -> str:
    bdir = get_settings().backups_dir / "files"
    bdir.mkdir(parents=True, exist_ok=True)
    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    bp = bdir / f"{sha}{p.suffix}"
    if not bp.exists():
        bp.write_bytes(data)
    conn.execute("INSERT INTO file_backups (id, root_id, rel_path, backup_path, sha256, action_id, created_at) VALUES (?,?,?,?,?,?,?)",
                 (new_id("fbk"), root["id"], rel, str(bp), sha, action_id, now_iso()))
    return str(bp)


def execute_action(conn: sqlite3.Connection, a: dict, reconcile_first: bool) -> dict:
    from jarvis.actions.executor import Blocked
    args = a["args"]
    try:
        root = get_root(conn, a["user_id"], args["root_id"])    # the proposal owner's root, re-checked at execution
        if not root["writable"]:
            raise Blocked("failed", "Workspace is read-only.")
        if a["action_type"] == "file.write":
            p = resolve_in_root(root, args["path"], must_exist=False)
            want = hashlib.sha256(args["content"].encode()).hexdigest()
            if p.exists():
                cur = hashlib.sha256(p.read_bytes()).hexdigest()
                if cur == want:
                    return {"ok": True, "method": "local_stat", "external_id": args["path"], "observed": {"sha256": cur},
                            "simulated": False, "message": f"File already has the approved content: {args['path']}"}
                if args["mode"] == "create" or cur != args.get("expected_sha256"):
                    raise Blocked("conflict", "File changed or appeared since the preview; nothing was overwritten.")
                _backup(conn, root, args["path"], p, a["id"])
            elif args["mode"] == "overwrite":
                raise Blocked("conflict", "File to overwrite no longer exists.")
            p.parent.mkdir(parents=True, exist_ok=True)
            resolve_in_root(root, args["path"], must_exist=False)  # re-check after mkdir
            tmp = p.with_name(f".{p.name}.jarvis-tmp")
            tmp.write_text(args["content"], encoding="utf-8")
            os.replace(tmp, p)
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            return {"ok": got == want, "method": "local_stat", "external_id": args["path"], "observed": {"sha256": got},
                    "simulated": False, "message": f"Wrote {root['label']}/{args['path']}"}
        s = resolve_in_root(root, args["src"], must_exist=False)
        d = resolve_in_root(root, args["dst"], must_exist=False)
        if not s.exists() and d.exists() and hashlib.sha256(d.read_bytes()).hexdigest() == args["expected_sha256"]:
            return {"ok": True, "method": "local_stat", "external_id": args["dst"], "simulated": False,
                    "observed": {"moved": True}, "message": "File was already moved"}
        if not s.exists():
            raise Blocked("conflict", "Source file no longer exists.")
        if d.exists():
            raise Blocked("conflict", "Destination now exists; not overwriting.")
        if hashlib.sha256(s.read_bytes()).hexdigest() != args["expected_sha256"]:
            raise Blocked("conflict", "Source changed since the preview.")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        ok = d.exists() and not s.exists()
        return {"ok": ok, "method": "local_stat", "external_id": args["dst"], "observed": {"moved": ok}, "simulated": False,
                "message": f"Moved {args['src']} → {args['dst']}"}
    except FileOpError as e:
        raise Blocked("failed", e.message) from e
