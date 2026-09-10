> 2026-09-10 作品页面协议更新：新的 Creator 导出必须包含 Agent 生成且已审阅的 `presentation.html`。`presentation.json` 为 `{ "schema_version": 1, "format": "agent_html_css", "document": "presentation.html" }`。页面与抉择的全部视觉均来自生成的 HTML/CSS；不套用默认主题、首页、地图或结局模板。旧 `theme/screens/stylesheets` 字段仅用于解析历史包，新播放页不会用它们补齐作品，缺少生成页面时提示重新生成导出。
>
> `presentation.html` 包含四个独立 `data-screen="title|play|map|ending"` 区域；播放页声明 `video data-player-video`、空 `data-slot="interaction"`。按钮以 `data-action` 声明 start/resume/map/map_back/toggle_play/replay/title/jump/reset，jump 和地图节点用真实 `data-node-ref`。宿主绑定可信媒体 URL、动作、进度和文本，不补布局。未访问地图节点隐藏，jump 仅允许已访问节点。倒计时节点 `data-interaction-countdown` 由抉择 HTML 自行设计，宿主只更新数字。两种 HTML 均禁止脚本、事件属性、外部资源，播放器通过 CSP 与无脚本 iframe 隔离。
>
> `manifest.json.authored_html` 与 `presentation.html` 保存同一生成文档，offline/hosted/Creator 复用 `authored-player.js`。`content_revision` 隔离离线进度版本。旧模板已从生产导出器与项目播放器移除；测试夹具只用作 mock 合约实例，不被生产代码读取。

> 首页必须绑定 `project.title`、`project.synopsis`，结局页必须绑定 `node.title`、`node.synopsis`，使不同分支展示真实结局。绑定节点不能包含按钮、视频或交互容器。交互容器出现抉择时由宿主接管点击，避免透明容器将点击穿透至视频。设计预览提供 1280×720 桌面与 390×720 手机视口，按编辑面板宽度缩放；这些是设备尺寸，不是作品布局模板。

# IVB — Interactive Video Bundle 格式规范 v1

> 本文是**唯一权威**。它描述 Creator(生产端)导出的 zip 到底长什么样,以及放映端
> (`plugins/apps/qwenpaw-creator/player`)解析时**必须**接受什么、**必须**拒绝什么。
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
| 封面与背景 | 不单独导出封面/缩略图；可选交互背景帧以内联 data URI 保存，动效仍可叠在暂停的视频上 |

## 1. 目录结构

```
<bundle>.zip
├── manifest.json          # 必需：剧情、媒体映射和同一份 authored_html
├── presentation.json      # 新作品必需：声明 agent_html_css 文档路径
├── presentation.html      # 新作品必需：Agent 生成并审阅的完整作品界面
├── index.html             # Creator 导出的离线播放器，file:// 可打开
└── segments/<slug>.mp4    # 每个剧情节点一个成片文件
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
  "authored_html": "<!DOCTYPE html>...", // 新作品的已审阅页面
  "content_revision": "...",         // 含页面、剧情和视频校验和的内容指纹
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
  "synopsis": "创意简报正文"         // 可空字符串
}
```

`duration_estimate` / `rating` / `tags` / `cover` 在 v1 **不存在**(无生产端来源)。

### 2.3 `segments` 与 `nodes`

