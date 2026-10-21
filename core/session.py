"""会话级模型与思考强度覆盖。

键用 ``event.unified_msg_origin``（群聊 / 私聊天然隔离），值落在 AstrBot 的
PluginKV 里（只允许 int/float/str/bytes/bool/dict/list/None）。
"""

from __future__ import annotations

import inspect
import re
from typing import Any

__all__ = ["SessionStore", "SESSION_PREFIX", "LIST_PREFIX"]

SESSION_PREFIX = "ofm:session:"
LIST_PREFIX = "ofm:lastlist:"

_EFFORT_SUFFIX = re.compile(r"\([^()]*\)\s*$")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class SessionStore:
    """薄封装：只依赖 ``get_kv_data`` / ``put_kv_data`` / ``delete_kv_data``。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    # ---- 会话覆盖 ---------------------------------------------------------
    async def get(self, umo: str) -> dict:
        """返回 ``{"model": str|None, "effort": str|None}``（键始终存在）。"""
        raw = await self._read(SESSION_PREFIX + str(umo), None)
        data = raw if isinstance(raw, dict) else {}
        return {"model": _text(data.get("model")), "effort": _text(data.get("effort"))}

    async def set_model(self, umo: str, model: str | None) -> None:
        state = await self.get(umo)
        state["model"] = _text(model)
        await self._write(SESSION_PREFIX + str(umo), state)

    async def set_effort(self, umo: str, effort: str | None) -> None:
        state = await self.get(umo)
        state["effort"] = _text(effort)
        await self._write(SESSION_PREFIX + str(umo), state)

    async def clear(self, umo: str) -> None:
        await self._delete(SESSION_PREFIX + str(umo))

    # ---- 最近一次列表（给 /ofm use <序号> 用） -----------------------------
    async def get_last_list(self, umo: str) -> list[str]:
        raw = await self._read(LIST_PREFIX + str(umo), None)
        if not isinstance(raw, list):
            return []
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]

    async def set_last_list(self, umo: str, model_ids: list[str]) -> None:
        ids = [str(item) for item in model_ids or [] if str(item).strip()][:100]
        await self._write(LIST_PREFIX + str(umo), ids)

    # ---- 组合 -------------------------------------------------------------
    @staticmethod
    def compose_model(model: str | None, effort: str | None) -> str | None:
        """``("x", "high")`` → ``"x(high)"``；``effort`` 为空时原样返回 ``model``。"""
        base = _text(model)
        if base is None:
            return None
        level = _text(effort)
        if level is None:
            return base
        stripped = _EFFORT_SUFFIX.sub("", base).strip() or base
        return f"{stripped}({level})"

    # ---- KV 兼容层 --------------------------------------------------------
    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        """AstrBot 的 KV 方法是 ``async`` 的，宿主桩可能是同步的——两种都吃。"""
        if inspect.isawaitable(value):
            return await value
        return value

    async def _read(self, key: str, default: Any) -> Any:
        try:
            return await self._maybe_await(self._plugin.get_kv_data(key, default))
        except Exception:  # 读失败退回默认值：会话覆盖不该把命令整体打死
            return default

    async def _write(self, key: str, value: Any) -> None:
        # 写失败必须让调用方知道（否则用户以为切成功了，实际没落到 KV）
        await self._maybe_await(self._plugin.put_kv_data(key, value))

    async def _delete(self, key: str) -> None:
        await self._maybe_await(self._plugin.delete_kv_data(key))
