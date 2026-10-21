"""契约 4：端点漂移 —— 换端口/换 key 后重新解析 endpoint，ensure() 必须就地修正。"""

from __future__ import annotations

import json

from . import support


def test_ensure_updates_source_on_endpoint_drift(environment, gateway_factory):
    env = environment
    provision = env.provision

    first = support.run(provision.ensure(env.plugin, env.endpoint()))
    assert first.ok is True, f"首次 ensure 失败：{first.degraded_reason!r}"

    stale_source = env.managed_source()
    stale_base = stale_source["api_base"]
    stale_key = stale_source["key"][0]

    moved = gateway_factory(key="ofm-drift-key-0002")
    assert moved.port != env.gateway.port, "新网关必须落在新端口上"
    support.write_settings(env.home, key=moved.key, port=moved.port)

    async def _reprovision():
        import importlib

        endpoints = importlib.import_module(f"{support.PACKAGE}.core.endpoints")
        resolved = await endpoints.resolve()
        return resolved, await provision.ensure(env.plugin, resolved, set_default=True)

    resolved, second = support.run(_reprovision())

    assert resolved.base_url.rstrip("/") == moved.base_url, (
        f"endpoint 重新解析后应指向新网关 {moved.base_url!r}，实际 {resolved.base_url!r}"
    )
    assert second.ok is True, f"漂移后 ensure 失败：{second.degraded_reason!r}"
    assert second.updated_source is True, (
        "端点已漂移却没报告 updated_source=True（配置会一直指向死端口/旧 key）"
    )

    source = env.managed_source()
    assert source["api_base"] == moved.openai_base_url, (
        f"api_base 应更新为 {moved.openai_base_url!r}，实际 {source['api_base']!r}"
    )
    assert source["key"] == [moved.key], f"key 应更新为 [{moved.key!r}]，实际 {source['key']!r}"

    serialized = json.dumps(env.cfg["provider_sources"], ensure_ascii=False)
    assert stale_base not in serialized, f"旧 api_base {stale_base!r} 仍残留在配置里：{serialized}"
    assert stale_key not in serialized, f"旧 key {stale_key!r} 仍残留在配置里：{serialized}"

    assert len(env.provider_sources) == 1, (
        f"漂移修正不应产生第二条 source，实际 {env.provider_sources}"
    )
    assert len(env.providers) == 1, f"漂移修正不应产生第二条 provider，实际 {env.providers}"
