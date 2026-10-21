"""Harness for the *integration* suite: real AstrBot 4.29.1 only.

Design rules followed here:

* The plugin is imported at its **real** module path
  (``data.plugins.<name>.main``) under a real ``ASTRBOT_ROOT``, exactly the way
  ``StarManager`` imports it. The lightweight package in ``tests/stubs`` is
  evicted from ``sys.path``/``sys.modules`` for the duration of a test and put
  back afterwards, so the 84 existing tests keep running against their stubs.
* ``plugin`` / ``ProviderManager`` / ``ProviderOpenAIOfficial`` are the real
  objects. Nothing about ``core/provision.py`` is stubbed: when the contract is
  not implemented yet the test fails with the contract gap named out loud.
* Configuration is the *live* ``astrbot.core.astrbot_config`` object mutated in
  place. ``ProviderManager`` captures ``from astrbot.core import astrbot_config``
  at import time, so replacing the object would silently desynchronise it;
  mutating in place is what real AstrBot does too.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect as pyinspect
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

INTEGRATION_DIR = Path(__file__).resolve().parent
TESTS_DIR = INTEGRATION_DIR.parent
PLUGIN_DIR = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / "stubs"

REAL_ASTRBOT_DIR = Path("/tmp/ab")
DATA_ROOT = Path("/tmp/abtest")
PLUGIN_DIRNAME = PLUGIN_DIR.name
PACKAGE = f"data.plugins.{PLUGIN_DIRNAME}"
MODULE_PATH = f"{PACKAGE}.main"

#: Contract constants (re-asserted against ``core.provision`` in the tests).
MANAGED_SOURCE_ID = "ofm-standalone"
MANAGED_PROVIDER_ID = "ofm-free"

#: Subcommands that already existed before the auto-provision contract.
BASELINE_SUBCOMMANDS = {
    "ofm status",
    "ofm models",
    "ofm use",
    "ofm effort",
    "ofm reset",
    "ofm doctor",
    "ofm usage",
    "ofm setup",
    "ofm announce",
}
#: Subcommands the contract adds.
NEW_SUBCOMMANDS = {"ofm provision", "ofm unprovision"}


# ---------------------------------------------------------------------------
# real-astrbot <-> stub switching
# ---------------------------------------------------------------------------
_REAL_CACHE: dict[str, ModuleType] = {}


def _cached_astrbot_modules() -> dict[str, ModuleType]:
    return {
        name: module
        for name, module in list(sys.modules.items())
        if name == "astrbot" or name.startswith("astrbot.")
    }


def _drop_path(entry: str) -> None:
    while entry in sys.path:
        sys.path.remove(entry)


def enter_real_astrbot() -> dict[str, ModuleType]:
    """Swap the lightweight stub package out for the real AstrBot 4.29.1.

    Returns the evicted modules so the caller can restore them afterwards.
    """

    evicted = _cached_astrbot_modules()
    for name in evicted:
        del sys.modules[name]

    for entry in (str(STUBS_DIR), str(DATA_ROOT), str(REAL_ASTRBOT_DIR)):
        _drop_path(entry)
    sys.path.insert(0, str(DATA_ROOT))
    sys.path.insert(0, str(REAL_ASTRBOT_DIR))

    for name, module in _REAL_CACHE.items():
        sys.modules[name] = module

    import astrbot  # noqa: PLC0415 - deliberately late, path is set above

    loaded = Path(astrbot.__file__).resolve()
    assert loaded.is_relative_to(REAL_ASTRBOT_DIR), (
        f"integration suite must run against the real AstrBot at {REAL_ASTRBOT_DIR}, "
        f"but `astrbot` resolved to {loaded}. sys.path[:3]={sys.path[:3]}"
    )
    return evicted


def exit_real_astrbot(evicted: dict[str, ModuleType]) -> None:
    """Park the real modules in the cache and hand ``sys.path`` back."""

    purge_plugin_modules()
    for name in list(_cached_astrbot_modules()):
        _REAL_CACHE[name] = sys.modules.pop(name)
    sys.modules.update(evicted)
    for entry in (str(DATA_ROOT), str(REAL_ASTRBOT_DIR)):
        _drop_path(entry)
    sys.path.insert(0, str(STUBS_DIR))


def ensure_plugin_symlink() -> Path:
    """``<ASTRBOT_ROOT>/data/plugins/<name>`` -> the working tree under test."""

    plugins_dir = DATA_ROOT / "data" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link = plugins_dir / PLUGIN_DIRNAME
    if link.is_symlink():
        if link.resolve() != PLUGIN_DIR.resolve():
            link.unlink()
            link.symlink_to(PLUGIN_DIR.resolve(), target_is_directory=True)
    elif link.exists():
        raise AssertionError(f"{link} exists and is not a symlink")
    else:
        link.symlink_to(PLUGIN_DIR.resolve(), target_is_directory=True)
    return link


# ---------------------------------------------------------------------------
# plugin module import
# ---------------------------------------------------------------------------
def purge_plugin_modules() -> None:
    """Forget the plugin (and its ``core`` package) plus its registered handlers."""

    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            del sys.modules[name]
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:  # pragma: no cover - stub astrbot during teardown
        return
    for handler in list(star_handlers_registry.get_handlers_by_module_name(MODULE_PATH)):
        star_handlers_registry.remove(handler)


def import_plugin_main() -> ModuleType:
    """Import ``data.plugins.<name>.main`` the way StarManager does."""

    purge_plugin_modules()
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(MODULE_PATH)
    except ModuleNotFoundError as exc:  # pragma: no cover - environment problem
        raise AssertionError(
            f"无法按真实模块路径导入插件：{MODULE_PATH}（ASTRBOT_ROOT={DATA_ROOT}）。"
            f"请确认 data/plugins/{PLUGIN_DIRNAME} 软链存在。原始错误：{exc}"
        ) from exc
    return module


def import_provision() -> ModuleType:
    """Import ``core/provision.py`` — the contract under test."""

    import_plugin_main()
    name = f"{PACKAGE}.core.provision"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"契约缺口：找不到模块 {name}。"
            "本套件按任务书契约调用 ensure/teardown/is_managed、"
            "MANAGED_SOURCE_ID / MANAGED_PROVIDER_ID 与 ProvisionResult，"
            "实现落地后本用例才会通过。"
            f"原始错误：{exc}"
        ) from exc


def star_class(module: ModuleType) -> type:
    """The plugin's ``Star`` subclass, discovered without reading the source."""

    from astrbot.api.star import Star

    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and value is not Star
        and issubclass(value, Star)
        and getattr(value, "__module__", "") == module.__name__
    ]
    assert candidates, f"{module.__name__} 中没有找到 astrbot.api.star.Star 子类"
    return candidates[0]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def base_config(provider_id: str = "") -> dict[str, Any]:
    """Minimal but real ``cmd_config.json`` shape that ProviderManager accepts."""

    return {
        "provider": [],
        "provider_sources": [],
        "provider_settings": {},
        "agent_runner": {
            "runner_type": "local",
            "config": {"model": {"provider_id": provider_id}},
        },
        "persona": [],
        "t2i_endpoint": "https://t2i.soulter.top/text2img",
        "platform_settings": {"unique_session": False},
        "wake_prefix": ["/"],
    }


