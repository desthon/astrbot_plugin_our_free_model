"""OFM 端点自发现：搞清「对面到底是哪种形态、监听在哪个端口」。

两种形态的 ``/health`` 指纹不同：

* standalone：``{"ok":true,"service":"our-free-model-standalone","product":"standalone",...}``
* 插件网关：``{"ok":true,"service":"our-free-model"}``（没有 product/version）

独立服务的端口持久化在 ``settings.json`` 的 ``standalonePort``；端口被占用时 OFM 会
向后顺延，所以探测时对候选端口做一个小范围扫描。插件网关（18899）只做识别，
管理面在那种形态下不可达，命令层会降级提示。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .secrets import (
    DEFAULT_PLUGIN_PORT,
    DEFAULT_STANDALONE_PORT,
    data_dirs,
    forward_key,
    forward_settings,
    read_settings,
    standalone_port,
)

__all__ = [
    "Endpoint",
    "EndpointNotFound",
    "resolve",
    "probe_health",
    "read_settings",
    "mode_from_health",
    "normalize_base_url",
]

#: 端口被占时 OFM 向后扫描的范围（含起始端口共 11 个）
PORT_SCAN = 11

#: 与 data_dirs() 的顺序一一对应
_MODE_BY_INDEX = ("standalone", "plugin")


@dataclass(frozen=True)
class Endpoint:
    """一个已确认身份的 OFM 网关。"""

    mode: str  # "standalone" | "plugin"
    base_url: str  # 形如 "http://127.0.0.1:18900"，无尾斜杠
    api_key: str
    data_dir: Path
    health: dict = field(default_factory=dict)


class EndpointNotFound(RuntimeError):
    """找不到可用的 OFM 网关；``diagnostic`` 是给用户看的中文可操作说明。"""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


def normalize_base_url(raw: str) -> str:
    """去掉尾部斜杠，并补上 ``http://``（只补裸 ``host:port`` 形式）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text.rstrip("/")


def mode_from_health(health: dict) -> str:
    """根据 ``/health`` 内容判断形态。"""
    if not isinstance(health, dict):
        return "plugin"
    product = health.get("product")
    service = health.get("service")
    service_text = service if isinstance(service, str) else ""
    if product == "standalone" or service_text.endswith("-standalone"):
        return "standalone"
    return "plugin"


async def probe_health(base_url: str, timeout: float = 3.0) -> dict | None:
    """请求 ``<base_url>/health``；非 200 / 非 JSON 对象 / 网络错误都返回 ``None``。"""
    url = normalize_base_url(base_url)
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url}/health")
    except Exception:  # 网络错误、超时、TLS 问题一律视为「探不到」
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return None
    return payload


def _port_for(mode: str, settings: dict) -> int:
    """取该形态应监听的端口。"""
    if mode == "standalone":
        return standalone_port(settings)
    forward = forward_settings(settings)
    try:
        port = int(forward.get("port"))
    except (TypeError, ValueError):
        return DEFAULT_PLUGIN_PORT
    return port if 0 < port < 65536 else DEFAULT_PLUGIN_PORT


def _data_dir_for(mode: str) -> Path:
    dirs = data_dirs()
    index = 0 if mode == "standalone" else 1
    if index < len(dirs):
        return dirs[index]
    return dirs[0]


def _diagnostic(tried: list[str]) -> str:
    """把探测过的地址压成一行（最多展示 6 个，避免刷屏）。"""
    if not tried:
        joined = "（无）"
    elif len(tried) <= 6:
        joined = "、".join(tried)
    else:
        joined = "、".join(tried[:6]) + f" …（共 {len(tried)} 个地址）"
    dirs = "、".join(str(item) for item in data_dirs())
    return (
        "没找到可用的 Our Free Model 网关。\n"
        f"已尝试：{joined}\n"
        f"数据目录：{dirs}\n"
        "下一步：① 确认 OFM 独立服务在运行（默认 127.0.0.1:18900，"
        "命令一般为 npm run start:standalone）；"
        "② 若端口改过，在插件配置里把 base_url 填成 http://127.0.0.1:<端口>；"
        "③ 若数据目录不在默认位置，设置环境变量 OFM_HOME 指向它。"
    )


