# IVB — Interactive Video Bundle 格式规范 v1

> 本文是**唯一权威**。它描述 Creator(生产端)导出的 zip 到底长什么样,以及放映端
> (`sandbox/ivb-player`)解析时**必须**接受什么、**必须**拒绝什么。
>
> `sandbox/互动视频/schema.json` 是早期形式定义,与任何一份真实实现都不符
> (它连 demo 自己的 manifest 都校验不过),保留仅作历史参考,**不再维护**。

## 0. 设计约束

| 约束 | 含义 |
|---|---|
| 自包含 | 包内不含任何外链资源;不依赖 Producer 的运行时、数据库、账户体系 |
| 结构不可变 | 包一旦产出就不再被改写;放映端只读 |
| 版本可判定 | `manifest.schema_version` 是整数,放映端用**区间**判断,不要求精确相等 |
| 双向可诊断 | 结构错误必须能定位到具体 id/文件/字段,不允许"加载失败" |
| 无图片资产 | v1 不含封面/缩略图。选项卡是文字卡。`thumbnail`/`cover` 字段**不存在**,不留空声明 |

## 1. 目录结构

```
<bundle>.zip
├── manifest.json          # 必需  内容层 + 表现层默认值(唯一事实源)
├── presentation.json      # 可选  表现层覆盖;缺失 → 放映端内置默认
├── index.html             # 可选  Creator 内置的免服务播放器(file:// 双击即播)
├── segments/<slug>.mp4    # 必需  每个分段一个成片文件
└── styles/<name>.css      # 可选  presentation.stylesheets 指向的自定义样式
```

`manifest.json` 与 `index.html` 内嵌的是**同一份 payload**。放映端只读
`manifest.json`(不解析 HTML),因此两者必须一致 —— 这条由 Creator 侧
单一代码路径保证,放映端不校验。

**路径收敛**:包内所有路径都是相对包根的 POSIX 风格路径。放映端解析前必须
`resolve()` 并断言结果仍在包根之下,拒绝 `..`、绝对路径、反斜杠。

## 2. 内容层 — `manifest.json`

### 2.1 顶层

```jsonc
{
  "schema_version": 1,              // 必需, 整数。放映端接受 [1, SUPPORTED_MAX]
  "entry_timeline_id": "timeline:open",   // 必需, 必须出现在 nodes 中
  "segments": { ... },              // 必需, 非空
  "nodes": { ... },                 // 必需, 非空
  "interactions": [ ... ],          // 必需, 可为空数组(见 §2.5 的守卫)
  "edge_index": { ... },            // 必需, 分支型项目非空
  "titles": { ... },                // 兼容字段, 见 §2.4
  "meta": { ... }                   // 必需
}
```

`timeline_id` / `edge_id` 的字形:`^[a-z][a-z0-9_-]*(:[A-Za-z0-9_ -]+)?$`
(Creator 侧 `EntityId` 的实际约束)。放映端**不**按 `:` 切分来推断类型,一律当不透明字符串。

### 2.2 `meta`

```jsonc
{
  "bundle_id": "project-01H...",    // 必需, 稳定;状态层按它隔离进度
  "title": "深夜便利店",             // 必需
  "tagline": "第一行项目描述",       // 可空字符串
  "synopsis": "创意简报正文",        // 可空字符串
  "accent": "#b8ff2e"               // 必需, 3 位或 6 位 hex;被 presentation 覆盖
}
```

`duration_estimate` / `rating` / `tags` / `cover` 在 v1 **不存在**(无生产端来源)。

### 2.3 `segments` 与 `nodes`

`segments` 是 `timeline_id -> 包内相对路径`。路径由 `timeline_id` 把 `:` 换成 `_`
再加 `.mp4` 派生,但放映端**按字面路径读文件**,不重新派生。

`nodes` 是 `timeline_id -> 节点对象`,承载故事地图与 DAG 邻接:

```jsonc
"timeline:open": {
  "title": "序章",                  // 可空;空则回退 timeline_id
  "synopsis": "",                   // 可空
  "children": ["timeline:a", "timeline:b"],  // 出边目标, 去重且保序
  "is_ending": false                // 严格等价于 children 为空
}
```

`nodes` 的键集合必须**恰好等于** `segments` 的键集合(Creator 侧 `_node_index`
即以此构造)。

### 2.4 `edge_index` 与 `titles`

`edge_index` 是 `edge_id -> 边对象`。边是选项文案的**单一事实源**:

```jsonc
"edge:take_key": {
  "label": "拿走钥匙",              // 选项显示文字, 必需字段(可空串)
  "prompt": "你听见门锁响了",        // 辅助文案, 必需字段(可空串)
  "target_timeline_id": "timeline:a",      // 必需, 必须命中 nodes
  "tone": "risky"                   // 可选;缺省 = 中性卡, 见 §3
}
```

`titles` 是 `timeline_id -> title` 的扁平表,与 `nodes[*].title` 同值。它的存在只为
让旧播放器不改代码就能读新包,放映端**优先读 `nodes`**,仅在 `nodes` 缺失时回退。
新代码不得依赖它。

