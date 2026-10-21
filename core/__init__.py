"""Our Free Model 插件的纯逻辑层。

本包**不 import** ``astrbot``、不注册任何 handler 装饰器：

AstrBot 按模块名登记 handler，handler 放在子模块里会导致插件卸载 / 重载时注销不
干净。所有带装饰器的函数都在 ``main.py``，本包只提供可单测的纯逻辑。
"""

from __future__ import annotations

__all__: list[str] = []
