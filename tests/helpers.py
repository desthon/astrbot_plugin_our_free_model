"""Black-box helpers: locate modules, build objects, call handlers, read text.

Everything here deliberately avoids naming private implementation details.  The
only assumptions are the *contract* signatures quoted in the task plus the
public protocol shapes; anything ambiguous is handled by an explicit probe (and
the probe's result is asserted, never silently tolerated).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any, Iterable

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / "stubs"

PACKAGE_ALIAS = "ofm_plugin_under_test"
CORE_MODULES = ("endpoints", "secrets", "ofm_client", "catalog", "session", "stats", "feed")


# ---------------------------------------------------------------------------
# sys.path / module loading
# ---------------------------------------------------------------------------
def ensure_import_paths() -> None:
    """Put the stub package first and the plugin root on ``sys.path``."""

    for entry in (str(STUBS_DIR), str(PLUGIN_DIR), str(TESTS_DIR)):
        while entry in sys.path:
            sys.path.remove(entry)
    sys.path.insert(0, str(STUBS_DIR))
    sys.path.insert(1, str(PLUGIN_DIR))
    if str(TESTS_DIR) not in sys.path:
        sys.path.append(str(TESTS_DIR))


ensure_import_paths()


def purge_plugin_modules() -> None:
    """Drop every cached module that belongs to the plugin under test.

    The implementation resolves ``$OFM_HOME``/data dirs at import time, so a
    test that changes the environment must force a fresh import.
    """

    for name in list(sys.modules):
        if name == "main" or name == "core" or name.startswith("core."):
            del sys.modules[name]
        elif name == PACKAGE_ALIAS or name.startswith(PACKAGE_ALIAS + "."):
            del sys.modules[name]


def _register_alias_package() -> None:
    """Make the plugin directory importable as a package (relative imports)."""

    if PACKAGE_ALIAS not in sys.modules:
        pkg = types.ModuleType(PACKAGE_ALIAS)
        pkg.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
        pkg.__package__ = PACKAGE_ALIAS
        sys.modules[PACKAGE_ALIAS] = pkg


def _alias_core_package() -> None:
    """Make ``core.*`` and ``<pkg>.core.*`` resolve to the same module objects.

    The plugin uses relative imports (``from .core import ...``), so loading it
    as ``<pkg>.main`` produces a *second* copy of ``core.session`` etc.  Without
    this aliasing, ``isinstance`` checks and monkeypatching in tests would hit a
    different class object than the plugin actually uses.
    """

    prefix = f"{PACKAGE_ALIAS}.core"
    package = sys.modules.get(prefix)
    if package is None:
        return
    sys.modules["core"] = package
    for name, module in list(sys.modules.items()):
        if name.startswith(prefix + "."):
            sys.modules["core." + name[len(prefix) + 1 :]] = module


def core_module(name: str):
    """Import ``core.<name>`` (or the package-relative equivalent)."""

    _register_alias_package()
    candidates = (f"core.{name}", f"{PACKAGE_ALIAS}.core.{name}")
    last: BaseException | None = None
    for candidate in candidates:
        try:
            module = importlib.import_module(candidate)
        except ModuleNotFoundError as exc:
            # Only treat "this module is absent" as a fallback trigger; an
            # ImportError *inside* the module must propagate untouched.
            if exc.name not in (candidate, f"core.{name}", f"{PACKAGE_ALIAS}.core.{name}") and not str(
                exc.name
            ).endswith(f"core.{name}"):
                raise
            last = exc
        except ImportError as exc:
            last = exc
        else:
            _alias_core_package()
            return sys.modules.get(f"core.{name}", module)
    assert last is not None
    raise last


def load_main():
    """Import the plugin entry module, honouring either import style."""

    purge_plugin_modules()
    try:
        return importlib.import_module("main")
    except ImportError:
        # ``main.py`` may use relative imports (``from .core import ...``); those
        # only resolve when it is imported as part of a package, so retry so.
        pass
    _register_alias_package()
    sys.modules.pop(f"{PACKAGE_ALIAS}.main", None)
    module = importlib.import_module(f"{PACKAGE_ALIAS}.main")
    _alias_core_package()
    return module


# ---------------------------------------------------------------------------
# endpoints / clients
# ---------------------------------------------------------------------------
def endpoint_class():
    return core_module("endpoints").Endpoint


def client_class():
    return core_module("ofm_client").OFMClient


def make_endpoint(base_url: str, api_key: str, data_dir: Path, mode: str = "standalone", health: Any = None):
    """Build an ``Endpoint`` using only contract field names."""

    return endpoint_class()(
        mode=mode,
        base_url=base_url,
        api_key=api_key,
        data_dir=Path(data_dir),
        health={"ok": True} if health is None else health,
    )


def _run(coro):
    return asyncio.run(coro)


def _probe_form(gateway, data_dir: Path, api_key: str) -> str:
    """Return the base_url spelling the client actually composes ``/v1`` onto."""

    Endpoint = endpoint_class()
    OFMClient = client_class()
    errors: list[str] = []
    for base in (gateway.base_url, gateway.openai_base_url):
        endpoint = Endpoint(
            mode="standalone",
            base_url=base,
            api_key=api_key,
            data_dir=Path(data_dir),
            health={"ok": True},
        )
        client = OFMClient(endpoint)
        try:
            _run(client.models())
            return base
        except Exception as exc:  # noqa: BLE001 - probing both spellings
            errors.append(f"{base} -> {type(exc).__name__}: {exc}")
        finally:
            try:
                _run(client.aclose())
            except Exception:  # noqa: BLE001
                pass
    raise AssertionError(
        "OFMClient 无法用任一 base_url 拼法打到假网关的 /v1/models，"
        f"两种拼法都失败: {' | '.join(errors)}"
    )


def working_base_url(gateway, data_dir: Path, api_key: str | None = None) -> str:
    """Discover the working base_url spelling while the gateway is healthy."""

    saved = gateway.mode
    gateway.mode = "ok"
    try:
        return _probe_form(gateway, data_dir, api_key or gateway.key)
    finally:
        gateway.mode = saved


class _ClientLease:
    """Context manager holding (client, endpoint) and closing the client."""

    def __init__(self, client, endpoint) -> None:
        self.client = client
        self.endpoint = endpoint

    def __enter__(self):
        return self.client, self.endpoint

    def __exit__(self, *exc_info: Any) -> None:
        try:
            _run(self.client.aclose())
        except Exception:  # noqa: BLE001 - closing must not mask assertions
            pass


def client_for(
    gateway,
    data_dir: Path,
    api_key: str | None = None,
    base_url: str | None = None,
    mode: str = "standalone",
    timeout: float = 10.0,
):
    """Build an ``OFMClient`` against the fake gateway (form auto-detected)."""

    api_key = api_key or gateway.key
    base = base_url or working_base_url(gateway, data_dir, api_key)
    endpoint = make_endpoint(base, api_key, data_dir, mode=mode)
    return _ClientLease(client_class()(endpoint, timeout=timeout), endpoint)


# ---------------------------------------------------------------------------
# settings / files
# ---------------------------------------------------------------------------
def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_settings(
    data_dir: Path,
    *,
    forward_key: str | None = "ofm-test-key-1234",
    standalone_port: int | None = None,
    forward_port: int | None = None,
    forward_enabled: bool = True,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write ``settings.json``.

    Both the top-level ``standalonePort`` and ``forward.port`` are populated
    because the documented schema has both places for a forward port; a correct
    implementation may read either one.
    """

    settings: dict[str, Any] = {}
    if forward_key is not None:
        settings["forwardKey"] = forward_key
    if standalone_port is not None:
        settings["standalonePort"] = standalone_port
    settings["forward"] = {
        "enabled": forward_enabled,
        "host": "127.0.0.1",
        "port": forward_port if forward_port is not None else standalone_port,
    }
    if extra:
        settings.update(extra)
    return write_json(Path(data_dir) / "settings.json", settings)


