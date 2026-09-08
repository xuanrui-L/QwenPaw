# IVB 放映服务镜像。单实例部署(SQLite 单写者),持久卷挂 /data。
#
#   docker build -t ivb-player:latest sandbox/ivb-player
#   docker run -d -p 8080:8080 -v /host/data:/data ivb-player:latest
#
# 容器重建/滚动发布不丢数据:ivb.db(含 WAL 边车)与 bundles/ 都在 /data 卷上。
FROM python:3.11-slim

WORKDIR /app

# 先装依赖再拷代码,利用构建层缓存。
COPY pyproject.toml ./
COPY ivb_player ./ivb_player
RUN pip install --no-cache-dir .

COPY run.sh ./run.sh
RUN chmod +x ./run.sh

# 持久卷:ivb.db + bundles/ 都落这里(见 docs/service-design.md §7)。
VOLUME ["/data"]
ENV IVB_DATA_DIR=/data \
    IVB_HOST=0.0.0.0 \
    IVB_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('IVB_PORT','8080'))"

CMD ["./run.sh"]
