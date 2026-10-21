"""契约 3：幂等 —— 连跑两次不重复写、不改已有条目。"""

from __future__ import annotations

from . import support


def test_ensure_is_idempotent(environment):
    env = environment
    provision = env.provision
    endpoint = env.endpoint()

    async def _twice():
        first = await provision.ensure(env.plugin, endpoint, set_default=True)
        second = await provision.ensure(env.plugin, endpoint, set_default=True)
        return first, second

    first, second = support.run(_twice())

    assert first.ok is True, f"第一次 ensure 失败：{first.degraded_reason!r}"
    assert second.ok is True, f"第二次 ensure 失败：{second.degraded_reason!r}"

    assert second.created_source is False, "第二次 ensure 不应再创建 source"
    assert second.created_provider is False, "第二次 ensure 不应再创建 provider"
    assert second.updated_source is False, (
        "端点没变时第二次 ensure 不应报告 updated_source=True（会把幂等做成每次都改配置）"
    )

    assert len(env.provider_sources) == 1, (
        f"provider_sources 应保持 1 条，实际 {len(env.provider_sources)} 条："
        f"{env.provider_sources}"
    )
    assert len(env.providers) == 1, (
        f"provider 应保持 1 条，实际 {len(env.providers)} 条：{env.providers}"
    )

    source = env.managed_source()
    provider = env.managed_provider()
    assert source["api_base"] == env.gateway.openai_base_url
    assert provider["provider_source_id"] == support.MANAGED_SOURCE_ID
    assert env.default_provider_id == support.MANAGED_PROVIDER_ID