# ---------------------------------------------------------------------------
# plugin instance / handlers
# ---------------------------------------------------------------------------
class FakeRequest:
    """Stand-in for AstrBot's ProviderRequest (only ``model`` matters here)."""

    def __init__(self, model: str = "base-model", **kwargs: Any) -> None:
        self.model = model
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeRequest(model={self.model!r})"


def make_event(message_str: str = "", umo: str = "umo::test", admin: bool = True, **kwargs: Any):
    from astrbot.api.event import AstrMessageEvent

    return AstrMessageEvent(
        message_str=message_str,
        unified_msg_origin=umo,
        admin=admin,
        **kwargs,
    )


def instantiate_plugin(plugin_cls, context=None, config=None):
    """Instantiate the plugin through ``Star(context, config)``-shaped ctor."""

    from astrbot.api.star import AstrBotConfig, Context

    ctx = context if context is not None else Context()
    cfg = config if config is not None else AstrBotConfig()

    try:
        signature = inspect.signature(plugin_cls.__init__)
    except (TypeError, ValueError):  # pragma: no cover - C-level ctor
        signature = None

    if signature is not None:
        kwargs: dict[str, Any] = {}
        unsupported = False
        for param in signature.parameters.values():
            if param.name == "self" or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            lowered = param.name.lower()
            if "config" in lowered:
                kwargs[param.name] = cfg
            elif "context" in lowered:
                kwargs[param.name] = ctx
            elif param.default is inspect.Parameter.empty:
                unsupported = True
        if not unsupported:
            try:
                return plugin_cls(**kwargs)
            except TypeError:
                pass

    for args in ((ctx, cfg), (ctx,), ()):
        try:
            return plugin_cls(*args)
        except TypeError:
            continue
    raise AssertionError(f"无法实例化插件类 {plugin_cls!r}（context/config 形状不匹配）")


