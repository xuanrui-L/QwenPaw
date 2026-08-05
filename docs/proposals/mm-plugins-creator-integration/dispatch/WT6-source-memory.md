# 任务派发 WT6 · 长素材记忆并入 Source Intelligence（`feat/creator-source-memory`）

## 你的任务
把 mm-plugins video-memory 的构建管线与查询逻辑以 Apache-2.0 合规方式移植进
Creator 的 Source Intelligence 体系：>20min 长素材自动构建层次图记忆（后台任务 +
执行授权），专家经 `query_source_memory` 工具按台词/语义/时间定位片段。**不新建
独立 memory 服务。本 WT 重心在真实素材测试。**

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT6 节 + §1.2 事实 4/5 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/services/media/source_intelligence.py`、
  `backend/services/source_analysis/service.py`、`backend/schemas/assets.py`
  （SourceIntelligenceIndex）、`backend/services/specialist_tools.py`、
  `backend/services/runtime_files/execution_store.py`（Task 机制）、
  `backend/models/config.py`、`plugin.json`。
- 上游移植来源（Apache-2.0，本地路径）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/video-memory/`
  —— `skill/script/build_memory/`（schema.py / build_graph.py /
  pipeline_worker.py / prompts.py / embeddings.py / time_utils.py）与
  `qwen_mm_plugins_video_memory/`（loader.py + tools/ 9 个查询工具）。

## 全局硬约束（引自总方案 §2.2 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；**不做 env 注入；不起子进程跑上游脚本**。
   手法 B：算法移植到 `backend/vendor/mm_plugins/video_memory/`（文件头版权 +
   修改标注，NOTICE.md 记 commit `077aea6`；目录样板遵循 WT3 定稿）。
2. 所有外部调用改走 Creator 后端：P2 子图抽取 VLM → `creator_vlm_model`；ASR →
   Creator ASR 模块现有 `transcribe()` 接口（开发期 fun-asr，集成后自动升级
   qwen3-asr-flash）；embedding → 新建薄客户端。
3. data model 先行：schema + 前端 contract + api-contract 测试同步。
4. 构建是计费操作：入执行授权，按时长线性费用预估。
5. pre-commit + 双 pytest 全绿；注释英文；真实构建仅人工验收且事先确认成本。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-source-memory -b feat/creator-source-memory dev/creator
```
基线为**当前 dev/creator**（无前置合并）；开发期**不 rebase 其他分支**，本分支
在最终集成阶段最后合入。隔离栈：
`dev-isolated.sh`、`QWENPAW_WORKING_DIR=~/.qwenpaw-memory`、端口 **8096**；凭据复制
自主实例 `~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

因无前置合并的两点适配（已定稿的接口约定）：
- **ASR**：一律经 `asr_model.transcribe(media_url)` 现有接口调用，开发期用
  fun-asr 验证即可，**不直接依赖 WT1 代码**；集成后自动获得 qwen3-asr-flash。
- **vendor 目录**：若基线上尚无 WT3 的 `backend/vendor/`，按总方案 §2.2 规范自行
  创建同样的目录结构与 NOTICE.md（条目只写自己移植的模块），集成时与 WT3 的
  NOTICE 合并。

