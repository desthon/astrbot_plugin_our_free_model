"""OFM 数据目录与密钥读取（只读，不写任何文件）。

Our Free Model 的运行时状态都落在数据目录里：

* ``settings.json`` —— 顶层 ``forwardKey``（明文，可能不存在）、``forward`` 配置、
  ``standalonePort``（独立服务实际监听的端口）；
* ``stats.json`` / ``feed.json`` —— 统计与公告缓存。

本模块只负责把 ``settings.json`` 读出来并做安全展示，不承担端点探测的工作
（那在 :mod:`core.endpoints`）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = [
    "DEFAULT_STANDALONE_PORT",
    "DEFAULT_PLUGIN_PORT",
    "data_dirs",
    "read_settings",
    "forward_key",
    "safe_mask",
    "standalone_port",
    "forward_settings",
]

DEFAULT_STANDALONE_PORT = 18900
DEFAULT_PLUGIN_PORT = 18899


def _expand(raw: str) -> Path:
    """把可能含 ``~`` 的字符串展开成绝对路径。"""
    return Path(raw).expanduser()


def data_dirs() -> list[Path]:
    """按 ``[standalone, plugin]`` 顺序返回候选数据目录。

    * standalone：``$OFM_HOME`` 或 ``~/.our-free-model``
    * plugin：``$DSH_HOME/our-free-model`` 或 ``~/.dsh/our-free-model``
    """
    ofm_home = os.environ.get("OFM_HOME") or "~/.our-free-model"
    dsh_home = os.environ.get("DSH_HOME") or "~/.dsh"
    dirs = [_expand(ofm_home), _expand(dsh_home) / "our-free-model"]
    unique: list[Path] = []
    for item in dirs:
        if item not in unique:
            unique.append(item)
    return unique


def read_settings(data_dir: Path) -> dict:
    """读取 ``<data_dir>/settings.json``；不存在、不可读、非对象一律返回 ``{}``。"""
    try:
        path = Path(data_dir).expanduser() / "settings.json"
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def forward_key(data_dir: Path) -> str | None:
    """取顶层 ``forwardKey``；缺失或为空时返回 ``None``。

    OFM 的 key 是「惰性铸造」的：从没打开过本地转发就根本没有这个字段。
    """
    value = read_settings(data_dir).get("forwardKey")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def safe_mask(key: str) -> str:
    """返回可安全贴在群里的 key 指纹，例如 ``ofm-…abcd``。

    只保留首段与前缀提示 + 末 4 位，短 key 只留末 2 位。
    """
    text = key.strip() if isinstance(key, str) else ""
    if not text:
        return ""
    if len(text) <= 8:
        return "…" + text[-2:]
    head = text.split("-", 1)[0] if "-" in text else text[:3]
    if len(head) > 8:
        head = head[:8]
    return f"{head}…{text[-4:]}"


def standalone_port(settings: dict) -> int:
    """从 settings 里取独立服务端口，非法值回落到 18900。"""
    value = settings.get("standalonePort") if isinstance(settings, dict) else None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STANDALONE_PORT
    if 0 < port < 65536:
        return port
    return DEFAULT_STANDALONE_PORT


def forward_settings(settings: dict) -> dict:
    """取 ``forward`` 子树（缺失时返回空 dict）。"""
    value = settings.get("forward") if isinstance(settings, dict) else None
    return value if isinstance(value, dict) else {}
