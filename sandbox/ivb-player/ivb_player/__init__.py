# -*- coding: utf-8 -*-
"""IVB 放映端包。

对外只暴露三件事:
- :func:`ivb_player.format.reader.open_bundle` 打开一个包
- :func:`ivb_player.format.validate.validate_bundle` 校验一个包
- :func:`ivb_player.server.app.create_app` 起一个放映服务
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
