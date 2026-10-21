"""契约 1：真 AstrBot 4.29.1 按真实模块路径装载插件，注册的形状对不对。

关于任务书里「9 条命令」的口径：真 registry 里 ``/ofm`` 命令**组**本身也登记成
一条 ``AdapterMessageEvent`` handler（当前实测 10 条：组根 + 9 个子命令）。
所以这里按**语义**断言：9 条既有子命令 + 契约新增 ``provision``/``unprovision``
= 11 条子命令 handler（含组根共 12 条 AdapterMessageEvent）。命令名从
``CommandFilter.get_complete_command_names()`` 取，不依赖实现给方法起什么名字。
"""

from __future__ import annotations

from . import support


def test_real_module_load_registers_hooks_and_commands(real_astrbot, home):
    module = support.import_plugin_main()
    assert module.__name__ == support.MODULE_PATH, (
        f"插件必须按真实模块路径导入，实际 {module.__name__}"
    )

    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(support.MODULE_PATH)
    assert handlers, (
        f"真 star_handlers_registry 里没有 {support.MODULE_PATH} 注册的任何 handler"
    )

    by_type: dict[object, list] = {}
    for handler in handlers:
        by_type.setdefault(handler.event_type, []).append(handler)

    loaded = by_type.get(EventType.OnAstrBotLoadedEvent, [])
    assert len(loaded) == 1, (
        "启动后自动接入依赖 @filter.on_astrbot_loaded()：期望恰好 1 个 "
        f"OnAstrBotLoadedEvent handler，实际 {[h.handler_name for h in loaded]}"
    )

    llm_request = by_type.get(EventType.OnLLMRequestEvent, [])
    assert len(llm_request) == 1, (
        "期望恰好 1 个 OnLLMRequestEvent handler（思考强度尾缀注入），实际 "
        f"{[h.handler_name for h in llm_request]}"
    )

    commands = support.command_names_for(support.MODULE_PATH)
    expected = support.BASELINE_SUBCOMMANDS | support.NEW_SUBCOMMANDS
    missing = expected - commands
    assert not missing, (
        "缺失的命令：" + str(sorted(missing)) + "；实际注册的命令：" + str(sorted(commands))
    )
    assert "ofm" in commands, f"命令组根 /ofm 未注册，实际 {sorted(commands)}"
    extra = commands - expected - {"ofm"}
    assert not extra, f"多出契约外的命令：{sorted(extra)}"

    subcommands = {name for name in commands if name != "ofm"}
    assert subcommands == expected, (
        f"子命令集合应为 9 条既有 + 2 条新增，实际 {sorted(subcommands)}"
    )
