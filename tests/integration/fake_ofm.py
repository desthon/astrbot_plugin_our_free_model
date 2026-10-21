"""Fake Our Free Model gateway for the *integration* suite.

Unlike ``tests/mock_ofm.py`` (which drives the lightweight-stub suite), this one
exists to be hit by the **real** AstrBot 4.29.1 provider stack, so it has to be
faithful on two counts and observant on one:

* ``GET /health``      — standalone fingerprint, used by endpoint discovery.
* ``GET /v1/models``   — Bearer-authenticated model catalogue.
* ``POST /v1/chat/completions`` — records the exact JSON body the real
  ``ProviderOpenAIOfficial`` puts on the wire (test 6 hangs on this).

Everything the real SDK sends is captured verbatim in ``.requests`` and the
chat bodies are additionally parsed into ``.chat_requests``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

DEFAULT_KEY = "ofm-integration-key-0001"
STANDALONE_FINGERPRINT = "our-free-model-standalone"

MODELS: list[dict[str, Any]] = [
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
]


def model_row(model_id: str, efforts: list[str] | None = None) -> dict[str, Any]:
    """One ``/v1/models`` row; effort metadata only when asked for."""

    row: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": 1792571613,
        "owned_by": "our-free-model",
        "context_window": 1048576,
    }
    if efforts is not None:
        row["x_ofm_efforts"] = list(efforts)
        row["x_ofm_effort_default"] = efforts[1] if len(efforts) > 1 else efforts[0]
    return row


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def log_message(self, *args: Any) -> None:  # keep pytest output clean
        return None

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
        parsed: dict[str, Any] | None = None
        if raw_body:
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError:
                parsed = None
        gateway.requests.append(
            {
                "method": method,
                "path": path,
                "authorization": self.headers.get("Authorization"),
                "raw": raw_body,
                "json": parsed,
            }
        )

        if path == "/health":
            return self._send(200, gateway.health_payload())

        authorized = self.headers.get("Authorization") == f"Bearer {gateway.key}"
        if not authorized:
            return self._send(
                401,
                {
                    "error": {
                        "type": "authentication_error",
                        "message": "invalid api key provided",
                    }
                },
            )

        if path == "/v1/models":
            if gateway.models_status != 200:
                return self._send(
                    gateway.models_status,
                    {
                        "error": {
                            "type": "server_error",
                            "message": "models endpoint failed (fault injection)",
                        }
                    },
                )
            return self._send(200, {"object": "list", "data": gateway.models_payload()})

        if path == "/v1/chat/completions" and method == "POST":
            body = parsed or {}
            gateway.chat_requests.append(body)
            model = str(body.get("model") or "")
            return self._send(
                200,
                {
                    "id": "chatcmpl-integration",
                    "object": "chat.completion",
                    "created": 1792571613,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "pong"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    },
                },
            )

        return self._send(
            404, {"error": {"type": "not_found", "message": f"no route for {path}"}}
        )


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    gateway: "FakeOFM"


class FakeOFM:
    """Runnable stdlib gateway. ``stop()`` is idempotent."""

    def __init__(
        self,
        key: str = DEFAULT_KEY,
        host: str = "127.0.0.1",
        port: int = 0,
        version: str = "9.9.9",
        models: list[dict[str, Any]] | None = None,
        models_status: int = 200,
    ) -> None:
        self.key = key
        self.models = [dict(row) for row in (models if models is not None else MODELS)]
        self.models_status = models_status
        self.host = host
        self.requested_port = port
        self.port = port
        self.version = version
        self.requests: list[dict[str, Any]] = []
        self.chat_requests: list[dict[str, Any]] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "FakeOFM":
        if self._server is not None:
            return self
        self._server = _Server((self.host, self.requested_port), _Handler)
        self._server.gateway = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
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

    def __enter__(self) -> "FakeOFM":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- addressing -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def openai_base_url(self) -> str:
        """Exactly what the contract says ``api_base`` must be."""

        return f"{self.base_url}/v1"

    def health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": STANDALONE_FINGERPRINT,
            "product": "standalone",
            "version": self.version,
        }

    def models_payload(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.models]

    def set_models(self, rows: list[dict[str, Any]]) -> "FakeOFM":
        """Swap the catalogue mid-test (model re-selection / drift cases)."""

        self.models = [dict(row) for row in rows]
        return self

    def request_count(self) -> int:
        return len(self.requests)
