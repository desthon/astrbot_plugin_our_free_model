"""Harness self-check: stubs and the fake gateway behave as the tests assume.

These tests pass with *no* implementation present — their job is to prove the
test rig itself is trustworthy (if they fail, every other verdict is suspect).
"""

from __future__ import annotations

import enum
import json

import httpx
import pytest

import helpers
from mock_ofm import DEFAULT_KEY, PLUGIN_FINGERPRINT, STANDALONE_FINGERPRINT


# ---------------------------------------------------------------------------
# astrbot stubs
# ---------------------------------------------------------------------------
def test_stub_star_exposes_in_memory_kv_store():
    from astrbot.api.star import AstrBotConfig, Context, Star, logger

    star = Star(Context(), AstrBotConfig({"default_model": "combo:recommended-free"}))
    assert isinstance(logger.name, str)

    assert star.get_kv_data("missing") is None
    star.put_kv_data("k", {"model": "combo:x"})
    assert star.get_kv_data("k") == {"model": "combo:x"}
    star.delete_kv_data("k")
    assert star.get_kv_data("k", "fallback") == "fallback"

    # config is dict-like in both directions
    assert star.config["default_model"] == "combo:recommended-free"
    assert star.config.default_model == "combo:recommended-free"


def test_stub_context_is_permissive():
    from astrbot.api.star import Context

    ctx = Context()
    assert ctx.get_using_provider() is None  # unknown API → no-op, no crash


def test_stub_filter_decorators_are_passthrough_and_tag_handlers():
    from astrbot.api.event import AstrMessageEvent, filter

    def handler(event):  # pragma: no cover - never called here
        return "ok"

    # with argument
    assert filter.command("status")(handler) is handler
    # bare
    assert filter.permission_type(handler) is handler
    # factory needing a call (permission type, hook)
    decorated = filter.permission_type(filter.PermissionType.ADMIN)(handler)
    assert decorated is handler

    marks = helpers.handler_marks(handler)
    kinds = [kind for kind, _ in marks]
    assert "command" in kinds
    assert "permission_type" in kinds
    assert any(args == ("status",) for kind, args in marks if kind == "command")

    # PermissionType is an enum.Flag with ADMIN
    assert issubclass(filter.PermissionType, enum.Flag)
    assert filter.PermissionType.ADMIN in filter.PermissionType

    event = AstrMessageEvent("hi", unified_msg_origin="umo::x")
    assert str(event.plain_result("hello")) == "hello"
    assert event.unified_msg_origin == "umo::x"


def test_stub_hook_decorator_marks_on_llm_request():
    from astrbot.api.event import filter

    async def hook(event, req):  # pragma: no cover - never called here
        return None

    assert filter.on_llm_request()(hook) is hook
    assert helpers.handler_marks(hook) == [("on_llm_request", ())]


# ---------------------------------------------------------------------------
# fake gateway
# ---------------------------------------------------------------------------
def test_mock_gateway_health_fingerprints(gateway, gateway_factory):
    standalone = httpx.get(f"{gateway.base_url}/health", timeout=5).json()
    assert standalone["ok"] is True
    assert standalone["service"] == STANDALONE_FINGERPRINT
    assert standalone["product"] == "standalone"

    plugin_mode = gateway_factory(fingerprint=PLUGIN_FINGERPRINT)
    payload = httpx.get(f"{plugin_mode.base_url}/health", timeout=5).json()
    assert payload == {"ok": True, "service": PLUGIN_FINGERPRINT}


def test_mock_gateway_requires_bearer_and_returns_models(gateway):
    unauthorized = httpx.get(f"{gateway.base_url}/v1/models", timeout=5)
    assert unauthorized.status_code == 401

    authorized = httpx.get(
        f"{gateway.base_url}/v1/models",
        headers={"Authorization": f"Bearer {DEFAULT_KEY}"},
        timeout=5,
    )
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["object"] == "list"
    by_id = {row["id"]: row for row in body["data"]}
    assert "deepseek-v4.1-flash-free" in by_id
    assert by_id["deepseek-v4.1-flash-free"]["x_ofm_effort_default"] == "high"
    assert by_id["bare-id"].get("x_ofm_efforts") is None


def test_mock_gateway_management_stats_shape(gateway):
    response = httpx.get(
        f"{gateway.base_url}/api/management/stats",
        headers={"Authorization": f"Bearer {DEFAULT_KEY}"},
        timeout=5,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requests"] == 3
    assert body["failedRequests"] == 1
    assert sum(s["input"] for s in body["samples"]) == 300
    assert sum(s["output"] for s in body["samples"]) == 100


@pytest.mark.parametrize(
    ("mode", "status", "needle"),
    [
        ("503", 503, "forwarding"),
        ("401", 401, "invalid api key"),
        ("404", 404, "ghost-model"),
        ("unavailable", 400, "Model is unavailable"),
    ],
)
def test_mock_gateway_fault_modes(gateway_factory, mode, status, needle):
    server = gateway_factory(mode=mode)
    response = httpx.get(
        f"{server.base_url}/v1/models",
        headers={"Authorization": f"Bearer {DEFAULT_KEY}"},
        timeout=5,
    )
    assert response.status_code == status
    assert needle in json.dumps(response.json())

    # 503 simulates "OFM did not bind the forward port": /health is down too.
    health = httpx.get(f"{server.base_url}/health", timeout=5)
    if mode == "503":
        assert health.status_code == 503
    else:
        assert health.status_code == 200
