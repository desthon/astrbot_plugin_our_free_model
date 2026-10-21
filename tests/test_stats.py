"""契约 10：用量三态 —— management API / stats.json 回退 / unavailable。"""

from __future__ import annotations

import asyncio

import pytest

import helpers
from conftest import DEFAULT_KEY

# 假 stats.json：samples 的求和与 aggregates 故意一致（400/80/4/2），
# 这样「读 aggregates」与「累加 samples」两种实现路径都能得到同一组数字。
STATS_FILE = {
    "version": 1,
    "days": {
        "2026-01-01": {"requests": 4, "failedRequests": 2, "input": 400, "output": 80, "tps": 20.0},
    },
    "models": {
        "deepseek-v4.1-flash-free": {"requests": 2, "failedRequests": 0, "input": 200, "output": 80, "tps": 20.0},
        "combo:recommended-free": {"requests": 2, "failedRequests": 2, "input": 200, "output": 0, "tps": None},
    },
    "requests": 4,
    "failedRequests": 2,
    "samples": [
        {"at": "2026-01-01T09:00:00Z", "model": "deepseek-v4.1-flash-free", "ok": True, "input": 100, "output": 40, "tps": 20.0},
        {"at": "2026-01-01T09:10:00Z", "model": "deepseek-v4.1-flash-free", "ok": True, "input": 100, "output": 40, "tps": 20.0},
        {"at": "2026-01-01T09:20:00Z", "model": "combo:recommended-free", "ok": False, "input": 100, "output": 0},
        {"at": "2026-01-01T09:30:00Z", "model": "combo:recommended-free", "ok": False, "input": 100, "output": 0},
    ],
    "since": "2026-01-01T00:00:00Z",
}


def _stats():
    return helpers.core_module("stats")


# ---------------------------------------------------------------------------
# management API
# ---------------------------------------------------------------------------
def test_usage_prefers_management_api(gateway, ofm_home):
    with helpers.client_for(gateway, ofm_home.path, DEFAULT_KEY) as (client, endpoint):
        usage = asyncio.run(_stats().usage(client, endpoint, ttl=0.0))

    assert usage.source == "management"
    assert usage.requests == 3
    assert usage.failed == 1
    assert usage.ok == 2
    assert usage.tokens_in == 300
    assert usage.tokens_out == 100
    assert usage.avg_tps is None or usage.avg_tps > 0


# ---------------------------------------------------------------------------
# stats.json fallback
# ---------------------------------------------------------------------------
def test_usage_falls_back_to_stats_file_on_management_401(gateway, ofm_home):
    ofm_home.write("stats.json", STATS_FILE)
    gateway.mode = "401"

    with helpers.client_for(gateway, ofm_home.path, DEFAULT_KEY) as (client, endpoint):
        usage = asyncio.run(_stats().usage(client, endpoint, ttl=0.0))

    assert usage.source == "stats_file"
    assert usage.requests == 4
    assert usage.ok == 2
    assert usage.failed == 2
    assert usage.tokens_in == 400
    assert usage.tokens_out == 80


def test_usage_from_stats_file_reads_aggregates(ofm_home):
    ofm_home.write("stats.json", STATS_FILE)

    usage = _stats().usage_from_stats_file(ofm_home.path)

    assert usage.source == "stats_file"
    assert usage.requests == 4
    assert usage.failed == 2
    assert usage.tokens_in == 400
    assert usage.tokens_out == 80


def test_usage_from_stats_file_tolerates_missing_file(tmp_path):
    usage = _stats().usage_from_stats_file(tmp_path / "no-such-dir")
    assert usage.source in ("stats_file", "unavailable")
    assert usage.requests == 0
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0


# ---------------------------------------------------------------------------
# neither source available
# ---------------------------------------------------------------------------
def test_usage_reports_unavailable_without_raising(tmp_path):
    closed_port = helpers.free_port()
    endpoint = helpers.make_endpoint(f"http://127.0.0.1:{closed_port}", DEFAULT_KEY, tmp_path)
    client = helpers.client_class()(endpoint, timeout=1.0)
    try:
        usage = asyncio.run(_stats().usage(client, endpoint, ttl=0.0))
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001
            pass

    assert usage.source == "unavailable"
    assert usage.requests == 0
    assert usage.ok == 0
    assert usage.failed == 0
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0


# ---------------------------------------------------------------------------
# last_failure
# ---------------------------------------------------------------------------
def test_last_failure_returns_latest_failed_sample(ofm_home):
    ofm_home.write("stats.json", STATS_FILE)

    failure = _stats().last_failure(ofm_home.path)

    assert isinstance(failure, dict)
    assert failure.get("ok") is False
    assert failure.get("model") == "combo:recommended-free"


def test_last_failure_is_none_when_everything_succeeded(tmp_path):
    helpers.write_json(
        tmp_path / "stats.json",
        {
            "version": 1,
            "requests": 1,
            "failedRequests": 0,
            "samples": [
                {"at": "2026-01-01T09:00:00Z", "model": "m", "ok": True, "input": 1, "output": 1},
            ],
        },
    )
    assert _stats().last_failure(tmp_path) is None
