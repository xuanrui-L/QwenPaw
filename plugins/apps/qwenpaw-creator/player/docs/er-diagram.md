# IVB Schema ER 图

字段命名**严格对齐 `docs/bundle-format.md`**,即 Creator 真实产出的形状
(`nodes` / `edge_index` / `*_timeline_id`,不是早期 `schema.json` 的
`edges` / `source_node_id`)。

参考了 `sandbox/互动视频/er-diagram.md`,并按 v1 定稿做了三处删减:
去掉 `users` / `tokens`(不做鉴权)、去掉 `variables`(无 Condition 实现)、
去掉 `cover` / `thumbnail`(包内无图片资产)。

## 0. 全景

```mermaid
graph LR
    P["Creator<br/>project.json"] -->|derive + 打包| Z["Bundle zip<br/>只读分发物"]
    Z --> R["Reader<br/>解析 + 校验"]
    R --> A["FastAPI<br/>内容端点 + 进度端点"]
    A --> U["播放器前端<br/>四屏 + 状态机"]
    U -->|进度写| S[("state.db<br/>本地可写")]
    R --> V["/api/validate<br/>诊断报告"]
```

三层通过 `bundle_id` 关联。**包只读、库可写**,两者物理分离。

---

## 1. 内容层 — manifest.json

```mermaid
erDiagram
    manifest ||--|| meta : "元信息"
    manifest ||--o{ nodes : "故事节点"
    manifest ||--o{ segments : "分段成片"
    manifest ||--o{ interactions : "抉择点"
    manifest ||--o{ edge_index : "边(选项事实源)"

    nodes ||--o{ nodes : "children 邻接(DAG)"
    nodes ||--|| segments : "一一对应"
    nodes ||--o{ interactions : "承载于"
    nodes ||--o{ edge_index : "作为目标"

    interactions ||--o{ interaction_options : ">=2 个选项"
    interaction_options }o--|| edge_index : "edge_ref"
    edge_index }o--|| nodes : "target_timeline_id"

    manifest {
        int  schema_version     "固定 1"
        string entry_timeline_id FK "唯一入口节点"
    }

    meta {
        string bundle_id PK "进度隔离键"
        string title
        string tagline
        string synopsis
        string accent "3/6 位 hex"
    }

    nodes {
        string timeline_id PK "不透明 id"
        string title
        string synopsis
        bool is_ending "= children 为空"
    }

    segments {
        string timeline_id PK,FK
        string path "包内相对路径"
    }

    interactions {
        string source_timeline_id PK,FK "所属节点"
        float  at_seconds PK "触发时刻, < 分段时长"
        string question
        float  countdown_seconds "可选, >0"
        string default_edge_ref FK "可选, ∈ options"
    }

    interaction_options {
        string edge_ref FK "指向边"
        json   hotspot "归一化矩形, 可空"
    }

    edge_index {
        string edge_id PK
        string label "选项文字"
        string prompt "辅助文案"
        string target_timeline_id FK
        string tone "safe|risky|danger, 可缺省"
    }
```

`titles` 表未画出:它是 `nodes[*].title` 的扁平副本,仅为旧播放器兼容保留,
新代码不得依赖。

---

## 2. 表现层 — presentation.json(可选)

```mermaid
erDiagram
    presentation ||--|| theme : "配色"
    presentation ||--o{ screens : "四屏行为"
    presentation ||--o{ stylesheets : "追加样式"
    screens ||--o| badge_labels : "tone 文案"

    presentation {
        int schema_version "独立编号"
    }

    theme {
        string accent
        string danger
        string warning "tone=risky"
        string success "tone=safe"
        string background
        string surface
        string surface_alt
        string text
        string text_dim
        string fog "地图未解锁色"
    }

    screens {
        string name PK "title|choice|map|ending"
        string cta_label "title"
        string layout "choice: v1 仅 list"
        int  reveal_depth "map: 下游展开深度"
        bool show_review "ending"
    }

    badge_labels {
        string tone PK "safe|risky|danger"
        string label
    }

    stylesheets {
        string path PK "包内相对路径"
    }
```

**全部字段可选**;缺任一项 → 放映端内置默认回填,只出 `warning`。
`stylesheets` 的 CSS 内容不进 JSON,按路径从包里读文本后注入 `<style>`。

---

## 3. 状态层 — state.db(SQLite)

```mermaid
erDiagram
    progress ||--o{ visits : "路径明细"
    progress ||--o{ endings : "解锁结局"
    progress ||--o{ choice_stats : "抉择分布"

    progress {
        int    user_id PK "v1 恒为 1"
        string bundle_id PK
        string current_timeline "断点续播位置"
        int    started_at
        int    updated_at
    }

    visits {
        int    id PK
        int    user_id FK
        string bundle_id FK
        string timeline_id "走过的节点"
        string choice_edge "进入本节点所走的边, 入口为 NULL"
        float  watched_seconds "timeupdate 累计"
        int    entered_at
    }

    endings {
        int    user_id PK,FK
        string bundle_id PK,FK
        string timeline_id PK,FK "nodes.is_ending 之一"
        int    unlocked_at
    }

    choice_stats {
        int    user_id PK,FK
        string bundle_id PK,FK
        string interaction_source PK "source_timeline_id"
        string edge_ref PK,FK
        int    count
        int    last_chosen_at
    }
```

### 3.1 为什么保留 `user_id`

列在、值恒 `1`、**不建 `users` / `tokens` 表**。将来加回多用户只需补鉴权与
外键,不需要迁移既有 `state.db`。

### 3.2 派生指标(不建表,查询即得)

| 指标 | 计算 |
|---|---|
| 覆盖率 | `COUNT(DISTINCT visits.timeline_id) / COUNT(nodes)` |
| 结局进度 | `COUNT(endings) / COUNT(nodes WHERE is_ending)` |
| 当前路径 | `visits` 按 `entered_at` 排序的 `timeline_id` 序列 |
| 抉择偏好 | `choice_stats` 按 `interaction_source` 分组的 `count` 占比 |

`variables` / `Condition` 不设计:内容层没有任何字段消费它,建表即死表。