### 2.5 `interactions`

抉择点数组。每项:

```jsonc
{
  "source_timeline_id": "timeline:open",  // 必需, 必须命中 nodes
  "at_seconds": 42.5,                     // 必需, >=0;应小于分段时长(越界只告警)
  "question": "你要拿走钥匙吗?",           // 必需, 非空
  "options": [                            // 必需, >=2 项, edge_ref 不得重复
    { "edge_ref": "edge:take_key", "hotspot": null },
    { "edge_ref": "edge:stay_put", "hotspot": null }
  ],
  "countdown_seconds": 10,                // 可选, >0;null = 不倒计时
  "default_edge_ref": "edge:stay_put"     // 可选;倒计时耗尽时走的边, 必须是 options 之一
}
```

`hotspot` 是 `{x, y, w, h}` 归一化矩形(`normalized_canvas`),用于热区点击;
`null` = 由播放器自动布局成卡片列表。v1 播放器**只实现列表布局**,
`hotspot` 原样保留、不解释。

`at_seconds` 语义:分段播到该秒时暂停并弹出抉择。抉择点在分段末尾时
(Creator 当前把所有 interaction 元素放在 timeline 尾部)等价于"看完再选"。

### 2.6 顺序保证

同一 `source_timeline_id` 内的抉择点必须按 `at_seconds` 升序;同秒按 `options[0].edge_ref`
字典序。Creator 侧排序键即 `at_seconds`,放映端不重排,遇到乱序直接判
`INTERACTION_ORDER_UNSTABLE`。

## 3. `tone` — 三档风险语义

`tone` 描述**观众读到选项时的心理预期**,不是结局好坏的事后标注。两轴判定:

| 档位 | 与角色目标的关系 | 代价 | 观众读到的是 |
|---|---|---|---|
| `safe` | 一致 | 可逆 / 无显著代价 | 常识选择,用来推剧情 |
| `risky` | **一致**(仍是角色想做的事) | **未知或偏高,但可承受** | 赌一把 |
| `danger` | **相悖** 或明知故犯 | **不可逆 / 大概率坏结局** | 作死 |

判定口诀(写给生产端的模型):

> 犹豫 `risky` 还是 `danger` 时问一句:"这个选择**几乎必然**带来坏结果吗?"
> 是 → `danger`;只是"可能要出事" → `risky`。

规则:

- 取值域封闭为 `safe | risky | danger`,大小写敏感,其他值判 `TONE_UNKNOWN`
- **缺省合法**(中性卡)。不要求生产端必须标注
- tone 挂在**边**上,不挂在选项上。选项只 `edge_ref`;渲染时取
  `option.tone ?? edge.tone`,而 `option.tone` 在 v1 不存在 → 事实单一来源是边
- 同一抉择点内三档可混用,不强制齐档

## 4. 图结构约束(DAG)

互动视频的故事图必须是**以 `entry_timeline_id` 为根有向无环图(DAG)**。
以下均为**致命**错误,放映端必须拒绝加载,Creator 必须拒绝导出:

| 规则 | 诊断码 |
|---|---|
| 无环:从任一节点出发不可回到自身 | `CYCLE_DETECTED` |
| 单根:除 entry 外,不可存在其他入度为 0 的节点 | `MULTIPLE_ROOTS` |
| 可达:`nodes` 中每个节点都从 entry 可达 | `UNREACHABLE_NODE` |
| 引用闭合:`children[*]` ⊆ `nodes.keys()` | `UNKNOWN_CHILD` |
| 引用闭合:`edge_index[*].target_timeline_id` ⊆ `nodes.keys()` | `EDGE_TARGET_UNKNOWN` |
| 引用闭合:`option.edge_ref` ⊆ `edge_index.keys()` | `EDGE_REF_UNRESOLVED` |
| `default_edge_ref` ∈ 同一抉择点的 `options[*].edge_ref` | `DEFAULT_EDGE_INVALID` |
| 分段文件真实存在且非空 | `SEGMENT_MISSING` |
| 选项数 ≥ 2 | `TOO_FEW_OPTIONS` |
| `is_ending == (children 为空)` | `ENDING_FLAG_MISMATCH` |

**告警级**(包仍可放映):`at_seconds` 超出分段探测时长
(`AT_SECONDS_OUT_OF_RANGE`)、分段 `mvhd` 无法解析出时长
(`SEGMENT_UNDURABLE`)、孤儿分段(`SEGMENT_ORPHAN`)、`titles` 与
`nodes[*].title` 分叉(`TITLES_DIVERGED`)。理由:成片真实长度由 compose
决定,导出前生产端无法可靠预算,拿它废包只会误杀。

### 4.1 "分岔必须有抉择点"守卫

**存在 `children.length > 1` 的节点,但该 `source_timeline_id` 上没有任何
interaction → 包无效。**

理由:没有抉择点的分岔,播放器只能走 `children[0]`,其余分支永久不可达 ——
包看起来是互动的(地图有支线、导出有 ZIP),播起来是纯被动片。这种包
不能静默通过。诊断码 `BRANCH_WITHOUT_INTERACTION`。

### 4.2 结局

