# 互动视频包格式与部署

## 包结构

```
my-video.zip
├── manifest.json          # 必需  内容与表现层配置
├── presentation.json      # 可选  表现层覆盖（配色/文案）
├── index.html             # 可选  免服务播放器（双击即播）
├── segments/              # 必需  每个分段一个 mp4
│   ├── timeline_open.mp4
│   └── timeline_a.mp4
└── styles/                # 可选  自定义样式
    └── choice.css
```

## manifest.json 示例

```json
{
  "schema_version": 1,
  "entry_timeline_id": "timeline:open",
  "meta": {
    "bundle_id": "project-001",
    "title": "深夜便利店",
    "accent": "#b8ff2e"
  },
  "segments": {
    "timeline:open": "segments/timeline_open.mp4",
    "timeline:a": "segments/timeline_a.mp4"
  },
  "nodes": {
    "timeline:open": {
      "title": "序章",
      "children": ["timeline:a"],
      "is_ending": false
    },
    "timeline:a": {
      "title": "结局",
      "children": [],
      "is_ending": true
    }
  },
  "edge_index": {
    "edge:go": {
      "label": "继续",
      "target_timeline_id": "timeline:a"
    }
  },
  "interactions": [
    {
      "source_timeline_id": "timeline:open",
      "at_seconds": 10.0,
      "question": "继续吗？",
      "options": [
        { "edge_ref": "edge:go" }
      ]
    }
  ]
}
```

**关键字段**：
- `schema_version`: 整数，当前 v1
- `entry_timeline_id`: 入口节点
- `meta.bundle_id`: 全局唯一 ID
- `segments`: timeline_id → mp4 路径
- `nodes`: 故事图节点（`children` 出边，`is_ending` 是否结局）
- `edge_index`: 选项文案（`label` 显示文字，`target_timeline_id` 目标）
- `interactions`: 抉择点（`at_seconds` 弹出时刻，`options` 选项列表）

## 部署

启动脚本 `run.sh`（配置全走环境变量，零配置也能起）：

```bash
# 本地：指定持久卷起服务
IVB_DATA_DIR=/mnt/ivb ./run.sh

# 容器：持久卷挂 /data（镜像见 Dockerfile）
docker run -d \
  --name ivb-player \
  -p 8080:8080 \
  -v /host/data:/data \
  ivb-player:latest
```

| 环境变量 | 缺省 | 说明 |
|---|---|---|
| `IVB_DATA_DIR` | `/data` | 持久卷根，存 `ivb.db` + `bundles/` |
| `IVB_HOST` / `IVB_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `IVB_ROOT_PATH` | 空 | 网关子路径前缀（如 `/ivb`） |
| `IVB_MAX_UPLOAD_BYTES` | 不限 | 上传包大小上限（字节） |

- `/data` 持久卷：容器重建/滚动发布不丢（`ivb.db` 含 WAL 边车须与 `bundles/` 同卷）
- 不要把持久卷放网络文件系统（NFS/EFS）：WAL 依赖本机文件锁
- 健康检查：`GET /api/health`
- **单实例部署**（SQLite 单写者）；多副本前必须先迁 PG（见 `service-design.md` §8）

## 上传与放映

服务不再启动期绑死一个包，改为运行时上传 + 多包路由：

```bash
# 上传一个 Creator 导出的包（X-User-Id 由网关注入，这里手动带上）
curl -X POST http://localhost:8080/api/projects \
  -H "X-User-Id: alice" \
  -F "file=@my-video.zip"

# 浏览器打开库页，点卡片进放映
open http://localhost:8080
```

- 库页 `GET /`：我的/全部切换，点卡片进 `/?p={project_id}`
- 上传契约见 `upload-protocol.md`（成功 201、校验失败 422）
- 开发期可选校验：`ivb validate my-video.zip -v`