def install_config(config: dict[str, Any] | None = None, *, provider_id: str = ""):
    """Reset the live ``astrbot.core.astrbot_config`` **in place**."""

    import astrbot.core as astrbot_core

    live = astrbot_core.astrbot_config
    live.clear()
    live.update(copy.deepcopy(config if config is not None else base_config(provider_id)))
    return live


def default_provider_id(config: dict[str, Any]) -> str:
    return (
        config.get("agent_runner", {}).get("config", {}).get("model", {}).get("provider_id", "")
    )


def config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Deep copy of the slices the contract is allowed to touch."""

    return {
        "provider_sources": copy.deepcopy(list(config.get("provider_sources", []))),
        "provider": copy.deepcopy(list(config.get("provider", []))),
        "default_provider_id": default_provider_id(config),
    }


# ---------------------------------------------------------------------------
# fake collaborators for the *real* ProviderManager
# ---------------------------------------------------------------------------
class FakeConfigManager:
    """Stands in for ``astrbot.core.astrbot_config_mgr.AstrBotConfigManager``."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.confs = {"default": config}
        self.default_conf = config

    def get_conf(self, umo: str | None = None):
        return self.confs["default"]


class FakePersonaManager:
    default_persona = None
    selected_default_persona_v3 = None
    persona_v3_config: list = []
    personas_v3: list = []

    def get_default_persona_v3(self, umo: str | None = None):
        return None


