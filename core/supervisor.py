"""拉起 / 停掉本机的 Our Free Model standalone 进程。

设计约束（和插件其它部分一致）：

* 只在**需要时**动手：``/health`` 已经通了就什么都不做（adopt），绝不抢用户手动起的服务；
* 只关自己拉起来的进程：PID 与「归谁所有」一起记在 KV 里，卸载插件时只杀自己的；
* 失败一律降级成中文原因，不抛异常到钩子里；
* 不引入第三方依赖：只用 ``asyncio`` 子进程 + ``node:`` 自带的 CLI。

OFM 的 standalone 入口是仓库里的 ``packages/standalone/cli.mjs``，**运行期无第三方依赖**
（``packages/standalone/package.json`` 里只有 devDependencies，用于构建 WebUI），
所以不需要先 ``npm install`` 就能起。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLI_REL = Path("packages/standalone/cli.mjs")
"""standalone 入口相对仓库根的位置。"""

SERVICE_NAME = "our-free-model-standalone"
"""``/health`` 里 ``service`` 字段的固定值，用来确认端口上跑的是 OFM 而不是别人。"""

KV_SERVICE = "svc_process"
"""KV 键：``{"pid": int, "owned": bool, "base_url": str, "started_at": int}``。"""

#: ``package.json`` 的 engines: ``^22.19.0 || >=24.0.0``
#: ``^22.19.0`` 在 semver 里是 ``>=22.19.0 <23.0.0``——23.x 整段不在许可范围内。
MIN_NODE_22 = (22, 19, 0)
MAX_NODE_22_EXCLUSIVE = (23, 0, 0)
MIN_NODE_24 = (24, 0, 0)
# 上游 engines 写的是 ``^22.19.0 || >=24.0.0``，但实测 Node 20 只卡在一个 API 上
# （node:worker_threads 的 markAsUncloneable 是 22.10 才有的导出）。
# 打上 patches/ofm-node20-compat.patch 后 20.19 起即可用，AstrBot 的安装脚本给的正是 20.x。
MIN_NODE_20 = (20, 19, 0)
NODE_20_PATCH_FROM = (22, 10, 0)  # 低于这个版本才需要补丁
NODE_20_PATCH_MARK = "node20-compat"
PATCH_BUNDLE = "packages/standalone/channels/business.mjs"

DEFAULT_START_PORT = 18900
LOG_TAIL_LINES = 12


@dataclass
class StartResult:
    """一次「确保本地服务在跑」的结果。"""

    ok: bool
    started: bool = False
    adopted: bool = False
    pid: int = 0
    base_url: str = ""
    port: int = 0
    reason: str = ""
    log_tail: str = ""

    def describe(self) -> str:
        if self.adopted:
            return f"接上已在运行的服务（{self.base_url}）"
        if self.started:
            return f"已启动：pid={self.pid}，{self.base_url}"
        return self.reason or "没有启动"


# ---------------------------------------------------------------------------
# 定位仓库与 node
# ---------------------------------------------------------------------------
def candidate_roots(explicit: str = "") -> list[Path]:
    """按优先级列出可能的 OFM 仓库位置。"""
    home = Path.home()
    items: list[str] = []
    if explicit.strip():
        items.append(explicit.strip())
    for env in ("OFM_REPO", "OFM_ROOT", "OUR_FREE_MODEL_REPO"):
        value = os.environ.get(env, "").strip()
        if value:
            items.append(value)
    items.extend(
        [
            str(home / "dsh-our-free-model"),
            str(home / "our-free-model"),
            str(home / ".our-free-model" / "repo"),
            "/opt/dsh-our-free-model",
            "/usr/local/lib/dsh-our-free-model",
        ]
    )
    roots: list[Path] = []
    for item in items:
        path = Path(item).expanduser()
        if path not in roots:
            roots.append(path)
    return roots


def is_repo(path: Path | None) -> bool:
    return bool(path) and (path / CLI_REL).is_file()


def find_repo(explicit: str = "") -> Path | None:
    for root in candidate_roots(explicit):
        if is_repo(root):
            return root
    return None


def which_node() -> str:
    return shutil.which("node") or ""


async def node_version(node: str) -> tuple[tuple[int, int, int] | None, str]:
    """返回 ``((major, minor, patch), 原始字符串)``；拿不到时版本为 None。"""
    if not node:
        return None, ""
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        return None, ""
    text = (out or b"").decode("utf-8", "replace").strip()
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None, text
    return tuple(int(part) for part in match.groups()), text  # type: ignore[return-value]


def version_ok(version: tuple[int, int, int] | None) -> bool:
    """能不能跑 OFM。

    上游 ``engines`` 是 ``^22.19.0 || >=24.0.0``；实测把唯一一处 Node 22 专有 API
    （``markAsUncloneable``）兼容掉之后，Node 20.19 起也能跑，而这正是 AstrBot 装机脚本
    给到的版本，所以下限放到 20.19 —— 低于 22.10 的版本由 :func:`needs_compat_patch` 要求打补丁。
    """
    if version is None:
        return False
    return version >= MIN_NODE_20


def needs_compat_patch(version: tuple[int, int, int] | None) -> bool:
    """低于 22.10 的 Node 必须给 OFM 打 Node20 兼容补丁。"""
    return version is not None and MIN_NODE_20 <= version < NODE_20_PATCH_FROM


def has_compat_patch(repo: Path) -> bool:
    """仓库里的 standalone 渠道包是否已带 Node20 兼容补丁。"""
    try:
        text = (repo / PATCH_BUNDLE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return NODE_20_PATCH_MARK in text


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------
async def probe_health(base_url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    """``/health`` 通且确实是 OFM 时返回 payload。"""
    import httpx

    url = base_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload


def tcp_reachable(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    """端口上有没有人在监听。

    ``settings.enabled=false`` 时 OFM 连 ``/health`` 都返回 503，但进程仍在正常监听；
    要判断「服务在跑、只是被关掉了」，只能看 TCP 层。
    """
    import socket

    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def settings_port(data_dir: str) -> int:
    """从数据目录的 ``settings.json`` 读实际监听端口（端口冲突时 OFM 会顺延）。"""
    try:
        path = Path(data_dir).expanduser() / "settings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    try:
        return int(payload.get("standalonePort") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------
def _service_env(data_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    if data_dir.strip():
        env["OFM_HOME"] = str(Path(data_dir).expanduser())
    return env


async def start(
    repo: Path,
    *,
    port: int = DEFAULT_START_PORT,
    data_dir: str = "",
    wait: float = 25.0,
    node: str = "",
    resolve_data_dir: str = "",
) -> StartResult:
    """确保服务在跑；已在跑则 adopt，不重复拉起。

    ``data_dir`` 是传给子进程的 ``OFM_HOME``；``resolve_data_dir`` 是「去哪里读
    settings.json 判断端口」，缺省与 ``data_dir`` 相同（留空则由调用方回退到默认目录）。
    """
    probe_dir = (resolve_data_dir or data_dir or "").strip()
    if probe_dir:
        existing_port = settings_port(probe_dir)
        if existing_port:
            payload = await probe_health(f"http://127.0.0.1:{existing_port}")
            if payload and payload.get("service") == SERVICE_NAME:
                return StartResult(
                    ok=True,
                    adopted=True,
                    base_url=f"http://127.0.0.1:{existing_port}",
                    port=existing_port,
                )
            if tcp_reachable(existing_port):
                # 进程在跑但 /health 503：最常见是 settings.enabled 被关掉。
                # 不重复拉起（数据目录 service.lock 也不允许），交给上层打开开关。
                return StartResult(
                    ok=True,
                    adopted=True,
                    base_url=f"http://127.0.0.1:{existing_port}",
                    port=existing_port,
                    reason="服务在跑，但 /health 返回 503（独立服务开关可能是关的）",
                )
    for candidate in (port, DEFAULT_START_PORT):
        payload = await probe_health(f"http://127.0.0.1:{candidate}")
        if payload and payload.get("service") == SERVICE_NAME:
            return StartResult(
                ok=True,
                adopted=True,
                base_url=f"http://127.0.0.1:{candidate}",
                port=candidate,
            )

    node_bin = node or which_node()
    if not node_bin:
        return StartResult(ok=False, reason="没找到 node：OFM standalone 需要 Node.js，请先安装")

    version, raw = await node_version(node_bin)
    if not version_ok(version):
        return StartResult(
            ok=False,
            reason=(
                f"node 版本太旧（{raw or '未知'}）：OFM standalone 需要 Node >= 20.19"
                "（20.19–22.9 需要额外打 Node20 兼容补丁）"
            ),
        )

    if not is_repo(repo):
        return StartResult(ok=False, reason=f"OFM 仓库不完整：{repo} 里没有 {CLI_REL}")

    if needs_compat_patch(version) and not has_compat_patch(Path(repo)):
        return StartResult(
            ok=False,
            reason=(
                f"node {raw} 需要 OFM 的 Node20 兼容补丁（否则启动会报 "
                "webidl.util.markAsUncloneable is not a function）："
                f"cd {repo} && git apply <插件目录>/patches/ofm-node20-compat.patch"
            ),
        )

    log_tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
    try:
        proc = await asyncio.create_subprocess_exec(
            node_bin,
            str(CLI_REL),
            "--port",
            str(port),
            cwd=str(repo),
            env=_service_env(data_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        return StartResult(ok=False, reason=f"启动 OFM 进程失败：{exc}")

    async def _drain() -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            log_tail.append(line.decode("utf-8", "replace").rstrip())

    reader = asyncio.create_task(_drain())
    deadline = time.monotonic() + max(3.0, wait)
    actual_port = 0
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            break
        candidate_port = settings_port(probe_dir) if probe_dir else 0
        for probe_port in [p for p in (candidate_port, port) if p]:
            payload = await probe_health(f"http://127.0.0.1:{probe_port}", timeout=1.0)
            if payload and payload.get("service") == SERVICE_NAME:
                actual_port = probe_port
                break
        if actual_port:
            break
        await asyncio.sleep(0.4)

    tail = "\n".join(list(log_tail)[-6:])
    if not actual_port:
        code = proc.returncode
        if code is None:
            # 起不来又没退出：别再挂着一个僵尸服务
            await stop(proc.pid)
            reason = f"等了 {int(wait)} 秒 /health 仍不通（进程已终止）"
        else:
            reason = f"OFM 进程启动后立即退出（exit={code}）"
        if "service.lock" in tail or "EEXIST" in tail:
            # 同一数据目录已有实例：不要报失败，按「在跑」处理，让上层去打开开关
            lock_port = settings_port(probe_dir) if probe_dir else port
            if lock_port and tcp_reachable(lock_port):
                return StartResult(
                    ok=True,
                    adopted=True,
                    base_url=f"http://127.0.0.1:{lock_port}",
                    port=lock_port,
                    reason="数据目录里已有实例在跑（service.lock），按接管处理",
                    log_tail=tail,
                )
            reason = "数据目录里已有服务在跑（service.lock）：同一数据目录只能有一个实例"
        return StartResult(ok=False, reason=reason, log_tail=tail)

    result = StartResult(
        ok=True,
        started=True,
        pid=proc.pid,
        base_url=f"http://127.0.0.1:{actual_port}",
        port=actual_port,
        log_tail=tail,
    )
    # 把读取任务解绑：进程活着时要继续吃掉 stdout，否则管道满了会阻塞服务
    reader.add_done_callback(lambda _task: None)
    return result


def pid_alive(pid: int) -> bool:
    """进程是否还活着。

    注意僵尸：子进程退出后如果没人 ``wait()``，PID 仍然可被 ``kill(pid, 0)`` 命中，
    但进程已经不工作、端口也已经释放。这里按 ``/proc/<pid>/stat`` 的状态位判死，
    否则「SIGTERM 之后仍然报告存活」会让 stop() 永远等不到结束。
    """
    if pid <= 0:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        stat = ""
    if stat:
        # 格式：pid (comm) state ...；comm 里可能有空格和括号，取最后一个 ')' 之后
        after = stat.rsplit(")", 1)[-1].strip()
        if after[:1] in ("Z", "z"):
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def stop(pid: int, *, graceful: float = 6.0) -> bool:
    """按进程组优雅停止：先 SIGTERM，超时再 SIGKILL。"""
    if not pid_alive(pid):
        return False
    for sig, wait in ((signal.SIGTERM, graceful), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            return True
        except Exception:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
            except Exception:
                return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            await asyncio.sleep(0.2)
    return not pid_alive(pid)
