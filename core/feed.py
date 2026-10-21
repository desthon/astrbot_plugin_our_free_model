"""读 OFM 的公告缓存 ``<data_dir>/feed.json``。

正文是服务端白名单 HTML，直接丢进群消息既难看也有风险，所以这里统一剥标签、
还原实体、压空白，再按 ``limit`` 截断。插件只读该文件，不做推送、不改写。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

__all__ = ["announcements", "plain_text", "parse_time"]

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_TAG = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^<>]*>")
_ENTITY = re.compile(r"&(#x?[0-9a-fA-F]+|[a-zA-Z]+);")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_BLANKLINES = re.compile(r"\n{3,}")

_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "mdash": "—",
    "ndash": "–",
    "hellip": "…",
    "lsquo": "‘",
    "rsquo": "’",
    "ldquo": "“",
    "rdquo": "”",
    "middot": "·",
}


def _unescape(text: str) -> str:
    """单趟还原命名实体与数字实体（不依赖 ``html`` 模块）。"""

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body[:1] == "#":
            try:
                code = int(body[2:], 16) if body[1:2].lower() == "x" else int(body[1:])
            except (ValueError, IndexError):
                return match.group(0)
            if 0 < code < 0x110000:
                try:
                    return chr(code)
                except ValueError:
                    return match.group(0)
            return match.group(0)
        return _ENTITIES.get(body.lower(), match.group(0))

    return _ENTITY.sub(replace, text)


def _load(data_dir: Path) -> dict | None:
    try:
        with (Path(data_dir).expanduser() / "feed.json").open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_time(value: object) -> float:
    """把 ISO 字符串 / 秒 / 毫秒时间戳统一成 epoch 秒；认不出返回 ``0.0``。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp / 1000.0 if stamp > 1e11 else stamp
    if not isinstance(value, str):
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    if text.isdigit():
        return parse_time(int(text))
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return 0.0


def announcements(data_dir: Path) -> list[dict]:
    """按 ``createdAt`` 倒序返回未过期公告；文件缺失或损坏时返回空列表。"""
    payload = _load(data_dir)
    if payload is None:
        return []
    rows = payload.get("announcements")
    if not isinstance(rows, list):
        return []
    now = time.time()
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        expires_at = parse_time(row.get("expiresAt")) if row.get("expiresAt") else 0.0
        if expires_at and expires_at <= now:
            continue
        items.append(row)
    items.sort(key=lambda item: parse_time(item.get("createdAt")), reverse=True)
    return items


def plain_text(item: dict, limit: int = 800) -> str:
    """把一条公告的 HTML 正文转成纯文本并截断（``limit<=0`` 表示不截断）。"""
    data = item if isinstance(item, dict) else {}
    raw = data.get("html") or data.get("text") or data.get("body") or data.get("content") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _BLOCK_TAG.sub("\n", text)
    text = _TAG.sub("", text)
    text = _unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKLINES.sub("\n\n", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[: max(limit - 1, 1)].rstrip() + "…"
    return text
