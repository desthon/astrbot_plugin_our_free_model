"""Our Free Model 的 AstrBot 接入插件（只支持同机 standalone 形态）。

产品边界（见 README）：

* 不注册 provider 适配器、不碰 ``ProviderManager``、不写 ``cmd_config.json``、
  不注册 Web API——``/ofm setup`` 只打印可手工粘贴的 4 个字段；
* 全部命令仅管理员可见，不提供 ``llm_tool``（否则非管理员可经 LLM 侧触发，属权限旁路）；
* 没有后台任务：公告只在 ``/ofm announce`` 时按需读取。

所有带装饰器的 handler 都在本文件——AstrBot 按模块名登记 handler，放进子模块会导致
插件重载时注销不干净；``core/*`` 只放纯逻辑。
"""

from __future__ import annotations

import functools
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import catalog
from .core import feed as feed_mod
from .core import secrets
from .core import stats as stats_mod
from .core.endpoints import Endpoint, EndpointNotFound, resolve
from .core.ofm_client import KIND_LABELS, OFMClient, OFMError
from .core.session import SessionStore

PLUGIN_NAME = "astrbot_plugin_our_free_model"

#: 单条消息上限（QQ 等平台对长文本不友好）
_CHUNK_LIMIT = 1500

#: 用来清空档位的关键词
_RESET_WORDS = {"clear", "off", "none", "reset", "默认", "清空", "取消"}

