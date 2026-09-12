# Creator「项目蓝图」层改造方案

> 配套演示：`creator-blueprint-demo.html`（仓库根目录，纯静态 mock，视觉与交互为目标态）
> 本文档给出从当前实现到目标态的完整改造路径：信息架构、数据模型、后端、前端、迁移与分期。
> 文内路径均相对 `plugins/apps/qwenpaw-creator/`。

---

## 1. 背景与目标

当前 Creator 的用户动线是 `首页 → 视频方案(PlanPage) → 制作工作台`，存在四个结构性缺口：

1. **叙事结构层缺失**：`Project.timelines` 是集合（`backend/services/project_files/models.py:1288`），但前端只消费首条（`ui/src/selectors/timelineElementSelectors.ts:27` `selectPrimaryTimeline`）。多集、分支剧情在域模型与 UI 上都不存在。
2. **剧本不可审阅**：分集剧本没有独立的文本形态与展示位，用户无法在生产开始前审改剧本——而这是人机协作的核心环节。
3. **前置过程不可见**：素材理解（`backend/services/source_analysis/`）、网页调研（`backend/services/web_grounding/`）、视觉开发（`VisualDevelopment`，models.py:597）的产物散落在 AssetsPage 平铺和 AgentDock 面板里，用户感知不到「先有理解/设计，才有分镜」的因果链。
4. **层级并列错误**：视频方案与资产库并列于顶部导航，但视频方案实际是某条时间线的详情页。

目标信息架构（四层，全场景统一心智模型）：

```
项目蓝图 (Blueprint)              ← 新增，项目首屏
 ├─ 结构区（按项目形态自适应：分支图 / 剧集列表 / 流程条）
 ├─ 剧本审阅面板（选中节点的文本剧本，可编辑、审阅通过）
 ├─ 视觉开发区（角色/场景设计卡 → 详情抽屉）
 └─ 调研与素材区（素材理解 / browser-use 调研 → 详情抽屉）
     └─ 时间线编辑 (PlanPage，参数化到节点)     ← 由蓝图下钻，不再是平级 Tab
         └─ 制作工作台 (R2VWorkbenchPage)
资产库 (AssetsPage)               ← 保留为全量检索视图，改按归属分组
```

统一交互范式：**每层都是「Agent 起草 → 用户审阅/微调 → 通过后下游推进；修改则级联标记下游 stale」**。

---

## 2. 数据模型变更（project_files/models.py，schema v8 → v9）

> 原则：Creator 是 data model 驱动的，改造遵循**先复用、后扩展**——优先套用既有通用机制
> （`EntityCollection` 的 order、`ArtifactSlot/ArtifactVersion` 的版本/选中/stale/溯源、
> `Review` 审阅、`provenance_refs`），只在"结构关系"无法表达时加最小字段。
> 最终新增仅为：**Timeline 3 个可选字段 + Project 1 个边列表 + ArtifactSlot 2 个 kind 枚举值**。

### 2.1 分集：复用 `timelines` EntityCollection（零新模型）

- **一个叙事节点 = 一条 Timeline**。顺序即 `timelines.order`（既有字段）；单视频项目 = 单条 timeline，也就是现状——存量项目天然合法，无需构造任何"图"。
- `Timeline` 增加 3 个可选展示字段：`title: str = ""`、`synopsis: str = ""`、`planned_duration_seconds: float | None`。不新建 NarrativeNode 模型。

### 2.2 分支：唯一的结构性新增（仅分支项目携带）

```python
class NarrativeEdge(BaseModel):
    edge_id: str
    source_timeline_id: str
    target_timeline_id: str
    label: str = ""        # 「选择A · 揭发真相」
    prompt: str = ""       # 抉择问题文案（同一 source 的多条边共享）

class Project(BaseModel):
    ...
    narrative_edges: list[NarrativeEdge] = []   # 新增，默认空
```