def handler_marks(func) -> list[tuple[str, tuple[Any, ...]]]:
    return list(getattr(func, "__astrbot_handlers__", []) or [])


def _bind(plugin_or_cls, func):
    """Bind a class-level handler to the real plugin instance."""

    if inspect.isclass(plugin_or_cls):
        return func
    owner = type(plugin_or_cls)
    if inspect.isfunction(func):
        return func.__get__(plugin_or_cls, owner)
    getter = getattr(func, "__get__", None)
    if getter is not None:
        return getter(plugin_or_cls, owner)
    return func


def find_command(plugin_or_cls, *names: str):
    """Find the handler registered for a command by its declared argument names.

    ``names`` are matched against any declared command argument, so both
    ``filter.command("status")`` and ``filter.command("ofm", "status")`` are
    found without assuming a method name.
    """

    wanted = {n.lower() for n in names}
    holders: list[Any] = [vars(type(plugin_or_cls))]
    if not inspect.isclass(plugin_or_cls):
        holders.append(vars(plugin_or_cls))

    seen: set[int] = set()
    for klass in type(plugin_or_cls).__mro__ if not inspect.isclass(plugin_or_cls) else plugin_or_cls.__mro__:
        holders.append(vars(klass))

    for holder in holders:
        for name, attr in holder.items():
            if not callable(attr) or id(attr) in seen:
                continue
            seen.add(id(attr))
            for kind, args in handler_marks(attr):
                if kind != "command":
                    continue
                declared = {str(a).lower() for a in args}
                if not declared or declared & wanted or str(name).lower() in wanted:
                    return _bind(plugin_or_cls, attr)
    raise AssertionError(
        f"未找到命令处理器 {names}；已注册的标记: "
        + repr(
            [
                (name, handler_marks(attr))
                for holder in holders
                for name, attr in holder.items()
                if callable(attr) and handler_marks(attr)
            ]
        )
    )