_MAX_ANNOUNCE = 10


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------
def _chunks(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """按行把长文本切成不超过 ``limit`` 的若干段。"""
    body = (text or "").strip()
    if not body:
        return ["（无内容）"]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in body.split("\n"):
        line_len = len(line) + 1
        if current and size + line_len > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        while len(line) > limit:  # 极端长的单行强制切断
            chunks.append(line[:limit])
            line = line[limit:]
            line_len = len(line) + 1
        current.append(line)
        size += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or ["（无内容）"]


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _fmt_time(value: object) -> str:
    stamp = feed_mod.parse_time(value)
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_use_args(args: list[str]) -> tuple[list[str], str | None, str | None]:
    """拆出 ``/ofm use`` 的位置参数与 ``--effort``；出错时返回中文说明。"""
    rest: list[str] = []
    effort: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--effort", "-e"):
            if index + 1 >= len(args):
                return [], None, "--effort 后面要跟一个强度值，例如：/ofm use combo:recommended-free --effort high"
            effort = args[index + 1]
            index += 2
            continue
        if token.startswith("--effort="):
            effort = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    if len(rest) > 1:
        return [], None, "只接受一个模型标识（完整 id 或 /ofm models 里的序号）。"
    if effort is not None and not effort.strip():
        return [], None, "--effort 的值不能为空。"
    return rest, effort, None


def _friendly(handler):
    """把端点/网关异常翻译成中文消息，任何情况下都不冒泡成栈。"""

    @functools.wraps(handler)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            return await handler(self, event, *args, **kwargs)
        except EndpointNotFound as exc:
            await self._send(event, exc.diagnostic)
        except OFMError as exc:
            await self._send(event, exc.human())
        except Exception as exc:  # 兜底：命令永远给一句话，不给栈
            log = getattr(self, "logger", logger)
            log.warning(f"[ofm] {handler.__name__} 执行失败：{exc}")
            await self._send(event, f"执行失败：{exc}\n可以试一次 /ofm doctor 看看卡在哪一步。")
        return None

    return wrapper


class OurFreeModel(Star):
    """插件入口：命令组 + 请求覆盖钩子。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        try:  # 不覆盖 AstrBot 可能自带的 logger 属性
            if not hasattr(self, "logger"):
                self.logger = logger
        except Exception:
            pass
        self.sessions = SessionStore(self)
        self._client: OFMClient | None = None
        self._endpoint: Endpoint | None = None
        self._endpoint_at: float = 0.0

    # ---- 生命周期 ---------------------------------------------------------
    async def terminate(self) -> None:
        """插件卸载 / 重载时关掉连接池。"""
        await self._drop_client()

    # ---- 内部：配置、端点、发送 -------------------------------------------
    def _cfg(self, name: str, default: Any) -> Any:
        cfg = getattr(self, "config", None)
        try:
            value = cfg.get(name, default) if hasattr(cfg, "get") else default
        except Exception:
            value = default
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return value

    def _models_ttl(self) -> float:
        return _as_float(self._cfg("models_ttl", 600), 600.0)

    def _stats_ttl(self) -> float:
        return _as_float(self._cfg("stats_ttl", 60), 60.0)

    def _page_limit(self) -> int:
        try:
            limit = int(self._cfg("models_page_limit", 30))
        except (TypeError, ValueError):
            limit = 30
        return max(limit, 1)

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        for chunk in _chunks(text):
            try:
                await event.send(event.plain_result(chunk))
            except Exception as exc:
                log = getattr(self, "logger", logger)
                log.warning(f"[ofm] 发送消息失败：{exc}")
                return

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._endpoint = None
        self._endpoint_at = 0.0
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _endpoint_and_client(self, force: bool = False) -> tuple[Endpoint, OFMClient]:
        """复用端点/客户端，最多 120 秒重新发现一次。"""
        now = time.monotonic()
        if (
            not force
            and self._client is not None
            and self._endpoint is not None
            and now - self._endpoint_at < 120
        ):
            return self._endpoint, self._client

        try:
            endpoint = await resolve(
                base_url=str(self._cfg("base_url", "") or ""),
                api_key=str(self._cfg("api_key", "") or ""),
                timeout=_as_float(self._cfg("diagnose_timeout", 3), 3.0),
            )
        except EndpointNotFound:
            # 端点没了：丢掉缓存的客户端，下一条命令会重新发现而不是拿到死连接
            await self._drop_client()
            raise
        previous = self._client
        timeout = _as_float(self._cfg("request_timeout", 120), 120.0)
        if (
            previous is None
            or previous.endpoint.base_url != endpoint.base_url
            or previous.endpoint.api_key != endpoint.api_key
        ):
            client = OFMClient(endpoint, timeout=timeout)
            if previous is not None:
                try:
                    await previous.aclose()
                except Exception:
                    pass
        else:
            client = previous
        self._endpoint = endpoint
        self._client = client
        self._endpoint_at = now
        return endpoint, client

    def _args(self, event: AstrMessageEvent, command: str) -> list[str]:
        """从原始消息里取命令后面的参数（兼容 AstrBot 是否剥离命令字）。"""
        text = getattr(event, "message_str", "") or ""
        tokens = text.split()
        while tokens:
            head = tokens[0].lstrip("/!#").strip().lower()
            if head in ("ofm", command.lower()):
                tokens.pop(0)
                continue
            break
        return tokens

    # ---- 命令组 -----------------------------------------------------------
    @filter.command_group("ofm")
    def ofm(self):
        """Our Free Model 运维命令（全部仅管理员）。"""

    # ---- /ofm status ------------------------------------------------------
    @ofm.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_status(self, event: AstrMessageEvent):
        # 强制重新探测：/health 只多一次请求，但状态页不能说假话
        endpoint, client = await self._endpoint_and_client(force=True)
        health = endpoint.health if isinstance(endpoint.health, dict) else {}
        mode_text = (
            "standalone（独立服务，管理面可用）"
            if endpoint.mode == "standalone"
            else "plugin（dsh 插件网关：只能聊天，管理面不可用）"
        )
        lines = [
            "Our Free Model 接入状态",
            f"模式：{mode_text}",
            f"地址：{endpoint.base_url}",
            f"密钥：{secrets.safe_mask(endpoint.api_key) or '未找到（settings.json 里没有 forwardKey）'}",
            f"数据目录：{endpoint.data_dir}",
        ]
        service = health.get("service")
        version = health.get("version")
        if service or version:
            lines.append(f"/health：✔ service={service or '?'} version={version or '?'}")
        else:
            lines.append("/health：✔ 可达（响应里没有 service/version 字段）")

        models = []
        try:
            models = await catalog.load(client, ttl=self._models_ttl())
        except OFMError as exc:
            lines.append(f"/v1/models：✘ {KIND_LABELS.get(exc.kind, exc.kind)}")
            lines.append(exc.human())
        if models:
            buckets = catalog.groups(models)
            counts = "、".join(
                f"{catalog.CHANNEL_LABELS[name]} {len(items)}"
                for name, items in buckets.items()
                if items
            )
            lines.append(f"模型：共 {len(models)} 个（{counts}；渠道为推断）")

        state = await self.sessions.get(event.unified_msg_origin)
        if state.get("model"):
            composed = SessionStore.compose_model(state.get("model"), state.get("effort"))
            lines.append(f"本会话覆盖：{composed}")
        else:
            lines.append("本会话覆盖：无（走 provider 的默认模型）")
        await self._send(event, "\n".join(lines))

    # ---- /ofm models ------------------------------------------------------
    @ofm.command("models")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_models(self, event: AstrMessageEvent):
        keyword = " ".join(self._args(event, "models")).strip()
        endpoint, client = await self._endpoint_and_client()
        models = await catalog.load(client, ttl=self._models_ttl())
        if not models:
            await self._send(
                event,
                f"/v1/models 没有返回任何模型（{endpoint.base_url}）。\n"
                "下一步：/ofm doctor 看鉴权与转发是否正常；目录为空通常是 forwardKey 不对或 OFM 还没拉到模型。",
            )
            return

        if keyword:
            selected = catalog.find(models, keyword)
            if not selected:
                await self._send(
                    event,
                    f"没有匹配「{keyword}」的模型。共 {len(models)} 个，用 /ofm models 看全部。",
                )
                return
        else:
            selected = models

        limit = self._page_limit()
        lines = [f"OFM 模型目录（匹配 {len(selected)} / 共 {len(models)}）"]
        shown: list[str] = []
        truncated = False
        for channel, items in catalog.groups(selected).items():
            if not items:
                continue
            lines.append(f"【{catalog.CHANNEL_LABELS[channel]}】{len(items)} 个")
            for model in items:
                if len(shown) >= limit:
                    truncated = True
                    break
                shown.append(model.id)
                lines.append(f"{len(shown):>2}. {catalog.describe(model)}")
            if truncated:
                break
        if shown:
            await self.sessions.set_last_list(event.unified_msg_origin, shown)
        if truncated:
            lines.append(f"只显示前 {limit} 条：用 /ofm models <关键词> 过滤（例如 /ofm models combo）。")
        lines.append("渠道是按模型 id 形态推断的，不代表服务端真实标记；能不能用仍以实际请求为准。")
        lines.append("用 /ofm use <序号|模型id> [--effort <level>] 切换。")
        await self._send(event, "\n".join(lines))

    # ---- /ofm use ---------------------------------------------------------
    @ofm.command("use")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_use(self, event: AstrMessageEvent):
        tokens, effort, error = _parse_use_args(self._args(event, "use"))
        if error:
            await self._send(event, error)
            return
        if not tokens:
            await self._send(
                event,
                "用法：/ofm use <模型id|序号> [--effort <level>]\n"
                "先 /ofm models 看列表，再 /ofm use 3 或 /ofm use deepseek-v4.1-flash-free。",
            )
            return

        umo = event.unified_msg_origin
        token = tokens[0]
        _, client = await self._endpoint_and_client()
        models = await catalog.load(client, ttl=self._models_ttl())

        model = None
        if token.isdigit():
            index = int(token)
            last = await self.sessions.get_last_list(umo)
            if not last:
                await self._send(event, "还没有序号列表：先发一次 /ofm models，再用 /ofm use <序号>。")
                return
            if not 1 <= index <= len(last):
                await self._send(
                    event, f"序号超出范围：当前列表只有 1–{len(last)}。先 /ofm models 重新看一遍。"
                )
                return
            model_id = last[index - 1]
            exact = catalog.find(models, model_id)
            model = exact[0] if exact else None
        else:
            matches = catalog.find(models, token)
            if not matches:
                await self._send(
                    event,
                    f"没找到「{token}」。用 /ofm models [关键词] 查一下，"
                    "或先 /ofm models 再 /ofm use <序号>。",
                )
                return
            if len(matches) > 1:
                lines = [f"「{token}」匹配到 {len(matches)} 个模型，请写完整 id："]
                for candidate in matches[:10]:
                    lines.append(f"· {candidate.id}")
                if len(matches) > 10:
                    lines.append(f"… 还有 {len(matches) - 10} 个，用更长的关键词过滤。")
                await self._send(event, "\n".join(lines))
                return
            model = matches[0]
            model_id = model.id

        if effort:
            if model is None:
                await self._send(
                    event,
                    f"目录里查不到 {model_id}，无法校验档位。去掉 --effort 重试，或先 /ofm models 刷新目录。",
                )
                return
            if not model.efforts:
                await self._send(
                    event,
                    f"{model.id} 没有声明思考强度档位（x_ofm_efforts 为空），去掉 --effort 重试。",
                )
                return
            if not catalog.supports_effort(model, effort):
                await self._send(
                    event,
                    f"{effort} 不是 {model.id} 的合法档位。\n"
                    f"可选：{'、'.join(model.efforts)}（默认 {model.effort_default or '未声明'}）。",
                )
                return

        previous = await self.sessions.get(umo)
        await self.sessions.set_model(umo, model_id)
        notes: list[str] = []
        if effort:
            await self.sessions.set_effort(umo, effort)
        else:
            old_effort = previous.get("effort")
            if old_effort and model is not None and model.efforts and not catalog.supports_effort(model, old_effort):
                await self.sessions.set_effort(umo, None)
                notes.append(f"已清除旧档位 {old_effort}（{model_id} 不支持）。")

        state = await self.sessions.get(umo)
        composed = SessionStore.compose_model(state.get("model"), state.get("effort"))
        lines = [f"已切换本会话模型：{composed}", "下一轮对话生效，只影响本会话（群聊与私聊互相隔离）。"]
        if model is not None and model.efforts:
            lines.append(f"可用档位：{'、'.join(model.efforts)}（默认 {model.effort_default or '未声明'}）")
        lines.extend(notes)
        await self._send(event, "\n".join(lines))

    # ---- /ofm effort ------------------------------------------------------
    @ofm.command("effort")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_effort(self, event: AstrMessageEvent):
        args = self._args(event, "effort")
        if len(args) > 1:
            await self._send(event, "用法：/ofm effort <level>（一次只改一个档位）。")
            return

        umo = event.unified_msg_origin
        state = await self.sessions.get(umo)
        _, client = await self._endpoint_and_client()
        models = await catalog.load(client, ttl=self._models_ttl())
        model_id = state.get("model")
        model = None
        if model_id:
            exact = catalog.find(models, model_id)
            model = exact[0] if exact else None

        if not args:
            lines = ["思考强度"]
            if not model_id:
                lines.append("当前会话没有模型覆盖：先 /ofm use <模型> 选模型，再改档位。")
            else:
                lines.append(f"当前模型：{model_id}")
                lines.append(f"当前档位：{state.get('effort') or '未设置（用模型默认档）'}")
                if model is None:
                    lines.append("目录里查不到这个模型（可能已下线），先 /ofm models 重新选。")
                elif model.efforts:
                    lines.append(
                        f"合法档位：{'、'.join(model.efforts)}（默认 {model.effort_default or '未声明'}）"
                    )
                else:
                    lines.append("该模型没有声明档位（x_ofm_efforts 为空）。")
            lines.append("清空档位：/ofm effort clear")
            await self._send(event, "\n".join(lines))
            return

        level = args[0].strip()
        if level.lower() in _RESET_WORDS:
            await self.sessions.set_effort(umo, None)
            await self._send(event, "已清空本会话的思考强度，回到模型默认档。")
            return

        if not model_id:
            await self._send(
                event,
                "当前会话没有模型覆盖：先 /ofm use <模型>，再用 /ofm effort <level>。\n"
                "（档位是跟着具体模型走的，没有模型就无法校验合法值。）",
            )
            return
        if model is None:
            await self._send(
                event, f"目录里查不到 {model_id}（可能已下线）：先 /ofm models 重新选模型，再设档位。"
            )
            return
        if not model.efforts:
            await self._send(
                event,
                f"{model.id} 没有声明思考强度档位（x_ofm_efforts 为空），设了也不会生效。\n"
                "换一个声明了档位的模型再试。",
            )
            return
        if not catalog.supports_effort(model, level):
            await self._send(
                event,
                f"{level} 不是 {model.id} 的合法档位（OFM 对非法档位是静默回落，不会报错）。\n"
                f"可选：{'、'.join(model.efforts)}（默认 {model.effort_default or '未声明'}）。",
            )
            return

        await self.sessions.set_effort(umo, level)
        composed = SessionStore.compose_model(model.id, level)
        await self._send(
            event,
            f"已设置本会话思考强度：{level}\n下一轮请求会以 {composed} 下发（尾缀形式，OFM 会 strip 后再查目录）。",
        )

    # ---- /ofm reset -------------------------------------------------------
    @ofm.command("reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_reset(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        state = await self.sessions.get(umo)
        await self.sessions.clear(umo)
        if state.get("model") or state.get("effort"):
            composed = SessionStore.compose_model(state.get("model"), state.get("effort"))
            await self._send(event, f"已清除本会话覆盖（原为 {composed}），回到 provider 默认模型。")
        else:
            await self._send(event, "本会话本来就没有覆盖，无需清除。")

    # ---- /ofm doctor ------------------------------------------------------
    @ofm.command("doctor")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_doctor(self, event: AstrMessageEvent):
        lines = ["Our Free Model 体检"]

        def mark(ok: bool, label: str, detail: str, action: str = "") -> str:
            head = "✔" if ok else "✘"
            text = f"{head} {label}：{detail}"
            if not ok and action:
                text += f"\n  → {action}"
            return text

        dirs = secrets.data_dirs()
        existing = [item for item in dirs if item.is_dir()]
        if existing:
            lines.append(mark(True, "数据目录", "、".join(str(item) for item in existing)))
        else:
            lines.append(
                mark(
                    False,
                    "数据目录",
                    "、".join(str(item) for item in dirs) + " 都不存在",
                    "确认 OFM 装在本机；数据目录不在默认位置时用环境变量 OFM_HOME 指向它。",
                )
            )

        settings_dir = None
        settings: dict = {}
        for candidate in dirs:
            data = secrets.read_settings(candidate)
            if data:
                settings_dir, settings = candidate, data
                break
        if settings_dir is not None:
            lines.append(mark(True, "settings.json", f"可读（{settings_dir / 'settings.json'}）"))
        else:
            lines.append(
                mark(
                    False,
                    "settings.json",
                    "读不到或不是合法 JSON",
                    "检查数据目录权限；或启动一次 OFM 让它生成配置。",
                )
            )

        key = secrets.forward_key(settings_dir) if settings_dir is not None else None
        if key:
            lines.append(mark(True, "forwardKey", f"存在（{secrets.safe_mask(key)}）"))
        else:
            lines.append(
                mark(
                    False,
                    "forwardKey",
                    "不存在（OFM 的 key 是惰性铸造的）",
                    "在 OFM 里打开一次本地转发并保存，它才会生成 forwardKey。",
                )
            )

        port = secrets.standalone_port(settings) if settings else secrets.DEFAULT_STANDALONE_PORT
        forward = secrets.forward_settings(settings) if settings else {}
        enabled = forward.get("enabled")
        lines.append(mark(True, "独立服务端口", f"{port}（settings.standalonePort）"))
        if enabled is True:
            lines.append(mark(True, "本地转发", f"已开启（forward.port={forward.get('port', port)}）"))
        elif enabled is None:
            lines.append(
                mark(
                    False,
                    "本地转发",
                    "settings.json 里没有 forward 段（默认关闭）",
                    "OFM → 设置 → 本地转发 打开并保存；否则非 /health 请求一律 503。",
                )
            )
        else:
            lines.append(
                mark(
                    False,
                    "本地转发",
                    "未开启（非 /health 请求会 503）",
                    "OFM → 设置 → 本地转发 打开并保存，再重试。",
                )
            )

        try:
            endpoint = await resolve(
                base_url=str(self._cfg("base_url", "") or ""),
                api_key=str(self._cfg("api_key", "") or ""),
                timeout=_as_float(self._cfg("diagnose_timeout", 3), 3.0),
            )
        except EndpointNotFound as exc:
            lines.append(mark(False, "/health", "探测不到", exc.diagnostic.replace("\n", " ")))
            lines.append("（/health 不通时，后面的网络项无法检查。）")
            await self._send(event, "\n".join(lines))
            return

        health = endpoint.health if isinstance(endpoint.health, dict) else {}
        lines.append(
            mark(
                True,
                "/health",
                f"{endpoint.base_url} 正常（service={health.get('service', '?')}，模式={endpoint.mode}）",
            )
        )

        client = OFMClient(endpoint, timeout=min(_as_float(self._cfg("request_timeout", 120), 120.0), 15.0))
        try:
            models_ok = False
            try:
                rows = await client.models()
                models_ok = True
                lines.append(mark(True, "/v1/models", f"可达，{len(rows)} 个模型"))
            except OFMError as exc:
                lines.append(
                    mark(
                        False,
                        "/v1/models",
                        KIND_LABELS.get(exc.kind, exc.kind),
                        exc.human().replace("\n", " "),
                    )
                )

            if models_ok:
                lines.append(
                    mark(
                        True,
                        "鉴权",
                        f"Bearer forwardKey 通过（{secrets.safe_mask(endpoint.api_key) or '空 key'}）",
                    )
                )
            elif not endpoint.api_key:
                lines.append(
                    mark(
                        False,
                        "鉴权",
                        "没有可用的 forwardKey",
                        "在 OFM 里重开一次本地转发；或把 key 粘到插件配置 api_key。",
                    )
                )
            else:
                lines.append(
                    mark(
                        False,
                        "鉴权",
                        f"可能失败（当前 {secrets.safe_mask(endpoint.api_key)}）",
                        "重开一次本地转发让 OFM 重新铸造 key，再更新插件配置。",
                    )
                )

            summary = await client.management_summary()
            stats_payload = await client.management_stats(limit=5)
            if summary is not None or stats_payload is not None:
                lines.append(mark(True, "管理 API", "summary / stats 可用"))
            else:
                last = client.last_error
                detail = KIND_LABELS.get(last.kind, last.kind) if last else "不可用"
                if endpoint.mode == "plugin":
                    action = (
                        "插件网关模式不暴露管理面（trust fence），这不影响聊天；"
                        "要统计请改用 standalone（18900），或看下面的 stats.json。"
                    )
                else:
                    action = (
                        "确认 base_url 指向 standalone 的端口；"
                        "/api/management/* 与 /v1 共用同一个 Bearer key。"
                    )
                lines.append(mark(False, "管理 API", detail, action))

            snapshot = stats_mod.usage_from_stats_file(endpoint.data_dir)
            if snapshot.source == "stats_file":
                lines.append(
                    mark(True, "stats.json", f"可读（记录 {_fmt_int(snapshot.requests)} 次请求）")
                )
            else:
                lines.append(
                    mark(
                        False,
                        "stats.json",
                        f"读不到（{endpoint.data_dir / 'stats.json'}）",
                        "文件不存在时 /ofm usage 只能走管理 API；确认数据目录对不对。",
                    )
                )
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

        lines.append("体检完成：✘ 项按箭头提示处理，修完重跑 /ofm doctor。")
        await self._send(event, "\n".join(lines))

    # ---- /ofm usage -------------------------------------------------------
    @ofm.command("usage")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_usage(self, event: AstrMessageEvent):
        endpoint, client = await self._endpoint_and_client()
        snapshot = await stats_mod.usage(client, endpoint, ttl=self._stats_ttl())
        labels = {
            "management": "管理 API（samples 最多 200 条）",
            "stats_file": "本地 stats.json（samples 最多 400 条）",
            "unavailable": "不可用",
        }
        lines = [f"OFM 用量（来源：{labels.get(snapshot.source, snapshot.source)}）"]
        if snapshot.source == "unavailable":
            lines.append("读不到统计：管理 API 不可用，stats.json 也读不到。")
            lines.append("下一步：/ofm doctor 看管理 API 与数据目录；插件网关模式下只有 stats.json 一条路。")
            await self._send(event, "\n".join(lines))
            return

        success_rate = (snapshot.ok / snapshot.requests * 100) if snapshot.requests else 0.0
        lines.append(f"请求：{_fmt_int(snapshot.requests)}（成功 {_fmt_int(snapshot.ok)} / 失败 {_fmt_int(snapshot.failed)}）")
        lines.append(f"成功率：{success_rate:.1f}%")
        lines.append(
            f"tokens：输入 {_fmt_tokens(snapshot.tokens_in)} / 输出 {_fmt_tokens(snapshot.tokens_out)}"
            "（按样本合计，不是全量）"
        )
        if snapshot.avg_tps is not None:
            lines.append(f"平均速度：{snapshot.avg_tps} tok/s（样本内有 tps 的请求）")
        else:
            lines.append("平均速度：样本里没有可用 tps")
        lines.append("统计只读：插件不写 stats.json。")
        await self._send(event, "\n".join(lines))

    # ---- /ofm setup -------------------------------------------------------
    @ofm.command("setup")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_setup(self, event: AstrMessageEvent):
        endpoint, client = await self._endpoint_and_client()
        state = await self.sessions.get(event.unified_msg_origin)
        model = state.get("model")
        if not model:
            models = []
            try:
                models = await catalog.load(client, ttl=self._models_ttl())
            except OFMError:
                models = []
            buckets = catalog.groups(models)
            for name in ("combo", "free", "aggregate"):
                if buckets.get(name):
                    model = buckets[name][0].id
                    break
            else:
                model = str(self._cfg("default_model", "combo:recommended-free"))
        model_field = SessionStore.compose_model(model, state.get("effort")) or model

        key = endpoint.api_key
        show_key = bool(self._cfg("show_api_key_in_setup", True))
        key_field = key if (key and show_key) else (secrets.safe_mask(key) or "<未找到 forwardKey>")
        lines = [
            "把下面 4 个字段填到 AstrBot WebUI → 配置 → 提供商 → 新建提供商：",
            "",
            "提供商类型：OpenAI 兼容（openai / openai_chat_completion）",
            f"API Base：{endpoint.base_url}/v1",
            f"Key：{key_field}",
            f"模型：{model_field}",
            "",
            "说明：",
            f"· Key 就是 OFM 的 forwardKey；群聊场景请只贴掩码 {secrets.safe_mask(key) or '（无）'}，完整值在 {endpoint.data_dir / 'settings.json'} 的 forwardKey 字段。",
            "· 若 OFM 还没开本地转发，先到 OFM → 设置 → 本地转发 打开并保存，否则所有请求都会 503。",
            "· 想按会话切模型 / 档位用 /ofm use，不用在 WebUI 里建多个 provider。",
        ]
        if not key:
            lines.append("· 现在没读到 forwardKey：打开一次本地转发后它才会被铸造，然后重跑 /ofm setup。")
        await self._send(event, "\n".join(lines))

    # ---- /ofm announce ----------------------------------------------------
    @ofm.command("announce")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @_friendly
    async def ofm_announce(self, event: AstrMessageEvent):
        args = self._args(event, "announce")
        try:
            limit = int(self._cfg("announce_limit", 3))
        except (TypeError, ValueError):
            limit = 3
        if args:
            try:
                limit = int(args[0])
            except (TypeError, ValueError):
                await self._send(event, "用法：/ofm announce [n]，n 是要显示的条数（1–10）。")
                return
        limit = min(max(limit, 1), _MAX_ANNOUNCE)

        endpoint, _ = await self._endpoint_and_client()
        items = feed_mod.announcements(endpoint.data_dir)
        if not items:
            await self._send(
                event,
                f"{endpoint.data_dir / 'feed.json'} 里没有可用公告（文件不存在、为空或全部过期）。\n"
                "下一步：在 OFM 里刷新一次公告缓存后再查。",
            )
            return

        lines: list[str] = []
        for index, item in enumerate(items[:limit], start=1):
            title = str(item.get("title") or "（无标题）").strip()
            level = str(item.get("level") or "").strip()
            when = _fmt_time(item.get("createdAt"))
            head = f"[{index}] {title}"
            tags = [tag for tag in (level, when) if tag]
            if tags:
                head += f"（{' · '.join(tags)}）"
            lines.append(head)
            body = feed_mod.plain_text(item, limit=800)
            if body:
                lines.append(body)
            link = item.get("link")
            if isinstance(link, str) and link.strip():
                lines.append(f"链接：{link.strip()}")
            lines.append("")

        if len(items) > limit:
            lines.append(
                f"共 {len(items)} 条，只显示最近 {limit} 条；"
                f"最多可看 {min(len(items), _MAX_ANNOUNCE)} 条：/ofm announce {min(len(items), _MAX_ANNOUNCE)}。"
            )
        await self._send(event, "\n".join(lines).strip())

    # ---- 请求覆盖钩子 -----------------------------------------------------
    @filter.on_llm_request()
    async def inject_session_model(self, event: AstrMessageEvent, req) -> None:
        """有会话覆盖时改写 ``req.model``；否则原样不动。绝不影响正常对话。"""
        try:
            state = await self.sessions.get(event.unified_msg_origin)
            model = state.get("model")
            if not model:
                return
            composed = SessionStore.compose_model(model, state.get("effort"))
            if composed and getattr(req, "model", None) != composed:
                req.model = composed
        except Exception as exc:  # 钩子里任何异常都必须吞掉
            try:
                log = getattr(self, "logger", logger)
                log.warning(f"[ofm] 覆盖请求模型失败（已忽略）：{exc}")
            except Exception:
                pass