结构形态完全由既有数据推导，不加枚举、不加确认布尔：
- `len(timelines) == 1` → 单视频（生产看板形态）
- `len(timelines) > 1 and not narrative_edges` → 线性多集（剧集列表形态）
- `narrative_edges 非空` → 分支图形态
- "结构已确认"复用 checkpoint/Review 的既有状态源，不在 Project 上冗余存储。

### 2.3 剧本：复用 ArtifactSlot/ArtifactVersion（只加一个 kind 值）

`ArtifactSlot.kind` 的 Literal 增加 `"timeline_script"`，`owner_ref = "timeline:<id>"`；
`ArtifactVersion` 指向 AssetIndex 中的 **markdown 文件**。由此免费获得整套既有机制：
多版本 + `selected_version_id` + `stale/stale_reason` + `provenance_refs` + Review 审阅 +
`input_fingerprint` 防重算。

剧本内容用**约定格式 markdown**，不新建 ScriptBlock schema：
- **统一概念**：口播文案、剪辑脚本不是独立概念，它们与剧集剧本是同一个 `timeline_script`，只是**体裁**不同（场次体 / 口播体 / 剪辑体）。体裁不入库为枚举，由内容形态自然体现，前端渲染器按块类型分支即可；审阅、版本、stale、入口在四类场景中完全一致。
- 场次头：`## 场 1 · 内景 · 旧宅大厅 · 夜`；台词：`**林晚**（低声）：……`；钩子：引用块；
- 素材时间码引用（剪辑场景）：链接约定 `[访谈A 01:02:13–01:02:21](source-version://<id>?in=3733&out=3741)`，前端解析为可点击回看 chip，反向链接 SourceIntelligence；
- 剧集剧本 / 口播文案 / 剪辑脚本三类共用同一约定，前端只是渲染器不同分支。

Shot/分镜作为剧本的派生物，其 `provenance_refs` 指回 script 的 ArtifactVersion——剧本改动沿既有 stale 链自动标记下游。

### 2.4 调研发现：同样复用 artifact 机制（再加一个 kind 值）

`ArtifactSlot.kind` 增加 `"research_report"`，`owner_ref` 为 project 或 visual-entity。
报告本体是 markdown（结论 + 逐页来源清单）；browser-use 截图 / 参考图作为普通 file +
ArtifactVersion 入 AssetIndex，用 `provenance_refs` 与报告互链；"结论注入了哪些设计"
由被注入实体（VisualVariant 等）的 `provenance_refs` 反向指向 research version——全部是既有字段。
素材理解不动：`SourceIntelligenceVersion`（models.py:237）原样消费。

### 2.5 新增 vs 复用一览

| 能力 | 复用 | 真正新增 |
|---|---|---|
| 分集/顺序 | timelines EntityCollection + order | Timeline.title/synopsis/planned_duration |
| 分支 | — | Project.narrative_edges |
| 剧本（含版本/审阅/stale） | ArtifactSlot/Version + Review + provenance | kind 枚举 +"timeline_script" |
| 调研报告（含截图溯源/注入去向） | 同上 + AssetIndex 文件 | kind 枚举 +"research_report" |
| 素材理解 | SourceIntelligenceVersion | 无 |
| 结构确认 | checkpoint/Review 状态 | 无 |
| 抉择交互 element | ElementCreation 判别联合（既定扩展点）+ MotionSpec(html_css) + Location 热区 | InteractionCreation（见 2.7） |
| 互动包导出 | AssetIndex 文件 + provenance stale 链 | InteractiveManifest + kind "interactive_bundle"（见 2.7） |

### 2.7 抉择交互与互动导出：两个**必要的**新模型

抉择点不只是"被展示"，它是**需要被生产的可交互产物**：观众要真实点击选项，
因此最终导出物**不是 mp4**，而是一个互动格式。这是全方案中唯二"复用无法覆盖、
必须新增结构"的地方（分支边之外）。

**(a) InteractionCreation —— 新的 ElementCreation 联合成员**

Element 判别联合（models.py:1092，R2V/T2V/Edit/Overlay/…）本身就是架构预留的扩展点，
新增一个 creation type 即可让抉择交互获得元素系统的全部能力（时间线挂载、span、
workbench 编辑、Review、版本、stale）：

