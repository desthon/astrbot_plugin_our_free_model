"""契约 9：``on_llm_request`` 钩子按会话覆盖改写 ``req.model``，且永不向上抛错。"""

from __future__ import annotations

import asyncio

import pytest

import helpers


def _plugin_and_hook():
    main = helpers.load_main()
    plugin = helpers.instantiate_plugin(helpers.find_plugin_class(main))
    hook = helpers.find_hook(plugin, "on_llm_request")
    return plugin, hook


def _call(hook, umo: str, model: str = "base-model"):
    req = helpers.FakeRequest(model=model)
    asyncio.run(helpers.call_hook(hook, helpers.make_event("hello", umo=umo), req))
    return req


def test_hook_leaves_model_untouched_without_session_override(ofm_home):
    """没有会话覆盖时，钩子必须是纯旁路：一个字段都不许改。"""

    _plugin, hook = _plugin_and_hook()

    req = _call(hook, "umo::hook::no-override")

    assert req.model == "base-model"


def test_hook_applies_model_and_effort_override(ofm_home):
    plugin, hook = _plugin_and_hook()
    store = helpers.ensure_hook_store(plugin, hook)

    umo = "umo::hook::override"
    asyncio.run(store.set_model(umo, "combo:x"))
    asyncio.run(store.set_effort(umo, "high"))

    req = _call(hook, umo)

    assert req.model == "combo:x(high)"


def test_hook_override_is_scoped_to_one_origin(ofm_home):
    plugin, hook = _plugin_and_hook()
    store = helpers.ensure_hook_store(plugin, hook)

    asyncio.run(store.set_model("umo::hook::scoped", "combo:y"))

    assert _call(hook, "umo::hook::scoped").model == "combo:y"
    assert _call(hook, "umo::hook::other").model == "base-model"


def test_hook_swallows_store_failures_and_keeps_model(ofm_home, monkeypatch):
    """KV 后端炸了也不能把异常抛给 AstrBot 请求链。

    直接打类级别的 ``get``：无论钩子是否经由该入口读取状态，只要读会话时抛错，
    异常都不得逃逸到请求链。
    """

    _plugin, hook = _plugin_and_hook()
    SessionStore = helpers.session_store_class()

    async def boom(*args, **kwargs):
        raise RuntimeError("kv backend exploded")

    monkeypatch.setattr(SessionStore, "get", boom)

    req = helpers.FakeRequest(model="base-model")
    asyncio.run(helpers.call_hook(hook, helpers.make_event("hello", umo="umo::hook::boom"), req))

    assert req.model == "base-model"
