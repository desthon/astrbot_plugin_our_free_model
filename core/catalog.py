"""``/v1/models`` 的归一、分组与缓存。

服务端只给 ``id / object / created / owned_by / context_window? / x_ofm_efforts? /
x_ofm_effort_default?``——**没有** name、vision、channel 字段，所以渠道只能按 id
形态启发式推断，输出里必须标注「推断」。``created`` 是每次请求现算的
``Date.now()``，绝不能拿来当缓存键。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ModelInfo",
    "CHANNEL_ORDER",
    "CHANNEL_LABELS",
    "channel_of",
    "parse_row",
    "load",
    "invalidate",
    "groups",
    "find",
    "supports_effort",
    "describe",
    "format_context",
]

#: 分组顺序（也是 /ofm models 的展示顺序）
CHANNEL_ORDER = ("free", "combo", "aggregate", "kilo", "eac", "unknown")

CHANNEL_LABELS = {
    "free": "免费道",
    "combo": "降级链（combo）",
    "aggregate": "聚合渠道",
    "kilo": "Kilo 免费池",
    "eac": "EAC 渠道",
    "unknown": "未分类",
}

#: 全部渠道标签都带「推断」含义，命令层会在列表末尾统一注明。
_EFFORT_SUFFIX = re.compile(r"\(([^()]*)\)\s*$")

#: 缓存放模块级：一个进程只关心少数几个 base_url。
_CACHE: dict[str, tuple[float, list["ModelInfo"]]] = {}


@dataclass(frozen=True)
class ModelInfo:
    """一条模型目录项（能力字段全部可能是 ``None``）。"""

    id: str
    context_window: int | None
    efforts: tuple[str, ...]
    effort_default: str | None
    channel: str


def _clean_id(model_id: Any) -> str:
    return model_id.strip() if isinstance(model_id, str) else ""


def _strip_effort_suffix(model_id: str) -> str:
    return _EFFORT_SUFFIX.sub("", model_id).strip()


def channel_of(model_id: str) -> str:
    """按 id 形态推断渠道（启发式，不能当真值用）。

    * ``combo:<name>`` → ``combo``
    * ``kilo-auto/...`` 或以 ``kilo`` 开头的池 → ``kilo``
    * ``eac:`` / ``eac/`` 显式前缀 → ``eac``
    * 其他含 ``/`` 的 ``<provider>/<model>`` → ``aggregate``
      （EAC 也是 ``<org>/<model>``，无法与聚合严格区分，故统一按聚合处理）
    * 结尾 ``-free`` → ``free``
    * 其余 → ``unknown``
    """
    raw = _clean_id(model_id)
    if not raw:
        return "unknown"
    model = _strip_effort_suffix(raw).lower()
    if model.startswith("combo:"):
        return "combo"
    if model.startswith("kilo") and "/" in model:
        return "kilo"
    if model.startswith("eac:") or model.startswith("eac/"):
        return "eac"
    if "/" in model:
        return "aggregate"
    if model.endswith("-free"):
        return "free"
    return "unknown"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _efforts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return ()
    seen: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def parse_row(row: dict) -> ModelInfo:
    """把一条 ``/v1/models`` 行转成 :class:`ModelInfo`；畸形字段一律降级为 ``None``。"""
    data = row if isinstance(row, dict) else {}
    model_id = _clean_id(data.get("id"))
    default = data.get("x_ofm_effort_default")
    effort_default = default.strip() if isinstance(default, str) and default.strip() else None
    return ModelInfo(
        id=model_id,
        context_window=_as_int(data.get("context_window")),
        efforts=_efforts(data.get("x_ofm_efforts")),
        effort_default=effort_default,
        channel=channel_of(model_id),
    )


def _cache_key(client: Any) -> str:
    endpoint = getattr(client, "endpoint", None)
    base = getattr(endpoint, "base_url", None)
    if isinstance(base, str) and base:
        return base
    return f"<client:{id(client)}>"


async def load(client: Any, ttl: float = 600.0, force: bool = False) -> list[ModelInfo]:
    """拉取并归一模型目录；同一 ``base_url`` 在 ``ttl`` 秒内复用缓存。"""
    key = _cache_key(client)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and not force:
        stamp, models = cached
        if now - stamp < ttl:
            return list(models)

    rows = await client.models()
    parsed: list[ModelInfo] = []
    seen: set[str] = set()
    for row in rows:
        info = parse_row(row)
        if not info.id or info.id in seen:
            continue
        seen.add(info.id)
        parsed.append(info)
    _CACHE[key] = (now, parsed)
    return list(parsed)


def invalidate(base_url: str = "") -> None:
    """清缓存；``base_url`` 为空时清全部（``/ofm refresh`` 之后调用）。"""
    if not base_url:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k.startswith(base_url.rstrip("/"))]:
        _CACHE.pop(key, None)


def groups(models: list[ModelInfo]) -> dict[str, list[ModelInfo]]:
    """按渠道分组，键顺序固定为 :data:`CHANNEL_ORDER`（空组也保留）。"""
    buckets: dict[str, list[ModelInfo]] = {name: [] for name in CHANNEL_ORDER}
    for model in models or []:
        channel = model.channel if model.channel in buckets else "unknown"
        buckets[channel].append(model)
    return buckets


def find(models: list[ModelInfo], token: str) -> list[ModelInfo]:
    """先精确匹配 id，再退化为大小写无关的子串匹配。"""
    needle = (token or "").strip()
    if not needle:
        return []
    exact = [m for m in models or [] if m.id == needle]
    if exact:
        return exact
    lowered = needle.lower()
    return [m for m in models or [] if lowered in m.id.lower()]


def supports_effort(m: ModelInfo, effort: str) -> bool:
    """该模型是否声明了此思考强度；模型没声明档位时一律 ``False``（由调用方提示）。"""
    level = (effort or "").strip().lower()
    if not level:
        return False
    return level in {item.lower() for item in m.efforts}


def _normalize_level(value: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def format_context(value: int | None) -> str:
    """``1048576`` → ``1M``；``128000`` → ``125K``；缺失 → ``未知``。"""
    if not value or value <= 0:
        return "未知"
    if value >= 1_048_576:
        scaled = value / 1_048_576
        text = f"{scaled:.1f}".rstrip("0").rstrip(".")
        return f"{text}M"
    if value >= 1024:
        return f"{value // 1024}K"
    return str(value)


def describe(m: ModelInfo) -> str:
    """单行摘要：``id · 上下文 · 档位 · 渠道``（不含换行）。"""
    efforts = "/".join(m.efforts) if m.efforts else "未声明"
    default = _normalize_level(m.effort_default)
    default_text = f"（默认 {default}）" if default else ""
    name = (m.id or "").replace("\n", " ").strip()
    return f"{name} · 上下文 {format_context(m.context_window)} · 档位 {efforts}{default_text} · 渠道 {m.channel}"
