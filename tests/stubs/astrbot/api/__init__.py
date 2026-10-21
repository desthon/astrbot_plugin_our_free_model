"""``astrbot.api`` stub surface.

The real package exports ``logger``, ``Star``, ``Context``, ``AstrBotConfig``
and the ``filter``/``event`` modules from here; plugins commonly use either
``from astrbot.api import ...`` or the deeper module paths, so both are wired.
"""

from .star import AstrBotConfig, Context, Star, StarMetadata, logger, register
from . import event, star  # noqa: F401  (exposed as attributes, mirrors upstream)
from .event import AstrMessageEvent, MessageEventResult, filter

__all__ = [
    "AstrBotConfig",
    "AstrMessageEvent",
    "Context",
    "MessageEventResult",
    "Star",
    "StarMetadata",
    "event",
    "filter",
    "logger",
    "register",
    "star",
]
