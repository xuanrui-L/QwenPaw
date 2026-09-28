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
| `X-User-Id` | header | string | 已认证用户标识,落 `owner_user_id`。**必须由可信上游注入**,ivb 不校验其真伪 |

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
- 同 `project_id` 重复上传 → 覆盖包体，更新目录，进度保留（**不改 `owner_user_id`**，首次上传者恒为 owner）

## 前置条件（调用方/平台负责）

本文只定义接口本身。ivb 不鉴权，只消费 `X-User-Id`；这个值需要由可信上游依据已认证结果
决定，否则进度隔离会**静默失效**（ivb 不报错）。具体如何达成由平台决定，背景与建议见
`platform-integration.md`，ivb 侧契约见 `service-design.md` §10。

另有两个与链路容量相关的事实请平台评估：单个包实测约 **136MB**，以及放映依赖
`206 Partial Content` 的 Range 分段请求。

典型调用方是 Creator 后端的发布代理（浏览器不直连 ivb）：它在宿主鉴权后取到
已认证用户，服务端生成 zip 再转发本接口，并带上 `X-User-Id`。