```python
class InteractionOption(BaseModel):
    edge_ref: str                       # 指向 Project.narrative_edges.edge_id：
                                        # 选项文案 / 目标集由边派生，单一事实源，改边即改选项
    hotspot: Location | None = None     # 点击热区，复用既有 normalized_canvas Location

class InteractionCreation(BaseModel):
    type: Literal["interaction"]
    question: str
    options: list[InteractionOption]
    countdown_seconds: float | None = None
    default_edge_ref: str | None = None # 倒计时超时的默认走向
    base_frame_ref: str | None = None   # 承接帧：上一片段 element_video 末帧（artifact ref）
    motion: MotionSpec                  # 复用既有 html_css 动效规格（html/fps/loop/design_notes）
    fallback: Literal["static_endcard", "split_publish"] = "split_publish"
```

挂载方式：作为源集 timeline 末尾的一个 element（span 循环 4-6s）。生产任务
`interaction_draft` 基于承接末帧生成 html_css 动效；用户在蓝图抉择节点详情 /
workbench 中编辑问题、选项、热区、动效 prompt，并可**真实点击试玩**（html 预览
本身可交互）。

**(b) InteractiveManifest + interactive_bundle —— 互动导出产物**

分支项目的"最终成片"是一个**互动包**，不是单一视频：

```python
class InteractionPoint(BaseModel):
    source_timeline_id: str
    at_seconds: float                   # 在该段成片中的触发时刻
    question: str
    options: list[InteractionOption]    # 含 target_timeline_id（由 edge 解析）
    countdown_seconds: float | None
    default_edge_ref: str | None

class InteractiveManifest(BaseModel):
    schema_version: int = 1
    entry_timeline_id: str
    segments: dict[str, str]            # timeline_id -> 该段成片 ArtifactVersion ref
    interactions: list[InteractionPoint]
```

- `ArtifactSlot.kind` 增加 `"interactive_bundle"`（owner_ref = project）；版本内容 =
  manifest.json + 自托管 HTML5 互动播放器 + 分段 mp4，文件全部走 AssetIndex；
  manifest 的 `provenance_refs` 指向各分段成片版本——**任何一集重生成，互动包沿既有
  stale 链自动标记过期**，组装门禁进入 work_graph（全部分支 done + interaction done → bundle ready）。
- 三种导出形态由同一 manifest 派生：① 自托管互动 HTML（zip，可直接分发/嵌入）；
  ② 互动平台容器（如抖音互动组件）—— manifest 转译为平台元数据；③ 降级线性 mp4 ——
  每条路径导出一部 + 按 `fallback` 策略（片尾静帧引导 / 分链发布）。

### 2.8 新增 vs 复用（含交互后的最终口径）

真正的新模型共四处，其余全部复用：`Timeline` 3 个可选展示字段、`Project.narrative_edges`、
`InteractionCreation`（element 联合成员）、`InteractiveManifest` + 3 个 ArtifactSlot kind 枚举值
（timeline_script / research_report / interactive_bundle）。

### 2.9 迁移（v8 → v9）

改动面极小，迁移近乎零成本：
- Timeline 新字段全部可选，默认空；存量单 timeline 项目自动呈现为"单视频看板"形态。
- `narrative_edges` 默认 `[]`；三个新 kind 值与新 creation type 只影响 Literal 校验（向后兼容）。
- 旧项目**不做剧本回填、不新增剧本 artifact**：一律按单节点（单视频生成 / 剪辑）形态呈现，剧本面板显示由既有数据映射的只读概要（`strategy.creative_brief`、各 element 的 intent/narrative、Shot 镜头表、EditPlan），并标注「本项目创建于剧本功能之前」。
- 旧路由 `/project/:id/plan` 重定向到主 timeline 的参数化路由（前端处理，见 5.1）。

---

## 3. 后端改造

### 3.1 检查点流（file_agent_runtime/checkpoints.py）

现状：项目级 `plan → design → direction` 三个硬检查点。改为两级：

