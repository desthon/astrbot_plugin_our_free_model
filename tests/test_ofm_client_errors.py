"""契约 3 + 4：错误语义归一（401 / 503 / 404 / Model is unavailable）。

所有错误都由**假网关的真实响应**驱动，不直接构造 ``OFMError``，这样断言的是
「客户端把协议错误翻译成 kind 的能力」，而不是数据类的构造形状。
"""

from __future__ import annotations

import asyncio

import pytest

import helpers
from conftest import DEFAULT_KEY

# mode -> (expected kind, expected HTTP status)
FAULT_CASES = {
    "auth": ("401", 401),
    "forward_disabled": ("503", 503),
    "model_not_found": ("404", 404),
    "model_unavailable": ("unavailable", None),
}


def _ofm_module():
    return helpers.core_module("ofm_client")


def _raise_models_error(gateway, data_dir, mode: str, api_key: str | None = None):
    """Drive ``OFMClient.models()`` into the gateway fault ``mode``."""

    # Discover the working base_url spelling with the *correct* key first; the
    # wrong key must only be exercised by the call under test.
    base_url = helpers.working_base_url(gateway, data_dir, gateway.key)
    gateway.mode = mode

    endpoint = helpers.make_endpoint(base_url, api_key or gateway.key, data_dir)
    client = _ofm_module().OFMClient(endpoint, timeout=10.0)
    try:
        asyncio.run(client.models())
    except _ofm_module().OFMError as exc:  # noqa: PERF203 - expected path
        return exc
    except Exception as exc:  # noqa: BLE001 - wrong taxonomy must be reported
        raise AssertionError(
            f"网关 mode={mode!r} 时 models() 抛了 {type(exc).__name__}，契约要求归一为 OFMError: {exc!r}"
        ) from exc
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001
            pass
    raise AssertionError(f"网关 mode={mode!r} 时 models() 没有抛 OFMError（错误被吞掉了）")


# ---------------------------------------------------------------------------
# 3. authentication
# ---------------------------------------------------------------------------
def test_wrong_key_raises_auth_error_with_human_readable_text(gateway, ofm_home):
    """错误 key → 401 → ``kind == "auth"``，``human()`` 是人话不带 traceback。"""

    error = _raise_models_error(gateway, ofm_home.path, "401", api_key="definitely-not-the-key")

    assert error.kind == "auth"
    assert error.status == 401

    human = error.human()
    assert isinstance(human, str) and human.strip()
    assert "Traceback" not in human
    assert 'File "' not in human
    assert "httpx" not in human.lower(), f"human() 不应泄漏底层库细节: {human!r}"


# ---------------------------------------------------------------------------
# 4. four mutually distinct kinds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("expected_kind", sorted(FAULT_CASES))
def test_error_kinds_are_normalised(gateway, ofm_home, expected_kind):
    mode, expected_status = FAULT_CASES[expected_kind]

    error = _raise_models_error(gateway, ofm_home.path, mode)

    assert error.kind == expected_kind, (
        f"网关 mode={mode!r} 应归一为 kind={expected_kind!r}，实际 kind={error.kind!r} "
        f"detail={error.detail!r}"
    )
    if expected_status is not None:
        assert error.status == expected_status
    assert isinstance(error.detail, str)


def test_four_error_kinds_have_pairwise_distinct_human_texts(gateway, ofm_home):
    """4 种 kind 的 ``human()`` 文案必须两两不同（否则用户无法区分故障原因）。"""

    humans: dict[str, str] = {}
    statuses: dict[str, int | None] = {}

    for kind, (mode, _status) in FAULT_CASES.items():
        error = _raise_models_error(gateway, ofm_home.path, mode)
        assert error.kind == kind
        humans[kind] = error.human()
        statuses[kind] = getattr(error, "status", None)

    for kind, text in humans.items():
        assert isinstance(text, str) and text.strip(), f"{kind} 的 human() 为空"

    duplicates = {
        (left, right): humans[left]
        for left in humans
        for right in humans
        if left < right and humans[left].strip() == humans[right].strip()
    }
    assert not duplicates, f"不同 kind 复用了同一句文案: {duplicates}"

    assert len({text.strip() for text in humans.values()}) == len(FAULT_CASES)


def test_forward_disabled_message_mentions_enabling_forwarding(gateway, ofm_home):
    """503 必须翻译成「去打开 OFM 的本地转发」，而不是抛 openai 异常或只写 503。"""

    error = _raise_models_error(gateway, ofm_home.path, "503")
    assert error.kind == "forward_disabled"

    human = error.human()
    assert ("转发" in human) or ("forward" in human.lower()), (
        f"forward_disabled 的提示必须指向「打开转发」，实际={human!r}"
    )
