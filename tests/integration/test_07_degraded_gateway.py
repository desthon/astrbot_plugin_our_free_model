"""契约 7：降级 —— 网关连不上时 ensure() 不抛异常、写清楚中文原因、不留半成品。"""

from __future__ import annotations

import re

from . import support

CJK = re.compile(r"[\u4e00-\u9fff]")


def test_ensure_degrades_when_gateway_unreachable(environment):
    env = environment
    provision = env.provision

    before = support.config_snapshot(env.cfg)
    assert before["provider_sources"] == [] and before["provider"] == [], (
        f"前置条件：接入前配置应为空，实际 {before}"
    )

    # 先在网关活着时用真实 resolve() 得到一个健康的 endpoint（health 已填好、
    # base_url 指向真实端口），再关掉网关：这就是「一分钟前还好用，现在连不上」
    # 的真实场景。用 resolve 而非手搓 Endpoint，是为了堵住「endpoint.health 为空
    # 所以不做探测」这条退路。
    async def _resolve_while_alive():
        import importlib

        endpoints = importlib.import_module(f"{support.PACKAGE}.core.endpoints")
        return await endpoints.resolve()

    dead_endpoint = support.run(_resolve_while_alive())
    assert dead_endpoint.health.get("ok") is True, (
        f"前置条件：网关活着时 resolve() 应拿到健康 endpoint，实际 health={dead_endpoint.health!r}"
    )
    assert dead_endpoint.base_url.rstrip("/") == env.gateway.base_url

    env.gateway.stop()

    result = support.run(provision.ensure(env.plugin, dead_endpoint), timeout=60)

    assert result is not None, "ensure() 必须返回 ProvisionResult，不能在网关不可达时返回 None"
    assert result.ok is False, (
        "网关不可达时 ensure() 必须返回 ok=False（抛异常或假装成功都会让用户拿到坏配置）"
    )
    reason = (result.degraded_reason or "").strip()
    assert reason, "降级时必须给出非空的 degraded_reason"
    assert CJK.search(reason), f"degraded_reason 应是面向用户的中文说明，实际 {reason!r}"

    after = support.config_snapshot(env.cfg)
    assert after["provider_sources"] == before["provider_sources"], (
        "降级时不得留下半成品 source 条目："
        f"接入前 {before['provider_sources']} → 之后 {after['provider_sources']}"
    )
    assert after["provider"] == before["provider"], (
        "降级时不得留下半成品 provider 条目："
        f"接入前 {before['provider']} → 之后 {after['provider']}"
    )
    assert after["default_provider_id"] == before["default_provider_id"], (
        "降级时不得改动默认 provider 接管状态"
    )
    assert support.MANAGED_PROVIDER_ID not in env.provider_manager.inst_map, (
        "降级时不应把 ofm-free 装进 ProviderManager"
    )
    assert provision.is_managed(env.plugin) is False, "降级后 is_managed() 应为 False"