class FakeDatabase:
    """ProviderManager only stores the handle; nothing is queried at build time."""


class FakeContext:
    """Just enough of ``astrbot.core.star.context.Context`` for a plugin instance.

    Every entry below is a *real* AstrBot-facing attribute name, so an
    implementation may reach configuration through any of the documented
    routes (``get_config`` / ``astrbot_config_mgr`` / ``provider_manager``)
    and still land on the same live config object.
    """

    def __init__(self, config, provider_manager, acm, persona_manager) -> None:
        self._config = config
        self.provider_manager = provider_manager
        self.astrbot_config_mgr = acm
        self.persona_manager = persona_manager
        self.db = FakeDatabase()

    def get_config(self, umo: str | None = None):
        return self._config

    def get_db(self):
        return self.db


def make_provider_manager(config: dict[str, Any]):
    """Build the real ``ProviderManager`` over the live config."""

    import astrbot.api  # noqa: F401  - real AstrBot needs this first (circular-import guard)
    from astrbot.core.provider.manager import ProviderManager

    acm = FakeConfigManager(config)
    persona_manager = FakePersonaManager()
    manager = ProviderManager(acm, FakeDatabase(), persona_manager)
    return manager, acm, persona_manager


# ---------------------------------------------------------------------------
# plugin instance
# ---------------------------------------------------------------------------
def plugin_config_object(config_path: Path):
    """The plugin config object StarManager would pass in (schema defaults)."""

    from astrbot.core.config import AstrBotConfig
    from astrbot.core.star.star_manager import PluginManager

    config_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = PLUGIN_DIR / "_conf_schema.json"
    if schema_path.exists():
        schema = PluginManager._load_plugin_config_schema(str(schema_path))
        return AstrBotConfig(config_path=str(config_path), schema=schema)
    return AstrBotConfig(config_path=str(config_path), default_config={})


def plugin_metadata() -> dict[str, Any]:
    """``metadata.yaml`` (author/name/version) — the file StarManager reads."""

    import yaml

    path = PLUGIN_DIR / "metadata.yaml"
    if not path.exists():
        return {"name": PLUGIN_DIRNAME, "author": ""}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def plugin_id() -> str:
    meta = plugin_metadata()
    author = str(meta.get("author") or "").strip()
    name = str(meta.get("name") or PLUGIN_DIRNAME).strip()
    return f"{author}/{name}"


def reset_plugin_kv() -> str:
    """Clear this plugin's KV namespace, like a fresh install would.

    The plugin persists provision state with ``put_kv_data`` (scope ``plugin``,
    scope_id ``plugin_id``); a shared ``/tmp/abtest`` data dir would otherwise
    leak that state between tests.
    """

    from astrbot.core import sp

    pid = plugin_id()

    async def _clear():
        await sp.clear_async("plugin", pid)

    try:
        run(_clear(), timeout=30)
    except Exception:  # KV store problems must not mask the assertion under test
        pass
    return pid


