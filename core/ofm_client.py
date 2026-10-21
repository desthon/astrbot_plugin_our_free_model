"""OFM 网关的 HTTP 客户端：把各种失败归一成可解释的 :class:`OFMError`。

只用外部契约（``/health``、``/v1/models``、``/api/management/*``），不碰任何
AstrBot 内部 API。
"""

from __future__ import annotations

from typing import Any

import httpx

from .endpoints import Endpoint

__all__ = ["OFMError", "OFMClient", "KIND_LABELS"]

#: 每种 kind 的中文短标签（用于 doctor / status 的逐项输出）
KIND_LABELS = {
    "forward_disabled": "转发未开启",
    "auth": "鉴权失败",
    "model_not_found": "模型不存在",
    "model_unavailable": "上游不可路由",
    "unreachable": "连不上网关",
    "upstream": "上游错误",
    "bad_response": "响应格式异常",
}

_HUMAN_TEXT = {
    "forward_disabled": (
        "OFM 的本地转发没开（非 /health 路径返回 503）。"
        "下一步：打开 Our Free Model → 设置 → 本地转发（forward）并保存，再重试。"
    ),
    "auth": (
        "鉴权失败：forwardKey 与 OFM 当前使用的 key 不一致。"
        "下一步：在 OFM 里关掉再打开一次本地转发，让它重新铸造 key，"
        "然后把 settings.json 里的 forwardKey 粘到插件配置的 api_key（留空则自动读文件）。"
    ),
    "model_not_found": (
        "OFM 目录里没有这个模型。"
        "下一步：用 /ofm models 看可用列表（目录有 10 分钟缓存），确认没有写错 id 再重试。"
    ),
    "model_unavailable": (
        "上游拒绝了这个模型的路由（Model is unavailable），通常是免费车道临时不可用。"
        "下一步：换一个模型，或用 combo: 降级链；也看看 /ofm announce 有没有故障公告。"
    ),
    "unreachable": (
        "连不上 OFM 网关。"
        "下一步：确认独立服务在运行（默认 127.0.0.1:18900）、端口是否被顺延，"
        "以及插件配置里的 base_url 是否正确。"
    ),
    "upstream": (
        "上游服务返回错误。"
        "下一步：稍等重试；持续出现时用 /ofm doctor 与 /ofm announce 区分「上游故障」还是「本地配置错」。"
    ),
    "bad_response": (
        "OFM 返回了无法解析的响应。"
        "下一步：确认 base_url 指向的是 OFM 本身（<host>:18900），而不是别的服务或反向代理。"
    ),
}


class OFMError(Exception):
    """归一化后的 OFM 调用错误。"""

    def __init__(self, kind: str, status: int | None = None, detail: str = "") -> None:
        self.kind = kind if kind in _HUMAN_TEXT else "upstream"
        self.status = status
        self.detail = (detail or "").strip()
        super().__init__(f"{self.kind}: {self.detail}" if self.detail else self.kind)

    def human(self) -> str:
        """中文可操作说明；不同 kind 的首句互不相同。"""
        text = _HUMAN_TEXT.get(self.kind, _HUMAN_TEXT["upstream"])
        if self.detail:
            return f"{text}\n技术细节：{self.detail[:300]}"
        return text


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit]


class OFMClient:
    """一个端点的异步客户端；用完请 ``await aclose()``。"""

    def __init__(self, endpoint: Endpoint, timeout: float = 120.0) -> None:
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.last_error: OFMError | None = None
        self._http: httpx.AsyncClient | None = None

    # ---- 生命周期 ---------------------------------------------------------
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"Accept": "application/json"}
            if self.endpoint.api_key:
                headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
            self._http = httpx.AsyncClient(
                base_url=self.endpoint.base_url,
                timeout=self.timeout,
                headers=headers,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    # ---- 内部请求 ---------------------------------------------------------
    def _map_response(self, response: httpx.Response) -> OFMError:
        status = response.status_code
        try:
            body = response.text
        except Exception:  # pragma: no cover - 极端情况下读不到 body
            body = ""
        detail = _short(body)
        lowered = body.lower()
        if "model is unavailable" in lowered:
            return OFMError("model_unavailable", status, detail)
        if status in (401, 403):
            return OFMError("auth", status, detail)
        if status == 404:
            if "model" in lowered:
                return OFMError("model_not_found", status, detail)
            return OFMError("bad_response", status, detail or "端点不存在（404）")
        if status == 503:
            return OFMError("forward_disabled", status, detail)
        if status == 429 or 400 <= status < 500 or status >= 500:
            return OFMError("upstream", status, detail)
        return OFMError("bad_response", status, detail)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            response = await self.http.request(
                method, path, json=json_body, timeout=timeout or self.timeout
            )
        except OFMError:
            raise
        except httpx.HTTPError as exc:
            error = OFMError("unreachable", None, f"{type(exc).__name__}: {exc}")
            self.last_error = error
            raise error from exc
        except Exception as exc:  # pragma: no cover - 兜底，绝不冒泡成栈
            error = OFMError("unreachable", None, f"{type(exc).__name__}: {exc}")
            self.last_error = error
            raise error from exc

        if response.status_code >= 400:
            error = self._map_response(response)
            self.last_error = error
            raise error

        try:
            payload = response.json()
        except ValueError as exc:
            error = OFMError("bad_response", response.status_code, "响应不是 JSON")
            self.last_error = error
            raise error from exc
        return payload

    # ---- /v1 -------------------------------------------------------------
    async def models(self) -> list[dict]:
        """``GET /v1/models``；返回原始行列表（未归一）。"""
        payload = await self._call("GET", "/v1/models")
        if not isinstance(payload, dict):
            raise OFMError("bad_response", None, "/v1/models 返回的不是对象")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise OFMError("bad_response", None, "/v1/models 缺少 data 数组")
        return [row for row in rows if isinstance(row, dict)]

    # ---- /api/management -------------------------------------------------
    async def management_summary(self) -> dict | None:
        """``GET /api/management/summary``；不可用时返回 ``None``（不抛）。"""
        return await self._optional("GET", "/api/management/summary")

    async def management_stats(self, limit: int = 200) -> dict | None:
        """``GET /api/management/stats``；``samples`` 截到 ``limit`` 条。"""
        payload = await self._optional("GET", "/api/management/stats")
        if not isinstance(payload, dict):
            return None
        samples = payload.get("samples")
        if isinstance(samples, list) and limit > 0 and len(samples) > limit:
            trimmed = dict(payload)
            trimmed["samples"] = samples[-limit:]
            return trimmed
        return payload

    async def refresh_models(self) -> dict | None:
        """``POST /api/management/models/refresh {"probe":true}``。"""
        return await self._optional(
            "POST", "/api/management/models/refresh", json_body={"probe": True}
        )

    async def _optional(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict | None:
        try:
            payload = await self._call(method, path, json_body=json_body)
        except OFMError:
            return None  # 原因已记在 last_error，由 doctor 展示
        return payload if isinstance(payload, dict) else None
