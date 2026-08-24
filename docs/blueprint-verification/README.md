# 项目蓝图改造 · 实施与真实验证报告

分支：`worktree-blueprint-impl`（基于 origin/dev/creator@2d0c3a7a 快进后实施）。
规范输入：`creator-blueprint-redesign-plan.md`、`creator-blueprint-implementation-prompt.md`、
交互基准 `ui/src/pages/BlueprintDemo*` + `ui/src/components/blueprint/*`（保留可运行）。

## 一、交付提交（时间序）

| 提交 | 内容 |
|---|---|
| 1be2dca9 | schema v9 域模型（Timeline 叙事字段 / narrative_edges / 3 个 artifact kind / InteractionCreation / InteractiveManifest）+ v8→v9 迁移（旧项目单节点映射、零回填） |
| cf401bc5 | 互动包组装（manifest 推导 + 自托管 HTML 播放器 + zip） |
| bcf3d1d8 | `GET /projects/{id}/interactive-bundle` 导出端点（fail-closed 409） |
| f5077b04 / 02e3a082 / 45070bbe | 真实 key E2E（manual_real）：建项目 → 真实 qwen 结构起草 → patch 结构 → dispatch 剧本节点真实起草落盘 → 真实 qwen-image 分镜经管线落盘 → 粗剪 200 |
| a41fe9fb | P2 后端：script markdown 双向解析、timeline_script 版本写回、structure/script 检查点（审阅模式配置驱动）、work_graph 剧本节点与 stale 收窄、SCRIPT_DRAFT 任务、选区 `artifact:<slot>@<version>` 注入 |
| 4960d342 | 粗剪 draft 渲染（element_video ▸ 分镜图 480p 拼接，零模型调用）+ 端点 |
| 91faa856 | P1 前端：BlueprintPage（三形态自适应）、参数化 `t/:timelineId/plan`、TopNav 蓝图/资产库、PlanPage 剧集栏+移除创作总纲、资产按归属分组、i18n、契约镜像 |
| fc193613 | P3 后端：interaction 动效起草（真实 LLM 产 html_css，data-edge-ref 校验）、work_graph interaction/bundle 门禁节点 |
| 2dbbc80a / b58b18c2 | 集成修复：Tour 首步指向蓝图；元素 outputs 为管线写回记录（回退错误预声明） |
| 5b0b9f2c | 集成修复：stale 重派以请求指纹定界持久 id，避免撞旧发布事务 |

## 二、测试基线

- 后端 `pytest tests -q`：**1173 passed**（基线 1122，新增 51）
- 前端 `tsc --noEmit` + `vitest run`：**253 passed / 48 files**
- 真实 key E2E（`tests/manual/test_real_blueprint_e2e.py`，`-m manual_real`）：**passed**，
  覆盖真实 qwen 结构与剧本起草、真实 qwen-image 分镜、依赖门禁（分镜依赖剧本）、
  bundle/粗剪 fail-closed、粗剪 200 真 mp4。

## 三、真实模型 / 真实 key 验证（DashScope）

后端以真实 key 启动（`uvicorn dev_main:app`，TEXT=qwen-plus、IMAGE=qwen-image-2.0-pro、
VIDEO 复用 LLM 凭证走 wan），前端 vite 5179，真实项目 `蓝图终验-互动短剧`：

1. **文本**：qwen-plus 起草两集梗概（JSON）与整集场次体剧本（含台词/括注，模型自发使用
   `source-version://` 时间码约定）→ `timeline_script` artifact 落盘。
2. **图像**：qwen-image-2.0-pro 经 storyboard 管线真实产出分镜（画面与 prompt 吻合），
   进入 DecisionTray StoryboardReview，人工 Keep 通过。
3. **视频**：wan 真实生成两条镜头视频（`video:el:sc01`、`video:el:sc02` 均 done）。
4. **检查点/授权真实闭环**：structure 检查点、付费生成确认均在 AgentDock DecisionTray
   真实弹出并经人工 Continue 放行（审阅模式 confirm 路径）。
5. **浏览器目验**（截图见本目录）：
   - `blueprint-real-1.png` 单视频生产看板（旧项目只读映射文案）
   - `blueprint-real-linear.png` 线性两集列表 + 粗剪帧带 + 活动条
   - `blueprint-real-script.png` 剧本审阅面板 + DecisionTray 真实分镜审阅
   - `verify-plan.png` 参数化时间线 + 左侧剧集栏（含真实分镜缩略）
   - `verify-branching.png` 分支画布形态
   - `verify-selection.png` 划选文本 → SelectionToolbar → dock 选区附件 chip
6. **粗剪**：`GET /timelines/{tid}/rough-cut` 返回真实 draft mp4（ffmpeg 拼接分镜/镜头）。
7. **成片合成**：`POST /timelines/{tid}/render` 两条时间线真实合成
   （`timeline:timeline:main:render`、`timeline:tl:ep2:render` 均落盘 final_video）。
8. **互动包端到端（真实产物 + 浏览器实点）**：`GET /interactive-bundle` 导出 zip
   （2,745,318 字节：`index.html` + `manifest.json` + `segments/timeline_main.mp4`
   1,644,142 B + `segments/tl_ep2.mp4` 1,104,453 B）。本地起 http 服务用 Playwright
   实际播放：入口段播完弹出选择层（问题「是否进入旧宅？」，选项「选择 · 进入旧宅」，
   来自 `edge_index`），点击后 `video.src` 切换到 `segments/tl_ep2.mp4` 且分支段可解码
   播放，覆盖层关闭 —— 判定 **PASS**（截图 `bundle-choice.png`、`bundle-branch.png`）。

## 四、实施中发现并修复的真实集成缺陷

1. Tour 首步锚定已移除的创作总纲块 → 文案改为指向蓝图（2dbbc80a）。
2. E2E 曾预声明 element outputs → 校验揭示 slot 是管线写回记录，回退（b58b18c2）。
3. stale 重派撞旧发布事务（script/interaction 执行服务）→ 持久 id 以请求指纹定界（5b0b9f2c）。
