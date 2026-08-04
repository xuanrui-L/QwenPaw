# Creator 一致性改造 PR 计划

> 与 `docs/proposals/master-cast-lineup/index.html` 配套，记录从 prompt discipline 到 Master Cast Lineup 的完整 PR 路线图。

## 当前状态总览

| PR | 主题 | 状态 | 依赖 |
|---|---|---|---|
| PR-A | Prompt discipline：分镜图无文字、一图一变体 | **进行中** | - |
| PR-B | Review-pending freeze：驳回后锁定重新生成与下游 | 待开始 | PR-A |
| PR-C | Rejection feedback popup + runtime injection | 待开始 | PR-B |
| PR-D | Variant canvas：变体画布（schema + resolver + UI + migration） | 待开始 | PR-A |
| **PR-E** | **Master Cast Lineup + 相对一致性** | **待开始** | PR-A、PR-D（可选） |
| PR-F | Style Reference Board（可选） | 待开始 | PR-E |

---

## PR-A：Prompt discipline

目标：让 storyboard/variant 图片不再出现文字、编号、宫格线，且“一图一变体”。

- [x] a1：定位并修改 `visual_development_agent.system.txt` 等 prompt 规则
- [ ] a2：更新 prompt sha256 引脚
- [ ] a3：补充/更新单元测试与 E2E 断言
- [ ] a4：pre-commit + scan + push + PR

**阻塞下一步的条件**：PR-A 合入 main。

---

## PR-B：Review-pending freeze

目标：当用户驳回某张视觉产物后，自动阻止该产物被继续用于下游生成，并给出明确提示。

- [ ] b1：在 asset index / artifact version 上增加 `review_status`（pending / approved / rejected）
- [ ] b2：执行层在选择 `selected_artifact_version_id` 或参考图时过滤 rejected
- [ ] b3：Agent 规划层读取 review 状态，避免基于 rejected 产物做计划
- [ ] b4：UI 在 rejected 产物上显示遮罩与重做入口

---

## PR-C：Rejection feedback popup + runtime injection

目标：把用户的驳回原因结构化地回注到下一轮生成 prompt 中。

- [ ] c1：设计 rejection feedback schema（category + free text + changed pointers）
- [ ] c2：前端 popup 收集反馈并写入 project state
- [ ] c3：执行层在重生成时把 feedback 注入 prompt / negative prompt
- [ ] c4：测试 feedback 注入后产物确实避免同类问题

---

## PR-D：Variant canvas

目标：给 `VisualVariant` 提供更直观的画布/血缘管理 UI。

- [ ] d1：schema：`VisualVariant` 扩展画布元数据（pose、angle、expression、costume 等标签）
- [ ] d2：resolver：variant 生成时自动按画布维度聚合参考图
- [ ] d3：UI：AssetsPage variant grid 改造为 canvas 视图
- [ ] d4：migration：旧 variant 数据自动补默认标签

**与 PR-E 的关系**：PR-E 中的 `canonical_variant_id`、`derived_from_variant_id`、`consistency_tags` 可以合入 PR-D，也可以作为 PR-E 的一部分。建议把 PR-D 的范围限定为“变体画布/血缘展示”，PR-E 负责“变体一致性规则与强制注入”。

---

## PR-E：Master Cast Lineup + 相对一致性

目标：解决多角色同框时的比例、风格、色调、时代感、空间关系一致性问题，并防止同一角色内部变体漂移。

### E0：数据模型（P0）

- [ ] `VisualCastLineup` 与 `VisualDevelopment.cast_lineups`
- [ ] `R2VCreation.cast_lineup_refs` / `Shot.cast_lineup_refs`
- [ ] `VisualEntity.canonical_variant_id`
- [ ] `VisualVariant.derived_from_variant_id` / `consistency_tags`
- [ ] `Shot.shot_cast_comparison_version_id` / `shot_cast_comparison_prompt`
- [ ] 校验逻辑与 schema 兼容

### E1：生成管线（P0）

- [ ] `GENERATE_CAST_LINEUP_IMAGE` 命令与 prompt 模板
- [ ] 自动注入 canonical variant artifact 到 Lineup 参考链
- [ ] 分镜图参考链：`Lineup → Shot Cast Sheet → 场景锚点 → 角色锚点 → 用户素材`
- [ ] 视频参考链：`Storyboard → Lineup → Shot Cast Sheet（可选） → 角色/场景锚点`
- [ ] 新 Variant 生成时自动 prepend canonical variant reference

### E2：Prompt 与规则（P0）

- [ ] `visual_development_agent.system.txt` 增加相对一致性与变体一致性章节
- [ ] `r2v_generation_director.system.txt` 增加 Lineup 显式引用规则
- [ ] 更新 prompt sha256 引脚

### E3：Shot 级对比图（P1）

- [ ] 生成命令与 sketch 风格 prompt
- [ ] 缓存/失效逻辑
- [ ] 参考链插入

### E4：变体一致性质检（P1）

- [ ] VLM 对比 canonical variant 与新 variant
- [ ] 核心身份特征（continuity）变更拦截

### E5：UI（P1）

- [ ] AssetsPage 新增 Cast Lineups 分类与卡片
- [ ] R2VWorkbenchPage Lineup 选择器与自动推荐
- [ ] ShotList 分镜级对比图缩略图
- [ ] Variant canonical 徽章与对比视图

### E6：测试与文档（P1）

- [ ] 单元测试：schema 校验、参考链解析、自动注入
- [ ] E2E：3 角色短剧验证从角色锚点 → Lineup → 分镜 → 视频的完整链路
- [ ] 更新 proposal 与开发者文档

---

## PR-F：Style Reference Board（可选 / P2）

目标：当监控到或人工反馈出现风格漂移时，引入独立于 Lineup 的风格参考板。

- [ ] f1：`VisualStyleBoard` schema 设计
- [ ] f2：风格/色调/材质参考图生成管线
- [ ] f3：自动注入到所有视觉生成链最前面
- [ ] f4：漂移检测与自动回退策略

---

## 建议的合入顺序

1. **PR-A 先合入**：它为所有后续一致性工作提供 prompt 纪律基础。
2. **PR-D 与 PR-E 可并行**，但最好在 PR-D 的 schema 改动落地后再合入 PR-E 的变体一致性字段，避免冲突。
3. **PR-B / PR-C** 可与 PR-E 并行，但 PR-C 的 rejection feedback 对 PR-E 的 VLM 质检重试非常有用。
4. **PR-F** 作为长期观察项，等 PR-E 上线后根据实际风格漂移数据决定是否启动。

---

## 下一步行动

1. 完成 PR-A 的 a2-a4（sha256、测试、pre-commit、PR）。
2. 打开 PR-E 的 Draft PR，先实现 E0/E1/E2 的最小可用版本（MVP）。
3. 在 PR-E MVP 中跑通一个 2 角色短剧的端到端链路。