def find_hook(plugin_or_cls, kind: str = "on_llm_request"):
    """Find the first handler tagged as an ``on_llm_request`` hook."""

    holders: list[Any] = []
    if not inspect.isclass(plugin_or_cls):
        holders.append(vars(plugin_or_cls))
    target_cls = plugin_or_cls if inspect.isclass(plugin_or_cls) else type(plugin_or_cls)
    for klass in target_cls.__mro__:
        holders.append(vars(klass))
    for holder in holders:
        for attr in holder.values():
            if callable(attr):
                for mark_kind, _args in handler_marks(attr):
                    if mark_kind == kind:
                        return _bind(plugin_or_cls, attr)
    raise AssertionError(f"未找到 {kind} 钩子（stub 的 filter.{kind} 会打标记）")


# ---------------------------------------------------------------------------
# session store discovery
# ---------------------------------------------------------------------------
def session_store_class():
    return core_module("session").SessionStore


def find_session_store(plugin):
    SessionStore = session_store_class()
    for holder in (vars(plugin), vars(type(plugin))):
        for value in holder.values():
            if isinstance(value, SessionStore):
                return value
    return None


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


STORE_ATTR_CANDIDATES = (
    "sessions",
    "_sessions",
    "session_store",
    "_session_store",
    "store",
    "_store",
    "session",
    "_session",
)


def ensure_hook_store(plugin, hook, umo: str = "umo::store-probe"):
    """Return the store instance the hook reads overrides from.

    Order of evidence:

    1. the store's own contract (``set_*`` → ``get`` round-trip) must work — if
       not, no hook can ever honour an override and *that* is the reported gap;
    2. prefer a ``SessionStore`` already held by the plugin;
    3. otherwise inject one under the conventional attribute names and keep the
       one the hook demonstrably reads.
    """

    SessionStore = session_store_class()
    probe_model = "probe-model-x"

    def make_store() -> Any:
        return SessionStore(plugin)

    async def roundtrip(store) -> dict:
        await store.set_model(umo + "::rt", probe_model)
        await store.set_effort(umo + "::rt", "high")
        try:
            return await store.get(umo + "::rt")
        finally:
            await store.clear(umo + "::rt")

    async def honours(store) -> bool:
        await store.set_model(umo, probe_model)
        try:
            req = FakeRequest(model="base-model")
            await _maybe_await(hook(make_event("", umo=umo), req))
            return str(req.model).startswith(probe_model)
        finally:
            await store.clear(umo)

    existing = find_session_store(plugin)
    canon = existing if existing is not None else make_store()
    state = _run(roundtrip(canon))
    state = state if isinstance(state, dict) else {}
    if not str(state.get("model") or "").startswith(probe_model):
        raise AssertionError(
            "SessionStore.get() 没有回读 set_model/set_effort 写入的状态："
            f"期望 model={probe_model!r} effort='high'，实际 {state!r}。"
            "这会让 on_llm_request 的会话覆盖永远不生效（契约 8/9 同时失败）。"
        )

    if existing is not None and _run(honours(existing)):
        return existing

    for attr in STORE_ATTR_CANDIDATES:
        store = make_store()
        try:
            setattr(plugin, attr, store)
        except (AttributeError, TypeError):
            continue
        if _run(honours(store)):
            return store

    raise AssertionError(
        "钩子没有从 SessionStore 读取会话覆盖（set_model 后 req.model 未变化，"
        f"而 store.get() 本身正常）；实例现有属性名={sorted(vars(plugin))}"
    )


# ---------------------------------------------------------------------------
# handler invocation → text
# ---------------------------------------------------------------------------
async def _drain_async(agen) -> list[Any]:
    items = []
    async for item in agen:
        items.append(item)
    return items


def _flatten(value: Any, out: list[Any]) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _flatten(item, out)
        return
    out.append(value)