```
structure   项目级（新增）：分集/分支结构草案 = timelines.order + narrative_edges + 各 Timeline 的 title/synopsis
script      节点级（新增）：单 Timeline 的 timeline_script artifact —— 复用既有 Review-on-artifact 机制，无新审阅通道
design      保持项目级：角色/场景设计跨节点共享（阵容锚机制不变）
plan/direction  收窄为节点级：作用域绑定 timeline_id
```

- **审阅模式配置驱动**：任何检查点是否需要人工确认，由项目级"审阅模式"配置决定（confirm / auto(yolo)）。yolo 下 structure/script/design 全部静默通过、生产授权直接放行；confirm 下按上表逐点确认。线性多集、图片生成确认、批量授权均从此配置派生，不做独立开关。
- 单视频项目 `structure` 自动通过（单节点无需确认），用户感知不到该检查点——保证简单场景零额外成本。
- Review 的 `ui_locator` 增加 `page: "blueprint"` 与 `nodeId` 字段，前端 `navigateToLocator` 据此深链到蓝图并选中节点（`ui/src/routing/locators.ts`）。

### 3.2 工作图（file_agent_runtime/work_graph.py）

- 节点增加 `timeline_id`；lane 生成从 `visual / lineup / element / compose` 扩展为 `visual / lineup / <Timeline 标题>×N / compose×N`（每条 timeline 有自己的 element lanes 和 compose 节点）。
- 新增节点 kind 直接映射新 artifact kind：`timeline_script`（剧本起草）、`research_report`（调研）、`source_intel`（素材理解），让前置过程进入同一张 DAG——AgentDock 的 WorkGraphPanel（`ui/src/components/agent/WorkGraphPanel.tsx`）无需大改即可按 lane 分组显示；蓝图页的"正在进行"活动条与节点卡进度条共用同一数据源（`GET /work-graph` 的 running 节点 + progress）。
- stale 传播沿 `provenance_refs` 走既有链路：script 版本变更只波及同 timeline 下游；visual/research 变更波及引用它的全部节点。

### 3.3 Agent 任务（file_agent_runtime/driver.py + prompts）

新增/调整任务类型：
1. **structure_draft**：从输入理解（小说/素材/商品资料）起草分集结构 = 批量创建 Timeline（title/synopsis/order）+ `narrative_edges`。分支剧情由 Agent 检测输入中的抉择点自动生成边；用户可在蓝图增删节点/边后要求重排。
2. **script_draft(timeline_id)**：起草该 timeline 的 `timeline_script` artifact（markdown）；剪辑场景要求每个段落带 `source-version://` 时间码链接（从 SourceIntelligenceIndex 检索）。
3. **research_run(topic)**：包装现有 web_grounding pipeline，产出 `research_report` artifact（markdown + 截图 artifact），结论注入 visual entity 设计约束时写入对方的 `provenance_refs`。
4. 生产调度按图推进：`structure 通过 → 各 timeline 的 script 并行起草 → script 通过 → 该 timeline 的 shots/分镜/生成`，与现有 scheduler 的 gated/ready 语义一致。

### 3.4 API（backend/api/）

- 复用既有 JSON-Pointer patch 通道（project-file routes)：蓝图上所有编辑（Timeline 标题/梗概、narrative_edges、剧本 markdown）都是对既有路径的 patch，**不新增写接口**。
- `work_graph_routes.py`：响应中透出 `timeline_id`，供前端分组。
- 新增只读辅助接口（可选，首版可由 project.json 快照直接派生省略）：`GET /projects/{id}/research`。
- 剧本审阅动作复用现有 Review resolve 接口。

### 3.5 选区与引用契约（前后端必须对齐）

前端的 `SelectionAttachment { text, ref, field, path, start, end, label }` 已通过
session 消息的 `context.selection` 传给后端（AgentDock 现状），但 ref/field/path 的
**语法必须是前后端共享的规范**，否则 Agent 不知道用户选中的是什么。约定如下：

**(a) ref 命名空间**（`context.selected.ref` / `selection.ref` / extraRefs 共用）：