## 实现规格（引自总方案 §三 WT6，已定稿）
> **移植清单（逐文件处置）**：schema.py 移植；build_graph.py + pipeline_worker.py
> 移植改造（编排改 async + Creator 并发控制，P2 并发度常量 4–8）；prompts.py 移植
> （进 Creator prompt 常量，不走占位符白名单——非 agent prompt）；embeddings.py 的
> 索引/余弦检索逻辑移植、HTTP 客户端重写；llm_client.py / env_config.py **不移植**；
> merge_memories.py 本期不引入；9 个查询工具 + loader.py 的查询逻辑移植进
> `services/media/source_memory.py`（带图谱内存缓存）。
>
> **data model 与配置**：
> - SourceIntelligenceIndex 增可选 `memory_ref: SourceMemoryRef{graphPath,
>   embeddingsPath, builtAt, macroCount} | None`；前端 contract 同步（**已定稿：
>   UI 仅展示「记忆已构建」徽标**）。
> - 产物 `runtime/source-intelligence/<index-id>/memory/{graph_memory.json,
>   embeddings.npz}`，随 sourceChecksum 失效。
> - **已定稿：新建独立 `creator_embedding_model` 配置树**（api_key 可选 reuse
>   vlm，model 默认 `qwen3-vl-embedding`，endpoint 为 DashScope 原生
>   multimodal-embedding，注意单请求 batch 上限）；`models/embedding_model.py`
>   （新薄客户端）：`async embed(inputs) -> list[vector]`，batch 切分 +
>   Throttling 指数退避。plugin.json / schemas / contracts / ModelConfigModal
>   同步增配置区块。
>
> **构建（写路径）**：`services/media/source_memory.py::build_source_memory()`
> ——source_analysis 完成常规 index 后，durationMs > 20min 且 embedding 已配置 →
> 创建后台 Task（不阻塞 index）；执行授权 + 费用预估 = f(时长)（P2 VLM 次数 ≈
> macro 数 ≈ 时长/3–8min + embedding 节点数）；P1 帧差分切割（ffmpeg）→ P2 每
> macro 一次 VLM 子图抽取 ∥ ASR 音轨转写入图 → P3 纯文本聚合 + 全节点 embedding。
>
> **查询（读路径）**：ToolSpec `query_source_memory(roles={SOURCE_INTELLIGENCE},
> requires_execution_authorization=False, wait=NONE, parameters={assetRef,
> query_type: enum[summary, super_events, macro_events, subgraph, search_nodes,
> search_ocr, search_asr, by_time, enumerate], query?, node_types?, macro_id?,
> start_ms?/end_ms?, top_k?})`；search_* 现场调 embedding（单条）；返回 JSON +
> 命中 macro 时间窗。`source_intelligence_agent.system` 增 `memory_guidance`
> 占位符（按资产是否有 memory_ref 注入）：定位 → subgraph 下钻 → **回原片窄窗
> 核验**。
>
> **投影**：P3 Root/SuperEvent 摘要写入 index 的 summary / semantic_entries 草稿
> （producer 标记 source_memory），外层 VLM 只审校。

## 测试与验收（重心）
- 单测：fixtures 预构建小型 graph_memory.json + npz 锁定 9 类查询分派与归一化；
  触发阈值 / 授权 / checksum 失效打桩；投影 schema；embedding batch 切分。
- **指定测试素材（真实构建 + 检索端到端，人工验收，全程 UI 操作）**：
  1. 猫视角法国之旅（自然场景、少台词——验视觉图谱与场景切割）：
     `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/CAT%20with%20CAMERA%20Explores%20FRANCE%20%F0%9F%87%AB%F0%9F%87%B7%20%20(%20Calming%20CAT%20POV%20).mp4`
  2. KPL 2026 夏季赛 成都AG超玩会 vs 重庆狼队 Game 5（解说密集、屏幕文字多——验
     ASR 台词检索 + OCR + 时间定位）：
     `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/%E3%80%90KPL%20Summer%202026%E3%80%91%E6%88%90%E9%83%BDAG%E8%B6%85%E7%8E%A9%E4%BC%9A%20vs%20%E9%87%8D%E5%BA%86%E7%8B%BC%E9%98%9F%20%EF%BD%9C%20Game%205%20%EF%BD%9C%20Stage%201%20-%20Jul%2003%20%EF%BD%9C%20%23honorofkings%20%23hokchannel.mp4`
- 验收口径：UI 导入 → 构建完成（授权与费用预估正确展示）→ 会话内「台词检索定位
  团战片段」（素材 2）与「语义检索定位场景转换」（素材 1），**读帧核对**定位时间窗
  与原片实际内容一致——不以工具返回文本为准。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行，本分支**最后合入**（见 dispatch/WT9）——因此集成时由你（或集成者）
  承担与 WT3 在 source_intelligence.py / schemas/assets.py / vendor NOTICE 上的
  冲突解决，开发期尽量把改动收敛在新文件/新字段。
- 热点：specialist_tools.py 只追加。
- 完成后回填总方案 WT6 节（附两个素材的构建成本、检索命中数据）。