def make_plugin(module: ModuleType, context, config_obj):
    """Instantiate the Star exactly as StarManager does, identity included."""

    cls = star_class(module)
    try:
        instance = cls(context=context, config=config_obj)
    except TypeError:
        instance = cls(context=context)

    meta = plugin_metadata()
    pid = plugin_id()
    # StarManager: setattr(cls, "plugin_id", ...) and setattr(star_cls, name/author/...)
    for target in (cls, instance):
        setattr(target, "plugin_id", pid)
        setattr(target, "name", str(meta.get("name") or PLUGIN_DIRNAME))
        setattr(target, "author", str(meta.get("author") or ""))
    return instance


# ---------------------------------------------------------------------------
# endpoints / OFM home
# ---------------------------------------------------------------------------
def write_settings(home: Path, *, key: str, port: int) -> Path:
    """``settings.json`` in the documented shape (top-level + ``forward`` block)."""

    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "forwardKey": key,
        "standalonePort": port,
        "forward": {"enabled": True, "host": "127.0.0.1", "port": port},
    }
    path = home / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_endpoint(gateway, data_dir: Path):
    """A real ``core.endpoints.Endpoint`` pointing at the fake gateway."""

    import_plugin_main()
    endpoints = importlib.import_module(f"{PACKAGE}.core.endpoints")
    return endpoints.Endpoint(
        mode="standalone",
        base_url=gateway.base_url,
        api_key=gateway.key,
        data_dir=Path(data_dir),
    )


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
def run(coro, timeout: float = 90.0):
    """Single event loop per test (locks created in one loop stay usable)."""

    async def _main():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_main())


def handler_names_for(module_name: str) -> list[tuple[Any, str]]:
    from astrbot.core.star.star_handler import star_handlers_registry

    return [
        (handler.event_type, handler.handler_name)
        for handler in star_handlers_registry.get_handlers_by_module_name(module_name)
    ]


def command_names_for(module_name: str) -> set[str]:
    """Flatten every ``CommandFilter``/``CommandGroupFilter`` complete name."""

    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    names: set[str] = set()
    for handler in star_handlers_registry.get_handlers_by_module_name(module_name):
        for event_filter in handler.event_filters:
            if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
                names.update(event_filter.get_complete_command_names())
    return names


# ---------------------------------------------------------------------------
# F1/F2 probes: model selection + drift revalidation
# ---------------------------------------------------------------------------
def require_updated_model(result) -> bool:
    """``ProvisionResult.updated_model`` (contract addition), loudly if absent."""

    if not hasattr(result, "updated_model"):
        fields = [name for name in getattr(result, "__dataclass_fields__", {})]
        raise AssertionError(
            "契约缺口：ProvisionResult 缺 updated_model 字段。"
            f"当前字段：{fields}。F1 要求「已有 provider 的 model 从目录消失 → 改写 + updated_model=True」。"
        )
    return bool(result.updated_model)


def drive_plugin_provision(plugin):
    """Await the plugin's own provisioning entry point (the one the hook uses)."""

    entry = getattr(plugin, "_ensure_provisioned", None)
    assert callable(entry), (
        "契约缺口：插件实例上没有 _ensure_provisioned()，无法从插件侧驱动一次真实接入"
    )
    return run(entry(), timeout=180)