| 前缀 | 含义 | 后端解析动作 |
|---|---|---|
| `timeline:<id>` | 叙事节点/整条时间线 | 注入 title/synopsis/剧本状态摘要 |
| `element:<id>` | 时间线元素（既有） | 现状不变 |
| `script:<timeline_id>` | 该节点剧本（timeline_script slot） | 注入选中版本 markdown（或选区上下文） |
| `visual-entity:<id>` | 视觉实体（既有 id 体系） | 注入 prompt/选中版本/引用关系 |
| `edge:<edge_id>` | 分支选择边 | 注入选项文案与目标节点 |
| `research:<slot_id>` | 调研报告 | 注入结论与来源清单 |
| `asset-version:` / `artifact-version:` | 既有 | 现状不变 |

**(b) field / path 语法**：
- `data-creator-field = "<ref>/<fieldName>"`（如 `script:tl-ep3/body`、`timeline:tl-ep3/synopsis`、`visual-entity:shen-xiu/prompt`），SelectionToolbar 现有 `refOfField` 逻辑（取 `/` 前段）即可反推 ref。
- `data-creator-path`：目标在 project.json 内时为 **JSON Pointer**（现状）；目标是 artifact 文本（剧本/调研 markdown）时为 `artifact:<slot_id>@<version_id>`，配合 `start/end` 字符偏移定位选区——后端据此读文件并截取"选区 ± 上下文窗口"注入 prompt，且回写建议时能生成精确的文本 diff。
- 版本一致性：selection 携带 `@<version_id>`，若 Agent 处理时选中版本已变更，driver 返回"选区所在版本已过期"提示而不是错位应用。

**(c) 同步落点**：前端 `contracts/creator`（RefSearchItem/SelectionAttachment/uiLocator 的 page 枚举加 `blueprint`）与后端 session 请求校验、driver 的 selection 解析器（`file_agent_runtime`）三处同一张表，任何前缀新增必须三处同步，建议提为共享常量文件并加双端契约测试。

---

## 4. 前端改造（ui/src/）

### 4.1 路由与导航

```
/                                  HomePage（不变）
/project/:id                       BlueprintPage（新增，项目默认落点）
/project/:id/t/:timelineId/plan    PlanPage（参数化；旧 /project/:id/plan 重定向到主节点）
/project/:id/t/:timelineId/plan/element/:elementId   R2VWorkbenchPage（同上）
/project/:id/assets                AssetsPage（保留）
```

- `app/router.tsx:34-121` 增删路由；`components/layout/TopNav.tsx:13-16` 的 `MAIN_TABS` 改为 `[blueprint, assets]`，plan/workbench 激活态归于 blueprint（层级从属关系）。
- `components/layout/Breadcrumb.tsx`：`项目蓝图 / 第N集·标题 / 时间线编辑 / 制作工作台`；节点名取自 `narrative.nodes[timeline.narrative_node_id]`。
- i18n：`nav.videoPlan(视频方案)` 语义改为「时间线编辑」，新增 `nav.blueprint`、`blueprint.*` 词条（locales/zh.json、en.json）。

### 4.2 BlueprintPage 组件树（新增 pages/BlueprintPage.tsx）

```
BlueprintPage
 ├─ BlueprintHeader            标题 + 形态 chips + 结构操作（插入节点/确认结构，仅多节点显示）
 ├─ StructureArea              按 narrative 形态选择渲染器：
 │   ├─ NarrativeGraphCanvas   分支图（节点卡 + SVG 边 + 抉择节点 + 悬停「时间线 »」快捷入口）
 │   ├─ EpisodeList            线性多集（行式列表：集号/标题/梗概/状态/时长/时间线入口 + 批量操作）
 │   └─ PipelineStrip          单节点（阶段流程条：理解→剧本→设计→生成→成片）
 ├─ ScriptReviewPanel          选中节点剧本：正文（ScriptBlock 渲染，contenteditable→patch）
 │                             + meta（梗概/阶段/登场实体/规划）
 │                             + 动作：提出修改 / 审阅通过 / [进入时间线编辑]（一级主按钮）
 ├─ VisualDevSection           VisualEntity 卡片横排（版本 tag、待确认高亮）→ DetailDrawer
 ├─ ResearchSection            SourceIntelligence + ResearchFinding 列表 → DetailDrawer
 └─ DetailDrawer               右侧抽屉，三种内容型：
     visual   设计图大图 + 版本 chips + 引用关系 + prompt 编辑 + 重新生成/确认设计
     research 结论 + browser-use 逐页溯源（截图/摘录）+ 注入去向 + 补充调研/采纳
     source   理解概要 + 关键分段时间码（点击回看）+ 重新解析/在脚本中引用
```

