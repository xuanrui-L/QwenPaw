# -*- coding: utf-8 -*-
"""部署入口:从环境变量装配多包放映应用,供 uvicorn ``--factory`` 加载。

刻意不依赖 ``cli.py``(那是开发期工具);容器/打包只需::

    uvicorn --factory ivb.serve:create_app_from_env \
        --host 0.0.0.0 --port 8080

环境变量(都有缺省,零配置也能起):

``IVB_DATA_DIR``
    持久卷根目录,存 ``ivb.db`` 与 ``bundles/``。缺省 ``/data``。
``IVB_DB_PATH``
    状态库路径,缺省 ``$IVB_DATA_DIR/ivb.db``。
``IVB_ROOT_PATH``
    挂在网关子路径下时的前缀(如 ``/ivb``),用于注入 ``<base href>``。缺省空。
``IVB_MAX_UPLOAD_BYTES``
    上传包大小上限(字节),缺省不限。
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from .server.app import create_app


def _int_or_none(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    return int(raw)


def create_app_from_env() -> FastAPI:
    """按环境变量建应用。uvicorn ``--factory`` 的目标。"""

    return create_app(
        os.environ.get("IVB_DATA_DIR", "/data"),
        db_path=os.environ.get("IVB_DB_PATH") or None,
        root_path=os.environ.get("IVB_ROOT_PATH", ""),
        max_upload_bytes=_int_or_none(
            os.environ.get("IVB_MAX_UPLOAD_BYTES"),
        ),
    )


__all__ = ["create_app_from_env"]
