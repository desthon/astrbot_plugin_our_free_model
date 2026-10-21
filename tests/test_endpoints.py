"""契约 1 + 2：端点发现与 forwardKey 缺失。

用例 1 的第二部分（端口漂移）是一个**显式二选一**的契约断言：

* 分支 A（首选）：plan §4.1 规定「端口被占会向后扫 10 个」，所以配置端口
  = 实际端口 - 3 时，`resolve()` 应靠 `/health` 指纹找到真实端口。
* 分支 B（退化）：若实现明确**不扫描**，则必须给出含配置端口号的
  `EndpointNotFound.diagnostic`，否则用户无法定位「端口漂移」这件事。

两条分支都由同一次调用决定，测试会把自己的判定理由写进断言消息。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import helpers
from conftest import DEFAULT_KEY


def _endpoints():
    return helpers.core_module("endpoints")


def _secrets():
    return helpers.core_module("secrets")


# ---------------------------------------------------------------------------
# 1. endpoint discovery
# ---------------------------------------------------------------------------
def test_resolve_discovers_standalone_gateway_by_health_fingerprint(gateway, ofm_home):
    """$OFM_HOME → settings.json(forwardKey + standalonePort) → resolve().

    只靠 ``/health`` 指纹匹配到的端点必须回报正确的 mode / base_url / key。
    ``base_url`` 是否带 ``/v1`` 由实现决定（协议事实里两种都在用），因此这里
    只断言「指向同一个 host:port」，不绑定路径拼法。
    """

    ofm_home.write_settings(forward_key=DEFAULT_KEY, standalone_port=gateway.port)

    endpoints = _endpoints()
    endpoint = asyncio.run(endpoints.resolve())

    assert endpoint.mode == "standalone"
    assert f":{gateway.port}" in endpoint.base_url
    assert endpoint.base_url.startswith("http://")
    assert endpoint.api_key == DEFAULT_KEY
    assert Path(str(endpoint.data_dir)).resolve() == ofm_home.path.resolve()
    assert isinstance(endpoint.health, dict)
    assert endpoint.health.get("ok") is True


def test_resolve_follows_port_drift_via_health_scan(gateway, ofm_home):
    """配置端口 = 实际端口 - 3：按 plan §4.1 应向后扫描并命中真实端口。

    判定理由：`probe_health` 与「/health 指纹」是契约的一部分，而 plan §4.1
    明确写了 ``range(port + 1, port + 11)`` 的扫描策略；不扫描就必须把端口
    写进 diagnostic（分支 B），两种情况下的用户可操作性不同但都必须成立。
    """

    configured_port = gateway.port - 3
    assert configured_port > 1024, "临时端口应足够大，便于构造 -3 漂移"

    ofm_home.write_settings(forward_key=DEFAULT_KEY, standalone_port=configured_port)

    endpoints = _endpoints()
    try:
        endpoint = asyncio.run(endpoints.resolve(timeout=2.0))
    except endpoints.EndpointNotFound as exc:
        diagnostic = f"{getattr(exc, 'diagnostic', '')} {exc}"
        assert str(configured_port) in diagnostic, (
            "分支 B（实现不扫描端口）：diagnostic 必须包含配置端口号，"
            f"实际 diagnostic={diagnostic!r}"
        )
    else:
        assert endpoint.mode == "standalone"
        assert f":{gateway.port}" in endpoint.base_url, (
            "分支 A（实现按 plan §4.1 扫描 +1..+10）：应命中真实端口 "
            f"{gateway.port}，实际 base_url={endpoint.base_url!r}"
        )
        assert endpoint.api_key == DEFAULT_KEY


# ---------------------------------------------------------------------------
# 2. missing forwardKey
# ---------------------------------------------------------------------------
def test_missing_forward_key_returns_none_and_actionable_diagnostic(ofm_home):
    """没有 forwardKey（从没开过转发）→ None，且 resolve 给出「去开转发」的提示。"""

    ofm_home.write_settings(forward_key=None, standalone_port=18999)

    secrets = _secrets()
    assert secrets.forward_key(ofm_home.path) is None

    settings = secrets.read_settings(ofm_home.path)
    assert isinstance(settings, dict)
    assert "forwardKey" not in settings

    endpoints = _endpoints()
    with pytest.raises(endpoints.EndpointNotFound) as excinfo:
        asyncio.run(endpoints.resolve(timeout=1.0))

    error = excinfo.value
    diagnostic = str(getattr(error, "diagnostic", "") or "")
    assert diagnostic, "EndpointNotFound 必须携带 diagnostic 文本"
    assert ("转发" in diagnostic) or ("forward" in diagnostic.lower()), (
        "diagnostic 必须要求用户去 OFM 里打开本地转发，实际="
        f"{diagnostic!r}"
    )


def test_data_dirs_includes_ofm_home(ofm_home):
    """``data_dirs()`` 必须把 $OFM_HOME 指向的目录纳入候选。"""

    secrets = _secrets()
    dirs = [Path(str(entry)).resolve() for entry in secrets.data_dirs()]
    assert ofm_home.path.resolve() in dirs


def test_read_settings_is_contract_shaped(ofm_home):
    """两处 ``read_settings`` 都要能吃下同一份 settings.json。"""

    ofm_home.write_settings(forward_key=DEFAULT_KEY, standalone_port=12345)

    for module_name in ("endpoints", "secrets"):
        settings = helpers.core_module(module_name).read_settings(ofm_home.path)
        assert isinstance(settings, dict)
        assert settings.get("forwardKey") == DEFAULT_KEY
        assert settings.get("standalonePort") == 12345
