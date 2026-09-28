from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GEN = Path(__file__).resolve().parents[1] / "fixtures" / "docs" / "generate.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("jarvis_test_doc_fixtures", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def gen():
    return _load_gen()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    from jarvis.config import reset_settings_cache
    from jarvis.core.db import get_conn, reset_init_cache

    reset_settings_cache()
    reset_init_cache()
    c = get_conn()
    yield c
    c.close()
    reset_settings_cache()
    reset_init_cache()
