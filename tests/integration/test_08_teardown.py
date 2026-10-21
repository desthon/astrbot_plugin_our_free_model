"""契约 8：teardown —— 拆干净、可重复调用、不动别人的默认 provider。"""

from __future__ import annotations

from . import support


def test_teardown_removes_managed_wiring_and_is_safe_to_repeat(environment):
    env = environment
    provision = env.provision

    result = support.run(provision.ensure(env.plugin, env.endpoint()))
    assert result.ok is True, f"前置 ensure 失败：{result.degraded_reason!r}"
    assert result.default_set is True, "前置条件：默认 provider 应由我们接管"
    assert support.MANAGED_PROVIDER_ID in env.provider_manager.inst_map

    removed = support.run(provision.teardown(env.plugin))
    assert removed is True, "首次 teardown 应返回 True（确实拆掉了东西）"

    assert env.provider_sources == [], (
        f"teardown 后 provider_sources 应为空，实际 {env.provider_sources}"
    )
    assert env.providers == [], f"teardown 后 provider 应为空，实际 {env.providers}"
    assert support.MANAGED_PROVIDER_ID not in env.provider_manager.inst_map, (
        "teardown 后真 ProviderManager.inst_map 里仍留着 ofm-free："
        f"{sorted(env.provider_manager.inst_map)}"
    )
    assert env.default_provider_id in ("", None), (
        f"原本由我们设置的默认 provider_id 应被清空，实际 {env.default_provider_id!r}"
    )
    assert provision.is_managed(env.plugin) is False, "teardown 后 is_managed() 应为 False"

    again = support.run(provision.teardown(env.plugin))
    assert again is False, "第二次 teardown 应返回 False（没有可拆的东西）"


def test_teardown_leaves_foreign_default_untouched(environment_with_default):
    env = environment_with_default
    provision = env.provision

    result = support.run(provision.ensure(env.plugin, env.endpoint()))
    assert result.ok is True
    assert result.default_set is False

    support.run(provision.teardown(env.plugin))

    assert env.default_provider_id == "someone-else", (
        "teardown 只能清空「我们自己设的」默认 provider，实际 "
        f"{env.default_provider_id!r}"
    )
    assert env.provider_sources == [] and env.providers == []
