"""``astrbot.api.star`` stub: ``Star`` base class with in-memory KV store."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")


class AstrBotConfig(dict):
    """Dict-like plugin config with attribute access, like the real one."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - attribute miss
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class Context:
    """Permissive host context: unknown attributes answer with a no-op.

    A plugin calling ``context.get_using_provider()`` (or anything else we do
    not model) must not explode during import/handler smoke tests.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = dict(kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


class StarMetadata:
    """Placeholder for the metadata object newer AstrBot versions attach."""

    def __init__(self, name: str = "", author: str = "", desc: str = "", version: str = "") -> None:
        self.name = name
        self.author = author
        self.desc = desc
        self.version = version


class Star:
    """Plugin base class.

    Contract surface used by the plugin under test:
    ``Star(context, config=None)`` plus the three KV methods, backed by a plain
    in-memory dict (no disk, no AstrBot database).
    """

    def __init__(self, context: Context | None = None, config: Any = None) -> None:
        self.context = context if context is not None else Context()
        self.config = config if config is not None else AstrBotConfig()
        self.name = type(self).__name__
        self._kv_store: dict[str, Any] = {}

    # -- KV store ---------------------------------------------------------
    def get_kv_data(self, key: str, default: Any = None) -> Any:
        return self._kv_store.get(key, default)

    def put_kv_data(self, key: str, value: Any = None) -> None:
        self._kv_store[key] = value

    def delete_kv_data(self, key: str) -> None:
        self._kv_store.pop(key, None)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} stub>"


def register(*args: Any, **kwargs: Any):
    """``@register(...)`` decorator: marks the class, does nothing else."""

    def _apply(cls):
        setattr(cls, "__astrbot_registered__", True)
        return cls

    if len(args) == 1 and callable(args[0]) and not kwargs:
        return _apply(args[0])
    return _apply