数据一律来自 `projectSnapshotStore`（快照 + patch），编辑走 JSON-Pointer；不引入第二套编辑机制。抽屉复用现有 detail rail 交互习惯（`ProjectLayout.tsx` 的 `data-detail-rail` 槽位，窄屏时 portal 进去）。

### 4.3 selectors 与 store

- `selectors/narrativeSelectors.ts`（新增）：`selectNarrativeGraph`、`selectNarrativeShape`（graph/list/single）、`selectNodeById`、`selectTimelineForNode`。
- `timelineElementSelectors.ts`：新增 `selectTimelineById`；`selectPrimaryTimeline` 保留为单节点回退，逐步下线调用点（PlanPage.tsx:65、Breadcrumb.tsx:23、AgentDock.tsx:57 等）。
- `workGraphStore`：按 `narrative_node_id` 分组的派生 selector，供 WorkGraphPanel 与蓝图节点卡上的阶段 chips 共用。
- 无需新 store；抽屉开合为组件局部状态。

### 4.4 AgentDock（全局悬浮不变）

- **审批唯一入口原则**：确认/通过/驳回类决策只存在于 DecisionTray（结构确认、剧本审阅、设计确认、交互确认、调研采纳全部走 Review），页面与详情面板**不得**放置平行的确认按钮——只保留编辑类动作（提出修改 / 重新生成 / 插入节点），并以文案提示"审阅在创作助手中完成"。DecisionTray 审批项带 `ui_locator.page` 深链回蓝图对应对象。
- **选中即引用**：蓝图中点选叙事节点 / 视觉实体 / 调研条目时，调用 `creatorInteractionStore.select(ref)`（既有机制），对象立即成为 dock 的上下文引用 chip，用户可直接对着它说"把这个改一下"。
- **划选文本加入上下文**：剧本正文、梗概、设计 Prompt、调研结论、素材分段等关键文本区标注 `data-creator-field`/`data-creator-field-label`，划选文字即出现既有 `SelectionToolbar` 悬浮条（"加入对话"），选区作为 SelectionAttachment 挂入 dock——与 PlanPage 现有机制同源，零新组件。
- **详情面板不遮 dock**：剧本审阅与前置产物详情改为**工作区内嵌面板**（只覆盖 workspace 列，AgentDock 常驻可见），杜绝"打开详情就没法跟 Agent 对话"的交互冲突。
- WorkGraphPanel lane 标题使用叙事节点标题（现有 `laneTitle` 机制扩展）。

### 4.5 场景自适应规则（前端唯一分叉点）

| narrative 形态 | 结构区 | 剧本体裁（同一 timeline_script） | 顶栏 chips |
|---|---|---|---|
| 单节点 · 生成（故事短片/商品视频） | 生产看板（阶段列 × 具体产物卡） | 场次体 / 口播体 | 单集生成 · 时长 · 比例 |
| 单节点 · 剪辑 | 生产看板（素材理解/核验列在前） | 剪辑体（段落带时间码引用） | 素材剪辑 · 时长 · 比例 |
| 多节点无边 | EpisodeList（行式列表 + 批量操作） | 场次体 · 第N集 | 线性N集 |
| 有边 | NarrativeGraphCanvas | 场次体 · 节点名 | 节点数 · 结局线数 |

