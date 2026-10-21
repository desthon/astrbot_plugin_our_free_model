"""契约 14（真进程）：插件能不能自己把 OFM standalone 拉起来。

这一层不 mock 任何东西——真 ``node``、真仓库里的 ``packages/standalone/cli.mjs``、
真 ``/health``、真 ``settings.json`` 里的 ``forwardKey``，最后真把它停掉。

仓库位置由 ``OFM_REPO``（或插件默认的搜索路径）决定；找不到就 skip，不制造假绿。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from . import support


def _supervisor() -> Any:
    """按文件路径加载 ``core/supervisor.py``：真进程用例不依赖 AstrBot 导入。"""

    path = support.ensure_plugin_symlink() / "core" / "supervisor.py"
    assert path.is_file(), f"找不到 supervisor 模块：{path}"
    name = "ofm_supervisor_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：@dataclass 需要能从 sys.modules 里回查自己的模块
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _require_repo() -> Path:
    repo = _supervisor().find_repo(os.environ.get("OFM_REPO", "") or os.environ.get("OFM_ROOT", ""))
    if repo is None:
        pytest.skip("本机没有 OFM 仓库（设 OFM_REPO 指向它才能跑真进程用例）")
    return repo


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])




@pytest.fixture
def data_dir(tmp_path) -> str:
    path = tmp_path / "ofm-home"
    path.mkdir()
    return str(path)


@pytest.fixture
def started(data_dir):
    """真起一个服务，用例结束一定停掉。"""

    sup = _supervisor()
    repo = _require_repo()
    port = _free_port()
    result = support.run(sup.start(repo, port=port, data_dir=data_dir, resolve_data_dir=data_dir, wait=40))
    try:
        yield sup, result, data_dir
    finally:
        if result.pid:
            support.run(sup.stop(result.pid, graceful=4.0))


def test_real_standalone_starts_healthy_and_exposes_key(started):
    sup, result, data_dir = started

    assert result.ok is True, f"启动失败：{result.reason}\n{result.log_tail}"
    assert result.started is True and result.adopted is False
    assert sup.pid_alive(result.pid), "进程应当还活着"

    payload = support.run(sup.probe_health(result.base_url))
    assert payload is not None, f"/health 不通：{result.base_url}"
    assert payload["service"] == sup.SERVICE_NAME
    assert payload["ok"] is True

    settings = json.loads((Path(data_dir) / "settings.json").read_text(encoding="utf-8"))
    assert settings.get("forwardKey", "").startswith("ofm-"), (
        f"服务启动后应当自己铸造 forwardKey，实际：{settings.get('forwardKey')!r}"
    )
    assert int(settings.get("standalonePort") or 0) == result.port


def test_real_start_adopts_instead_of_duplicating(started):
    sup, result, data_dir = started
    assert result.ok and result.started

    again = support.run(
        sup.start(_require_repo(), port=result.port, data_dir=data_dir, resolve_data_dir=data_dir, wait=10)
    )

    assert again.ok is True
    assert again.adopted is True and again.started is False, "第二次必须是接管，不能起第二个进程"
    assert again.port == result.port


def test_real_stop_releases_port(started):
    sup, result, _data_dir = started
    assert result.ok and result.started

    stopped = support.run(sup.stop(result.pid, graceful=8.0))

    assert stopped is True
    deadline = time.time() + 5
    while time.time() < deadline and sup.pid_alive(result.pid):
        time.sleep(0.2)
    assert not sup.pid_alive(result.pid), "SIGTERM 之后进程必须消失"
    assert support.run(sup.probe_health(result.base_url)) is None, "端口应当已经关掉"


def test_plugin_starts_service_then_provisions_from_scratch(environment, tmp_path, monkeypatch):
    """零配置闭环：settings.json 不存在 → 插件拉服务 → 服务写 key → ensure() 接成 provider。"""

    env = environment
    repo = _require_repo()
    # environment 的 gateway fixture 会预置一份 settings.json；这一轮要的是真空目录
    fresh = tmp_path / "empty-ofm-home"
    fresh.mkdir()
    monkeypatch.setenv("OFM_HOME", str(fresh))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "empty-dsh-home"))
    settings = fresh / "settings.json"
    assert not settings.exists(), "前置条件：这一轮从「什么都没有」开始"

    env.plugin.config["auto_start"] = True
    env.plugin.config["ofm_root"] = str(repo)
    env.plugin.config["ofm_start_port"] = _free_port()

    service = support.run(env.plugin._ensure_service(), timeout=120)
    try:
        assert service is not None and service.ok is True, (
            f"插件没能自己拉起服务：{getattr(service, 'reason', service)}"
        )
        assert service.started is True
        assert settings.exists(), "服务起来后应当已经在数据目录里写 settings.json（含 forwardKey）"

        outcome = support.run(env.plugin._ensure_provisioned(), timeout=120)
        assert outcome is not None and outcome.ok is True, (
            f"服务起来了但接不进 provider：{env.plugin._provision_detail()}"
        )
        assert outcome.provider_id == env.provision.MANAGED_PROVIDER_ID
        assert len(env.providers) == 1, f"应当恰好写入 1 个托管 provider，实际：{env.providers}"
        source = env.managed_source()
        assert str(source.get("api_base", "")).endswith("/v1"), (
            f"托管 source 的 api_base 应当指向本地服务的 /v1，实际：{source}"
        )
        provider = env.managed_provider()
        assert provider.get("provider_source_id") == source.get("id"), (
            f"provider 应当引用托管 source，实际：{provider}"
        )
        assert str(provider.get("model") or "").strip(), f"provider 缺 model：{provider}"
        # 用户把「独立服务」总开关关掉 → /v1 一律 503 → ensure() 要能自己打开
        import httpx

        key = json.loads(settings.read_text(encoding="utf-8"))["forwardKey"]
        headers = {"Authorization": f"Bearer {key}"}
        off = httpx.post(
            f"{service.base_url}/api/management/settings",
            headers=headers,
            json={"enabled": False},
            timeout=10,
        )
        assert off.status_code == 200 and off.json()["settings"]["enabled"] is False
        assert httpx.get(f"{service.base_url}/v1/models", headers=headers, timeout=10).status_code == 503

        healed = support.run(env.plugin._ensure_provisioned(), timeout=120)
        assert healed is not None and healed.ok is True, (
            f"总开关被关掉后没能自愈：{env.plugin._provision_detail()}"
        )
        again = httpx.get(f"{service.base_url}/v1/models", headers=headers, timeout=10)
        assert again.status_code == 200, f"/v1 应当恢复 200，实际 {again.status_code}"
        detail = env.plugin._provision_detail()
        assert "独立服务开关" in detail or "独立服务开关" in (healed.note or ""), (
            f"自愈这件事要让用户看得到（doctor/详情），实际 detail={detail!r} note={healed.note!r}"
        )
    finally:
        support.run(env.plugin._stop_owned_service(), timeout=60)
