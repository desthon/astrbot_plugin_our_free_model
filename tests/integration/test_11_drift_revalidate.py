"""F2 契约：端点漂移复检 + 300 秒节流。

- 成功接入后，KV ``provision_last`` 必须记录 ``base_url`` / ``model`` / ``checked_at``。
- ``_maybe_schedule_provision()`` 在「上次成功 + 距上次检查 > 300s + 解析出的
  base_url 与记录不同」时也要调度 ``ensure()``（不再只在失败时）。
- 300 秒内重复触发**不得**产生任何新的 HTTP 请求（防「每条消息都打网关」）。

两条用例都不用 sleep 来等 300 秒：直接把 KV 里的 ``checked_at`` 往前挪——这等价于
「进程重启后从持久化状态恢复」，也是实现读取该字段的自然路径。
"""

from __future__ import annotations

from . import support

STALE_AFTER = 301.0
FRESH_WITHIN = 30.0


def _requests_total(*gateways) -> int:
    return sum(gateway.request_count() for gateway in gateways)


def _assert_provision_last_shape(payload, gateway, model) -> None:
    assert isinstance(payload, dict) and payload, (
        f"契约缺口：KV provision_last 为空（实际 {payload!r}）"
    )
    for key in ("base_url", "model", "checked_at"):
        assert key in payload, (
            f"契约缺口：provision_last 缺 {key}。F2 要求记录 base_url / model / "
            f"checked_at，实际键：{sorted(payload)}"
        )
    recorded = str(payload["base_url"])
    assert recorded.startswith(gateway.base_url), (
        f"provision_last['base_url']={recorded!r} 应指向本次实际使用的端点 "
        f"{gateway.base_url!r}"
    )
    assert payload["model"] == model, (
        f"provision_last['model']={payload['model']!r} 与配置 model={model!r} 不一致"
    )
    assert payload["checked_at"], "provision_last['checked_at'] 为空，无法判断 300 秒节流"


# ---------------------------------------------------------------------------
# (e) 漂移超过 300 秒 → 触发复检：配置、实例端点、请求计数三者都要动
# ---------------------------------------------------------------------------
def test_drift_beyond_throttle_window_revalidates(environment, gateway_factory):
    env = environment

    first = support.drive_plugin_provision(env.plugin)
    assert first is not None and first.ok is True, (
        f"前置条件：插件侧接入失败（result={first!r}）"
    )
    model = env.managed_provider()["model"]
    _assert_provision_last_shape(support.provision_last_payload(), env.gateway, model)

    moved = gateway_factory(key="ofm-drift-f2-0001")
    assert moved.port != env.gateway.port, "新网关必须落在新端口上"
    support.write_settings(env.home, key=moved.key, port=moved.port)
    env.gateway.stop()

    support.rewind_provision_checked_at(env.plugin, STALE_AFTER)

    before = _requests_total(env.gateway, moved)
    note = support.fire_maybe_schedule(env.plugin)
    assert note["task_error"] is None, (
        f"_maybe_schedule_provision() 调度的任务抛异常：{note['task_error']}"
    )
    after = _requests_total(env.gateway, moved)

    source = env.managed_source()
    assert source["api_base"] == moved.openai_base_url, (
        "复检后 api_base 应更新为新端点 "
        f"{moved.openai_base_url!r}，实际 {source['api_base']!r}"
    )

    surface, base_url = support.plugin_endpoint_base_url(env.plugin)
    assert moved.base_url in base_url, (
        f"实例端点仍指向旧地址：观测面 {surface} = {base_url!r}，期望指向 {moved.base_url!r}"
    )

    assert moved.request_count() > 0, (
        "复检没有向新网关发出任何请求（请求计数 0），说明 _maybe_schedule_provision() "
        "并没有真的触发 ensure/resolve"
    )
    assert after > before, f"网关请求总数应增加：before={before} after={after}"


# ---------------------------------------------------------------------------
# (f) 300 秒内触发 → 一个 HTTP 请求都不许新增
# ---------------------------------------------------------------------------
def test_no_requests_within_throttle_window(environment, gateway_factory):
    env = environment

    first = support.drive_plugin_provision(env.plugin)
    assert first is not None and first.ok is True, (
        f"前置条件：插件侧接入失败（result={first!r}）"
    )
    original_api_base = env.managed_source()["api_base"]

    moved = gateway_factory(key="ofm-drift-f2-0002")
    support.write_settings(env.home, key=moved.key, port=moved.port)
    env.gateway.stop()
    support.rewind_provision_checked_at(env.plugin, FRESH_WITHIN)

    before = (env.gateway.request_count(), moved.request_count())
    note = support.fire_maybe_schedule(env.plugin)
    assert note["task_error"] is None, (
        f"_maybe_schedule_provision() 调度的任务抛异常：{note['task_error']}"
    )
    after = (env.gateway.request_count(), moved.request_count())

    assert after == before, (
        "距上次检查不足 300 秒时不得产生任何新的 HTTP 请求（这是"
        f"「每条消息都打网关」的回归门）：before={before} after={after}"
    )
    assert env.managed_source()["api_base"] == original_api_base, (
        "节流窗口内不应改写 api_base，实际 "
        f"{env.managed_source()['api_base']!r}（原 {original_api_base!r}）"
    )
