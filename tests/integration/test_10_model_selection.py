"""F1 契约：``ensure()`` 先问 ``GET /v1/models``，按固定优先级选 model。

优先级（任务书 F1）：配置项 ``default_model`` 在目录 → ``combo:recommended-free``
→ 第一个 ``combo:*`` → 第一个 ``-free`` 结尾 → 第一个 id；目录拉不到（非 200 /
解析失败）→ 用配置值、``ok`` 仍 True，但「未校验」必须可观测。

断言只认**运行时事实**：网关真的收到了 ``/v1/models``、真 provider 实例真的能用
选中的 model 发出一条 200 的 chat，而不只是配置里换了个字符串。
"""

from __future__ import annotations

from . import support
from .fake_ofm import model_row

COMBO = "combo:recommended-free"
FALLBACK = "deepseek-v4.1-flash-free"


def _chat_bodies(gateway):
    return [
        request["json"]
        for request in gateway.requests
        if request["path"] == "/v1/chat/completions"
    ]


# ---------------------------------------------------------------------------
# (a) 目录里没有 combo → 选第一个 -free，并且真实例能用它跑通
# ---------------------------------------------------------------------------
def test_selects_free_model_from_catalogue_and_instance_can_chat(environment):
    env = environment
    env.gateway.set_models([model_row(FALLBACK, ["disabled", "low", "high", "max"])])

    result = support.run(env.provision.ensure(env.plugin, env.endpoint()))
    assert result.ok is True, f"ensure 失败：{result.degraded_reason!r}"

    models_requests = [
        request for request in env.gateway.requests if request["path"] == "/v1/models"
    ]
    assert models_requests, (
        "ensure() 没有 GET /v1/models（实际请求："
        f"{[(r['method'], r['path']) for r in env.gateway.requests]}）。"
        "F1 要求先看目录再写配置，否则选出的 model 与网关实际提供的不一致。"
    )

    provider = env.managed_provider()
    assert provider["model"] == FALLBACK, (
        f"目录只有 {FALLBACK!r} 时 provider.model 应为它，实际 {provider['model']!r}"
    )
    assert result.model == FALLBACK, (
        f"ProvisionResult.model={result.model!r} 与配置不一致"
    )

    # 真实例：不是只改了字符串
    inst_map = env.provider_manager.inst_map
    assert support.MANAGED_PROVIDER_ID in inst_map, (
        f"inst_map 里没有 ofm-free（现有 {sorted(inst_map)}）"
    )
    instance = inst_map[support.MANAGED_PROVIDER_ID]
    assert instance.get_model() == FALLBACK, (
        f"provider 实例 model={instance.get_model()!r}，期望 {FALLBACK!r}"
    )

    response = support.run(instance.text_chat(prompt="hi"))
    assert getattr(response, "completion_text", None) == "pong", (
        f"用选中 model 的对话没走通，response={response!r}"
    )
    bodies = _chat_bodies(env.gateway)
    assert bodies, "假网关没收到 /v1/chat/completions"
    assert bodies[-1].get("model") == FALLBACK, (
        f"网关收到的 model={bodies[-1].get('model')!r}，期望 {FALLBACK!r}"
    )


# ---------------------------------------------------------------------------
# (b) /v1/models 500 → 用配置值、ok 仍 True、「未校验」可观测
# ---------------------------------------------------------------------------
def test_models_endpoint_failure_falls_back_to_configured_model(environment):
    env = environment
    env.gateway.models_status = 500

    result = support.run(env.provision.ensure(env.plugin, env.endpoint()))

    models_requests = [
        request for request in env.gateway.requests if request["path"] == "/v1/models"
    ]
    assert models_requests, (
        "ensure() 没尝试 GET /v1/models（不能因为怕失败就不问目录）"
    )
    assert result.ok is True, (
        f"目录拉不到不应让接入失败（ok 必须仍为 True），实际 ok={result.ok}，"
        f"degraded_reason={result.degraded_reason!r}"
    )

    provider = env.managed_provider()
    configured = env.plugin.config.get("default_model")
    assert provider["model"] == COMBO, (
        f"目录不可用时 provider.model 应回落到配置项值 {COMBO!r}，实际 {provider['model']!r}"
    )
    assert result.model == COMBO, f"ProvisionResult.model={result.model!r}，期望 {COMBO!r}"

    surfaces = support.provision_annotation_surfaces(env.plugin, env.provision, result)
    joined = "\n".join(f"[{name}] {text}" for name, text in surfaces.items())
    assert "未校验" in joined, (
        "目录拉不到时必须能观测到「未校验」注记（doctor / inspect_state / KV 任一处），"
        f"实际观测面：\n{joined}"
    )

    assert configured == COMBO, (
        "契约要求新增配置项 default_model 且默认值为 "
        f"{COMBO!r}；当前 plugin.config['default_model']={configured!r}"
    )


