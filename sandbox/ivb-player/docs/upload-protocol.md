# IVB 上传接口

## 端点

```
POST /api/projects
Content-Type: multipart/form-data
```

## 请求

| 字段 | 位置 | 类型 | 说明 |
|---|---|---|---|
| `file` | form | zip | Creator 导出的互动视频包 |
| `user_id` | header | string | 用户标识，用于进度隔离 |

## 响应

**成功 201**

```json
{
  "ok": true,
  "project_id": "project-01H...",
  "title": "深夜便利店",
  "node_count": 7,
  "ending_count": 3,
  "interaction_count": 4
}
```

**校验失败 422**

```json
{
  "ok": false,
  "diagnostics": [
    { "code": "MANIFEST_MISSING", "severity": "fatal", "message": "包内缺少 manifest.json" }
  ]
}
```

**其它**：400（非 zip）、401（缺 user_id）、413（超大小限制）、500（内部错误）

## 行为

- 校验通过 → 落盘 `bundles/{project_id}/`，写目录表，返回 201
- 校验失败 → 返回 422 + 诊断，不入库
- 同 `project_id` 重复上传 → 覆盖包体，更新目录，进度保留
