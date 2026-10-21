"""契约 7 + 8：``compose_model`` 拼装与会话隔离。"""

from __future__ import annotations

import asyncio

import pytest

import helpers


def _session():
    return helpers.core_module("session")


def _store():
    from astrbot.api.star import Star

    return _session().SessionStore(Star())


# ---------------------------------------------------------------------------
# 7. compose_model
# ---------------------------------------------------------------------------
def test_compose_model_appends_effort_suffix():
    SessionStore = _session().SessionStore
    assert SessionStore.compose_model("combo:x", "high") == "combo:x(high)"
    assert SessionStore.compose_model("deepseek-v4.1-flash-free", "max") == "deepseek-v4.1-flash-free(max)"


def test_compose_model_without_effort_returns_bare_model():
    SessionStore = _session().SessionStore
    assert SessionStore.compose_model("combo:x", None) == "combo:x"
    assert SessionStore.compose_model("combo:x", "") == "combo:x"


@pytest.mark.parametrize("model", ["", None])
def test_compose_model_never_produces_dangling_effort_suffix(model):
    """没有模型名时绝不能产出 ``(high)`` 这种悬空后缀。"""

    SessionStore = _session().SessionStore
    result = SessionStore.compose_model(model, "high")
    assert result in (None, ""), (
        f"compose_model({model!r}, 'high') 应返回 None 或空串，实际={result!r}"
    )
    assert "(" not in str(result)


def test_compose_model_without_model_and_effort_is_empty():
    SessionStore = _session().SessionStore
    result = SessionStore.compose_model("", None)
    assert result in (None, "")


# ---------------------------------------------------------------------------
# 8. session isolation
# ---------------------------------------------------------------------------
def test_session_store_isolates_models_between_origins():
    store = _store()

    asyncio.run(store.set_model("umo::a", "model-a"))
    asyncio.run(store.set_model("umo::b", "model-b"))

    state_a = asyncio.run(store.get("umo::a"))
    state_b = asyncio.run(store.get("umo::b"))

    assert isinstance(state_a, dict) and isinstance(state_b, dict)
    assert state_a.get("model") == "model-a"
    assert state_b.get("model") == "model-b"


def test_session_store_isolates_effort_and_clear_affects_one_origin():
    store = _store()

    asyncio.run(store.set_model("umo::a", "model-a"))
    asyncio.run(store.set_model("umo::b", "model-b"))
    asyncio.run(store.set_effort("umo::a", "high"))

    assert asyncio.run(store.get("umo::a")).get("effort") == "high"
    assert asyncio.run(store.get("umo::b")).get("effort") is None

    asyncio.run(store.clear("umo::a"))

    cleared = asyncio.run(store.get("umo::a"))
    survivor = asyncio.run(store.get("umo::b"))
    assert isinstance(cleared, dict)
    assert cleared.get("model") is None
    assert cleared.get("effort") is None
    assert survivor.get("model") == "model-b"


def test_session_store_get_returns_empty_dict_for_unknown_origin():
    store = _store()
    state = asyncio.run(store.get("umo::never-seen"))
    assert isinstance(state, dict)
    assert state.get("model") is None