其余区块（剧本面板、视觉开发、调研素材、抽屉、粗剪预览带）全场景同构；剪辑场景隐藏视觉开发区。单节点场景用**生产看板**而非单薄的流程条：每个阶段一列，列内是可点开的具体产物卡（理解结论 / 剧本 / 设计图 / 镜头槽 / 成片门禁），过程状态一屏可见。

### 4.6 PlanPage 剧集快速切换：左侧可完全收起的剧集栏

时间线编辑页**左侧**一条剧集栏（展开 200px：缩略图 + 集名 + 状态点 + 进度条），当前集高亮，点击即切换 timeline。**收起后不占任何列宽**——展开入口是 Plan 头部横栏（成片/导出所在行）左端的普通按钮「剧集 · N」（原创作总纲折叠块的位置），带文字标签，与顶栏 logo 左侧"返回项目列表"箭头无语义歧义；不用悬浮把手。**不设"返回蓝图"按钮**——返回走顶栏"项目蓝图"主导航。同时移除 PlanPage 头部的"创作总纲"折叠块：总纲/梗概信息已由蓝图承载，Plan 页专注编排。数据零新增：节点列表 = `timelines.order`（+ `narrative_edges` 排序），缩略图取该 timeline 首个 element 的既有缩略产物。已在 demo 实现（`BlueprintDemoPlanPage.tsx` 包装真实 PlanPage）。

### 4.7 蓝图 / 时间线 / 资产库的职责与互链

三者是**同一份 project.json 的三种投影**，不新增任何数据：

| 视图 | 职责 | 投影方式 |
|---|---|---|
| 项目蓝图 | 结构与创作决策（为什么/是什么）：结构、剧本、视觉开发、调研、过程状态 | 按创作阶段切片 |
| 时间线编辑 | 单节点的编排执行（怎么拼） | 按单条 timeline |
| 资产库 | 全量库存与管理（东西在哪）：按归属分组 + 检索 + 上传 | 按 AssetIndex 归属（owner_ref）分组 |

导航关系：顶栏仅 蓝图/资产库 两个平级入口；时间线是蓝图的下钻。互链规则：蓝图抽屉详情 ↔ 资产库详情为同一 DetailView 组件（同一 artifact 数据）；资产库每个分组带「在蓝图中查看归属」，蓝图/剧本内的实体缩略图点击即打开同一详情。demo 中 `/blueprint-demo/:id/assets` 已实现该分组视图与互链。

### 4.8 粗剪预览带（蓝图底部 · 零新增结构）

蓝图底部一条可折叠的**粗剪预览带**，让用户在生产早期就能低成本"看到片子"：

- **帧的派生规则（纯投影，无新模型）**：预览带以 **element 为粒度**——本质上就是"全部镜头的分镜图按时间序排成一条胶片"。每帧取该 element 当前最优可用画面：`element_video` 选中版首帧 ▸ 缺则 `r2v_storyboard_image`（分镜图是每个生成 element 的必经产物，天然全覆盖）▸ 再缺则所引用实体的 `visual_asset_image`——全部是既有 ArtifactSlot kind。帧上标注来源类型（成片帧/分镜/待分镜），多集时以集为分段；点击帧定位到对应节点/镜头。
- **播放粗剪**：调既有 ffmpeg compose 走低分辨率 draft 拼接（现有产物直接串接，不触发任何模型生成），产物就是普通 `timeline_render` ArtifactVersion，draft 标记放 `metadata`（既有 dict 字段）——同样零新增结构。
- 帧就绪率（如 14/26）本身就是最直观的整体进度表达，与 work-graph counts 同源。

---

## 5. 分期落地

**Phase 1 — 蓝图壳子 + 单节点/线性形态（无域模型风险，1-2 周）**
- 路由参数化 + BlueprintPage + PipelineStrip/EpisodeList + Breadcrumb/TopNav 改造
- schema v9 迁移（单节点回填），剧本面板对旧项目只读
- 视觉开发区 / 调研素材区消费既有 VisualDevelopment 与 SourceIntelligence（ResearchFinding 可后置）
- 验收：存量项目行为不回退；新项目默认落蓝图页