`segments` 是 `timeline_id -> 包内相对路径`。Creator 用 timeline ID 的 UTF-8
字节十六进制编码再加 `.mp4`，避免 `a:b` 与 `a_b` 碰撞，也兼容 file:// URL。
放映端按 manifest 中的字面路径读文件，不重新派生；重复 ZIP 成员或两个节点
引用同一分段路径均为致命错误 `DUPLICATE_MEMBER`。

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
  "source_timeline_id": "timeline:open",   // Creator 输出；旧包可缺省
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
  "at_seconds": 42.5,                     // 必需, 有限数且 >=0;越界拒绝
  "question": "你要拿走钥匙吗?",           // 必需, 非空
  "options": [                            // 必需, >=2 项, edge_ref 不得重复
    { "edge_ref": "edge:take_key", "hotspot": null },
    { "edge_ref": "edge:stay_put", "hotspot": null }
  ],
  "countdown_seconds": 10,                // 可选, >0;null = 不倒计时
  "default_edge_ref": "edge:stay_put",    // 开启倒计时则必需，且属于 options
  "motion_html": "<!DOCTYPE html>...",   // 新作品必需，Agent 生成的 CSS-only HTML
  "base_frame_data_uri": null             // 可选，PNG/JPEG/WebP data URI
}
```

`hotspot` 使用 Creator `ElementLocation`：`x/y/width/height/anchor_x/anchor_y`
均为 normalized_canvas 数值，另可带 `rotation_degrees/opacity`。宿主将位置、
尺寸、锚点、旋转与透明度应用到对应按钮；`null` 使用 Agent 生成的布局。

`at_seconds` 语义：分段播到该秒时暂停并弹出抉择。Creator 预览、离线 HTML
与放映端共用交互运行时；宿主拥有点击导航和倒计时。打开地图、切到后台时
暂停倒计时与 CSS 动画，返回后继续。问句和按钮文案以 manifest 为准。

生成 HTML 只允许安全的静态标记和 CSS，禁止脚本、事件属性、外链资源、
CSS url/import/转义及表单导航。每个选项必须恰有一个 `button[data-edge-ref]`。
问句文字节点使用 `data-question`，按钮文字使用 `data-option-label`。
浏览器额外使用禁脚本 iframe sandbox 和 CSP；分支目标始终由宿主读取边数据。
可选背景帧由 Creator 验证为已索引的 PNG/JPEG/WebP，最大 8 MB，再以内联数据导出。

### 2.6 顺序保证

一个节点最多一个启用的抉择点；选择后即离开本节点。连续问题需要拆成不同
节点。多个抉择点、非有限数、错误热区、外源边或不完整选项覆盖，均报
`INTERACTION_CONTRACT`。选项必须覆盖源节点全部出边，不能借用另一节点的边。

## 3. `tone` — 三档风险语义

`tone` 描述**抉择那一刻这条选择看起来的风险预期**,不是对结局好坏的回溯判定。
两轴判定:

| 档位 | 与角色目标的关系 | 代价 | 这一档的含义 |
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

### 表现方式

`tone` 只表示剧情语义，保留在边数据中。当前宿主不根据 tone 自动添加颜色、徽标或风险卡片；具体视觉由 Agent 按项目设计要求创作，不存在内置风险样式。

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

`AT_SECONDS_OUT_OF_RANGE` 为致命错误。Creator HTTP 导出还用 ffprobe 校验
实际视频流和时长，拒绝缺失、过期、来源选择不一致的成片及待审阅改动。
作品界面或交互动效缺失/过期也会阻断导出。`fallback=static_endcard` 不再触发固定选项模板；静态选项也必须生成并审阅 HTML。

告警级仍包括：放映端无法解析 `mvhd` 时长 (`SEGMENT_UNDURABLE`)、孤儿分段
(`SEGMENT_ORPHAN`)、`titles` 与节点标题不一致 (`TITLES_DIVERGED`)。

### 4.1 "分岔必须有抉择点"守卫

**存在 `children.length > 1` 的节点,但该 `source_timeline_id` 上没有任何
interaction → 包无效。**

理由:没有抉择点的分岔,播放器只能走 `children[0]`,其余分支永久不可达 ——
包看起来是互动的(地图有支线、导出有 ZIP),播起来是纯被动片。这种包
不能静默通过。诊断码 `BRANCH_WITHOUT_INTERACTION`。

### 4.2 结局

`children` 为空的节点即结局节点。结局数 = `nodes` 中 `is_ending` 为真的数量。
不引入独立的 `endings` 表(内容层),避免与状态层的解锁记录重名混淆。

## 5. 表现层 — Agent 生成的完整 HTML

新作品的 `presentation.json` 为：

```json
{
  "schema_version": 1,
  "format": "agent_html_css",
  "document": "presentation.html"
}
```

`presentation.html` 和 `manifest.authored_html` 必须完全一致。两者不一致、页面不安全、必备结构/动作缺失或地图引用错误时，Reader 拒绝包。历史包的 theme/screens/stylesheets 仍可解析，但播放器不再据此生成页面；缺少 authored HTML 时提示返回 Creator 重新生成。

四个互不嵌套的页面必须各有一个。以下是行为协议，不包含任何布局、颜色或按钮形态：

| 页面 | 必备控件 | 可选动作 |
|---|---|---|
| title 首页 | start、resume | map、reset |
| play 播放 | video[data-player-video]、空 data-slot=interaction、toggle_play、map、replay | title |
| map 剧情地图 | map_back、每个真实剧情节点的 data-node-ref | jump、title、reset |
| ending 结局 | replay、title | map、reset |

控件均以 `button[data-action]` 绑定。开始、重新开始与剧情地图不可因剧本省略。`replay` 从故事入口重新播放，保留探索记录；`reset` 清空探索记录回首页。未访问地图节点由宿主隐藏，`jump` 仅回看已访问节点。地图打开时暂停视频与倒计时。其余布局、文案、装饰、响应式样式与动效全部由生成 HTML 决定。

Creator 的 `interactive_presentation` 保存 `design_prompt`、逐页 `screens` 和生成结果 `motion`。`screens[title|play|map|ending]` 有独立的 `design_prompt` 与 `controls[action] = {label, design_prompt}`。这是 Agent 与前端共享的设计意图模型。显式按钮文案会在生成时校验，不满足时先尝试修正；必备按钮没有单独配置时仍由 Agent 设计。逐页或按钮修改使页面待重新生成，不会让视频、剧本过期。

抉择的 `creation.design_prompt` 描述该选择层，`options[*].design_prompt` 描述单个选项按钮。选项文案与走向仍来自 narrative_edges。前端“交互设计”提供四页实际预览、桌面/手机切换、按钮定位和抉择走向说明，保存设计后生成、审阅，再导出。

## 6. 状态层 — SQLite

见 `docs/er-diagram.md` §3。契约要点:

- 单文件 `state.db`,与包**分离存放**(包是只读分发物,状态是本地积累物)
- 进度按 `user_id` 与 project 隔离；用户身份来自服务受信任的请求头。覆盖上传还须匹配已有作品的 owner。
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

HTTP 导出持有项目生命周期锁，并在同一份快照上完成门禁与组包：

| 约束 | 实现位置 |
|---|---|
| 无待审阅改动、无排队/运行中的制作任务 | `interactive_bundle_routes._assemble` |
| live 剧情节点闭合、单根、全可达、无环，排除历史快照 | `derive_interactive_manifest` / `_validate_story_graph` |
| 每个节点选择了有效且当前的成片，选项完整覆盖出边 | `derive_interactive_manifest` |
| 媒体文件与索引校验和一致，实际视频流可读，抉择时间不超实际时长 | HTTP 导出 ffprobe 与来源检查 |
| 页面和抉择 HTML 已生成、未过期、安全且协议完整 | `assemble_interactive_bundle` / HTML validators |
| 必备页面、开始、重新开始、剧情地图，以及显式按钮文案完整 | `validate_presentation_html` |
| `presentation.html` 与 manifest 内 HTML 一致 | 导出单一数据源与 Reader 双向校验 |

Agent 的 `workspace_schema.system.txt` 与自动生成的 Project Schema 同步提供上述设计字段、行为语义、任务目标、审阅和导出门禁。`bundle:project` 的 DONE 表示导出准备完成，不代表已发布；不得为等待不存在的自动出包任务而反复修改快照。

跨端回归 `player/tests/test_creator_contract.py` 直接调用 Creator 导出器，把产物交给 Reader / Server 验证。独立播放器和 Creator 共享同一份行为控制器与镜像 HTML 校验器；编辑器的定位边框只存在于审阅预览，不写入导出作品。
