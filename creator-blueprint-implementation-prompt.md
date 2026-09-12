# 任务 Prompt：实现 Creator「项目蓝图」层（前后端全量）

> 用法：将本文件作为任务输入交给实施者（agent 或工程师）。两份权威输入必须先读：
> ① 设计方案 `creator-blueprint-redesign-plan.md`（数据模型/后端/前端/契约/分期的唯一规范）；
> ② 交互基准 demo：`plugins/apps/qwenpaw-creator/ui/src/pages/BlueprintDemo*.tsx` 与
> `ui/src/components/blueprint/*`（视觉与交互的可运行样板，`npm run dev` 后访问
> `http://127.0.0.1:5179/#/blueprint-demo` 查看，四~五个场景可切换）。

---

## 角色与目标

你是 QwenPaw Creator 的全栈实施者。目标：把「项目蓝图」层从 demo 变为生产功能——
用户输入一本小说/一个故事/商品资料/一批素材后，先在蓝图页看到并审改
**叙事结构（单集/线性多集/分支）、每节点剧本、视觉开发、调研与素材理解、粗剪预览**，
审阅通过后逐节点进入时间线编辑与生产；分支项目最终导出**可交互的互动包**而非 mp4。

## 硬性原则（违反即返工）

1. **Data model 驱动，先复用后扩展**。允许的新模型仅限方案 2.8 节四处：
   `Timeline.title/synopsis/planned_duration_seconds`、`Project.narrative_edges`、
   `InteractionCreation`（ElementCreation 联合新成员）、`InteractiveManifest`；
   外加 3 个 ArtifactSlot kind：`timeline_script` / `research_report` / `interactive_bundle`。
   剧本、调研、粗剪、结构确认一律走既有 ArtifactSlot/Version、Review、provenance、
   EntityCollection、checkpoint 机制。任何额外新模型必须先停下来说明理由征得同意。
2. **审批唯一入口**：确认/通过/驳回只存在于 AgentDock DecisionTray（Review 机制）。
   页面与详情面板只放编辑类动作（提出修改/重新生成/插入节点），并保留
   "审阅在创作助手中完成"的提示文案。
3. **选中即引用 + 划选加入上下文**：交互对象点选调 `creatorInteractionStore.select(ref)`；
   关键文本区标注 `data-creator-field`；ref/field/path 语法严格按方案 3.5 的契约表，
   前端 contracts、后端 session 校验、driver 解析器三处同步并有双端契约测试。
4. **面板不遮 AgentDock**：所有详情/剧本面板为工作区内嵌面板（demo 已示范）。
5. **主题令牌纪律**：组件内禁止字面量颜色（demo 曾因 `#fffdfa`/`bg-white` 在 dark
   模式下泛白，已修为 token）；只用 `--color-*` 变量。
6. **场景自适应由数据推导**（方案 4.5）：单节点→生产看板；多节点无边→剧集列表；
   有边→分支图。不引入场景枚举驱动 UI。
7. Agent 生成的 html_css 动效预览必须 `iframe sandbox` 隔离。

## 实施范围（按 Phase 顺序交付，每个 Phase 独立可验收）

### Phase 1 — 蓝图壳子 + 单节点/线性形态（不动生产语义）
- schema v9 迁移（方案 2.9）：新字段全可选，存量项目自动成"单节点"形态；保留 v8 备份。
  **旧项目不做剧本回填、不新增剧本 artifact**：一律映射为单节点（单视频生成/剪辑）
  形态，剧本面板展示既有信息（creative_brief / element intent / Shot 镜头表 / EditPlan）
  的只读映射，并标注「创建于剧本功能之前」。
- 路由：`/project/:id` 默认落蓝图（已确认）；`/project/:id/t/:timelineId/plan` 参数化，
  旧 `/plan` 重定向主 timeline；TopNav 主导航改为 蓝图/资产库 两枚。