async def resolve(base_url: str = "", api_key: str = "", timeout: float = 3.0) -> Endpoint:
    """定位 OFM 网关。

    * 传了 ``base_url`` 就直接探测它（失败即报错，不做别的猜测）；
    * 否则按 ``[standalone, plugin]`` 顺序读各自数据目录的端口并扫 11 个候选；
    * 都没命中时再兜底扫一遍两个默认端口。
    """
    explicit = normalize_base_url(base_url)
    if explicit:
        health = await probe_health(explicit, timeout)
        if health is None:
            raise EndpointNotFound(
                f"手动配置的 OFM 地址 {explicit} 没有响应 /health。\n"
                "下一步：确认独立服务已启动；或删掉插件配置里的 base_url 让插件自动发现；"
                "或检查端口是否被 OFM 顺延到了下一个可用端口。"
            )
        mode = mode_from_health(health)
        data_dir = _data_dir_for(mode)
        key = (api_key or "").strip() or forward_key(data_dir) or ""
        return Endpoint(mode=mode, base_url=explicit, api_key=key, data_dir=data_dir, health=health)

    tried: list[str] = []
    for index, data_dir in enumerate(data_dirs()):
        mode = _MODE_BY_INDEX[index] if index < len(_MODE_BY_INDEX) else "plugin"
        settings = read_settings(data_dir)
        port = _port_for(mode, settings)
        candidates = [port] + [port + offset for offset in range(1, PORT_SCAN)]
        for position, candidate in enumerate(candidates):
            if not 0 < candidate < 65536:
                continue
            url = f"http://127.0.0.1:{candidate}"
            # 首个候选端口给足超时，顺延扫描用短超时，避免整体卡住
            wait = timeout if position == 0 else min(timeout, 0.8)
            health = await probe_health(url, wait)
            tried.append(url)
            if health is None:
                continue
            actual = mode_from_health(health)
            actual_dir = data_dir if actual == mode else _data_dir_for(actual)
            key = forward_key(actual_dir) or forward_key(data_dir) or (api_key or "").strip()
            return Endpoint(
                mode=actual,
                base_url=url,
                api_key=key,
                data_dir=actual_dir,
                health=health,
            )

    # 兜底：数据目录可能不在默认位置，但服务在默认端口上跑着
    for port in (DEFAULT_STANDALONE_PORT, DEFAULT_PLUGIN_PORT):
        url = f"http://127.0.0.1:{port}"
        if url in tried:
            continue
        health = await probe_health(url, min(timeout, 1.5))
        tried.append(url)
        if health is None:
            continue
        actual = mode_from_health(health)
        data_dir = _data_dir_for(actual)
        key = forward_key(data_dir) or (api_key or "").strip()
        return Endpoint(mode=actual, base_url=url, api_key=key, data_dir=data_dir, health=health)

    raise EndpointNotFound(_diagnostic(tried))


async def resolve_or_none(base_url: str = "", api_key: str = "", timeout: float = 3.0) -> Endpoint | None:
    """``resolve`` 的容错版本：找不到时返回 ``None``，不抛异常。"""
    try:
        return await resolve(base_url=base_url, api_key=api_key, timeout=timeout)
    except EndpointNotFound:
        return None


async def gather_health(urls: list[str], timeout: float = 1.5) -> dict[str, dict]:
    """并发探测多个地址，返回 ``{url: health}``（只含成功的）。"""
    results = await asyncio.gather(
        *(probe_health(url, timeout) for url in urls), return_exceptions=True
    )
    found: dict[str, dict] = {}
    for url, result in zip(urls, results):
        if isinstance(result, dict):
            found[url] = result
    return found