def result_text(value: Any) -> str:
    """Best-effort text extraction from a handler return value."""

    chunks: list[str] = []
    flat: list[Any] = []
    _flatten(value, flat)
    for item in flat:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if isinstance(item, (bytes, bytearray)):
            chunks.append(bytes(item).decode("utf-8", "replace"))
            continue
        for attr in ("get_plain_text", "text", "message"):
            candidate = getattr(item, attr, None)
            if callable(candidate):
                try:
                    resolved = candidate()
                except TypeError:
                    continue
                if isinstance(resolved, str):
                    chunks.append(resolved)
                    break
            elif isinstance(candidate, str):
                chunks.append(candidate)
                break
        else:
            chunks.append(str(item))
    return "\n".join(chunk for chunk in chunks if chunk)


def call_handler_text(handler, event, *args: Any, **kwargs: Any) -> str:
    """Call a handler directly (sync / async / generator) and return its text.

    AstrBot handlers may either *return* a result or reply through
    ``event.send(...)``; both shapes are collected.
    """

    result = handler(event, *args, **kwargs)

    if inspect.isasyncgen(result):
        text = result_text(_run(_drain_async(result)))
        resolved = None
    elif inspect.isawaitable(result):
        resolved = _run(_maybe_await(result))
        if inspect.isasyncgen(resolved):
            text = result_text(_run(_drain_async(resolved)))
        elif inspect.isgenerator(resolved):
            text = result_text(list(resolved))
        else:
            text = result_text(resolved)
    elif inspect.isgenerator(result):
        text = result_text(list(result))
    else:
        text = result_text(result)

    sent = getattr(event, "sent", None) or []
    sent_text = result_text(sent) if sent else ""
    return "\n".join(chunk for chunk in (text, sent_text) if chunk)


async def call_hook(hook, event, req) -> Any:
    return await _maybe_await(hook(event, req))


def iter_handlers(plugin_or_cls) -> Iterable[tuple[str, Any]]:
    target_cls = plugin_or_cls if inspect.isclass(plugin_or_cls) else type(plugin_or_cls)
    for klass in target_cls.__mro__:
        for name, attr in vars(klass).items():
            if callable(attr) and handler_marks(attr):
                yield name, attr


# ---------------------------------------------------------------------------
# plugin class / command invocation helpers (appended)
# ---------------------------------------------------------------------------
def find_plugin_class(main_module):
    """Locate the ``Star`` subclass exported by ``main`` without naming it."""

    from astrbot.api.star import Star

    candidates = [
        value
        for value in vars(main_module).values()
        if isinstance(value, type) and issubclass(value, Star) and value is not Star
    ]
    if not candidates:
        raise AssertionError(
            "main.py 里没有找到 Star 子类（插件入口必须定义一个继承 astrbot.api.star.Star 的类）"
        )

    def score(cls) -> int:
        return sum(1 for value in vars(cls).values() if callable(value) and handler_marks(value))

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def first_command_handler(plugin_or_cls):
    for _name, attr in iter_handlers(plugin_or_cls):
        if any(kind == "command" for kind, _args in handler_marks(attr)):
            return _bind(plugin_or_cls, attr)
    raise AssertionError("插件没有注册任何 filter.command 处理器")


def invoke_command_text(plugin, name: str, umo: str = "umo::cmd", text: str | None = None) -> str:
    """Call ``/ofm <name>`` directly and return the reply text.

    Prefers the handler whose decorator declares that subcommand; if the plugin
    registers a single dispatcher for ``/ofm`` instead, falls back to it with the
    full command text in ``event.message_str`` (both layouts are legitimate).
    """

    event_text = text if text is not None else f"/ofm {name}"
    event = make_event(event_text, umo=umo)
    try:
        handler = find_command(plugin, name)
    except AssertionError:
        handler = first_command_handler(plugin)
    return call_handler_text(handler, event)


def free_port() -> int:
    """A TCP port that was just released (connection refused, not timeout)."""

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
