"""契约 14：自动拉起 OFM standalone（supervisor）。

判定的是「插件能不能自己把服务拉起来」这一层的**决策逻辑**：找仓库、查 node 版本、
已在跑就接管、不在跑才起进程、只停自己起的进程。真实的进程拉起在
``tests/integration/test_12_autostart.py`` 里用真 node 跑。
"""

from __future__ import annotations

import re

import pytest

import helpers


def _sup():
    return helpers.core_module("supervisor")


async def _never_up(base_url, timeout=1.5):
    """单元测试里的 /health 桩：一个端口都不通，也绝不发真请求。"""
    return None


# ---------------------------------------------------------------------------
# 仓库定位与 node 版本判定
# ---------------------------------------------------------------------------
def test_find_repo_requires_cli_entry(tmp_path, monkeypatch):
    sup = _sup()
    for env in ("OFM_REPO", "OFM_ROOT", "OUR_FREE_MODEL_REPO"):
        monkeypatch.delenv(env, raising=False)

    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    assert sup.find_repo(str(empty)) is None, "空目录不该被当成 OFM 仓库"

    repo = tmp_path / "ofm"
    (repo / "packages" / "standalone").mkdir(parents=True)
    (repo / "packages" / "standalone" / "cli.mjs").write_text("// cli\n", encoding="utf-8")
    assert sup.find_repo(str(repo)) == repo


def test_candidate_roots_prefers_explicit_and_env(tmp_path, monkeypatch):
    sup = _sup()
    monkeypatch.setenv("OFM_REPO", str(tmp_path / "from-env"))
    roots = sup.candidate_roots(str(tmp_path / "explicit"))
    assert roots[0] == tmp_path / "explicit", "显式配置必须排第一"
    assert tmp_path / "from-env" in roots, "环境变量 OFM_REPO 必须被考虑"


@pytest.mark.parametrize(
    "version,expected",
    [
        ((20, 18, 0), False),
        ((20, 19, 0), True),  # AstrBot 装机脚本给的就是这一档
        ((20, 19, 2), True),
        ((22, 18, 0), True),
        ((22, 19, 0), True),
        ((23, 9, 9), True),
        ((24, 0, 0), True),
        ((25, 3, 1), True),
        (None, False),
    ],
)
def test_version_ok_accepts_the_node_astrbot_ships(version, expected):
    """上游 engines 是 ``^22.19 || >=24``，但兼容补丁后 Node 20.19 起也能跑。"""

    assert _sup().version_ok(version) is expected


@pytest.mark.parametrize(
    "version,expected",
    [
        ((20, 19, 2), True),
        ((22, 9, 0), True),
        ((22, 10, 0), False),  # 22.10 起原生就有 markAsUncloneable
        ((24, 0, 0), False),
    ],
)
def test_needs_compat_patch_only_below_22_10(version, expected):
    assert _sup().needs_compat_patch(version) is expected


