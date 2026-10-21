"""``core.secrets`` 的掩码契约：``safe_mask`` 必须真的遮蔽、且不炸短 key。"""

from __future__ import annotations

import pytest

import helpers
from conftest import DEFAULT_KEY


def _secrets():
    return helpers.core_module("secrets")


def test_safe_mask_hides_full_key():
    masked = _secrets().safe_mask(DEFAULT_KEY)

    assert isinstance(masked, str) and masked
    assert masked != DEFAULT_KEY
    assert DEFAULT_KEY not in masked, f"掩码结果仍包含明文 key: {masked!r}"
    assert len(masked) < len(DEFAULT_KEY) + 8


@pytest.mark.parametrize("key", ["", "a", "ab", "abc", "x" * 200])
def test_safe_mask_survives_degenerate_keys(key):
    masked = _secrets().safe_mask(key)
    assert isinstance(masked, str)
    if len(key) > 4:
        assert key not in masked


def test_safe_mask_is_deterministic():
    secrets = _secrets()
    first = secrets.safe_mask(DEFAULT_KEY)
    second = secrets.safe_mask(DEFAULT_KEY)
    assert first == second


def test_forward_key_reads_top_level_field(ofm_home):
    ofm_home.write_settings(forward_key="abc123def456", standalone_port=18900)
    assert _secrets().forward_key(ofm_home.path) == "abc123def456"


def test_forward_key_none_when_field_absent(ofm_home):
    ofm_home.write_settings(forward_key=None, standalone_port=18900)
    assert _secrets().forward_key(ofm_home.path) is None
