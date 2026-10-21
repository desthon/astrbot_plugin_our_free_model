"""契约 2：``ensure()`` 首次接入 —— 写两条配置，并让真 ProviderManager 载入它。"""

from __future__ import annotations

from . import support


def test_ensure_writes_source_and_provider_then_manager_loads_it(environment):
    env = environment
    provision = env.provision

    assert provision.MANAGED_SOURCE_ID == support.MANAGED_SOURCE_ID, (
        f"MANAGED_SOURCE_ID 契约值应为 {support.MANAGED_SOURCE_ID!r}，"
        f"实际 {provision.MANAGED_SOURCE_ID!r}"
    )
    assert provision.MANAGED_PROVIDER_ID == support.MANAGED_PROVIDER_ID, (
        f"MANAGED_PROVIDER_ID 契约值应为 {support.MANAGED_PROVIDER_ID!r}，"
        f"实际 {provision.MANAGED_PROVIDER_ID!r}"
    )

    result = support.run(provision.ensure(env.plugin, env.endpoint(), set_default=True))
    assert result.ok is True, (
        f"网关健康时 ensure() 应返回 ok=True，实际 degraded_reason={result.degraded_reason!r}"
    )

    # --- provider_sources 的形状 ----------------------------------------
    source = env.managed_source()
    assert source["type"] == "openai_chat_completion", source
    assert source["provider_type"] == "chat_completion", source
    assert source["enable"] is True, source
    assert source["api_base"] == env.gateway.openai_base_url, (
        f"api_base 必须是 <endpoint.base_url>/v1，实际 {source['api_base']!r}，"
        f"期望 {env.gateway.openai_base_url!r}"
    )
    assert source["key"] == [env.gateway.key], (
        f"key 必须是 [<endpoint.api_key>]，实际 {source['key']!r}"
    )

    # --- provider 的形状 -------------------------------------------------
    provider = env.managed_provider()
    assert provider["provider_source_id"] == support.MANAGED_SOURCE_ID, provider
    assert provider["enable"] is True, provider
    model = provider["model"]
    assert isinstance(model, str) and model.strip(), (
        f"provider.model 必须是非空字符串（ensure 未显式给 model 时取默认），实际 {model!r}"
    )
    assert model == result.model, (
        f"ProvisionResult.model={result.model!r} 与写入配置的 model={model!r} 不一致"
    )

    # --- 真 ProviderManager 真的把它装起来了 -----------------------------
    inst_map = env.provider_manager.inst_map
    assert support.MANAGED_PROVIDER_ID in inst_map, (
        "ensure() 写入了配置，但真 ProviderManager 的 inst_map 里没有 "
        f"{support.MANAGED_PROVIDER_ID!r}（现有：{sorted(inst_map)}）。"
        "零配置接入要求「装完即用」，仅写配置文件、不触发 ProviderManager 载入/重载，"
        "用户在重启前无法使用该 provider。"
    )

    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    instance = inst_map[support.MANAGED_PROVIDER_ID]
    assert isinstance(instance, ProviderOpenAIOfficial), (
        f"inst_map 里应是真 ProviderOpenAIOfficial，实际 {type(instance)!r}"
    )
    assert instance.get_model() == model, (
        f"provider 实例的 model={instance.get_model()!r} 与配置 {model!r} 不一致"
    )

    # --- 默认接管 --------------------------------------------------------
    assert result.created_source is True, "首次 ensure 应报告 created_source=True"
    assert result.created_provider is True, "首次 ensure 应报告 created_provider=True"
    assert result.default_set is True, "原 provider_id 为空时应报告 default_set=True"
    assert result.provider_id == support.MANAGED_PROVIDER_ID, result
    assert result.source_id == support.MANAGED_SOURCE_ID, result
    assert env.default_provider_id == support.MANAGED_PROVIDER_ID, (
        f"默认模型 provider_id 应被设为 {support.MANAGED_PROVIDER_ID!r}，"
        f"实际 {env.default_provider_id!r}"
    )
    assert provision.is_managed(env.plugin) is True, "接入后 is_managed() 应为 True"
