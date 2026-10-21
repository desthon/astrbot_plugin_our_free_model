"""契约 5 + 6：模型目录的 channel 归一、字段解析与 effort 白名单校验。"""

from __future__ import annotations

import asyncio

import pytest

import helpers
from conftest import DEFAULT_KEY


def _catalog():
    return helpers.core_module("catalog")


# ---------------------------------------------------------------------------
# 5. channel_of
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("deepseek-v4.1-flash-free", "free"),
        # 注意：它也以 -free 结尾，但组合链必须优先判定为 combo
        ("combo:recommended-free", "combo"),
        # 也含 "/" 且以 free 结尾，Kilo 免费池必须优先于通用 provider/model 分支
        ("kilo-auto/free", "kilo"),
        ("bare-id", "unknown"),
    ],
)
def test_channel_of_classifies_known_shapes(model_id, expected):
    assert _catalog().channel_of(model_id) == expected


def test_channel_of_aggregate_or_eac_for_vendor_slash_model():
    """``vendor/some-model``：断言 ``aggregate`` 或 ``eac`` 之一。

    判定理由：``provider/model`` 与 ``org/model`` 在 ``/v1/models`` 里**没有任何
    标记字段**可区分（plan §4.3 明确写了聚合渠道与 EAC 渠道同形），插件只能
    按启发式归类，任一归类都符合契约；但必须落在这两类之一，不能是 unknown。
    """

    channel = _catalog().channel_of("vendor/some-model")
    assert channel in {"aggregate", "eac"}, (
        "provider/model 形状必须归为聚合渠道或 EAC 渠道之一，实际="
        f"{channel!r}（plan §4.3 启发式表）"
    )
    assert channel != "unknown"


def test_channel_of_combo_beats_free_suffix():
    """``combo:x`` 的优先级：组合链是独立语义，不能被 -free 后缀吞掉。"""

    catalog = _catalog()
    assert catalog.channel_of("combo:recommended-free") == "combo"
    assert catalog.channel_of("combo:anything") == "combo"


# ---------------------------------------------------------------------------
# parse_row
# ---------------------------------------------------------------------------
def test_parse_row_reads_context_window_and_efforts():
    catalog = _catalog()
    info = catalog.parse_row(
        {
            "id": "deepseek-v4.1-flash-free",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
            "context_window": 1048576,
            "x_ofm_efforts": ["disabled", "low", "high", "max"],
            "x_ofm_effort_default": "high",
        }
    )

    assert info.id == "deepseek-v4.1-flash-free"
    assert info.context_window == 1048576
    assert tuple(info.efforts) == ("disabled", "low", "high", "max")
    assert info.effort_default == "high"
    assert info.channel == "free"
    assert isinstance(info.channel, str)


def test_parse_row_tolerates_missing_optional_fields():
    """``bare-id`` 既无 context_window 也无 x_ofm_efforts，字段必须为 None/空。"""

    catalog = _catalog()
    info = catalog.parse_row({"id": "bare-id", "object": "model", "created": 1, "owned_by": "our-free-model"})

    assert info.id == "bare-id"
    assert info.context_window is None
    assert tuple(info.efforts) == ()
    assert info.effort_default is None
    assert info.channel == "unknown"


# ---------------------------------------------------------------------------
# 6. supports_effort
# ---------------------------------------------------------------------------
def _info_with_efforts():
    return _catalog().parse_row(
        {
            "id": "deepseek-v4.1-flash-free",
            "context_window": 1048576,
            "x_ofm_efforts": ["disabled", "low", "high", "max"],
            "x_ofm_effort_default": "high",
        }
    )


@pytest.mark.parametrize("effort", ["disabled", "low", "high", "max"])
def test_supports_effort_accepts_declared_levels(effort):
    catalog = _catalog()
    assert catalog.supports_effort(_info_with_efforts(), effort) is True


# 大小写与首尾空白归一不在此断言：折叠后仍落在 x_ofm_efforts 内，属于实现自由；
# 这里只钉死「归一后依然不在列表里」的取值，避免把实现自由度当契约。
@pytest.mark.parametrize("effort", ["extreme", "medium", "light", ""])
def test_supports_effort_rejects_undeclared_levels(effort):
    """非法档必须被插件拦下（不能静默回落给上游）。"""

    catalog = _catalog()
    assert catalog.supports_effort(_info_with_efforts(), effort) is False, (
        f"{effort!r} 不在 x_ofm_efforts 内，supports_effort 必须返回 False"
    )


def test_supports_effort_false_when_model_declares_no_efforts():
    catalog = _catalog()
    bare = catalog.parse_row({"id": "bare-id"})
    assert catalog.supports_effort(bare, "high") is False


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------
def test_load_fetches_and_parses_catalog_from_gateway(gateway, ofm_home):
    catalog = _catalog()

    with helpers.client_for(gateway, ofm_home.path, DEFAULT_KEY) as (client, _endpoint):
        models = asyncio.run(catalog.load(client, ttl=0.0, force=True))

    by_id = {info.id: info for info in models}
    assert set(by_id) == {
        "deepseek-v4.1-flash-free",
        "combo:recommended-free",
        "kilo-auto/free",
        "vendor/some-model",
        "bare-id",
    }
    assert tuple(by_id["combo:recommended-free"].efforts) == ("light", "balanced", "deep")
    assert by_id["combo:recommended-free"].effort_default == "balanced"
    assert by_id["deepseek-v4.1-flash-free"].channel == "free"
    assert by_id["kilo-auto/free"].channel == "kilo"
    assert by_id["bare-id"].efforts == ()


def test_groups_and_find_bucket_models(gateway, ofm_home):
    """``groups``/``find`` 是命令面的数据来源，至少必须能把模型分桶与检索。"""

    catalog = _catalog()
    with helpers.client_for(gateway, ofm_home.path, DEFAULT_KEY) as (client, _endpoint):
        models = asyncio.run(catalog.load(client, ttl=0.0, force=True))

    grouped = catalog.groups(models)
    assert isinstance(grouped, dict)
    flat = [info.id for bucket in grouped.values() for info in bucket]
    assert sorted(flat) == sorted(info.id for info in models)

    assert [info.id for info in catalog.find(models, "combo")] == ["combo:recommended-free"]
    assert [info.id for info in catalog.find(models, "deepseek")] == ["deepseek-v4.1-flash-free"]
    assert catalog.find(models, "no-such-model-fragment") == []


def test_describe_returns_readable_string():
    catalog = _catalog()
    text = catalog.describe(_info_with_efforts())
    assert isinstance(text, str) and text.strip()
    assert "Traceback" not in text
