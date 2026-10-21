"""Integration conftest: real AstrBot 4.29.1, real provider stack.

Important interaction with the existing suite
--------------------------------------------
``tests/conftest.py`` injects the lightweight ``tests/stubs`` package so the 84
unit tests run without AstrBot. This conftest loads *after* it (parent conftest
first) and, per test, swaps the real 4.29.1 package in for the duration of the
test only: real modules are parked in a cache, the stub modules and the stubs
``sys.path`` entry are restored on teardown. That is why the whole-suite command
``pytest tests -q`` keeps the unit tests green while these run against reality.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from . import support


@pytest.fixture(autouse=True)
def real_astrbot(monkeypatch, tmp_path):
    """Run this test against the real AstrBot 4.29.1; restore stubs afterwards."""

    monkeypatch.setenv("ASTRBOT_ROOT", str(support.DATA_ROOT))
    monkeypatch.setenv("DEMO_MODE", "False")

    evicted = support.enter_real_astrbot()
    # A per-test plugin-config file keeps AstrBotConfig writes out of the tree.
    monkeypatch.setenv("DSH_IT_PLUGIN_CONFIG", str(tmp_path / "plugin_config.json"))
    try:
        support.ensure_plugin_symlink()
        yield
    finally:
        support.exit_real_astrbot(evicted)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """``$OFM_HOME`` for this test (endpoint discovery source of truth)."""

    path = tmp_path / "ofm-home"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OFM_HOME", str(path))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-empty"))
    return path


@pytest.fixture
def gateway_factory():
    from .fake_ofm import FakeOFM

    started: list[FakeOFM] = []

    def _make(**kwargs) -> FakeOFM:
        server = FakeOFM(**kwargs).start()
        started.append(server)
        return server

    try:
        yield _make
    finally:
        for server in started:
            server.stop()


@pytest.fixture
def gateway(home, gateway_factory):
    """Healthy fake standalone OFM gateway, already published in settings.json."""

    server = gateway_factory()
    support.write_settings(home, key=server.key, port=server.port)
    return server


@dataclass
class Environment:
    """Everything one integration test needs, all of it real AstrBot."""

    cfg: dict[str, Any]
    acm: Any
    provider_manager: Any
    persona_manager: Any
    module: ModuleType
    context: Any
    plugin: Any
    gateway: Any
    home: Path
    data_dir: Path
    extra: dict[str, Any] = field(default_factory=dict)

    # -- convenience ------------------------------------------------------
    @property
    def provision(self) -> ModuleType:
        """``core/provision.py``, imported lazily.

        Deliberately *not* a fixture: a missing module must surface as a failed
        test with the contract gap spelled out, not as a collection/fixture error.
        """

        cached = self.extra.get("provision")
        if cached is None:
            cached = support.import_provision()
            self.extra["provision"] = cached
        return cached

    @property
    def provider_sources(self) -> list[dict]:
        return self.cfg["provider_sources"]

    @property
    def providers(self) -> list[dict]:
        return self.cfg["provider"]

    @property
    def default_provider_id(self) -> str:
        return support.default_provider_id(self.cfg)

    def managed_source(self) -> dict:
        matches = [
            entry
            for entry in self.provider_sources
            if entry.get("id") == support.MANAGED_SOURCE_ID
        ]
        assert len(matches) == 1, (
            f"provider_sources 里应恰好有 1 条 id={support.MANAGED_SOURCE_ID}，"
            f"实际 {len(matches)} 条：{self.provider_sources}"
        )
        return matches[0]

    def managed_provider(self) -> dict:
        matches = [
            entry
            for entry in self.providers
            if entry.get("id") == support.MANAGED_PROVIDER_ID
        ]
        assert len(matches) == 1, (
            f"provider 里应恰好有 1 条 id={support.MANAGED_PROVIDER_ID}，"
            f"实际 {len(matches)} 条：{self.providers}"
        )
        return matches[0]

    def endpoint(self, gateway=None):
        return support.make_endpoint(gateway or self.gateway, self.data_dir)


def _build_environment(home, gateway, tmp_path, *, provider_id: str = "") -> Environment:
    support.install_config(provider_id=provider_id)  # live config, in place
    import astrbot.core as astrbot_core

    cfg = astrbot_core.astrbot_config

    manager, acm, persona_manager = support.make_provider_manager(cfg)
    module = support.import_plugin_main()
    context = support.FakeContext(cfg, manager, acm, persona_manager)
    plugin_config = support.plugin_config_object(
        tmp_path / "plugin_config.json",
    )
    support.reset_plugin_kv()
    plugin = support.make_plugin(module, context, plugin_config)

    return Environment(
        cfg=cfg,
        acm=acm,
        provider_manager=manager,
        persona_manager=persona_manager,
        module=module,
        context=context,
        plugin=plugin,
        gateway=gateway,
        home=home,
        data_dir=home,
    )


@pytest.fixture
def environment(real_astrbot, home, gateway, tmp_path) -> Environment:
    """Fresh install: no provider configured, default provider id empty."""

    return _build_environment(home, gateway, tmp_path)


@pytest.fixture
def environment_with_default(real_astrbot, home, gateway, tmp_path) -> Environment:
    """Someone else already owns ``agent_runner.config.model.provider_id``."""

    return _build_environment(home, gateway, tmp_path, provider_id="someone-else")
