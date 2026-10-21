"""契约 6（最关键）：真实 payload 对拍。

从真 ``ProviderManager.inst_map["ofm-free"]`` 取出真 ``ProviderOpenAIOfficial``，
用带思考强度尾缀的 model 调 ``text_chat``，然后断言假网关**收到**的请求体里
``model`` 就是原样的 ``deepseek-v4.1-flash-free(high)``。

这一条能一次抓住三类实现错误：
1. 尾缀在接入时被规范化/剥掉（配置里就写丢了），
2. 尾缀被拆到别的字段（例如 extra_body.reasoning_effort），
3. provider 配置写错，请求根本没到我们配的网关（api_base 拼错时 SDK 会打默认域名）。
"""

from __future__ import annotations

import json

from . import support

SUFFIX_MODEL = "deepseek-v4.1-flash-free(high)"


def test_real_provider_payload_keeps_effort_suffix(environment):
    env = environment
    provision = env.provision

    result = support.run(provision.ensure(env.plugin, env.endpoint()))
    assert result.ok is True, f"ensure 失败：{result.degraded_reason!r}"

    inst_map = env.provider_manager.inst_map
    assert support.MANAGED_PROVIDER_ID in inst_map, (
        "inst_map 里没有 ofm-free，无法对拍真实 payload（现有："
        f"{sorted(inst_map)}）"
    )

    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    provider = inst_map[support.MANAGED_PROVIDER_ID]
    assert isinstance(provider, ProviderOpenAIOfficial), (
        "对拍必须发生在真 ProviderOpenAIOfficial 上，实际 "
        f"{type(provider).__module__}.{type(provider).__name__}"
    )

    async def _chat():
        return await provider.text_chat(prompt="hi", model=SUFFIX_MODEL)

    response = support.run(_chat())
    assert getattr(response, "completion_text", None) == "pong", (
        f"真实调用没拿到假网关的回复，response={response!r}；"
        "说明请求没有落在我们配的网关上"
    )

    bodies = [
        request["json"]
        for request in env.gateway.requests
        if request["path"] == "/v1/chat/completions"
    ]
    assert bodies, (
        "假网关没收到任何 /v1/chat/completions 请求；实际收到的路径："
        f"{[request['path'] for request in env.gateway.requests]}"
    )
    assert len(bodies) == 1, f"一次 text_chat 应只发一次请求，实际 {len(bodies)} 次"

    body = bodies[0]
    assert body.get("model") == SUFFIX_MODEL, (
        "网关收到的 model 字段必须是带尾缀的原样字符串 "
        f"{SUFFIX_MODEL!r}，实际 {body.get('model')!r}；"
        "被剥掉或改写说明尾缀没有穿过真实 payload 构建"
    )

    messages = body.get("messages") or []
    assert messages, f"请求体没有 messages：{body}"
    assert any("hi" in json.dumps(message, ensure_ascii=False) for message in messages), (
        f"messages 里没有找到 prompt 'hi'：{json.dumps(messages, ensure_ascii=False)}"
    )

    # 尾缀不许被塞进别的字段/别的消息内容里
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    assert SUFFIX_MODEL not in serialized_messages, (
        "思考强度尾缀出现在了 messages 里，说明它被当成内容拼进去了，"
        f"而不是留在 model 字段：{serialized_messages}"
    )
