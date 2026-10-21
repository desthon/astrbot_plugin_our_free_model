"""用量统计归一：管理 API 优先，失败回落 ``stats.json``。

两种来源的字段形状一致（``requests`` / ``failedRequests`` / ``samples``），差别只在
``samples`` 的长度：管理 API 只回 200 条，``stats.json`` 是 400 条环形数组。
所以 token 与平均速度只能按「样本合计」展示，命令层会显式标注。

``stats.json`` 只读——OFM 自己会就地做版本迁移，插件不写它。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Usage", "usage", "usage_from_stats_file", "last_failure", "invalidate_usage_cache"]


@dataclass(frozen=True)
class Usage:
    """归一后的用量快照。"""

    source: str  # "management"|"stats_file"|"unavailable"
    requests: int
    ok: int
    failed: int
    tokens_in: int
    tokens_out: int
    avg_tps: float | None


_UNAVAILABLE = Usage(
    source="unavailable",
    requests=0,
    ok=0,
    failed=0,
    tokens_in=0,
    tokens_out=0,
    avg_tps=None,
)

#: base_url → (monotonic 时间戳, Usage)
_CACHE: dict[str, tuple[float, Usage]] = {}


def _read_json(path: Path) -> dict | None:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(float(value.strip())), 0)
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _bag(payload: dict) -> dict:
    """把可能嵌套在 ``stats`` 下的字段摊平到一层。"""
    nested = payload.get("stats")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({k: v for k, v in payload.items() if k != "stats"})
        return merged
    return payload


def _normalize(payload: dict, source: str) -> Usage | None:
    """把一份统计载荷转成 :class:`Usage`；完全认不出字段时返回 ``None``。"""
    data = _bag(payload if isinstance(payload, dict) else {})
    samples = data.get("samples")
    sample_rows = [row for row in samples if isinstance(row, dict)] if isinstance(samples, list) else []

    has_totals = any(key in data for key in ("requests", "failedRequests", "totalRequests"))
    if not has_totals and not sample_rows:
        return None

    sample_ok = sum(1 for row in sample_rows if row.get("ok") is True)
    sample_failed = sum(1 for row in sample_rows if row.get("ok") is False)

    requests = _int(data.get("requests", data.get("totalRequests")))
    failed = _int(data.get("failedRequests"))
    if requests == 0 and sample_rows:
        requests = len(sample_rows)
    if "failedRequests" not in data and "requests" not in data and "totalRequests" not in data:
        failed = sample_failed
    if failed > requests:
        failed = requests
    ok = requests - failed if requests else sample_ok

    tokens_in = sum(_int(row.get("input")) for row in sample_rows)
    tokens_out = sum(_int(row.get("output")) for row in sample_rows)

    speeds = [value for value in (_float(row.get("tps")) for row in sample_rows) if value]
    avg_tps = round(sum(speeds) / len(speeds), 2) if speeds else _float(data.get("avgTps"))

    return Usage(
        source=source,
        requests=requests,
        ok=ok,
        failed=failed,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        avg_tps=avg_tps,
    )


def usage_from_stats_file(data_dir: Path) -> Usage:
    """直接读 ``<data_dir>/stats.json``。"""
    payload = _read_json(Path(data_dir).expanduser() / "stats.json")
    if payload is None:
        return _UNAVAILABLE
    normalized = _normalize(payload, "stats_file")
    return normalized or _UNAVAILABLE


async def usage(client, endpoint, ttl: float = 60.0) -> Usage:
    """优先管理 API，失败回落 ``stats.json``，都不可用则 ``source="unavailable"``。"""
    key = getattr(endpoint, "base_url", "") or ""
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and ttl > 0:
        stamp, cached_usage = cached
        if now - stamp < ttl:
            return cached_usage

    result: Usage | None = None
    try:
        payload = await client.management_stats(limit=200)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        result = _normalize(payload, "management")

    if result is None:
        result = usage_from_stats_file(Path(getattr(endpoint, "data_dir", ".")))

    if ttl > 0 and result.source != "unavailable":
        _CACHE[key] = (now, result)
    return result


def invalidate_usage_cache(base_url: str = "") -> None:
    """清用量缓存；空参数清全部。"""
    if not base_url:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k.startswith(base_url.rstrip("/"))]:
        _CACHE.pop(key, None)


def last_failure(data_dir: Path) -> dict | None:
    """``samples`` 里最后一条 ``ok=false`` 的行（用于「刚才是怎么失败的」）。"""
    payload = _read_json(Path(data_dir).expanduser() / "stats.json")
    if payload is None:
        return None
    samples = payload.get("samples")
    if not isinstance(samples, list):
        return None
    for row in reversed(samples):
        if isinstance(row, dict) and row.get("ok") is False:
            return row
    return None
