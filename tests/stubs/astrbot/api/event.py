"""``astrbot.api.event`` stub: event object plus the ``filter`` namespace.

The ``filter`` decorators are pass-through (they return the function unchanged)
exactly as the task requires, but each one also *tags* the function with
``__astrbot_handlers__``.  That tagging is the only hook the black-box tests
need: it lets us find "the handler for ``/ofm status``" without assuming a
method name, which keeps the tests independent of the implementation's layout.
"""

from __future__ import annotations

import enum
from typing import Any, Callable

HandlerMark = tuple[str, tuple[Any, ...]]


def _tag(func: Callable, kind: str, args: tuple[Any, ...]) -> Callable:
    marks: list[HandlerMark] = list(getattr(func, "__astrbot_handlers__", []))
    marks.append((kind, args))
    try:
        setattr(func, "__astrbot_handlers__", marks)
    except (AttributeError, TypeError):  # pragma: no cover - exotic callables
        pass
    return func


def _factory(kind: str):
    """Build a decorator usable both bare and with arguments."""

    def decorator(*args: Any, **kwargs: Any):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return _tag(args[0], kind, ())
        return lambda func: _tag(func, kind, args)

    decorator.__name__ = kind
    return decorator


class CommandGroup:
    """Mirrors AstrBot's ``CommandGroup``.

    Real AstrBot's ``filter.command_group(name)`` does **not** hand back the bare
    function: it returns a ``CommandGroup`` object whose ``.command(sub)``
    decorator registers subcommands, which is the idiomatic plugin layout::

        @filter.command_group("ofm")
        def ofm(self): ...

        @ofm.command("status")
        async def status(self, event): ...

    The stub therefore returns this callable wrapper (still callable, so a plain
    ``@filter.command_group("x")`` handler keeps working) and tags both the group
    and every subcommand so tests can find handlers without knowing method names.
    """

    def __init__(self, name: str = "", func: Callable | None = None) -> None:
        self.name = name
        self.func = func
        self.commands: dict[str, Callable] = {}
        self.subgroups: dict[str, "CommandGroup"] = {}
        _tag(self, "command_group", (name,))

    # -- behave enough like the decorated function -----------------------
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.func is None:
            return None
        return self.func(*args, **kwargs)

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        if instance is None or self.func is None:
            return self
        return self.func.__get__(instance, owner)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CommandGroup({self.name!r}, subs={sorted(self.commands)})"

    # -- registration ----------------------------------------------------
    def command(self, name: Any = None, *args: Any, **kwargs: Any):
        if callable(name) and not args and not kwargs:  # bare @group.command
            func, name = name, None
            _tag(func, "command", ())
            _tag(func, "command_group_member", (self.name,))
            return func

        def decorator(func: Callable) -> Callable:
            _tag(func, "command", tuple([name]) if name else ())
            _tag(func, "command_group_member", (self.name,))
            if name:
                self.commands[str(name)] = func
            return func

        return decorator

    def group(self, name: str):
        def decorator(func: Callable) -> "CommandGroup":
            sub = CommandGroup(name, func)
            self.subgroups[name] = sub
            return sub

        return decorator


def _command_group_factory(name: Any = "", *args: Any, **kwargs: Any):
    if callable(name) and not args and not kwargs:  # bare @filter.command_group
        return CommandGroup("", name)
    return lambda func: CommandGroup(str(name), func)


class PermissionType(enum.Flag):
    """Mirrors upstream's Flag-based permission enum."""

    ADMIN = enum.auto()
    MEMBER = enum.auto()
    ALL = ADMIN | MEMBER


class MessageEventResult:
    """Minimal result object; tests read it back as text."""

    def __init__(self, text: str = "") -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text

    def get_plain_text(self) -> str:
        return self.text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MessageEventResult({self.text!r})"


class AstrMessageEvent:
    """Fake message event carrying everything the plugin may read."""

    def __init__(
        self,
        message_str: str = "",
        unified_msg_origin: str = "",
        sender_id: str = "10001",
        group_id: str | None = None,
        admin: bool = True,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.message_str = message_str
        self.unified_msg_origin = unified_msg_origin
        self._sender_id = sender_id
        self._group_id = group_id
        self._admin = admin
        self._extra: dict[str, Any] = dict(extra or {})
        self.sent: list[Any] = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    # -- results ----------------------------------------------------------
    async def send(self, message: Any) -> None:
        """Proactive send, mirroring AstrBot's ``event.send(...)``.

        Handlers that reply this way return ``None``; tests read ``event.sent``
        to recover the reply text.
        """

        self.sent.append(message)

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult(text)

    def make_result(self, text: str) -> MessageEventResult:
        return MessageEventResult(text)

    # -- accessors the plugin is likely to use ----------------------------
    def get_message_str(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str | None:
        return self._group_id

    def get_session_id(self) -> str:
        return self.unified_msg_origin

    def is_admin(self) -> bool:
        return self._admin

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return self._extra
        return self._extra.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def stop_event(self) -> None:
        self._extra["__stopped__"] = True

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.message_str


class _FilterNamespace:
    """``filter`` object: decorators + ``PermissionType``."""

    PermissionType = PermissionType
    command_group = staticmethod(_command_group_factory)
    command = staticmethod(_factory("command"))
    permission_type = staticmethod(_factory("permission_type"))
    on_llm_request = staticmethod(_factory("on_llm_request"))
    on_llm_response = staticmethod(_factory("on_llm_response"))
    event_message_type = staticmethod(_factory("event_message_type"))
    regex = staticmethod(_factory("regex"))
    llm_tool = staticmethod(_factory("llm_tool"))
    on_astrbot_loaded = staticmethod(_factory("on_astrbot_loaded"))
    platform_adapter_type = staticmethod(_factory("platform_adapter_type"))


filter = _FilterNamespace()

__all__ = ["AstrMessageEvent", "CommandGroup", "MessageEventResult", "PermissionType", "filter"]
