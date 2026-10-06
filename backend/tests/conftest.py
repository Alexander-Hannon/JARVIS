"""Shared fixtures. Provider calls go to LABELED contract fakes (simulated=True) — these tests prove
Jarvis's own logic, not live provider success."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime

# Never load AJ's real .env (remote mode, API keys, OAuth secrets) into the test suite.
os.environ["JARVIS_ENV_FILE"] = os.devnull
for _k in [k for k in os.environ if k.startswith("JARVIS_") and k != "JARVIS_ENV_FILE"]:
    del os.environ[_k]

import pytest

PASSWORD = "correct horse battery"
USERNAME = "aj"
OWNER = "usr_owner"
LOOPBACK = ("127.0.0.1", 50000)   # a direct local client (TestClient's default peer name is not loopback)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_ALLOWED_HOSTS", json.dumps(["127.0.0.1", "localhost", "testserver"]))
    monkeypatch.delenv("JARVIS_ANTHROPIC_API_KEY", raising=False)
    # share downloaded STT weights across tests instead of re-downloading into each tmp dir
    from pathlib import Path
    monkeypatch.setenv("JARVIS_MODELS_DIR", str(Path(__file__).resolve().parents[1] / "data" / "models"))
    from jarvis.agents import context
    from jarvis.config import reset_settings_cache
    from jarvis.core import util
    from jarvis.core.db import reset_init_cache
    from jarvis.integrations import accounts
    from jarvis.llm import provider
    reset_settings_cache()
    reset_init_cache()
    accounts.clear_overrides()
    provider.set_provider(None)
    context.invalidate()
    util._now_override = None
    yield tmp_path
    accounts.clear_overrides()
    provider.set_provider(None)
    util._now_override = None
    context.invalidate()
    reset_settings_cache()
    reset_init_cache()


@pytest.fixture
def app(env):
    from jarvis.api.app import create_app
    return create_app()


@pytest.fixture
def conn(app):
    from jarvis.core.db import get_conn
    c = get_conn()
    yield c
    c.close()


class Api:
    def __init__(self, client, csrf):
        self.c = client
        self.csrf = csrf

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, json=None, **kw):
        return self.c.post(path, json=json, headers={"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}, **kw)

    def patch(self, path, json=None):
        return self.c.patch(path, json=json, headers={"X-CSRF-Token": self.csrf})

    def put(self, path, json=None):
        return self.c.put(path, json=json, headers={"X-CSRF-Token": self.csrf})

    def delete(self, path, json=None):
        return self.c.request("DELETE", path, json=json, headers={"X-CSRF-Token": self.csrf})


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, client=LOOPBACK)
    r = c.post("/api/auth/setup", json={"username": USERNAME, "password": PASSWORD, "display_name": "AJ"})
    assert r.status_code == 200, r.text
    return Api(c, r.json()["csrf_token"])


@pytest.fixture
def fakes(client, conn):
    """Connected Todoist + Google Calendar accounts backed by contract fakes, with explicit grants."""
    from jarvis.integrations import accounts as A
    from jarvis.integrations.fakes import FakeGoogleCalendar, FakeTodoist
    td, gc = FakeTodoist(), FakeGoogleCalendar()
    A.set_override("todoist", td)
    A.set_override("google_calendar", gc)
    conn.execute("BEGIN")
    tid = A.upsert_account(conn, OWNER, "todoist", "aj@example.test (SIMULATED)", "fake-token", "api_token",
                           external_account_id="u1")
    A.set_grants(conn, tid, "todoist_project", "write", [("proj-jarvis", "Jarvis")], default_target="proj-jarvis")
    gid = A.upsert_account(conn, OWNER, "google", "aj@example.test (SIMULATED)", json.dumps({"access_token": "x"}), "oauth_tokens",
                           external_account_id="aj@example.test", config={"purposes": ["calendar"]})
    A.set_grants(conn, gid, "calendar", "read", [("primary", "AJ")])
    A.set_grants(conn, gid, "calendar", "write", [("primary", "AJ")], default_target="primary")
    conn.execute("COMMIT")
    return {"todoist": td, "calendar": gc, "todoist_account": tid, "google_account": gid}


def freeze(dt: datetime):
    from jarvis.core import util
    util._now_override = lambda: dt


def run_worker(conn, n: int = 50) -> int:
    from jarvis.worker import run_once
    return run_once(conn, "test-worker", max_jobs=n)


CHICAGO_AFTERNOON = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)   # Mon 3:00 PM CDT
