"""Runtime configuration. Secrets come from the environment or files outside the DB; never from git."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    # JARVIS_ENV_FILE lets tests/tools point at a different (or empty) settings file instead of AJ's real .env.
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=os.environ.get("JARVIS_ENV_FILE", str(REPO_ROOT / ".env")),
                                      extra="ignore")

    data_dir: Path = BACKEND_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8765
    # Remote profile must be explicitly enabled and is only valid behind authenticated HTTPS.
    remote_mode: bool = False
    public_origin: str | None = None          # e.g. https://jarvis.example.ts.net (remote profile)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    session_ttl_hours: int = 24 * 14          # absolute lifetime (NIST 800-171 3.1.11)
    session_idle_hours: float = 24 * 7        # signed out after this long unused; enterprises set e.g. 0.25 (3.1.10)
    # Reverse proxies (e.g. 127.0.0.1 for `tailscale serve`) whose X-Forwarded-For is believed for client-IP
    # attribution and throttling. A forwarded request is never treated as "loopback" for setup.
    trusted_proxies: list[str] = Field(default_factory=list)
    # Accounts unused this long are disabled at their next sign-in (3.5.6); 0 turns it off. The owner is exempt.
    dormant_days: int = 90
    # Second factor (800-171 3.5.3): auto = everyone in remote mode, privileged roles on a loopback-only install;
    # privileged | all | off (off is for tests and is flagged by doctor).
    mfa_policy: str = "auto"
    # personal = AJ-style single-person install (personal connectors, personal seed); enterprise = company install.
    edition: str = "personal"
    # Secret key for encrypting stored tokens. If unset, read from <data_dir>/secret.key
    # (created on setup with 0600 perms). Never stored in the database.
    secret_key: str | None = None

    # Speech-to-text
    stt_engine: str = "faster-whisper"
    stt_model: str = "base.en"
    stt_device: str = "cpu"                   # "cuda" for the GPU profile
    stt_compute_type: str = "int8"            # "float16" for GPU profile
    max_audio_bytes: int = 25 * 1024 * 1024
    max_audio_seconds: int = 300
    stt_min_confidence: float = 0.55          # below this, voice approvals are refused

    # Model provider
    anthropic_api_key: str | None = None
    model_default: str = "claude-opus-5-5"

    # Google OAuth (web server flow). Client secret only from env.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant: str = "common"      # "common" covers personal outlook.com and work/school accounts
    spotify_client_id: str | None = None  # PKCE public client: no secret needed

    # Web Push VAPID
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:owner@localhost"

    # Test/contract mode: external providers replaced with labeled contract mocks.
    contract_mocks: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    models_dir_override: Path | None = Field(default=None, alias="JARVIS_MODELS_DIR")

    @property
    def models_dir(self) -> Path:
        return self.models_dir_override or (self.data_dir / "models")

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.models_dir, self.files_dir, self.backups_dir, self.data_dir / "audio_tmp"):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
