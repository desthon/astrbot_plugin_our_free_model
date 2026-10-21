"""把 OFM 网关作为 AstrBot 的**托管 provider** 自动接进来。

产品语义（README「它改了什么配置」一节是同一份说明）：

* 装插件 + 起 OFM standalone 就能聊天，不需要在 WebUI 手工粘贴 4 个字段；
* 只碰两个托管条目：``provider_sources[ofm-standalone]`` 与 ``provider[ofm-free]``；
* 只在全局默认模型为空时接管 ``agent_runner.config.model.provider_id``，
  用户已有的默认、或用户在 WebUI 里的显式选择，一律不覆盖。

本模块不 ``import astrbot``、不注册任何 handler（handler 全在 ``main.py``），只操作
调用方传进来的插件对象与它背后的 ``ProviderManager``；对外三个入口
（:func:`ensure` / :func:`teardown` / :func:`is_managed`）都保证**内部异常不冒泡**：

* ``ensure`` 失败 → ``ProvisionResult(ok=False, degraded_reason=中文原因)``
* ``teardown`` 失败 → ``False``（等价于「没有改动」）
* ``is_managed`` 失败 → ``False``

字段事实（AstrBot 4.29.1 核实）：

* 配置对象是 ``plugin.context.provider_manager.acm.default_conf``（``AstrBotConfig``，
  可写、有 ``save_config()`` / ``save_config_async()``）；
* provider 与 source 是两份配置，加载时 ``get_merged_provider_config()`` 按
  ``provider_source_id`` 合并，**provider 侧优先**；
* OpenAI 兼容适配器读合并后的 ``type`` / ``provider_type`` / ``api_base`` /
  ``key``(list) / ``timeout`` / ``model``；
* ``ProviderManager.reload(entry)`` 会先 terminate 再 load，用于让实例换掉旧 base_url。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import httpx

from .endpoints import normalize_base_url

__all__ = [
    "MANAGED_PROVIDER_ID",
    "MANAGED_SOURCE_ID",
    "DEFAULT_MODEL",
    "KV_DEFAULT_SET",
    "ProvisionResult",
    "ensure",
    "teardown",
    "is_managed",
    "inspect_state",
    "pick_model",
]

#: ``provider_sources`` 里的托管条目 id
MANAGED_SOURCE_ID = "ofm-standalone"
#: ``provider`` 里的托管条目 id
MANAGED_PROVIDER_ID = "ofm-free"
#: 托管 provider 的默认模型（复用插件配置 ``default_model``）
DEFAULT_MODEL = "combo:recommended-free"
#: 「默认 provider 是本插件设的」标记，落在插件 KV 里
KV_DEFAULT_SET = "provision_default_set"

#: 适配器类型（连接信息放 source，模型选择放 provider）
_ADAPTER_TYPE = "openai_chat_completion"
_PROVIDER_TYPE = "chat_completion"
_DEFAULT_TIMEOUT = 120

#: 接入前的可达性探测超时（秒）；目录请求沿用同一预算
_PROBE_TIMEOUT = 3.0

#: 目录接口（OpenAI 兼容）
_CATALOG_PATH = "/v1/models"
_MANAGEMENT_SETTINGS_PATH = "/api/management/settings"

#: 首选组合名：用户通常自建组合，默认目录里往往没有它，所以要校验而不是写死
RECOMMENDED_MODEL = DEFAULT_MODEL

#: 模型来源标签（``/ofm doctor`` 直接显示这几个词）
MODEL_SOURCE_CONFIG = "配置"
MODEL_SOURCE_COMBO = "组合"
MODEL_SOURCE_FREE = "免费"
MODEL_SOURCE_FIRST = "兜底"
MODEL_SOURCE_UNVERIFIED = "未校验"

#: 目录拉不到时给用户看的注记
NOTE_UNVERIFIED = "模型未校验（目录不可用）"

#: source 条目里**由本插件拥有**的字段；其余字段（proxy / custom_headers …）不动
_SOURCE_KEYS = ("id", "type", "provider_type", "api_base", "key", "timeout", "enable")
#: provider 条目里由本插件拥有的字段
_PROVIDER_KEYS = ("id", "provider_source_id", "type", "provider_type", "enable", "model")


@dataclass(frozen=True)
class ProvisionResult:
    """一次 ``ensure()`` 的结果。

    ``ok=False`` 时 ``degraded_reason`` 是能直接给用户看的中文原因；
    ``created_*`` / ``updated_*`` / ``default_set`` 只在本次真的改了东西时为 True，
    所以「连跑两次」的第二次应当全为 False。

    F1 新增的两个字段：

    * ``updated_model``：已有托管 provider 的 model 被改写（目录里没有它了）时为 True；
    * ``model_source`` / ``note``：选中的模型是怎么来的（配置 / 组合 / 免费 / 兜底 /
      未校验）与需要提示用户的一句话。
    """

    ok: bool
    provider_id: str
    source_id: str
    model: str
    created_source: bool
    created_provider: bool
    updated_source: bool
    default_set: bool
    degraded_reason: str = ""
    updated_model: bool = False
    model_source: str = ""
    note: str = ""


class _Unavailable(RuntimeError):
    """拿不到 ProviderManager / 配置对象（AstrBot 版本不兼容或不在宿主里）。"""


# ---------------------------------------------------------------------------
# 兼容层小工具
# ---------------------------------------------------------------------------
async def _maybe_await(value: Any) -> Any:
    """宿主/桩的 KV 与保存方法可能是 async 也可能是同步的——两种都吃。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