- BlueprintPage：结构区（看板/列表两形态）+ 剧本审阅内嵌面板（只读兜底）+
  视觉开发/调研素材入口与详情 + 粗剪预览带（方案 4.8 派生规则）+ 「正在进行」活动条
  （数据源 GET /work-graph）。
- PlanPage：左侧可完全收起的剧集栏（收起后零列宽，展开入口为 Plan 头部横栏左端的
  普通按钮「剧集 · N」，非悬浮把手）；移除头部"创作总纲"折叠块。
- 资产库改按归属分组（owner_ref 投影），组头「在蓝图中查看归属」互链。
- i18n（zh/en）、BlueprintTour 骨架、`useNarrowWorkspace` 窄屏适配。

### Phase 2 — 剧本与结构成为一等公民
- `timeline_script` artifact + 剧本 markdown 双向解析器（独立模块 + 快照测试，
  块类型与 `source-version://` 时间码链接按方案 2.3）；contentEditable 编辑走块级
  diff patch，Agent 重写不得覆盖人工修改。
- checkpoints：新增 structure（项目级）与 script（timeline 级，Review-on-artifact），
  plan/direction 作用域收窄到 timeline；单节点项目 structure 静默通过。
- agent 任务：structure_draft / script_draft(timeline_id) / research_run(topic)
  （方案 3.3）；`research_report` 落盘含 browser-use 截图与注入去向。
- work_graph：节点带 timeline_id，lane 按节点分组；新增 script/research/source_intel
  节点 kind；stale 沿 provenance 收窄传播；media_call 预算按节点分池。
- 选区契约（方案 3.5）全链路：SelectionAttachment 的 artifact 定位
  （`artifact:<slot>@<version>` + start/end）、版本过期提示、driver 注入选区上下文。

### Phase 3 — 分支剧情与互动导出
- `narrative_edges` + 分支图画布（增删节点/拖边/抉择点）+ 观众抉择详情
  （可点击试玩，demo 已示范交互形态）。
- `InteractionCreation` element（方案 2.7a）：interaction_draft 动效任务基于上一段
  末帧；workbench/ElementDetail 支持编辑问题/选项/热区/动效。
- `InteractiveManifest` + `interactive_bundle`（方案 2.7b）：组装门禁进 work_graph；
  三种导出（自托管 HTML 互动包优先，平台容器转译后置，降级线性 mp4 + fallback 策略）。

## 验收标准（每条都要可演示）

1. 存量旧项目升级 v9 后行为零回退；新项目输入小说 → 蓝图出现分集列表与剧本草稿。
2. 四类场景（分支短剧/线性 12 集/单集生成/素材剪辑/商品 30s）蓝图形态正确自适应，
   剧本体裁（场次/口播/剪辑体）同一入口同一审阅流。
3. 审批只出现在 DecisionTray；剧本审阅通过后该节点自动推进设计/分镜。
4. 划选剧本一句台词 →"加入对话"→ Agent 收到带定位的选区并能精确改写该句
   （返回 diff 命中原字符区间）。
5. 蓝图粗剪带帧数 = 全部 element 数，来源优先级正确；点"播放粗剪"得到 draft
   timeline_render（不消耗模型调用）。
6. 分支项目导出 zip：本地打开 index.html 可点击选项走通两条结局；任一分支重生成后
   互动包自动标 stale。
7. dark/light 双主题、窄屏 detail-rail、i18n 双语全部正常；`tsc`、后端类型检查、
   双端契约测试、既有测试套件全绿。

## 明确的边界与禁止事项

- 不改 AgentDock/DecisionTray/SelectionToolbar 的核心机制，只做数据接入与 locator 扩展。
- 不引入新的状态管理或第二套编辑通道（一切写操作走 JSON-Pointer patch / artifact 版本）。
- **审阅模式配置**是横切原则（方案 3.1）：所有检查点/生产授权/图片生成确认是否需要人工，统一由项目级 confirm/yolo 配置派生，禁止散落独立开关。
- demo 文件（BlueprintDemo*）保持可运行直至对应生产页上线后再删除。
- 方案第 7 节决策点已全部确认，按表中结论执行。
