"""Encryption for stored provider tokens / private URLs.

The Fernet key lives outside the database: JARVIS_SECRET_KEY env var, or <data_dir>/secret.key (0600).
If the key is missing, reading stored secrets fails loudly instead of silently degrading.
Note: only secret columns are encrypted; the SQLite database as a whole is NOT encrypted.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from jarvis.config import get_settings


class SecretKeyUnavailable(RuntimeError):
    pass


def key_path() -> Path:
    return get_settings().data_dir / "secret.key"


def ensure_key() -> None:
    s = get_settings()
    if s.secret_key:
        return
    p = key_path()
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())


def _fernet() -> Fernet:
    s = get_settings()
    if s.secret_key:
        return Fernet(s.secret_key.encode())
    p = key_path()
    if not p.exists():
        raise SecretKeyUnavailable(
            f"Encryption key not found at {p} and JARVIS_SECRET_KEY is unset; stored credentials cannot be read.")
    return Fernet(p.read_bytes().strip())


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken as e:
        raise SecretKeyUnavailable("Stored credential could not be decrypted with the current key.") from e


def seal(plaintext: str) -> str:
    """Encrypt a short secret for a TEXT column (e.g. PKCE verifiers)."""
    return encrypt(plaintext).decode()


def unseal(sealed: str) -> str:
    return decrypt(sealed.encode())


def key_status() -> dict:
    s = get_settings()
    if s.secret_key:
        return {"ok": True, "source": "env"}
    p = key_path()
    if not p.exists():
        return {"ok": False, "source": None, "detail": "missing"}
    mode = oct(p.stat().st_mode & 0o777)
    return {"ok": True, "source": "file", "path": str(p), "mode": mode}


def redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    return "•" * 6 + value[-keep:] if len(value) > keep * 2 else "•" * 6