**Phase 2 — 剧本与结构成为一等公民（2-3 周）**
- ScriptDocument + structure/script 检查点 + agent structure_draft/script_draft 任务
- 线性多集端到端：分集起草 → 逐集审阅 → 逐集生产；work_graph 节点作用域改造
- ResearchFinding 落盘 + 调研抽屉溯源

**Phase 3 — 分支剧情（2-3 周）**
- NarrativeChoice 边 + 分支图画布（增删节点/拖边）+ 抉择点交互
- 分支间资产共享的成本提示、InteractionCreation 生产（interaction_draft 动效任务 + 可点击试玩编辑）、InteractiveManifest 组装与三种导出形态（见 2.7）

## 6. 实施前查漏清单（验收发现的补充项）

1. **主题令牌纪律**：demo 曾出现 dark 模式下 `focus:bg-[#fffdfa]`、`bg-white` 的白底 bug（已修）。正式实现必须只用 CSS token（`--color-bg-*`），CI 建议加 lint 规则禁止 blueprint 组件内出现字面量颜色。
2. **剧本 markdown 解析器需成文规范**：块类型（场次头/动作/台词/钩子/segment/时间码链接）的解析与序列化必须双向无损，是"人工 contentEditable 编辑 → 块级 diff → Agent 重写不覆盖人工修改"的前提。建议独立模块 + 快照测试（`lib/scriptMarkdown.ts` ↔ 后端同构规则）。
3. **单文件写锁与多集并行**：project.json 单写者模型下，12 集并行生产的 patch 吞吐需要评估；剧本正文在 artifact 文件中（不进 project.json）已经缓解，但 work_graph 状态推进仍集中写。必要时按现有 request-sequence 机制做写合并。
4. **交互动效试玩沙箱**：html_css 动效预览可点击试玩必须放 `iframe sandbox`，禁止在工作区直接注入 Agent 生成的 HTML（XSS 边界）。
5. **窄工作区适配**：蓝图内嵌面板与 PlanPage 左侧剧集栏需接入现有 `useNarrowWorkspace`/detail-rail 机制（窄屏时面板 portal 到底部 rail）。
6. **i18n 与新手引导**：demo 全部为硬编码中文；正式实现需补 `blueprint.*` 词条与 BlueprintTour（现有 onboarding 体系）。
7. **迁移回滚**：schema v9 迁移需保留 v8 快照副本（project_files 现有备份机制确认覆盖）。
8. **粗剪预览的分支路径**：分支项目"播放粗剪"需选定一条路径；帧带默认展示全部节点、按集分段（demo 现状），播放时取"入口 → 默认边"路径。
9. **选区过期语义**：3.5(b) 的 `@version_id` 过期处理需要 UI 呈现（重新定位 or 丢弃提示）。

## 7. 决策点（已确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 线性多集 structure 是否人工确认 | **随项目审阅模式配置**：confirm 模式确认一次，yolo 静默通过（见 3.1 审阅模式原则） |
| 2 | 剧本审阅粒度 | **已确认**：整本 + 选区批注起步；逐场通过/驳回（场次锁定、局部重写）后置 |
| 3 | 单集/剪辑项目默认落点 | **已确认**：默认蓝图，剧本通过后自动跳时间线 |
| 4 | 批量生产的预算授权 | **随审阅模式配置**：confirm 模式一次总授权 + 超 80% 二次确认；yolo 直接放行 |
| 5 | 互动包首发形态 | **已确认**：自托管 HTML 先行，平台容器后置 |
| 6 | 粗剪与图片生成的预算/确认 | 粗剪拼接不计预算；分镜图/设计图等图片生成不记预算，但其生成前确认与否随审阅模式配置 |
| 7 | 旧项目处理 | **已确认**：不做剧本回填、不新增剧本——旧项目一律映射为单节点（单视频生成/剪辑）形态，蓝图剧本面板展示既有信息（creative_brief / 镜头表 / EditPlan）的只读映射 |
| 8 | `/project/:id`（项目根路由）默认落点 | 由重定向时间线改为**落蓝图页** |
