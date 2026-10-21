"""契约 5：不夺权 —— 用户已经指定了默认 provider 时，接入不许改它。"""

from __future__ import annotations

from . import support

OWNER = "someone-else"


def test_ensure_does_not_steal_existing_default(environment_with_default):
    env = environment_with_default
    provision = env.provision
    assert env.default_provider_id == OWNER, "前置条件：配置里已有别人的默认 provider"

    result = support.run(provision.ensure(env.plugin, env.endpoint(), set_default=True))

    assert result.ok is True, f"网关健康时 ensure 应 ok=True，实际 {result.degraded_reason!r}"
    assert result.default_set is False, (
        "已存在默认 provider 时 default_set 必须是 False（否则会把用户的模型顶掉）"
    )
    assert env.default_provider_id == OWNER, (
        f"agent_runner.config.model.provider_id 被改成了 {env.default_provider_id!r}，"
        f"应保持 {OWNER!r}"
    )

    # 配置本身仍应写入（不夺权 ≠ 不接入）
    source = env.managed_source()
    assert source["api_base"] == env.gateway.openai_base_url
    env.managed_provider()
