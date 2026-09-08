#!/usr/bin/env bash
# IVB 放映服务启动脚本(容器/打包用)。
#
# 配置全走环境变量,零配置也能起(详见 ivb_player/serve.py 文档):
#   IVB_DATA_DIR          持久卷根,默认 /data(存 ivb.db + bundles/)
#   IVB_HOST / IVB_PORT   监听地址,默认 0.0.0.0:8080
#   IVB_ROOT_PATH         网关子路径前缀(如 /ivb),默认空
#   IVB_MAX_UPLOAD_BYTES  上传包大小上限(字节),默认不限
#   IVB_LOG_LEVEL         uvicorn 日志级别,默认 info
#
# 用法:
#   ./run.sh                        # 用默认值起服务
#   IVB_DATA_DIR=/mnt/ivb ./run.sh  # 指定持久卷
set -euo pipefail

HOST="${IVB_HOST:-0.0.0.0}"
PORT="${IVB_PORT:-8080}"
export IVB_DATA_DIR="${IVB_DATA_DIR:-/data}"

# 持久卷必须存在且可写:ivb.db 与 bundles/ 都落这里,容器重建不丢。
# 单实例部署(SQLite 单写者);多副本前必须先迁 PG(见 docs/service-design.md §8)。
mkdir -p "$IVB_DATA_DIR"

exec uvicorn --factory ivb_player.serve:create_app_from_env \
  --host "$HOST" --port "$PORT" --log-level "${IVB_LOG_LEVEL:-info}"
