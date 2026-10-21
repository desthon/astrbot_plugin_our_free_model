"""契约 11：公告过滤（过期剔除）+ 纯文本化（剥标签/还原实体/截断）。"""

from __future__ import annotations

import helpers

FEED = {
    "fetchedAt": "2026-01-02T00:00:00Z",
    "source": "https://feed.example.invalid/feed.json",
    "announcements": [
        {
            "id": "a1",
            "title": "有效公告",
            "level": "info",
            "html": '<b>粗体</b> &amp; <a href="https://example.invalid/x">链接</a>',
            "createdAt": "2026-01-01T00:00:00Z",
            "expiresAt": "2099-01-01T00:00:00Z",
            "pinned": True,
        },
        {
            "id": "a2",
            "title": "过期公告",
            "level": "urgent",
            "html": "<b>过期</b>",
            "createdAt": "2025-01-01T00:00:00Z",
            "expiresAt": "2025-02-01T00:00:00Z",
        },
    ],
    "knownIds": [],
}


def _feed():
    return helpers.core_module("feed")


def test_announcements_drop_expired_entries(ofm_home):
    ofm_home.write("feed.json", FEED)

    items = _feed().announcements(ofm_home.path)

    assert [item["id"] for item in items] == ["a1"]
    assert all(item["id"] != "a2" for item in items)


def test_announcements_returns_empty_for_missing_feed(tmp_path):
    assert _feed().announcements(tmp_path / "no-such-dir") == []


def test_plain_text_strips_tags_and_unescapes_entities():
    item = FEED["announcements"][0]

    text = _feed().plain_text(item)

    assert "粗体" in text
    assert "链接" in text
    assert "<b>" not in text and "</b>" not in text
    assert "<a" not in text and "</a>" not in text
    assert "&amp;" not in text
    assert " & " in text or "&" in text
    assert "https://example.invalid/x" not in text, "纯文本公告不应保留原始 href"


def test_plain_text_truncates_to_limit():
    long_item = {
        "id": "long",
        "title": "长文",
        "level": "info",
        "html": "<b>" + ("长" * 2000) + "</b>",
        "createdAt": "2026-01-01T00:00:00Z",
        "expiresAt": "2099-01-01T00:00:00Z",
    }

    text = _feed().plain_text(long_item, limit=120)

    assert len(text) <= 120, f"limit=120 时长度应 <= 120，实际 {len(text)}"
    assert text.strip()
    assert "<b>" not in text


def test_plain_text_default_limit_is_800():
    long_item = {
        "id": "long-default",
        "title": "长文",
        "html": "<i>" + ("字" * 3000) + "</i>",
        "createdAt": "2026-01-01T00:00:00Z",
    }

    text = _feed().plain_text(long_item)

    assert len(text) <= 800
    assert text.strip()


def test_plain_text_handles_item_without_html():
    text = _feed().plain_text({"id": "plain", "title": "只有标题", "level": "info"})
    assert isinstance(text, str)
    assert "只有标题" in text or text == ""
