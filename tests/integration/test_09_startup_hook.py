"""契约 9：启动钩子 —— @filter.on_astrbot_loaded() 必须真的自己把接入跑完。

这里不手写 ensure 调用，而是像 ``AstrBotCoreLifecycle.start()`` 那样从
``star_handlers_registry`` 取出 handler 并 await（生命周期里就是
``await handler.handler()``，self 由 StarManager 用 ``functools.partial`` 绑好，
本测试自行绑同一个实例）。网关就绪 + settings.json 就位 = 用户零操作。
"""

from __future__ import annotations

import functools

from . import support


def _bind(handler, plugin):
    target = handler.handler
    if isinstance(target, functools.partial):
        return target
    return functools.partial(target, plugin)


def test_astrbot_loaded_hook_provisions_without_manual_steps(environment):
    env = environment
    provision = env.provision

    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    handlers = [
        handler
        for handler in star_handlers_registry.get_handlers_by_module_name(
            support.MODULE_PATH
        )
        if handler.event_type is EventType.OnAstrBotLoadedEvent
    ]
    assert len(handlers) == 1, (
        "找不到唯一的 on_astrbot_loaded handler，实际："
        f"{[handler.handler_name for handler in handlers]}"
    )

    assert env.provider_sources == [] and env.providers == [], (
        f"前置条件：钩子执行前不应有配置，实际 {env.cfg['provider_sources']} / {env.cfg['provider']}"
    )

    support.run(_bind(handlers[0], env.plugin)())

    source = env.managed_source()
    assert source["api_base"] == env.gateway.openai_base_url, (
        "钩子应通过 OFM_HOME 自己解析 endpoint 并写入 api_base，实际 "
        f"{source['api_base']!r}，期望 {env.gateway.openai_base_url!r}"
    )
    provider = env.managed_provider()
    assert provider["provider_source_id"] == support.MANAGED_SOURCE_ID, provider
    assert isinstance(provider.get("model"), str) and provider["model"].strip(), (
        f"钩子写入的 provider 必须带 model，实际 {provider.get('model')!r}"
    )

    assert env.default_provider_id == support.MANAGED_PROVIDER_ID, (
        "空配置下钩子应顺手接管默认 provider（auto_provision 默认 true），实际 "
        f"{env.default_provider_id!r}"
    )
    assert provision.is_managed(env.plugin) is True

    inst_map = env.provider_manager.inst_map
    assert support.MANAGED_PROVIDER_ID in inst_map, (
        "钩子跑完后真 ProviderManager 里没有 ofm-free（现有："
        f"{sorted(inst_map)}），装完即用不成立"
    )