`children` 为空的节点即结局节点。结局数 = `nodes` 中 `is_ending` 为真的数量。
不引入独立的 `endings` 表(内容层),避免与状态层的解锁记录重名混淆。

## 5. 表现层 — `presentation.json`(可选)

目的:**UI 样式不硬编码在 HTML 里**。放映端内置一套默认样式,包可用本文件覆盖。
文件缺失 → 全部用内置默认,不产生诊断告警以外的任何错误。

```jsonc
{
  "schema_version": 1,                  // 必需
  "theme": {                            // 全部可选, 缺项回退内置
    "accent":  "#b8ff2e",               // 主色(荧光绿)
    "danger":  "#ff3355",               // 危险色, tone=danger 边框
    "warning": "#ffb547",               // tone=risky 边框
    "success": "#5fd68a",               // tone=safe 边框
    "background": "#05070a",            // 最底色
    "surface": "#0a0d11",               // 卡片底
    "surface_alt": "#11161c",
    "text": "#e8f0d8",
    "text_dim": "#7d8a72",
    "fog": "#1a222b"                    // 故事地图未解锁色
  },
  "screens": {                          // 全部可选
    "title":   { "cta_label": "开始游戏", "secondary_label": "剧情地图" },
    "choice":  { "layout": "list",       // v1 只接受 list
                 "badge_labels": { "safe": "○ 稳妥",
                                   "risky": "△ 冒险",
                                   "danger": "✕ 危险" } },
    "map":     { "reveal_depth": 1 },    // 已解锁节点的下游展开深度
    "ending":  { "show_review": true }   // 结局页展示已走过的抉择
  },
  "stylesheets": ["styles/choice.css"]  // 包内相对路径, 追加于内置样式之后
}
```

诊断级别(全部为 `warning`,不阻断播放):
`PRESENTATION_UNREADABLE` / `PRESENTATION_VERSION_UNSUPPORTED` /
`THEME_COLOR_MALFORMED` / `STYLESHEET_MISSING` / `SCREEN_FIELD_UNKNOWN`。

`variant`(按节点覆盖控件形态)在 v1 **不实现**:demo 的渲染器全文零次读取它,
且它的 key 在播放器里就被自己拍平废掉了。规范不收录未被任何实现验证过的接口。

## 6. 状态层 — SQLite

见 `docs/er-diagram.md` §3。契约要点:

- 单文件 `state.db`,与包**分离存放**(包是只读分发物,状态是本地积累物)
- 所有表带 `user_id`,v1 恒为 `1`,列**保留不删** —— 将来加回鉴权不迁库
- 没有 `users` / `tokens` 表
- `variables` 表**不建**(无 Condition 实现,建了就是死表)
- 清空进度必须是真 `DELETE`,不允许用"把 current_node 写成空串"冒充

## 7. 版本策略

- **兼容性定义**:`schema_version` 的**主语义**由本文固定。加字段 = 版本号不变
  (放映端必须忽略未知字段);改语义 / 删字段 / 改必填 = 版本号 +1
- 放映端维护 `MIN_SUPPORTED` / `SUPPORTED_MAX` 两个常量,报
  `MANIFEST_VERSION_UNSUPPORTED` 时**同时给出自己的支持区间**
- `presentation.json` 的版本独立编号

## 8. Creator 侧导出前置条件

导出前 Creator 必须校验(不通过则拒绝出包并点名)。已实现部分:

| 约束 | 实现位置 |
|---|---|
| 每个可达分段都有已选定成片 | `derive_interactive_manifest` 的 `missing` 门禁 |
| `option.edge_ref` 必须命中已有边 | `derive_interactive_manifest` 的 `known_edges` 门禁 |
| 选项数 ≥ 2 | `_validate_story_graph` |
| 无环 | `_validate_story_graph`(报环路径 `a -> b -> a`) |
| 分岔节点必须有抉择点(§4.1) | `_validate_story_graph` |
| 产出 `presentation.json` | `assemble_interactive_bundle` → `_presentation_payload` |

其余 §4 约束在 Creator 侧**由构造保证而不是靠检查**:`nodes` 只从
`manifest.segments`(= 从入口可达集)生成,`children` 由 `narrative_edges`
现拼,所以全可达 / 单根 / 引用闭合 / `is_ending == (children 为空)` 不可能
被违反。这也是环需要单独拦的原因 —— 它是唯一“构造允许、但是错的”形状。

`at_seconds` 是否落在成片时长内**不是**导出门禁:Creator 只能拿到
`planned_duration_seconds`,真实长度由 compose 决定。它属于放映端的告警。

结构起草提示词(`workspace_schema.system.txt`)必须同步教三档语义:
`edge_index[*].tone` 是模型在起草 `narrative_edges` 时填的,不教就恒空,
三档表现层形同不存在。

跨端回归:`sandbox/ivb-player/tests/test_creator_contract.py` 直接调用 Creator
导出器,把产物交给放映端 Reader / Server 验证零诊断。两侧字段名、默认值、
badge 文案任何一处飘移都会在那里红 —— 这就是“校验规则只实现一份”的机制。