def _plugin_cfg(plugin: Any, name: str, default: Any) -> Any:
    cfg = getattr(plugin, "config", None)
    try:
        value = cfg.get(name, default) if hasattr(cfg, "get") else default
    except Exception:
        value = default
    return default if value is None else value


def _timeout_value(plugin: Any) -> int:
    try:
        value = int(_plugin_cfg(plugin, "request_timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def _model_name(plugin: Any, model: str | None) -> str:
    text = str(model or "").strip()
    if text:
        return text
    fallback = str(_plugin_cfg(plugin, "default_model", DEFAULT_MODEL) or "").strip()
    return fallback or DEFAULT_MODEL


def _manager(plugin: Any):
    context = getattr(plugin, "context", None)
    manager = getattr(context, "provider_manager", None)
    if manager is None or not hasattr(manager, "inst_map"):
        raise _Unavailable("拿不到 AstrBot 的 ProviderManager")
    return manager


def _conf(manager: Any) -> Any:
    acm = getattr(manager, "acm", None)
    conf = getattr(acm, "default_conf", None)
    if conf is None:
        confs = getattr(acm, "confs", None)
        conf = confs.get("default") if isinstance(confs, dict) else None
    if conf is None or not hasattr(conf, "get"):
        raise _Unavailable("拿不到 AstrBot 的默认配置对象")
    return conf


def _mapping_list(conf: Any, key: str) -> list:
    """取 ``conf[key]``；不是 list 时换成空 list（并让调用方保存）。"""
    value = conf.get(key)
    if not isinstance(value, list):
        value = []
        conf[key] = value
    return value


def _find_by_id(rows: Any, wanted: str) -> dict | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == wanted:
            return row
    return None


def _drifted(existing: dict, desired: dict, keys: tuple[str, ...]) -> bool:
    """只看本插件拥有的字段：用户改过的其他字段不触发改写。"""
    return any(existing.get(key) != desired.get(key) for key in keys)


def _inst_map(manager: Any) -> dict:
    value = getattr(manager, "inst_map", None)
    return value if isinstance(value, dict) else {}


def _sync_manager(manager: Any, conf: Any) -> None:
    """把 manager 缓存的 list 引用指回配置里的 list。

    ``ProviderManager.__init__`` 抓的是**同一个 list 对象**；一旦插件把
    ``conf["provider_sources"]`` 整个换成新 list（原本缺键时），manager 还握着旧
    引用，``get_merged_provider_config()`` 就找不到 source。WebUI 也是这么同步的。
    """
    for attr, key in (("providers_config", "provider"), ("provider_sources_config", "provider_sources")):
        value = conf.get(key)
        if isinstance(value, list):
            try:
                setattr(manager, attr, value)
            except Exception:
                pass


async def _save(conf: Any) -> None:
    saver_async = getattr(conf, "save_config_async", None)
    if callable(saver_async):
        await _maybe_await(saver_async())
        return
    saver = getattr(conf, "save_config", None)
    if callable(saver):
        await _maybe_await(saver())
        return
    raise _Unavailable("配置对象不支持保存（AstrBot 版本不兼容）")


async def _reload_provider(manager: Any, entry: dict) -> None:
    """让实例用新配置重建（主要目的是换掉旧的 base_url / key）。"""
    reload_fn = getattr(manager, "reload", None)
    if callable(reload_fn):
        await _maybe_await(reload_fn(dict(entry)))
        return
    terminate_fn = getattr(manager, "terminate_provider", None)
    if callable(terminate_fn):
        await _maybe_await(terminate_fn(entry.get("id")))
    await _maybe_await(manager.load_provider(dict(entry)))


# ---------------------------------------------------------------------------
# KV（默认值接管标记）
# ---------------------------------------------------------------------------
async def _kv_get(plugin: Any, key: str, default: Any = None) -> Any:
    reader = getattr(plugin, "get_kv_data", None)
    if not callable(reader):
        return default
    try:
        return await _maybe_await(reader(key, default))
    except Exception:
        return default


async def _kv_put(plugin: Any, key: str, value: Any) -> None:
    writer = getattr(plugin, "put_kv_data", None)
    if not callable(writer):
        return
    try:
        await _maybe_await(writer(key, value))
    except Exception:
        pass


async def _kv_delete(plugin: Any, key: str) -> None:
    deleter = getattr(plugin, "delete_kv_data", None)
    if not callable(deleter):
        return
    try:
        await _maybe_await(deleter(key))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 可达性：网关连不上时什么都不写
# ---------------------------------------------------------------------------
async def _gateway_reachable(client: httpx.AsyncClient, base_url: str) -> bool:
    """``GET /health`` 通不通（用调用方给的 client，不另起连接池）。

    配置一旦写下去，AstrBot 就会把对话路由到它；OFM 没起来时写配置等于让所有
    对话都撞死在死端口上（还会把用户默认模型指向一个坏 provider）。所以先探一次：
    不通就整次接入降级、一个字段都不改，留给后续自愈。
    """
    if not base_url:
        return False
    try:
        response = await client.get(f"{base_url}/health")
    except Exception:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is not False


# ---------------------------------------------------------------------------
# 模型目录：模型名必须真的在网关目录里，否则首条对话 404
# ---------------------------------------------------------------------------
async def enable_standalone_api(client: httpx.AsyncClient, base_url: str, api_key: str) -> bool:
    """把 OFM 的 ``enabled`` 打开（``/v1`` 的总闸，默认开，用户可能手动关掉）。

    ``settings.json`` 里的 ``forward.enabled`` 是 18899 那个独立转发进程的开关，
    与 standalone 的 ``/v1`` 无关——这两个字段曾被混为一谈，别再混了。
    """
    if not base_url or not api_key:
        return False
    try:
        response = await client.post(
            f"{base_url}{_MANAGEMENT_SETTINGS_PATH}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"enabled": True},
        )
    except Exception:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    settings = payload.get("settings") if isinstance(payload, dict) else None
    return isinstance(settings, dict) and settings.get("enabled") is True


async def _fetch_catalog(client: httpx.AsyncClient, base_url: str, api_key: str) -> list[str] | None:
    """``GET /v1/models`` 的 id 列表；拿不到（老网关 / 接口异常 / 空目录）返回 ``None``。"""
    if not base_url:
        return None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = await client.get(f"{base_url}{_CATALOG_PATH}", headers=headers)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    ids = [
        row["id"].strip()
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip()
    ]
    return ids or None


def _model_source(model: str, configured: str = "") -> str:
    """给一个模型名贴来源标签（与 :func:`pick_model` 的优先级一一对应）。"""
    if configured and model == configured:
        return MODEL_SOURCE_CONFIG
    if model.startswith("combo:"):
        return MODEL_SOURCE_COMBO
    if model.endswith("-free"):
        return MODEL_SOURCE_FREE
    return MODEL_SOURCE_FIRST


def model_source(model: str, configured: str = "") -> str:
    """公开包装：给 ``/ofm doctor`` 显示托管模型的来源标签。"""
    return _model_source(model, configured)


def pick_model(catalog: list[str], configured: str) -> tuple[str, str]:
    """按**固定优先级**从目录里选一个模型，返回 ``(model, 来源标签)``。

    顺序（确定性，不随机）：配置值 → ``combo:recommended-free`` → 第一个
    ``combo:*`` → 第一个 ``*-free`` → 第一个 id。
    """
    known = set(catalog)
    if configured and configured in known:
        return configured, MODEL_SOURCE_CONFIG
    if RECOMMENDED_MODEL in known:
        return RECOMMENDED_MODEL, MODEL_SOURCE_COMBO
    for model_id in catalog:
        if model_id.startswith("combo:"):
            return model_id, MODEL_SOURCE_COMBO
    for model_id in catalog:
        if model_id.endswith("-free"):
            return model_id, MODEL_SOURCE_FREE
    return catalog[0], MODEL_SOURCE_FIRST


def _resolve_model(
    configured: str,
    catalog: list[str] | None,
    existing: str = "",
) -> tuple[str, str, str]:
    """决定这次要写进 provider 的模型，返回 ``(model, 来源, 注记)``。

    * 已有托管 provider 的 model 仍在目录里 → 沿用（幂等：不改写）；
    * 否则按 :func:`pick_model` 的优先级重选；
    * 目录拉不到 → 用配置值并带「模型未校验」注记（``ok`` 仍为 True）。
    """
    if catalog is None:
        return configured, MODEL_SOURCE_UNVERIFIED, NOTE_UNVERIFIED
    if existing and existing in set(catalog):
        return existing, _model_source(existing, configured), ""
    model, source = pick_model(catalog, configured)
    return model, source, ""


# ---------------------------------------------------------------------------
# 期望的配置形状
# ---------------------------------------------------------------------------
def _source_config(endpoint: Any, timeout: int) -> dict:
    base_url = str(getattr(endpoint, "base_url", "") or "").rstrip("/")
    key = str(getattr(endpoint, "api_key", "") or "")
    return {
        "id": MANAGED_SOURCE_ID,
        "type": _ADAPTER_TYPE,
        "provider_type": _PROVIDER_TYPE,
        "api_base": f"{base_url}/v1",
        "key": [key] if key else [""],
        "timeout": timeout,
        "enable": True,
    }


def _provider_config(model: str) -> dict:
    return {
        "id": MANAGED_PROVIDER_ID,
        "provider_source_id": MANAGED_SOURCE_ID,
        "type": _ADAPTER_TYPE,
        "provider_type": _PROVIDER_TYPE,
        "enable": True,
        "model": model,
    }


# ---------------------------------------------------------------------------
# 默认 provider 接管
# ---------------------------------------------------------------------------
def _default_provider_id(conf: Any) -> str:
    agent_runner = conf.get("agent_runner")
    if not isinstance(agent_runner, dict):
        return ""
    config = agent_runner.get("config")
    if not isinstance(config, dict):
        return ""
    model_conf = config.get("model")
    if not isinstance(model_conf, dict):
        return ""
    value = model_conf.get("provider_id")
    return value.strip() if isinstance(value, str) else ""


def _runner_type(conf: Any) -> str:
    agent_runner = conf.get("agent_runner")
    if not isinstance(agent_runner, dict):
        return ""
    value = agent_runner.get("runner_type")
    return value if isinstance(value, str) else ""


def _take_over_default(conf: Any) -> bool:
    """``provider_id`` 为空时写成本插件的 provider；非空一律不动。"""
    if _default_provider_id(conf):
        return False
    agent_runner = conf.get("agent_runner")
    if not isinstance(agent_runner, dict):
        agent_runner = {}
        conf["agent_runner"] = agent_runner
    config = agent_runner.get("config")
    if not isinstance(config, dict):
        config = {}
        agent_runner["config"] = config
    model_conf = config.get("model")
    if not isinstance(model_conf, dict):
        model_conf = {}
        config["model"] = model_conf
    model_conf["provider_id"] = MANAGED_PROVIDER_ID
    return True


def _clear_default(conf: Any) -> bool:
    if _default_provider_id(conf) != MANAGED_PROVIDER_ID:
        return False
    agent_runner = conf.get("agent_runner")
    config = agent_runner.get("config") if isinstance(agent_runner, dict) else None
    model_conf = config.get("model") if isinstance(config, dict) else None
    if not isinstance(model_conf, dict):
        return False
    model_conf["provider_id"] = ""
    return True


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
async def ensure(
    plugin: Any,
    endpoint: Any,
    *,
    model: str | None = None,
    set_default: bool = True,
) -> ProvisionResult:
    """幂等接入：建/修两个托管条目、（可选）接管空默认，然后校验实例。

    * 条目已存在且字段一致 → 不改写、不保存，``created_*`` / ``updated_*`` 全 False；
    * 端点漂移（OFM 换了端口或 forwardKey）→ 就地更新 source 并 ``reload`` 实例；
    * 模型必须真的在网关目录里：先 ``GET /v1/models``，按固定优先级选一个，
      已有托管 model 仍在目录里则沿用；目录拉不到时用配置值并带注记；
    * ``agent_runner.config.model.provider_id`` 为空才写，非空一律不动；
    * 创建/更新后 ``inst_map`` 里拿不到实例 → ``ok=False`` + 中文 ``degraded_reason``。

    任何内部异常都被吃掉，降级成 ``ok=False``。
    """

    configured = _model_name(plugin, model)
    healed_enabled = False
    flags: dict[str, Any] = {
        "ok": False,
        "provider_id": MANAGED_PROVIDER_ID,
        "source_id": MANAGED_SOURCE_ID,
        "model": configured,
        "created_source": False,
        "created_provider": False,
        "updated_source": False,
        "default_set": False,
        "updated_model": False,
        "model_source": MODEL_SOURCE_UNVERIFIED,
        "note": "",
    }
    try:
        manager = _manager(plugin)
        conf = _conf(manager)

        base_url = normalize_base_url(str(getattr(endpoint, "base_url", "") or ""))
        api_key = str(getattr(endpoint, "api_key", "") or "")

        # 一个连接池走完 /health + /v1/models：接入不该为每次探测各建一个
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            if not await _gateway_reachable(client, base_url):
                shown = base_url or "（空地址）"
                flags["degraded_reason"] = (
                    f"连不上 OFM 网关（{shown}/health 没响应），先把 standalone 起起来；"
                    "本次没有改动任何配置，插件会在后续对话里自动重试"
                )
                return ProvisionResult(**flags)

            if not api_key.strip():
                # OpenAI 适配器拿不到 key 会在实例化时直接抛 Missing credentials；
                # 与其写一半配置再降级，不如一个字段都不写（OFM 的 key 是惰性铸造的，
                # 用户打开一次本地转发它就会出现，之后自愈会自动接上）。
                flags["degraded_reason"] = (
                    "OFM 还没铸出 forwardKey（settings.json 里没有），"
                    "先到 OFM → 设置 → 本地转发 打开并保存；"
                    "本次没有改动任何配置，key 出现后会自动接上"
                )
                return ProvisionResult(**flags)

            catalog = await _fetch_catalog(client, base_url, api_key)
            if catalog is None and await enable_standalone_api(client, base_url, api_key):
                # 用户把 OFM 的「独立服务」总开关关掉了：/v1 会 503，这里替它打开一次
                catalog = await _fetch_catalog(client, base_url, api_key)
                healed_enabled = True

        existing_entry = _find_by_id(conf.get("provider"), MANAGED_PROVIDER_ID)
        existing_model = str(existing_entry.get("model") or "") if isinstance(existing_entry, dict) else ""
        chosen, model_source, note = _resolve_model(configured, catalog, existing_model)
        flags["model"] = chosen
        flags["model_source"] = model_source
        if healed_enabled:
            suffix = "已自动打开 OFM 的独立服务开关（settings.enabled）"
            note = f"{note}；{suffix}" if note else suffix
        flags["note"] = note

        desired_source = _source_config(endpoint, _timeout_value(plugin))
        desired_provider = _provider_config(chosen)

        sources = _mapping_list(conf, "provider_sources")
        providers = _mapping_list(conf, "provider")

        created_source = created_provider = updated_source = updated_model = False
        dirty = False

        source_entry = _find_by_id(sources, MANAGED_SOURCE_ID)
        if source_entry is None:
            source_entry = dict(desired_source)
            sources.append(source_entry)
            created_source = True
            dirty = True
        elif _drifted(source_entry, desired_source, _SOURCE_KEYS):
            source_entry.update(desired_source)
            updated_source = True
            dirty = True

        provider_entry = _find_by_id(providers, MANAGED_PROVIDER_ID)
        provider_drift = False
        if provider_entry is None:
            provider_entry = dict(desired_provider)
            providers.append(provider_entry)
            created_provider = True
            dirty = True
        elif _drifted(provider_entry, desired_provider, _PROVIDER_KEYS):
            # 例如目录里已经没有旧模型了：改写 model 并重建实例，否则首条对话 404。
            previous_model = str(provider_entry.get("model") or "")
            provider_entry.update(desired_provider)
            provider_drift = True
            updated_model = previous_model != chosen
            dirty = True

        _sync_manager(manager, conf)

        default_set = False
        if set_default:
            default_set = _take_over_default(conf)
            if default_set:
                dirty = True
            # provider_id 已经是我们的（上一次 ensure 设的）→ 标记继续成立，
            # 但不算「本次接管」，所以 default_set 仍是 False。
            if _default_provider_id(conf) == MANAGED_PROVIDER_ID:
                if await _kv_get(plugin, KV_DEFAULT_SET, None) is not True:
                    await _kv_put(plugin, KV_DEFAULT_SET, True)

        if dirty:
            await _save(conf)

        loaded = _inst_map(manager).get(MANAGED_PROVIDER_ID)
        if dirty or provider_drift or loaded is None:
            await _reload_provider(manager, provider_entry)
            loaded = _inst_map(manager).get(MANAGED_PROVIDER_ID)

        if dirty and default_set:
            # 让 load_provider 在下次重建时能按配置挑中我们（该属性在 __init__ 时缓存）
            try:
                manager.default_chat_provider_id = MANAGED_PROVIDER_ID
            except Exception:
                pass

        flags.update(
            created_source=created_source,
            created_provider=created_provider,
            updated_source=updated_source,
            default_set=default_set,
            updated_model=updated_model,
        )
        if loaded is None:
            flags["degraded_reason"] = "provider 实例未加载，检查 OFM 是否在跑，或看 AstrBot 日志里的适配器报错"
            return ProvisionResult(**flags)
        flags["ok"] = True
        return ProvisionResult(**flags)
    except _Unavailable as exc:
        flags["degraded_reason"] = f"{exc}（AstrBot 版本不兼容或不在宿主环境里）"
        return ProvisionResult(**flags)
    except Exception as exc:
        flags["degraded_reason"] = f"自动接入失败：{_short(exc)}"
        return ProvisionResult(**flags)


async def teardown(plugin: Any) -> bool:
    """删掉两个托管条目、停掉实例；默认值是本插件设的则清空。返回是否有改动。"""

    changed = False
    try:
        manager = _manager(plugin)
        conf = _conf(manager)

        sources = conf.get("provider_sources")
        if isinstance(sources, list):
            kept = [row for row in sources if not (isinstance(row, dict) and row.get("id") == MANAGED_SOURCE_ID)]
            if len(kept) != len(sources):
                conf["provider_sources"] = kept
                changed = True

        providers = conf.get("provider")
        if isinstance(providers, list):
            kept = [row for row in providers if not (isinstance(row, dict) and row.get("id") == MANAGED_PROVIDER_ID)]
            if len(kept) != len(providers):
                conf["provider"] = kept
                changed = True

        _sync_manager(manager, conf)

        if _inst_map(manager).get(MANAGED_PROVIDER_ID) is not None:
            terminate_fn = getattr(manager, "terminate_provider", None)
            if callable(terminate_fn):
                await _maybe_await(terminate_fn(MANAGED_PROVIDER_ID))
            changed = True

        marker = await _kv_get(plugin, KV_DEFAULT_SET, None)
        # 默认值是本插件设的（KV 标记）就清；标记丢了但默认值仍指着 ofm-free 也清——
        # provider 马上就要被删掉，留一个悬空默认只会让所有对话都找不到模型。
        if _clear_default(conf):
            changed = True
        if marker is not None:
            await _kv_delete(plugin, KV_DEFAULT_SET)

        if changed:
            await _save(conf)
        return changed
    except Exception:
        return False


def is_managed(plugin: Any) -> bool:
    """配置里是否已存在托管条目（拿不到配置时返回 False）。"""
    try:
        conf = _conf(_manager(plugin))
    except Exception:
        return False
    sources = conf.get("provider_sources")
    providers = conf.get("provider")
    return _find_by_id(sources, MANAGED_SOURCE_ID) is not None or _find_by_id(
        providers, MANAGED_PROVIDER_ID
    ) is not None


def inspect_state(plugin: Any) -> dict:
    """给 ``/ofm doctor`` 与 ``/ofm unprovision`` 用的只读快照（同步、永不抛）。"""

    state: dict[str, Any] = {
        "available": False,
        "reason": "",
        "source_present": False,
        "provider_present": False,
        "instance_loaded": False,
        "default_provider_id": "",
        "default_is_ours": False,
        "runner_type": "",
        "model": "",
        "api_base": "",
        "key_present": False,
    }
    try:
        manager = _manager(plugin)
        conf = _conf(manager)
    except Exception as exc:
        state["reason"] = _short(exc)
        return state
    state["available"] = True
    source_entry = _find_by_id(conf.get("provider_sources"), MANAGED_SOURCE_ID)
    provider_entry = _find_by_id(conf.get("provider"), MANAGED_PROVIDER_ID)
    state["source_present"] = source_entry is not None
    state["provider_present"] = provider_entry is not None
    state["instance_loaded"] = _inst_map(manager).get(MANAGED_PROVIDER_ID) is not None
    state["default_provider_id"] = _default_provider_id(conf)
    state["default_is_ours"] = state["default_provider_id"] == MANAGED_PROVIDER_ID
    state["runner_type"] = _runner_type(conf)
    if source_entry is not None:
        state["api_base"] = str(source_entry.get("api_base") or "")
        keys = source_entry.get("key")
        state["key_present"] = bool(keys) and any(isinstance(item, str) and item for item in keys)
    if provider_entry is not None:
        state["model"] = str(provider_entry.get("model") or "")
    return state