def fire_maybe_schedule(plugin, settle: float = 1.0) -> dict:
    """Call ``_maybe_schedule_provision()`` and wait for whatever it scheduled.

    ``_maybe_schedule_provision()`` is a scheduler, not the work itself, so the
    test has to drain the task it spawns. Returns diagnostics instead of raising,
    so the assertions (not the plumbing) decide pass/fail.
    """

    async def _main() -> dict:
        note: dict[str, Any] = {"called": False, "task_seen": False, "task_error": None}
        entry = getattr(plugin, "_maybe_schedule_provision", None)
        assert callable(entry), (
            "契约缺口：插件实例上没有 _maybe_schedule_provision()，F2 无法触发复检"
        )
        note["called"] = True
        outcome = entry()
        if pyinspect.isawaitable(outcome):
            await asyncio.wait_for(outcome, timeout=180)

        task = getattr(plugin, "_provision_task", None)
        if isinstance(task, asyncio.Future):
            note["task_seen"] = True
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=180)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    note["task_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(settle)
        return note

    return run(_main(), timeout=240)


def provision_last_payload(plugin_id_value: str | None = None):
    """The ``provision_last`` KV payload (F2 records base_url/model/checked_at)."""

    from astrbot.core import sp

    pid = plugin_id_value or plugin_id()
    return run(sp.get_async("plugin", pid, "provision_last", None), timeout=30)


def _shift_timestamp(value: Any, seconds: float) -> Any:
    """Move a timestamp back by ``seconds`` (epoch number or ISO string)."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value - seconds
    if isinstance(value, str):
        from datetime import datetime, timedelta

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (parsed - timedelta(seconds=seconds)).isoformat()
    return None


def rewind_provision_checked_at(plugin, seconds: float) -> dict:
    """Pretend the last check happened ``seconds`` ago — **without sleeping**.

    The KV entry is the persisted truth (the "process restarted" reading), so it
    is rewritten there; the in-memory mirror, when it exists, is moved too so the
    fixture stays self-consistent.
    """

    payload = provision_last_payload()
    assert isinstance(payload, dict) and payload, (
        f"契约缺口：KV provision_last 为空（实际 {payload!r}）。"
        "F2 要求它记录 base_url/model/checked_at，否则 300 秒节流无从判断。"
    )
    assert "checked_at" in payload, (
        "契约缺口：provision_last 里没有 checked_at。实际键："
        f"{sorted(payload)}（F2 要求 base_url / model / checked_at）"
    )
    shifted = _shift_timestamp(payload["checked_at"], seconds)
    assert shifted is not None, (
        f"provision_last['checked_at'] 既不是 epoch 数字也不是 ISO 时间串：{payload['checked_at']!r}"
    )
    payload["checked_at"] = shifted

    from astrbot.core import sp

    run(
        sp.put_async("plugin", plugin_id(), "provision_last", payload),
        timeout=30,
    )
    mirrored = getattr(plugin, "_provision_last", None)
    if isinstance(mirrored, dict):
        mirrored["checked_at"] = shifted
        mirrored.update({k: v for k, v in payload.items() if k not in mirrored})
    return payload


def plugin_endpoint_base_url(plugin) -> tuple[str, str]:
    """Where the plugin currently points its client.

    Returns ``(surface_name, base_url)`` so a failure message can say exactly
    which observation surface was used. ``OFMClient`` exposes ``.endpoint``
    rather than ``.base_url`` (runtime fact), hence the fallbacks.
    """

    async def _main() -> tuple[str, str]:
        for name in ("_endpoint_and_client", "_resolve_endpoint"):
            fn = getattr(plugin, name, None)
            if not callable(fn):
                continue
            try:
                value = fn()
                if pyinspect.isawaitable(value):
                    value = await asyncio.wait_for(value, timeout=120)
            except Exception:  # noqa: BLE001 - try the next surface
                continue
            if isinstance(value, tuple) and value:
                client, endpoint = (value[1], value[0]) if len(value) > 1 else (None, value[0])
                for label, candidate in (
                    (f"plugin.{name}()->client.base_url", getattr(client, "base_url", None)),
                    (
                        f"plugin.{name}()->client.endpoint.base_url",
                        getattr(getattr(client, "endpoint", None), "base_url", None),
                    ),
                    (f"plugin.{name}()->Endpoint.base_url", getattr(endpoint, "base_url", None)),
                ):
                    if candidate:
                        return label, str(candidate)
            else:
                candidate = getattr(value, "base_url", None)
                if candidate:
                    return f"plugin.{name}()->Endpoint.base_url", str(candidate)
        cached = getattr(plugin, "_endpoint", None)
        if getattr(cached, "base_url", None):
            return "plugin._endpoint.base_url", str(cached.base_url)
        return "未找到", ""

    return run(_main(), timeout=180)


def provision_annotation_surfaces(plugin, provision_module, result=None) -> dict[str, str]:
    """Every user-visible place the provision state can be read from.

    F1(b) requires the "未校验" note to be **observable**; which surface carries
    it is an implementation choice, so all documented ones are collected and the
    assertion searches the union (a missing note fails everywhere).
    """

    surfaces: dict[str, str] = {}

    inspect_state = getattr(provision_module, "inspect_state", None)
    if callable(inspect_state):

        async def _inspect():
            value = inspect_state(plugin)
            if pyinspect.isawaitable(value):
                value = await value
            return value

        try:
            surfaces["provision.inspect_state()"] = json.dumps(
                run(_inspect(), timeout=60), ensure_ascii=False, default=str
            )
        except Exception as exc:  # noqa: BLE001
            surfaces["provision.inspect_state()"] = f"<error {type(exc).__name__}: {exc}>"

    for name in ("_provision_detail", "_describe_provision"):
        fn = getattr(plugin, name, None)
        if not callable(fn):
            continue
        try:
            value = fn(result) if name == "_describe_provision" else fn()
            surfaces[f"plugin.{name}()"] = (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            )
        except Exception as exc:  # noqa: BLE001
            surfaces[f"plugin.{name}()"] = f"<error {type(exc).__name__}: {exc}>"

    if result is not None:
        surfaces["ProvisionResult"] = json.dumps(
            getattr(result, "__dict__", {}), ensure_ascii=False, default=str
        )

    try:
        surfaces["KV provision_last"] = json.dumps(
            provision_last_payload(), ensure_ascii=False, default=str
        )
    except Exception as exc:  # noqa: BLE001
        surfaces["KV provision_last"] = f"<error {type(exc).__name__}: {exc}>"

    return surfaces


MODEL_SOURCE_LABELS = ("配置", "组合", "免费", "兜底", "未校验")


def _labels_in_dict(value: Any, hits: list[tuple[str, str]], path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _labels_in_dict(item, hits, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _labels_in_dict(item, hits, f"{path}[{index}]")
    elif isinstance(value, str) and value in MODEL_SOURCE_LABELS:
        hits.append((path, value))


def doctor_model_source(plugin, provision_module, result=None) -> tuple[str | None, str]:
    """The model-source label as the **doctor block** can see it.

    F1 requires ``/ofm doctor`` to show the selected model **and its source**
    (配置 / 组合 / 免费 / 兜底 / 未校验). ``ProvisionResult`` alone does not count:
    doctor renders persisted/inspected state, so the label has to survive in
    ``inspect_state()``, the provision detail text, or the ``provision_last`` KV.

    Returns ``(label, evidence)``; ``label`` is ``None`` when no surface carries
    it, and ``evidence`` is a readable dump for the failure message.
    """

    import re

    surfaces = provision_annotation_surfaces(plugin, provision_module, result)
    # doctor 读的是持久化/可查看的状态，读不到内存里的这个 result 对象本身
    surfaces.pop("ProvisionResult", None)
    hits: list[tuple[str, str]] = []
    for name, text in surfaces.items():
        # dict-shaped dumps are JSON strings; reparse so keys keep their names
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            found: list[tuple[str, str]] = []
            _labels_in_dict(parsed, found, name)
            hits.extend(found)
        pattern = re.search(
            r"(?:来源|模型来源|来自)[：: ]*(" + "|".join(MODEL_SOURCE_LABELS) + ")",
            text,
        )
        if pattern:
            hits.append((f"{name}:来源标注", pattern.group(1)))

    evidence = "\n".join(f"[{name}] {text[:240]}" for name, text in surfaces.items())
    if result is not None:
        evidence += "\n[ProvisionResult] " + json.dumps(
            {k: v for k, v in getattr(result, "__dict__", {}).items() if k in ("model", "model_source", "note")},
            ensure_ascii=False,
        )
    for path, label in hits:
        return label, f"{path} = {label!r}\n{evidence}"
    return None, evidence
