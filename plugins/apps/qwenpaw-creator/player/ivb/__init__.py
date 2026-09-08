# -*- coding: utf-8 -*-
"""IVB 放映端包。

对外只暴露三件事:
- :func:`ivb.format.reader.inspect_bundle` 读一个包(不抛异常,只出诊断);
  确知读的是合规包时,才用会抛 :class:`BundleError` 的 ``read_bundle``
- :func:`ivb.format.validate.validate_bundle` 校验一个包
- :func:`ivb.server.app.create_app` 起一个放映服务
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
