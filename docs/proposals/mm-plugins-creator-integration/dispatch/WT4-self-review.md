# 任务派发 WT4 · 自我审阅模块（`feat/creator-self-review`）

## 你的任务
新增成片自我审阅模块：compose 后抽帧回看 → VLM 六维检查 → 结构化报告反馈剪辑专家
迭代（≤3 轮）。整体由代码级总开关控制，默认关闭，前端零感知。**本 WT 的重心不在
框架代码，而在 prompt 调试与实际 case 测试。**

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT4 节 + §1.3 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/services/media_files/local_execution.py`
  （COMPOSE_FINAL_VIDEO 产出路径 / `_materialize_and_publish()`）、
  `backend/models/config.py`、`backend/services/file_agent_runtime/`（driver +
  subagents）、`backend/observability/tracing.py`（trace_event / traced_async）。
- 自评协议方法论来源（只读）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/video-edit/skill/`
  （review/ 自评协议：六维检查、≤3 轮回看）。
- 预算数学：`backend/vendor/mm_plugins/image_budget.py`（上游
  `src/shared/image.py` 的 budget_to_pixels / smart_resize）——基线上不存在时
  自行同规范移植（见下方 Worktree 准备节的说明）。

## 全局硬约束（引自总方案 §2.2 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 env 注入；不做进程直调。
2. **总开关默认关闭**：`CREATOR_SELF_REVIEW_ENABLED` env，不进 plugin.json /
   ModelConfigModal / 前端 contract——前端完全无感知。
3. 自评是**建议者不是门禁**：不阻塞交付，不与既有 checkpoint 审批体系重叠。
4. VLM 用现有 `creator_vlm_model` 后端，不新增模型配置。
5. pre-commit + 双 pytest 全绿；注释英文；评测集真实 VLM 跑标记 manual/skip。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-self-review -b feat/creator-self-review dev/creator
```
基线为**当前 dev/creator**（无前置合并；`feat/motion-js-timeline` 对
`local_execution.py` 的改动留待最终集成时先于本分支合入，你只需保持挂钩为
单点 if，冲突即可机械解决）。
隔离栈：`dev-isolated.sh`、`QWENPAW_WORKING_DIR=~/.qwenpaw-review`、端口 **8094**、
凭据复制自主实例 `~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

注意：因无前置合并，`backend/vendor/mm_plugins/image_budget.py` 在你的基线上
大概率不存在——按总方案 §2.2 vendoring 规范（文件头保留上游版权 + 修改标注、
NOTICE 记 commit 077aea6）自行从上游 `src/shared/image.py` 移植该单文件，与 WT3
逐字同源，集成时归一。

## 实现规格（引自总方案 §三 WT4，已定稿）
> **开关**：config.py 增 `SELF_REVIEW_ENABLED = _bool_env(
> "CREATOR_SELF_REVIEW_ENABLED", False)` + `is_self_review_enabled()`。
>
> **模块 `services/render_review/`**：
> - `frames.py`：`extract_review_frames(video_path, *, max_frames=24) ->
>   list[ReviewFrame{timestamp_ms, image_path}]`（ffmpeg 均匀抽帧 + 首尾帧必采，
>   分辨率经 image_budget.smart_resize 对齐 VLM 预算）；
>   `probe_audio_profile(video_path) -> AudioProfile{has_audio,
>   loudness_segments}`（ffmpeg ebur128 概要）。
> - `protocol.py`：六维协议 prompt 模板（画面质量/时长匹配/节奏/配音含 TTS 轨与
>   ducking/字幕同步与溢出/工程正确性：黑帧、静音段、分辨率）；输出 Pydantic
>   schema `RenderReviewReport{video_ref, round, findings:
>   list[ReviewFinding{dimension, passed, severity: minor|major,
>   evidence_timestamp_ms, suggestion}], verdict: pass|revise}`（落 schemas/，
>   前端 contract 本期不加）。
> - `review.py`：抽帧 → VLM 多图评审 → 报告写
>   `runtime/render-review/{video_id}/round-{n}.json` + trace_event
>   （component="render_review"）。
> - 迭代环（已定稿：回合消息方式）：verdict=revise 时把 findings 以结构化 JSON
>   文本作为**回合 user message** 送入 AI_EDITING_DIRECTOR 下一次 specialist run
>   （不改 prompt spec、不加 placeholder）；`MAX_REVIEW_ROUNDS=3`，3 轮后不阻塞
>   交付，报告随成片留存。
>
> **挂钩**：local_execution.py COMPOSE_FINAL_VIDEO 发布成功后
> `if is_self_review_enabled(): asyncio.create_task(run_review_loop(...))`，异步
> 不阻塞 compose 返回。

## Prompt 调试与实际 case 测试（重心，引自总方案）
> - 评测集 `backend/tests/fixtures/render_review/`：历史成片 ≥2 + 人工构造缺陷片
>   （黑帧/音画错位/字幕溢出/配音缺失/节奏拖沓各 ≥1），每例配 expected.json
>   （人工标注六维结论）；
> - 回归脚本 `tests/render_review/test_eval_set.py`（真实 VLM，manual/skip 标记，
>   人工触发）：以「缺陷检出零漏报、误报 ≤1/例」为 prompt 迭代准绳，每轮 prompt
>   修改跑全集回归；
> - 真实 case：隔离栈开开关，「创意生成」「素材剪辑」两链路各跑一个完整项目，
>   人工核对证据帧-结论一致、修订建议可执行（按用户测试三准则：UI 操作、读帧
>   看实际内容）。

## 自动化测试
开关关闭零行为差异回归；打桩 VLM pass/revise 两态断言轮数与终止；报告 schema
单测；音频概要单测。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9，集成序：motion 先、本分支后）。
- 热点：config.py 只加开关常量；local_execution.py 只在成片发布点加一处挂钩
  （保持单点，便于集成时与 motion 分支解冲突）。
- 完成后回填总方案 WT4 节（附评测集通过率数据）。
