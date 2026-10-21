"""Fake Our Free Model gateway: ``http.server`` + ``threading``, zero deps.

Implements exactly the protocol facts the plugin contract depends on:

    GET  /health                          (no auth, mode fingerprint)
    GET  /v1/models                       (Bearer)
    GET  /api/management/stats            (Bearer)
    POST /api/management/models/refresh   (Bearer)
    POST /v1/chat/completions             (Bearer, for completeness)

Fault modes are switchable at runtime (``gateway.mode = "503"``) and from the
command line, so the same object serves both pytest fixtures and manual use:

    python tests/mock_ofm.py --mode 401 --port 18899

Modes
-----
ok            normal service
503           forwarding disabled: every path, including /health, answers 503
401           every authenticated path answers 401
404           /v1/models answers 404 ``model "ghost-model" not found``
unavailable   /v1/models answers 400 ``Model is unavailable ...``
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

DEFAULT_KEY = "ofm-test-key-1234"

STANDALONE_FINGERPRINT = "our-free-model-standalone"
PLUGIN_FINGERPRINT = "our-free-model"

MODES = ("ok", "503", "401", "404", "unavailable")


def models_payload() -> dict[str, Any]:
    """``/v1/models`` body, shaped per the documented protocol."""

    rows = [
        {
            "id": "deepseek-v4.1-flash-free",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
            "context_window": 1048576,
            "x_ofm_efforts": ["disabled", "low", "high", "max"],
            "x_ofm_effort_default": "high",
        },
        {
            "id": "combo:recommended-free",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
            "context_window": 128000,
            "x_ofm_efforts": ["light", "balanced", "deep"],
            "x_ofm_effort_default": "balanced",
        },
        {
            "id": "kilo-auto/free",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
            "context_window": 200000,
            "x_ofm_efforts": ["low", "high"],
            "x_ofm_effort_default": "low",
        },
        {
            "id": "vendor/some-model",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
            "context_window": 32000,
        },
        {
            "id": "bare-id",
            "object": "model",
            "created": 1792571613,
            "owned_by": "our-free-model",
        },
    ]
    return {"object": "list", "data": rows}


# Sample-wise sums deliberately equal the declared aggregates (300/100/3/1), so
# the assertion "tokens match the fake data" holds whether the implementation
# sums samples or reads the aggregate fields.
STATS_SAMPLES = [
    {"at": "2026-01-01T10:00:00Z", "model": "deepseek-v4.1-flash-free", "ok": True, "input": 100, "output": 40, "tps": 25.0},
    {"at": "2026-01-01T10:05:00Z", "model": "combo:recommended-free", "ok": False, "input": 50, "output": 0, "tps": None},
    {"at": "2026-01-01T10:09:00Z", "model": "deepseek-v4.1-flash-free", "ok": True, "input": 150, "output": 60, "tps": 30.0},
]


def stats_payload() -> dict[str, Any]:
    return {
        "samples": [dict(sample) for sample in STATS_SAMPLES],
        "requests": 3,
        "failedRequests": 1,
        "models": ["deepseek-v4.1-flash-free", "combo:recommended-free"],
        "input": 300,
        "output": 100,
        "tokensIn": 300,
        "tokensOut": 100,
        "since": "2026-01-01T00:00:00Z",
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def log_message(self, *args: Any) -> None:  # silence stderr spam
        return None

    # -- plumbing ---------------------------------------------------------
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route("POST")

    def _route(self, method: str) -> None:
        gateway = self.server.gateway
        path = (urlparse(self.path).path or "/").rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        gateway.calls.append((method, path, self.headers.get("Authorization"), raw_body))

        mode = gateway.mode

        # /health is unauthenticated in ok mode; in 503 mode OFM does not bind
        # the forward port at all, which we model as 503 on every path.
        if path == "/health":
            if mode == "503":
                return self._send(503, {"ok": False, "error": {"message": "forward disabled"}})
            return self._send(200, gateway.health_payload())

        if mode == "503":
            return self._send(
                503,
                {
                    "error": {
                        "type": "forward_disabled",
                        "message": "local forwarding is not enabled in Our Free Model settings",
                    }
                },
            )

        authorized = self.headers.get("Authorization") == f"Bearer {gateway.key}"
        if mode == "401" or not authorized:
            return self._send(
                401,
                {"error": {"type": "authentication_error", "message": "invalid api key provided"}},
            )

        if path == "/v1/models":
            if mode == "404":
                return self._send(
                    404,
                    {"error": {"type": "not_found", "message": 'model "ghost-model" not found'}},
                )
            if mode == "unavailable":
                return self._send(
                    400,
                    {
                        "error": {
                            "type": "upstream_error",
                            "message": "Model is unavailable: upstream router rejected this model",
                        }
                    },
                )
            return self._send(200, models_payload())

        if path == "/api/management/stats":
            return self._send(200, stats_payload())

        if path == "/api/management/summary":
            return self._send(
                200,
                {
                    "ok": True,
                    "service": gateway.fingerprint,
                    "version": gateway.version,
                    "models": 5,
                    "requests": 3,
                    "failedRequests": 1,
                },
            )

        if path == "/api/management/models/refresh" and method == "POST":
            return self._send(200, {"ok": True, "refreshed": True, "count": 5})

        if path == "/v1/chat/completions" and method == "POST":
            try:
                body = json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                body = {}
            model = str(body.get("model") or "")
            if model.startswith("ghost"):
                return self._send(
                    404, {"error": {"type": "not_found", "message": f'model "{model}" not found'}}
                )
            if "unavailable" in model:
                return self._send(
                    400,
                    {"error": {"type": "upstream_error", "message": "Model is unavailable"}},
                )
            return self._send(
                200,
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        return self._send(404, {"error": {"type": "not_found", "message": f"no route for {path}"}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    gateway: "MockOFM"


class MockOFM:
    """Runnable fake gateway. ``start()`` is idempotent-safe via ``._server``."""

    def __init__(
        self,
        mode: str = "ok",
        key: str = DEFAULT_KEY,
        host: str = "127.0.0.1",
        port: int = 0,
        fingerprint: str = STANDALONE_FINGERPRINT,
        version: str = "9.9.9",
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.key = key
        self.host = host
        self.requested_port = port
        self.port = port
        self.fingerprint = fingerprint
        self.version = version
        self.calls: list[tuple[str, str, str | None, bytes]] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "MockOFM":
        self._server = _Server((self.host, self.requested_port), _Handler)
        self._server.gateway = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "MockOFM":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- addressing -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def openai_base_url(self) -> str:
        return f"{self.base_url}/v1"

    def health_payload(self) -> dict[str, Any]:
        if self.fingerprint == STANDALONE_FINGERPRINT:
            return {
                "ok": True,
                "service": STANDALONE_FINGERPRINT,
                "product": "standalone",
                "version": self.version,
            }
        return {"ok": True, "service": PLUGIN_FINGERPRINT}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Our Free Model gateway")
    parser.add_argument("--mode", default="ok", choices=MODES)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument(
        "--fingerprint",
        default=STANDALONE_FINGERPRINT,
        choices=[STANDALONE_FINGERPRINT, PLUGIN_FINGERPRINT],
    )
    args = parser.parse_args()

    gateway = MockOFM(
        mode=args.mode,
        key=args.key,
        host=args.host,
        port=args.port,
        fingerprint=args.fingerprint,
    ).start()
    print(f"mock OFM listening on {gateway.base_url} mode={args.mode}", flush=True)
    try:
        gateway._thread.join()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()


if __name__ == "__main__":
    main()
