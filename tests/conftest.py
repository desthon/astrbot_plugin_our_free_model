"""Shared fixtures: stub import path, temp data dirs, fake gateway lifecycle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / "stubs"

# Must happen before any plugin import: the stub package has to win over a
# (possibly absent) real astrbot, and the plugin root must be importable.
for entry in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if entry in sys.path:
        sys.path.remove(entry)
sys.path.insert(0, str(STUBS_DIR))
sys.path.insert(1, str(PLUGIN_DIR))
sys.path.append(str(TESTS_DIR))

import helpers  # noqa: E402  (path setup above is a precondition)
from mock_ofm import DEFAULT_KEY, MockOFM, PLUGIN_FINGERPRINT, STANDALONE_FINGERPRINT  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_plugin_modules():
    """Every test imports the plugin from scratch (env vars are read at import)."""

    helpers.purge_plugin_modules()
    yield
    helpers.purge_plugin_modules()


@pytest.fixture
def gateway():
    """Healthy fake gateway on a random port."""

    server = MockOFM(mode="ok", key=DEFAULT_KEY).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def gateway_factory():
    """Factory for extra gateway instances (other fingerprints/keys/modes)."""

    started: list[MockOFM] = []

    def _make(**kwargs):
        kwargs.setdefault("key", DEFAULT_KEY)
        kwargs.setdefault("mode", "ok")
        server = MockOFM(**kwargs).start()
        started.append(server)
        return server

    try:
        yield _make
    finally:
        for server in started:
            server.stop()


class Home:
    """Temp OFM data dir plus the env vars that point the plugin at it."""

    def __init__(self, path: Path, monkeypatch, key: str = DEFAULT_KEY) -> None:
        self.path = path
        self.monkeypatch = monkeypatch
        self.key = key

    def activate(self) -> "Home":
        # OFM_HOME is the documented switch; DSH_HOME is pinned to an empty dir
        # so discovery cannot wander into a real installation on this machine.
        self.monkeypatch.setenv("OFM_HOME", str(self.path))
        self.monkeypatch.setenv("DSH_HOME", str(self.path.parent / "dsh-empty"))
        return self

    def write_settings(self, **kwargs) -> Path:
        return helpers.write_settings(self.path, **kwargs)

    def write(self, name: str, payload) -> Path:
        return helpers.write_json(self.path / name, payload)


@pytest.fixture
def ofm_home(tmp_path, monkeypatch):
    """Temp data dir already exported as ``$OFM_HOME`` (call ``.activate()``)."""

    path = tmp_path / "ofm-home"
    path.mkdir(parents=True, exist_ok=True)
    return Home(path, monkeypatch).activate()


@pytest.fixture
def settings_payload():
    return helpers.write_settings


__all__ = [
    "DEFAULT_KEY",
    "Home",
    "PLUGIN_FINGERPRINT",
    "STANDALONE_FINGERPRINT",
    "gateway",
    "gateway_factory",
    "ofm_home",
]
