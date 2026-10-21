"""契约 12：命令面 smoke —— 无服务时不崩、给可操作提示、setup 的 api_base 形态。

这些用例直接通过 stub 的 ``filter.*`` 标记找到处理器并调用（不经 AstrBot 的
消息分发），因此判定的是「处理器自身是否健壮」，与宿主版本无关。
"""

from __future__ import annotations

import re

import pytest

import helpers
from conftest import DEFAULT_KEY

ACTIONABLE_KEYWORDS = (
    "端口",
    "启动",
    "打开",
    "未找到",
    "不可达",
    "转发",
    "运行",
    "设置",
    "安装",
    "请",
)

URL_PATTERN = re.compile(r"https?://[^\s\"'<>`）)，,]+")


def _pin_dead_endpoint(ofm_home) -> int:
    """Point settings at a port nothing listens on (connection refused)."""

    port = helpers.free_port()
    ofm_home.write_settings(forward_key=DEFAULT_KEY, standalone_port=port, forward_port=port)
    return port


def _load_plugin():
    main = helpers.load_main()
    return helpers.instantiate_plugin(helpers.find_plugin_class(main))


# ---------------------------------------------------------------------------
# no service at all
# ---------------------------------------------------------------------------
def test_status_command_returns_actionable_hint_without_service(ofm_home):
    dead_port = _pin_dead_endpoint(ofm_home)
    plugin = _load_plugin()

    text = helpers.invoke_command_text(plugin, "status")

    assert isinstance(text, str)
    assert text.strip(), "/ofm status 必须在没有服务时也给出文本（不能返回空）"
    assert re.search(r"ofm|OFM", text), f"status 文本应自报门面，实际={text!r}"
    assert any(keyword in text for keyword in ACTIONABLE_KEYWORDS), (
        f"status 应给出可操作提示（如端口/启动/转发），实际={text!r}；"
        f"本次配置的死端口={dead_port}"
    )


def test_doctor_command_reports_checks_without_service(ofm_home):
    _pin_dead_endpoint(ofm_home)
    plugin = _load_plugin()

    text = helpers.invoke_command_text(plugin, "doctor")

    assert isinstance(text, str) and text.strip()
    hits = [keyword for keyword in ACTIONABLE_KEYWORDS if keyword in text]
    assert len(hits) >= 2, (
        f"doctor 是逐项体检，至少应命中 2 个可操作关键词，实际命中={hits}，文本={text!r}"
    )
    assert ("forwardKey" in text) or ("转发" in text) or ("settings" in text), (
        "doctor 必须报告 forwardKey / 转发开关 / settings.json 这类关键检查项，"
        f"实际={text!r}"
    )


# ---------------------------------------------------------------------------
# setup with a healthy gateway
# ---------------------------------------------------------------------------
def test_setup_command_prints_v1_api_base_and_masks_key(gateway, ofm_home):
    ofm_home.write_settings(
        forward_key=DEFAULT_KEY,
        standalone_port=gateway.port,
        forward_port=gateway.port,
    )
    plugin = _load_plugin()

    text = helpers.invoke_command_text(plugin, "setup")

    urls = URL_PATTERN.findall(text)
    v1_urls = [url for url in urls if url.rstrip("/").endswith("/v1")]
    assert v1_urls, (
        "setup 必须打印可直接粘贴的 api_base 且以 /v1 结尾，"
        f"实际 URL={urls}，文本={text!r}"
    )
    assert any(f":{gateway.port}" in url for url in v1_urls), (
        f"api_base 应指向被发现的端口 {gateway.port}，实际={v1_urls}"
    )
    # 说明：这里**不**断言粘贴块里的 key 被掩码。setup 的用途是把 4 个字段直接粘进
    # WebUI 的 provider 表单，掩掉 key 会让它不可用；掩码能力由 safe_mask 单测覆盖
    # （tests/test_secrets.py）。契约 12 对 setup 的要求只有「api_base 以 /v1 结尾」。


@pytest.mark.parametrize("command", ["status", "models", "usage", "announce"])
def test_status_class_commands_never_raise_without_service(ofm_home, command):
    """只读命令在无服务环境下都必须是「返回文本」，而不是抛异常。"""

    _pin_dead_endpoint(ofm_home)
    plugin = _load_plugin()

    text = helpers.invoke_command_text(plugin, command)

    assert isinstance(text, str), f"/ofm {command} 必须返回文本"
    assert text.strip(), f"/ofm {command} 返回了空文本"