# ---------------------------------------------------------------------------
# (c) 目录里的 model 变了 → 重跑 ensure 改写 + updated_model=True
# ---------------------------------------------------------------------------
def test_model_rewritten_with_updated_model_flag_when_catalogue_changes(environment):
    env = environment
    env.gateway.set_models([model_row(COMBO, ["light", "balanced", "deep"]), model_row(FALLBACK)])
    first = support.run(env.provision.ensure(env.plugin, env.endpoint()))
    assert first.ok is True, f"首次 ensure 失败：{first.degraded_reason!r}"
    assert env.managed_provider()["model"] == COMBO, (
        f"目录里有 combo 时应优先选它，实际 {env.managed_provider()['model']!r}"
    )

    env.gateway.set_models([model_row(FALLBACK)])
    second = support.run(env.provision.ensure(env.plugin, env.endpoint()))

    assert second.ok is True, f"二次 ensure 失败：{second.degraded_reason!r}"
    assert support.require_updated_model(second) is True, (
        "目录里已没有 combo:recommended-free，model 被改写时必须报告 updated_model=True"
    )
    provider = env.managed_provider()
    assert provider["model"] == FALLBACK, (
        f"provider.model 应被改写为 {FALLBACK!r}，实际 {provider['model']!r}"
    )
    assert second.model == FALLBACK


# ---------------------------------------------------------------------------
# (d) 选择确定性：同一目录连调两次，选到同一个 id
# ---------------------------------------------------------------------------
def test_model_selection_is_deterministic(environment):
    env = environment
    # 两个都符合「第一个 -free」，按目录顺序取靠前的那个（不是字典序）
    env.gateway.set_models([model_row("zzz-last-free"), model_row("aaa-first-free")])

    first = support.run(env.provision.ensure(env.plugin, env.endpoint()))
    second = support.run(env.provision.ensure(env.plugin, env.endpoint()))

    assert first.ok is True and second.ok is True
    assert first.model == "zzz-last-free", (
        "「第一个以 -free 结尾」应按目录返回顺序，实际选了 "
        f"{first.model!r}（目录顺序：zzz-last-free, aaa-first-free）"
    )
    assert second.model == first.model, (
        f"同一目录两次选择不一致：{first.model!r} vs {second.model!r}"
    )
    assert env.managed_provider()["model"] == first.model
    assert support.require_updated_model(second) is False, (
        "model 仍在目录里且未变化时不应报告 updated_model=True（否则每次 ensure 都改配置）"
    )


# ---------------------------------------------------------------------------
# 规则 1：配置项 default_model 命中目录时优先于 combo:recommended-free
# ---------------------------------------------------------------------------
def test_configured_default_model_wins_when_present(environment):
    env = environment
    env.gateway.set_models(
        [model_row(COMBO, ["light", "balanced"]), model_row("combo:custom-free"), model_row(FALLBACK)]
    )
    env.plugin.config["default_model"] = "combo:custom-free"

    result = support.run(env.provision.ensure(env.plugin, env.endpoint()))

    assert result.ok is True, f"ensure 失败：{result.degraded_reason!r}"
    assert result.model == "combo:custom-free", (
        "配置项 default_model 命中目录时必须优先使用它（规则 1），实际选了 "
        f"{result.model!r}（目录里同时有 {COMBO!r}）"
    )
    assert env.managed_provider()["model"] == "combo:custom-free"
    assert getattr(result, "model_source", None) == "配置", (
        f"来源应标为「配置」，实际 {getattr(result, 'model_source', 'MISSING')!r}"
    )


# ---------------------------------------------------------------------------
# doctor 可见性：/ofm doctor 的「自动接入」块要显示 model 与来源
# ---------------------------------------------------------------------------
def test_doctor_reports_verified_model_source(environment):
    env = environment
    # 目录里没有 default_model 的默认值(combo:recommended-free)，命中规则 3：第一个 combo:*
    env.gateway.set_models([model_row("combo:other-free", ["light", "balanced"]), model_row(FALLBACK)])

    result = support.drive_plugin_provision(env.plugin)
    assert result is not None and result.ok is True, f"接入失败：{result!r}"

    label, evidence = support.doctor_model_source(env.plugin, env.provision, result)
    assert result.model == "combo:other-free", (
        f"前置条件：应命中规则 3（第一个 combo:*），实际 {result.model!r}"
    )
    assert label == "组合", (
        "doctor 的「自动接入」块必须显示 model 来源；按规则 3 选中的 combo:other-free 应标为「组合」。"
        "在 inspect_state / 接入详情 / provision_last 里都没找到来源标签。\n"
        f"观测面：\n{evidence}"
    )
    assert "combo:other-free" in evidence, (
        "doctor 块还应显示实际选中的 model（combo:other-free）\n观测面：\n" + evidence
    )


def test_doctor_reports_unverified_model_source(environment):
    env = environment
    env.gateway.models_status = 500

    result = support.drive_plugin_provision(env.plugin)
    assert result is not None and result.ok is True, f"目录不可用不应导致接入失败：{result!r}"

    label, evidence = support.doctor_model_source(env.plugin, env.provision, result)
    assert label == "未校验", (
        "目录拉不到时 doctor 必须显示「未校验」来源，而不是静默显示一个没验证过的 model。\n"
        f"观测面：\n{evidence}"
    )
