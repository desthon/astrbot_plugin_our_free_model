"""Minimal stand-in for the real ``astrbot`` package.

Only what is required to ``import main`` and to call handlers directly is
implemented.  Nothing here is a behavioural mock of AstrBot's plugin runtime:
the point is a *black box* around the plugin under test, not a reimplementation
of the host.
"""

__version__ = "0.0.0-stub"