def test_start_refuses_old_node_without_patch(tmp_path, monkeypatch):
    """Node 20 且仓库没打补丁时，要在启动前给可执行的指令而不是让 JS 抛错。"""
    sup = _sup()
    repo = _repo(tmp_path)
    monkeypatch.setattr(sup, "which_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(sup, "probe_health", _never_up)

    async def fake_version(node):
        return (20, 19, 2), "v20.19.2"

    monkeypatch.setattr(sup, "node_version", fake_version)

    result = helpers._run(sup.start(repo, port=18900))

    assert result.ok is False
    assert "补丁" in (result.reason or ""), result.reason
    assert "ofm-node20-compat.patch" in (result.reason or "")
    assert result.started is False


def test_start_accepts_old_node_with_patch(tmp_path, monkeypatch):
    """补丁在位时 Node 20 不应被拦（真正的起进程交给下游，这里只验闸门）。"""
    sup = _sup()
    repo = _repo(tmp_path)
    bundle = repo / "packages" / "standalone" / "channels" / "business.mjs"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("// node20-compat applied\n", encoding="utf-8")
    monkeypatch.setattr(sup, "which_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(sup, "probe_health", _never_up)

    async def fake_version(node):
        return (20, 19, 2), "v20.19.2"

    async def fake_exec(*a, **kw):
        raise RuntimeError("不真的起进程")

    monkeypatch.setattr(sup, "node_version", fake_version)
    monkeypatch.setattr(sup.asyncio, "create_subprocess_exec", fake_exec)

    result = helpers._run(sup.start(repo, port=18900))

    assert "补丁" not in (result.reason or ""), result.reason


# ---------------------------------------------------------------------------
# start() 的降级分支
# ---------------------------------------------------------------------------
def _repo(tmp_path) -> "Path":
    """一个最小的「看起来像 OFM 仓库」的目录（有 cli.mjs）。"""
    repo = tmp_path / "ofm"
    (repo / "packages" / "standalone").mkdir(parents=True)
    (repo / "packages" / "standalone" / "cli.mjs").write_text("// cli\n", encoding="utf-8")
    return repo


def test_start_without_node_reports_actionable_reason(tmp_path, monkeypatch):
    sup = _sup()
    repo = tmp_path / "ofm"
    (repo / "packages" / "standalone").mkdir(parents=True)
    (repo / "packages" / "standalone" / "cli.mjs").write_text("// cli\n", encoding="utf-8")
    monkeypatch.setattr(sup, "which_node", lambda: "")
    monkeypatch.setattr(sup, "probe_health", _never_up)

    result = helpers._run(sup.start(repo, node=""))

    assert result.ok is False
    assert result.started is False
    assert "node" in result.reason, f"原因要提到 node：{result.reason!r}"


def test_start_with_broken_repo_reports_reason(tmp_path, monkeypatch):
    sup = _sup()
    repo = tmp_path / "ofm"
    repo.mkdir()

    async def good_version(_node):
        return (22, 19, 0), "v22.19.0"

    monkeypatch.setattr(sup, "node_version", good_version)
    monkeypatch.setattr(sup, "probe_health", _never_up)

    result = helpers._run(sup.start(repo, node="/usr/bin/node"))

    assert result.ok is False
    assert "cli.mjs" in result.reason or "不完整" in result.reason


def test_start_adopts_running_service_without_spawning(tmp_path, monkeypatch):
    sup = _sup()
    repo = tmp_path / "ofm"
    (repo / "packages" / "standalone").mkdir(parents=True)
    (repo / "packages" / "standalone" / "cli.mjs").write_text("// cli\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text('{"standalonePort": 18907}', encoding="utf-8")

    async def fake_health(base_url, timeout=1.5):
        return {"ok": True, "service": sup.SERVICE_NAME}

    async def boom(*args, **kwargs):
        raise AssertionError("服务已经通了，不该再拉起新进程")

    monkeypatch.setattr(sup, "probe_health", fake_health)
    monkeypatch.setattr(sup.asyncio, "create_subprocess_exec", boom)
    monkeypatch.setattr(sup, "which_node", lambda: "")

    result = helpers._run(sup.start(repo, port=18900, data_dir="", resolve_data_dir=str(tmp_path)))

    assert result.ok is True and result.adopted is True, result
    assert result.started is False
    assert result.port == 18907, "应当采纳 settings.json 里记录的实际端口"


def test_stop_returns_false_for_dead_pid():
    assert helpers._run(_sup().stop(999_999)) is False


# ---------------------------------------------------------------------------
# 插件侧：开关与服务记录
# ---------------------------------------------------------------------------
def test_auto_start_off_skips_spawn(tmp_path):
    main = helpers.load_main()
    plugin = helpers.instantiate_plugin(
        helpers.find_plugin_class(main), config={"auto_start": False, "auto_provision": True}
    )

    result = helpers._run(plugin._ensure_service())

    assert result is not None and result.ok is False
    assert "auto_start" in result.reason


def test_stop_command_does_not_touch_foreign_service(ofm_home):
    plugin = helpers.instantiate_plugin(
        helpers.find_plugin_class(helpers.load_main()), config={"auto_start": True}
    )

    text = helpers.invoke_command_text(plugin, "stop")

    assert "不会" in text or "不属于自己" in text, text


def test_doctor_shows_local_service_block(ofm_home, monkeypatch):
    """doctor 必须能回答「为什么没起来」：node 版本、仓库路径、是否插件拉起。"""

    plugin = helpers.instantiate_plugin(
        helpers.find_plugin_class(helpers.load_main()), config={"auto_start": True, "ofm_root": ""}
    )

    text = helpers.invoke_command_text(plugin, "doctor")

    assert "本地服务" in text, f"doctor 缺「本地服务」块：{text}"
    assert "auto_start" in text, text
    assert "OFM 仓库" in text, text
    # node 那行必须给出可判断的信息：有版本号，或明确「没找到 node」
    assert re.search(r"v\d+\.\d+\.\d+", text) or "没找到 node" in text, (
        f"doctor 的 node 项既没有版本号也没说没找到：{text}"
    )


# ---------------------------------------------------------------------------
# /v1 总闸（settings.enabled）自愈
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.exc is not None:
            raise self.exc
        return self.response


def testenable_standalone_api_posts_settings_patch():
    sup = _sup()
    provision = helpers.core_module("provision")
    client = _FakeClient(_FakeResponse(200, {"settings": {"enabled": True}}))

    ok = helpers._run(
        provision.enable_standalone_api(client, "http://127.0.0.1:18900", "ofm-test")
    )

    assert ok is True
    assert client.calls, "应当向 /api/management/settings 发一次 POST"
    call = client.calls[0]
    assert call["url"].endswith("/api/management/settings"), call["url"]
    assert call["json"] == {"enabled": True}
    assert call["headers"]["Authorization"] == "Bearer ofm-test"


@pytest.mark.parametrize(
    "client",
    [
        _FakeClient(_FakeResponse(503, {"error": "nope"})),
        _FakeClient(_FakeResponse(200, None)),
        _FakeClient(exc=RuntimeError("boom")),
    ],
)
def testenable_standalone_api_stays_quiet_on_failure(client):
    provision = helpers.core_module("provision")
    assert helpers._run(
        provision.enable_standalone_api(client, "http://127.0.0.1:18900", "ofm-test")
    ) is False


def testenable_standalone_api_needs_key():
    provision = helpers.core_module("provision")
    client = _FakeClient(_FakeResponse(200, {"settings": {"enabled": True}}))
    assert helpers._run(provision.enable_standalone_api(client, "http://x", "")) is False
    assert client.calls == [], "没有 key 时不该发请求（会 401）"
