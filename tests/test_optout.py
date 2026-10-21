"""契约 13：opt-out 持久化 + 失败上限后的慢速重试（v0.3.1）。

两条都是「拆掉插件之后别再烦我 / 别因为一时失败就永久放弃」的语义：

* ``/ofm unprovision`` 必须把 ``auto_provision=false`` **写回插件配置**，否则重启后
  插件会把用户刚拆掉的托管条目重新装回来；
* 自愈失败次数用尽（3 次）**不等于永久放弃**：降到每 1800 秒试一次，OFM 晚起很久
  也能自己恢复，而不是必须重启 AstrBot。
"""

from __future__ import annotations

import time

import helpers


def _plugin(**config):
    main = helpers.load_main()
    cfg = {"auto_provision": True}
    cfg.update(config)
    return helpers.instantiate_plugin(helpers.find_plugin_class(main), config=cfg)


# ---------------------------------------------------------------------------
# opt-out 持久化
# ---------------------------------------------------------------------------
def test_unprovision_persists_opt_out(ofm_home):
    """没有东西可拆时也要落盘 opt-out：用户的意图与「有没有干货」无关。"""

    plugin = _plugin()
    text = helpers.invoke_command_text(plugin, "unprovision")

    assert plugin.config.get("auto_provision") is False, (
        "契约缺口：/ofm unprovision 没有把 auto_provision=false 写回插件配置，"
        f"下次重启会重新接入。实际配置：{dict(plugin.config)!r}"
    )
    assert "auto_provision" in text, f"回复里没有说明 opt-out 已写回：{text!r}"
    assert "/ofm provision" in text, f"回复没给回滚入口：{text!r}"


def test_unprovision_is_idempotent(ofm_home):
    """连跑两次不炸、状态一致。"""

    plugin = _plugin(auto_provision=False)
    first = helpers.invoke_command_text(plugin, "unprovision")
    second = helpers.invoke_command_text(plugin, "unprovision")

    assert plugin.config.get("auto_provision") is False
    assert isinstance(first, str) and first
    assert isinstance(second, str) and second


# ---------------------------------------------------------------------------
# 失败上限后的慢速重试
# ---------------------------------------------------------------------------
def test_exhausted_retries_fall_back_to_slow_window():
    """3 次用尽后：300 秒不够，1800 秒才重试；而不是永远不再调度。"""

    async def scenario():
        plugin = _plugin()
        plugin._provision_failures = 3

        plugin._provision_last = {"ok": False, "checked_at": int(time.time()) - 301}
        plugin._maybe_schedule_provision()
        too_early = plugin._provision_task

        plugin._provision_last = {"ok": False, "checked_at": int(time.time()) - 1801}
        plugin._maybe_schedule_provision()
        late = plugin._provision_task
        if late is not None:
            late.cancel()
        return too_early, late

    too_early, late = helpers._run(scenario())

    assert too_early is None, (
        "失败上限用尽后，300 秒的窗口不应再触发重试（应等 1800 秒），"
        f"实际调度了：{too_early!r}"
    )
    assert late is not None, (
        "契约缺口：失败上限用尽后彻底不再调度，OFM 晚起就只能靠重启 AstrBot 恢复"
    )


def test_success_path_uses_normal_window():
    """上次成功时走 300 秒复检窗口（漂移复检），不受失败计数影响。"""

    async def scenario():
        plugin = _plugin()
        plugin._provision_failures = 3  # 成功路径不该看这个计数器
        plugin._provision_last = {
            "ok": True,
            "checked_at": int(time.time()) - 301,
            "base_url": "http://127.0.0.1:18900",
        }
        plugin._maybe_schedule_provision()
        task = plugin._provision_task
        if task is not None:
            task.cancel()
        return task

    assert helpers._run(scenario()) is not None, (
        "上次成功、距上次检查超过 300 秒时应调度端点复检"
    )
