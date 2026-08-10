# Qwen-MM-Plugins × QwenPaw Creator 全面融入技术实现方案

> 状态：Final v1（2026-08-03）——全部决策已定稿（✅），各 WT 可按本文档直接实施
> 上游基线：Qwen-MM-Plugins，本地路径 `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins`
> （`release` 分支 commit `077aea6`，Apache-2.0 正式发布版）
> 本仓基线：各 WT 直接从当前 `dev/creator` 拉出（**例外：WT5 从
> `feat/creator-tts-voice` 拉出**）；`feat/creator-tts-voice` 与
> `feat/motion-js-timeline` 不预先合入，所有分支在**最终集成阶段**统一合并（见 §2.3）
> 原则：本文档是各并行 worktree 的唯一实现依据，技术细节以此为准，逐 worktree 迭代细化。

---

## 一、项目背景

### 1.1 两个系统各自是什么

**QwenPaw Creator**（`plugins/apps/qwenpaw-creator/`，详见其 README / README_zh）是一个
**Agentic 视频创作平台**：用户给出目标、提供素材、把控方向，由一个 Agent 团队（编剧、
导演、视觉开发、动效、剪辑等专家角色）完成规划、生成、剪辑与合成，并把每个重要决策
交还给用户确认。两条创作路线共用一个项目入口：从创意生成短剧，或把既有素材剪成成片。

其架构的核心特征是 **data-driven**：**前端与后端的每一个组件都锚定在 data model 的
定义上**，项目内容本身就是可寻址的 Agent 上下文——

- 领域层：单文件 Project 领域模型 + 文件系统持久化（无 SQLite），Timeline、Element
  Plan、资产、字幕、动效、转场全部是有 schema 定义的项目对象，可引用、可定位、可编辑、
  可审阅；Agent 与人工编辑作用于同一份实时项目状态。
- 契约层：后端所有 HTTP/内部传输走 Pydantic v2 契约（`backend/schemas/`），前端有对应
  的 TypeScript contracts（`ui/src/contracts/creator/`），两侧由 api-contract 测试锁定
  一致性。**任何新能力接入都必须先落 data model 定义，再落组件实现**——这是本方案所有
  worktree 的共同前提。
- 交互层：时间轴选区、片段、字幕、素材等对象经「添加到对话」成为 AgentDock 上下文，
  用户用自然语言精确指挥，或打开详情直接改字段。
- 治理层：模型配置按用途拆分（`creator_text_model` / `creator_vlm_model` /
  `creator_image_model` / `creator_asr_model` / `creator_video_model` /
  `creator_tts_model` / `creator_media_oss` …），三级优先级（请求作用域 tool config →
  持久化 model_config.json → 环境变量）；计费操作受执行授权
  （`requires_execution_authorization`）与创作检查点约束。
- 专家工具：`services/specialist_tools.py` 注册，注入四个可委派角色
  （`SOURCE_INTELLIGENCE` / `VISUAL_DEVELOPMENT` / `R2V_GENERATION_DIRECTOR` /
  `AI_EDITING_DIRECTOR`）。
- 素材理解：`services/media/source_intelligence.py` + `services/source_analysis/`，
  外层 VLM 多模态分析产出不可变 SourceIntelligenceIndex。

**Qwen-MM-Plugins**（本地路径
`/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins`）是面向 Qwen
模型的多模态「眼睛 + 手」原语库，六个能力包，「Agent Skill（SKILL.md）+ MCP Server
（stdio）」双形态交付：

| 能力包 | 定位 | 关键内容 |
|---|---|---|
| core 👁 | 工程化视觉读取 | read_image / read_video（budget 预算 + 32px 对齐 + 15MB 预剪）、visualize（按扩展名分派 13 个渲染器）、ocr / grounding / segmentation / transcribe_audio（qwen3-asr-flash）、web_search / image_search / web_extractor（Serper） |
| video-memory 🧠 | 长视频层次图记忆 | 构建管线（P1 帧差分场景切割 → P2 并发子图抽取 → P3 层次聚合 + embedding，含 ASR 音轨），产物 graph_memory.json + embeddings.npz；9 个纯本地查询工具 |
| video-edit ✋ | 剪辑方法论 + 生成工具 | qwen_image / qwen_tts / wan_s2v / wan_t2v / happyhorse 五个 DashScope 生成工具；交付前自评协议（六维检查、≤3 轮回看） |
| blender / freecad | 3D 工作台瘦客户端 | 与视频主链路无关，**不引入** |
| edu-agent 🎓 | 纯 Skill 无 MCP | 题目 → 讲解视频（hyperframes + TTS），经 WT7 的外置 skill 机制接入 |

上游工具契约：每个工具 = `TOOL`（Pydantic args）+ `handle(dict) -> content blocks` 的
纯函数。**本方案不以运行时依赖方式使用它**（见 §2.2），它的角色是：协议参考实现
（远程 API 类）+ 算法移植来源（本地计算类，Apache-2.0 合规 vendoring）。

### 1.2 关键上游事实（release `077aea6` 复核结论）

1. **生成类工具只从 env 读 Key**：qwen_image / happyhorse / wan_s2v / qwen_tts 均
   `get_env("DASHSCOPE_API_KEY")`，不接受 api_key 入参。
2. `shared/api_dashscope.py` 的 `API_V1` 在 **import 时求值**；pip 安装占用顶层包名
   `shared` 与 `mcp_framework`。—— 事实 1、2 与 Creator 分 Key 治理、进程纯净性相抵触，
   是 §2.2「不做 env 注入、不做进程直调」决策的直接依据。
3. **qwen3-asr-flash 走 `MultiModalConversation`**（aigc 多模态对话 API），与 Creator
   现有 fun-asr 的 `services/audio/asr/transcription` 异步文件转写 API 不是同一 endpoint。
4. video-memory MCP 查询工具 9 个：get_summary / get_super_events / get_macro_events /
   get_subgraph / search_nodes / search_ocr_text / search_asr_text / search_by_time /
   enumerate_events；构建脚本目录另含 `merge_memories.py`（多段合并，仅构建侧脚本，
   本期不引入）。embedding 走 DashScope 原生 multimodal-embedding endpoint，模型
   `qwen3-vl-embedding`，单请求 batch 有上限。
5. P1 分段为 **帧差分场景切割**（ffmpeg，纯本地），构建成本主要在 P2 的 VLM 调用
   （次数 ≈ MacroEvent 数）与 embedding 向量化。
6. **visualize 渲染器 13 个**（`qwen_mm_plugins_core/renderers/`）：pdf（pypdfium2）、
   office（libreoffice 中转）、data（pandas/matplotlib 表格）、subtitle、code、svg
   （resvg-py）、notebook、web（playwright）、latex、model3d、geo、drawio、
   _blender_render；懒加载，依赖可按子集裁剪。
7. **`image_search` 的公共 host 问题**：Serper Lens API 的 payload 只有
   `{"url": image_url}` 一种形态（Google Lens 封装，**不支持 base64 / 文件上传**），
   必须给公网可访问 URL。上游为保持 OSS-free（外部用户无 OSS），把本地图上传到
   **uguu.se（匿名免费临时图床）**换 URL（`PUBLIC_UPLOAD_URL`）；其 core `oss.py`
   （OSS_AK/SK + oss2 签名 URL）只服务视频帧场景，image_search 并未使用。另注意：
   Creator 的 DashScope 临时存储产出的 `oss://` URL 仅 DashScope 模型端可解析
   （需 resolve header），**不是公网 URL，同样不能给 Serper**。Creator 侧替代方案
   见 WT2。
8. 主许可证 **Apache-2.0**，无 AGPL 成分——移植合规可行，义务见 §2.2。
9. 生成协议实测细节（供手法 A 对照，均从上游源码核验）：
   - happyhorse 提交路径 `services/aigc/video-generation/video-synthesis`（异步）；
     模型名按模式分化：`happyhorse-1.0-t2v` / `-i2v` / `-r2v` / `-video-edit`；
     t2v `input={"prompt"}`；i2v `input.media=[{"type":"first_frame","url"}]`；
     video_edit `input.media=[{"type":"video","url"}]`（输入 3–60s，>15s 自动截前
     15s，时长跟随输入）；parameters 含 resolution（720P/1080P）/duration（3–15s）。
   - wan_s2v：detect `POST {API_V1}/services/aigc/image2video/face-detect`，
     `{"model":"wan2.2-s2v-detect","input":{"image_url"}}`（免费）；generate 模型
     `wan2.2-s2v`，`input={"image_url","audio_url"}`，`parameters={"resolution":
     "480P"|"720P"}`；人像图约束单边 400–7000px；audio_url 必填。
   - qwen_image：同一 multimodal-generation endpoint；t2i 与 edit 用
     `qwen-image-2.0-pro`（messages 内图片 content 在前、text 指令在后，edit 1–3 图），
     translate 用 `qwen-mt-image`；parameters 含 `size`（W*H）与 `watermark:false`。
   - 重试参考值：service 线性重试 ×10（backoff 1s）；Throttling.* 指数退避 ×4
     （base 2s + jitter 1s）；轮询 GET+json 整体包在重试内（已计费任务不可因瞬断放弃）。

### 1.3 已完成部分：TTS 接入（`feat/creator-tts-voice`）

- `models/tts_model.py`：httpx 直连 DashScope 原生 REST 的**协议对齐薄客户端**
  （qwen3-tts-flash 合成 + qwen-voice-enrollment 音色克隆，后者超出 mm-plugins 能力）；
- `creator_tts_model` 配置树贯穿 plugin.json → `models/config.py` → schemas →
  前端 contracts → ModelConfigModal，**data model 先行**；
- 专家工具 `tts_generation` / `create_character_voice` 按 Key 动态注册
  （`is_tts_configured()` gate）；
- `services/media_files/audio_execution.py` 完成混音 / ducking / 落盘，Asset Index 已有
  audio kind。

**TTS 范式六要素**（后续所有能力接入必须复刻）：
① 一切能力封装为 Creator 原生工具/服务，mm-plugins 仅作参考实现；
② data model 定义先行（Pydantic schema + 前端 contract 同步）；
③ `creator_*_model` 三级配置树（涉及模型配置时）；
④ 可选能力按配置动态注册，未配置零负担；
⑤ 计费操作 `requires_execution_authorization=True` + 现有 poller（`wait=TASK`）；
⑥ 产物经 AssetFileStore 落盘写 Asset Index（远端 URL 24h 过期，必须落盘）。

---

## 二、总体融入要求与安排

### 2.1 八条融入要求

| # | 要求 | 实现路径 | 承接 |
|---|---|---|---|
| 1 | qwen3-asr-flash 支持——现有配置先实测 | asr_model.py 内按 model 名分派协议分支 | WT1 |
| 2 | 自评独立模块，代码级总开关，暂不开放前端 | `services/render_review/` + config 开关；重心 prompt 调试与实际 case | WT4 |
| 3 | 生成侧更多模型支持 | qwen_image edit/translate；视频 t2v/i2v/edit 模式矩阵 | WT5 |
| 4 | 数字人为额外 provider | `models/s2v_model.py` + `creator_s2v_model` | WT5 |
| 5 | 长素材记忆与素材理解合并 | video-memory 移植进 Source Intelligence | WT6 |
| 6 | 多格式读取转为 Agent 工具，融入素材理解 | visualize 渲染器移植 + `read_document` 工具 | WT3 |
| 7 | edu-agent 以 skill 接入 | 外置 skill 手动配置机制，edu-agent 首个用例 | WT7 |
| 8 | Qwen-MM Grounding 能力完整并入 Creator，Creator 不低于上游能力基线 | 原生实现 `web_search` / `image_search` / `web_extractor` 等价能力，含 Lens、bbox、候选二次确认、重试与 OSS→免费图床自动路由 | WT2 |

统一取舍：blender / freecad 不引入；video-edit 剪辑工作流不引入（只吸收自评协议）；
qwen_tts 已被 TTS 分支覆盖且超出，不再接。

### 2.2 统一接入原则：一切封装为 Creator 原生工具

**硬性约束：不引入 `qwen-mm-plugins` 为运行时依赖；不做任何 env 注入；不做任何
进程内直调 handle。** 两种落地手法：

**能力基线约束：凡本方案明确接入的 Qwen-MM-Plugins 能力，Creator 的原生实现必须
覆盖上游已经提供的输入形态、核心工作流、失败降级与真实使用场景；可以增强安全性、
可观测性和数据结构，但不得以“架构不同”为由删减能力。Grounding 的最低基线以
Qwen-MM-Plugins `web_search` / `image_search` / `web_extractor` 及
`video_search.md` 的“先识别、再检索、再抽取确认”工作流为准。**

- **手法 A · 协议对齐薄客户端**（远程 API 类）：对照 §1.2-9 的协议细节在
  `backend/models/` 或 provider 层自建 httpx 客户端，Key 走 `creator_*_model` 配置树。
- **手法 B · 算法移植（Apache-2.0 合规 vendoring）**（本地计算类）：移植为 Creator
  内部代码。合规义务：移植文件头保留上游版权声明并标注修改（Apache-2.0 §4b）；
  `backend/vendor/NOTICE.md` 集中声明来源仓库、commit `077aea6`、许可证与模块清单；
  移植代码集中放 `backend/vendor/mm_plugins/`（WT3 定稿目录样板，全项目沿用）。

  **vendor 目录样板（WT3 已定稿并落地，全项目逐字沿用）**：
  - 布局：`backend/vendor/{__init__.py, NOTICE.md}` +
    `backend/vendor/mm_plugins/{__init__.py, image_budget.py}` +
    `backend/vendor/mm_plugins/renderers/{__init__, pdf, office, data,
    subtitle, code, svg, notebook, web}.py`；
  - 文件头模板（置于 coding pragma / flake8 noqa 之后，逐字）：
    ```python
    # Vendored from Qwen-MM-Plugins (Apache-2.0), release commit 077aea6.
    # Upstream path: <上游仓内路径>
    # Modified for QwenPaw Creator. See backend/vendor/NOTICE.md.
    ```
  - NOTICE.md 含来源仓库/commit/许可证 + 逐模块修改清单表；
  - **跨 WT 共用文件 `image_budget.py`**：内容以 `feat/creator-doc-reader`
    分支该文件为准逐字一致（`budget_to_pixels`/`smart_resize` +
    内联常量 `TOKEN_SIZE=32`、`DEFAULT_BUDGET="normal"`、
    `IMAGE_BUDGET_TOKENS={"small":256,"normal":1024,"large":2048}`、
    `IMAGE_MIN_PIXELS`）；WT4/WT6 若未见到已合代码则按本样板自行移植，
    集成时机械归一。

### 2.3 Worktree 划分与最终集成

**合并策略（已定稿）：不做任何前置合并，全部分支开发完成后在最终集成阶段统一
合入 `dev/creator`。**各 WT 从当前 `dev/creator` 拉出；开发期间只推自己的特性
分支，**不发起合并、不互相 rebase**；跨分支依赖一律降为接口约定（见下表备注），
冲突留待集成阶段按序解决。

| WT | 分支 | 内容 | 拉取基线 | 端口 |
|---|---|---|---|---|
| WT1 | `feat/creator-asr-qwen3` | qwen3-asr-flash 实测 + 协议分支 | dev/creator | 8091 |
| WT2 | `feat/creator-grounding-serper` | Serper 并入 web grounding | dev/creator | 8092 |
| WT3 | `feat/creator-doc-reader` | 多格式文档读取 + 融入素材理解 | dev/creator | 8093 |
| WT4 | `feat/creator-self-review` | 自我审阅模块 + 总开关 | dev/creator | 8094 |
| WT5 | `feat/creator-gen-providers` | 图像双模式 → 视频模式矩阵 → s2v | **feat/creator-tts-voice**（5c 依赖 TTS audio 资产与动态注册范式） | 8095 |
| WT6 | `feat/creator-source-memory` | video-memory 移植并入素材理解 | dev/creator | 8096 |
| WT7 | `feat/creator-external-skills` | 外置 skill 机制 + edu-agent（纯后端配置） | dev/creator | 8097 |
| WT8 | `feat/motion-js-timeline`（既有） | hyperframes 动效引擎完善 | 既有分支原地继续 | 8098 |

跨分支接口约定（开发期解耦的依据）：
- WT6 的 ASR 转写一律经 `asr_model.transcribe(media_url)` 现有接口调用（开发期用
  fun-asr 即可验证），不直接依赖 WT1 代码；集成后自动获得 qwen3-asr-flash。
- WT4 与 WT3/WT6 共用的 vendored `image_budget.py`：先完成方定目录样板，后继方
  若未见到已合代码则自行同规范移植同一文件，集成时归一（实现必须逐字相同源自
  上游同一文件，冲突即可机械解决）。
- WT4 挂钩与 WT8 分支共同触碰 `local_execution.py`：WT4 保持单点挂钩（成片发布
  后一处 if），集成时 motion 先合、self-review 后合。

**最终集成阶段（独立任务，见 dispatch/WT9 修订版 v3）**：采用**功能维度分线先行
合并**——集成分支 `integration/mm-plugins` 上按五条功能线依次合并，每线合完立即
做该功能的完整真实验收（对应 acceptance 文件），使每个增量都是可独立交付、可
独立归因的用户能力：F1 检索增强（WT2）→ F2 内容生成（WT5，含 TTS，合后验证
TTS 分支冗余并归档）→ F3 成片质量（WT8→WT4）→ F4 素材理解（WT1→WT3→WT6）
→ F5 生态扩展（WT7，可插任意间隙）→ 阶段 E 仅验跨功能交叉链路 → 一次性合回
dev/creator。归因机制：逐分支 `--no-ff` merge + tag + 快门禁，失败 30 分钟定位
不了即 revert 退回。背景：主线开发期已合入计划外 PR #64/#68/#69/#73，各分支已
rebase 至新基线。完整规范以 `dispatch/WT9-final-integration.md` 为准。

冲突热点（开发期各自只追加、集成期按序解决）：`specialist_tools.py`（WT3/5/6/7
只追加 ToolSpec，不改既有条目）；`models/config.py`（WT1 轻 / WT4 开关 / WT5 配置树
/ WT7 skill 读取）；`plugin.json`（WT2/3/5 各自追加 block）；`ModelConfigModal.tsx`
（WT2 grounding 区块 / WT5 image/video/s2v 区块）；`source_intelligence.py` 与
`schemas/assets.py`（WT3 轻 / WT6 重）；`prompts/`（WT4/6/7 各自新增 placeholder，
占位符校验机制天然防冲突漏改）；`local_execution.py` 与
`ai_editing_director.system.txt`（WT8 分支与 WT4 交叠，集成时 motion 先合）。

### 2.4 隔离开发栈约定

每个 worktree 运行独立栈：根目录 `dev-isolated.sh`（记入 `.git/info/exclude`），独立
`QWENPAW_WORKING_DIR` 与端口（主实例 8088 不动）。模型凭据复制
`~/.qwenpaw-poc/creator-runtime/config/model_config.json`（主实例数据根为
`~/.qwenpaw-poc`，端口 8088），跨 workspace 解密需设
`QWENPAW_KEYRING_ACCOUNT`。

### 2.5 全局质量门禁

- pre-commit + 双 pytest 门禁；注释精简且英文。
- data model 变更必须同步 Pydantic schema、前端 contract 与 api-contract 测试。
- 生成结果验收必须**查看实际媒体内容**，不允许仅以 URL 存在 / HTTP 200 判定。
- 端到端验收以新手用户视角走前端 UI，不绕过 UI 直调 API 改数据。
- 计费模型自动化测试默认打桩（respx），真实调用仅在人工验收步骤且事先确认成本。
- **真实模型验证范围限定为阿里系百炼（DashScope）模型**：含 LLM/VLM、
  qwen3-asr、qwen-image、wan/happyhorse 视频、qwen3-tts 系列 TTS 与音色复刻、
  qwen3-vl-embedding。**非百炼 provider 不做真实调用验证**（seedance2/火山
  引擎、OpenAI 兼容 image、Whisper 等），只保留打桩单测与本地校验/配置回显测试。
- 每个 WT 除开发期打桩测试外，另有配套的**真实模型调用测试项目**：见
  `acceptance/WT1-*.md` … `WT9-*.md`，与 `dispatch/` 同号配对，作为各 WT 交付
  验收依据（TTS 基线回归见 acceptance/WT5 的 5t 组）。

---

## 三、各 Worktree 技术方案

> 状态：✅ 已定稿（全部）。实现合入 `dev/creator` 后回填实际差异。

### WT1 · qwen3-asr-flash 支持（`feat/creator-asr-qwen3`）🔵

**现状事实**（`models/asr_model.py`）：
- 对外入口 `transcribe(media_url: str) -> ASRResult`；
  `ASRResult{provider, model, segments: tuple[ASRSegment]}`，
  `ASRSegment{start_ms, end_ms, text, confidence=1.0, speaker=None}`。
- 分派：`get_asr_provider()` 为 "whisper" 走 `_whisper()`，否则 `_fun_asr()`。
- `_fun_asr()`：`_fun_asr_file_url()`（本地/视频经 ffmpeg 抽音轨 →
  `upload_local_file_to_dashscope_temp()` 得 48h `oss://` URL）→ POST
  `{base}/services/audio/asr/transcription`（`X-DashScope-Async: enable`）→ 轮询
  `tasks/{task_id}` → 下载 transcription.json → `_sentences()` 解析。
- config getter 已齐：`get_asr_api_key/base_url/model_name/provider/language/
  timeout_seconds`、`is_asr_enabled`。**现有实现无重试机制**（失败直接抛）。

**Step 1 · 零代码实测**：隔离栈（8091）UI 配置 ASR model=`qwen3-asr-flash`
（provider 不动），走一次 `transcribe_source_audio`。预期在 transcription 提交或任务
阶段报模型不支持错误；记录确切报错入本节。

**Step 2 · 协议分支**（手法 A）：
1. `transcribe()` 的 fun-asr 分支前插入：
   `if model.casefold().startswith("qwen3-asr"): return await _qwen3_asr(media_url)`。
   不新增 provider 枚举 / 配置项。
2. 新函数 `_qwen3_asr(media_url) -> ASRResult`：
   - 复用 `_fun_asr_file_url()` 取 `oss://` URL（多模态端点同样支持
     `X-DashScope-OssResourceResolve: enable`，Step 1 一并实测确认；不成立则回退为
     公网 http URL 路径）；
   - 分块：ffprobe 取时长，>270s 时 ffmpeg `-f segment -segment_time 270` 按 4.5min
     切块（静音边界对齐为增强项，首版固定步长即可），逐块上传+转写；
   - 请求：POST `{asr_base 的 host}/api/v1/services/aigc/multimodal-generation/generation`，
     body `{"model": model, "input": {"messages": [{"role":"user","content":
     [{"audio": url}]}]}, "parameters": {"result_format":"message",
     "asr_options": {"language": get_asr_language() or omitted}}}`；
   - 解析 `output.choices[0].message.content[*].text` 得句子序列；块内句子按时长均摊
     start/end（`confidence=0.0` 标记为估算，下游据此区分精度），跨块加块偏移；
   - 返回 `ASRResult(provider="fun-asr", model=model, segments=...)`（provider 字段
     维持 dashscope 家族语义，避免 schema 变更）。
3. 重试（对照 §1.2-9 参数）：模块级新增
   `_post_with_retry(client, url, payload, *, attempts=3, throttle_attempts=4)`——
   瞬断线性退避、`Throttling.*` code 指数退避；仅新分支使用，不动 fun-asr 现行为。

**测试**：单测（respx）——model 分派、分块偏移回填、Throttling 重试、空音轨、
`language` 透传；集成（真实 Key 人工）——<5min 与 >5min 素材各一段，抽查 segments
与原音一致。**验收**：UI 配好模型名后 Source Intelligence 面板可见转写产出，无 UI 改动。

---

### WT2 · Serper 并入 Web Grounding（`feat/creator-grounding-serper`）🔵

**现状事实**（`services/web_grounding/providers/`）：
- 文本链 `search.py::search_web(query, max_sources, timeout)`：Tavily（有 key 时）→
  无结果且 `native_search_enabled` 时 DashScope web_search；返回
  `{query, sources, issues, provider, providers, providers_attempted}`。
- 视觉链 `search_visual_refs()`：按 `visual_search_provider_order()`（默认
  `("tavily","dashscope_web_search_image")` 过滤可用项）**以文搜图**，达
  min_results(2) 停止。**现状没有任何以图搜图路径**。
- source 归一化结构：文本 `{title,url,snippet,provider,query,score}`；视觉
  `{url,thumbnail_url,source_url,title,provider,query,...}`。
- 配置链：`get_web_grounding_tavily_api_key()` 等 getter 齐备；plugin.json
  `creator_web_grounding` 已有 enabled / tavily_api_key / native_search_enabled /
  search_* / validation_source 等字段；前端 `GroundingConfig extends ModelConfigItem`。

**改动**：
1. `providers/adapters.py` 增（与 `_search_tavily*` 同构）：
   - `_search_serper(client, query, limit) -> list[dict]`：POST
     `https://google.serper.dev/search`，header `X-API-KEY`；organic 结果映射
     `{title, url, snippet, provider:"serper", query, score:None}`；
   - `_search_serper_visuals(client, query, limit)`：POST `/images`，映射视觉 source
     结构（provider:"serper"）。
2. `providers/serper.py`（新）：`SERPER_SEARCH_URL` / `SERPER_IMAGES_URL` 常量
   （对齐 tavily.py 只放常量的分层）。
3. `providers/config.py`：`serper_api_key()`（转调新 getter）；
   `DEFAULT_VISUAL_SEARCH_PROVIDERS = ("tavily","serper","dashscope_web_search_image")`；
   文本链顺位同理 Tavily → Serper → DashScope（成本/召回折中，评审可调）。
4. `providers/search.py`：两条链各插入 serper 尝试，`providers_attempted` 照记。
5. 配置贯通（data model 先行）：
   - `models/config.py`：`get_web_grounding_serper_api_key()`（grounding config
     `serper_api_key` → env `SERPER_API_KEY` / `WEB_GROUNDING_SERPER_API_KEY`）；
   - plugin.json `creator_web_grounding.config_fields` 追加
     `{"name":"serper_api_key","type":"password",...}`；
   - `schemas/models.py` GroundingConfig + `ui/src/contracts/creator/models.ts`
     GroundingConfig 增 `serper_api_key`；ModelConfigModal grounding 区块加输入框。
6. **Serper Lens 以图搜图（必交付）**——对齐 Qwen-MM-Plugins `image_search`：
   - Lens API 只收 `{"url"}`，无 base64/文件上传形态；DashScope 临时存储的
     `oss://` 非公网不可直接使用；
   - 输入同时支持 Creator 本地图片引用和已通过 SSRF 校验的公网 http(s) 图片 URL；
   - 支持可选 `bbox=[x1,y1,x2,y2]`，使用 0–1000 归一化坐标。需要裁剪时先取得安全的
     本地副本，再严格校验 bbox、裁剪并重新托管；裁剪失败必须返回可读 issue，不能
     静默退回整图搜索；
   - **本地/裁剪后图片的公网 URL 获取采用自动二选一路由**：检测到完整可用的
     `creator_media_oss` 配置时，只走该 OSS，私有上传后用 oss2
     `bucket.sign_url("GET", key, 900)` 生成 15min presigned URL；没有配置 OSS 时，
     自动 POST `https://uguu.se/upload`，multipart 字段为 `files[]`，取得免费临时
     公网 URL 后调用 Lens；
   - “配置了 OSS 但 OSS 上传/签名失败”必须返回明确 issue，并继续改传免费图床，
     确保 Lens grounding 不因 OSS 故障中断；
   - OSS readiness 必须显式分为 `absent / ready / invalid`：全部 OSS 字段为空才是
     `absent` 并走 Uguu；必填字段齐全且校验通过才是 `ready` 并优先走 OSS；只要填写了
     任一 OSS 字段但配置不完整或无效即为 `invalid`，保留配置 issue 后走 Uguu；
   - Uguu 不增加任何用户配置或密钥字段；上传成功响应必须解析 `files[0].url` 并校验为
     公网 HTTPS URL。托管通路统一记录为 `direct_url / creator_oss / uguu`，失败使用
     稳定 issue code（如 `creator_oss_invalid`、`creator_oss_upload_failed`、
     `uguu_upload_failed`），但不得把签名 URL/临时 URL 写入长期持久化来源字段；
   - 继续保留 Creator 更严格的输入保护：公网 URL SSRF 校验、本地路径限制在
     `CREATOR_DATA_ROOT`、防符号链接逃逸、真实光栅图解码校验和 8MB 上限。
7. **补齐 Qwen-MM Grounding 能力基线（必交付）**：
   - 新增 Serper `/scrape` 的 Creator 原生 `web_extractor` 等价能力，输入为 URL 列表
     与 goal，携带 `includeMarkdown:true`，每个页面最多保留 8000 字符，并保留来源 URL；
   - 文本搜索支持一组 queries，逐 query 保留结果归属、标题、摘要、日期和 URL；
   - 对画面中的具体人物、地点、品牌、型号、事件等身份或事实，执行“Lens 发现候选
     → `web_search` 交叉检索 → 对最相关 URL 执行 `web_extractor` → 证据一致后确认”的
     闭环；证据不足或冲突时不得把视觉猜测写成确定事实；
   - 对齐并增强上游重试能力：web search 与 Lens 最多 10 次、Uguu 上传最多 5 次、
     scrape 最多 3 次，指数退避上限 10 秒；只对网络错误、超时、429 和 5xx 重试，
     其他 4xx 立即失败；所有尝试写入 `providers_attempted` / issues；
   - 上述能力封装为 Creator 原生 grounding pipeline，不增加 qwen-mm-plugins
     运行时依赖，也不要求 Agent 直接调用上游 MCP。

**测试**：respx 打桩 Serper `/search`、`/images`、`/lens`、`/scrape` 与 Uguu
`/upload`；覆盖多 query、bbox、本地/公网图片、OSS 有配置、OSS 无配置、OSS 已配置但
失败、瞬时错误重试、不可重试 4xx、候选二次检索与网页抽取；隔离栈分别用“有 OSS”
和“无 OSS”两态跑真实 Lens，并核对内容、来源、providers_attempted 与 issues。

**已定稿决策（2026-08-04 修订）**：① Serper 顺位定在 Tavily 之后、DashScope
之前；② Qwen-MM-Plugins 已有的 Grounding 能力是 Creator 的最低能力基线，Lens、
bbox、网页抽取和候选确认闭环均为必交付；③ 本地/裁剪图片有 OSS 配置时走配置的
OSS，没有 OSS 配置时自动走 Uguu 免费临时图床。

**实现回填（feat/creator-grounding-serper，2026-08-03，含首轮评审修复，与定稿
规格的实际差异）**：
1. Serper 请求体携带上游 qwen-mm-plugins 实测同款区域参数（/search、/images：
   `gl/hl/location`；/lens：`gl/hl`）并传 `num`；经确认“与 mm-plugins 保持一致
   即可，不做 Serper 真实 Key 验收”，Serper 三端点均以 respx 打桩验证真实
   URL/请求体；Tavily 链路沿用既有 `_FakeClient` 样板。此处是 2026-08-03 的历史
   实现记录；“不做真实 Key 验收”已被 2026-08-04 修订后的验收标准废止。
2. `_search_serper_lens(client, image_url, limit, *, query="")` 比方案签名多
   limit/query（对齐兄弟 adapter；query 仅作结果标注，签名 URL 不泄入结果）。
3. Lens 接入点：context entities 支持 `reference_image`（triage 归一化透传 →
   visual_jobs → pipeline 先 Lens 后文本回退，issues/providers_attempted 并入
   trace）；提供 `search_visual_refs_by_image()` 供后续复用。
4. **Lens 输入安全边界（评审 P1 修复）**：http(s) 引用必须通过
   `validate_public_remote_url()` SSRF 公网校验（拒绝 loopback/私网/保留地址/
   带凭据 URL）；本地引用 resolve 后必须落在 `CREATOR_DATA_ROOT` 内（防提示
   注入外泄任意本地文件），并复用 staging 的光栅图验证 + 8MB 上限；均降级为
   可读 issue。本地素材 presign：
   `media_transport.upload_image_for_temporary_public_url()`（私有 ACL + oss2
   `bucket.sign_url("GET", key, 900)`，前缀 `grounding_lens/`）。
5. **serper_api_key 入密钥保护链（评审 P1 修复）**：加入
   `model_routes._SECRET_FIELDS`，加密落盘、GET 返回 `__CREATOR_SECRET__`
   掩码、保存时掩码保留原值（既有 tavily_api_key 仍为明文历史行为，未动）。
6. 文本链 serper 无 key 时记 `serper_api_key_missing` issue；前端
   ModelConfigModal grounding 区块新增“次选 Serper”卡片，`ModelBadges.tsx`
   searchReady 已识别 serper-only 配置（评审 P2 修复）。
7. **Tavily 套餐兼容 bug 修复**：文本/图片搜索默认不再发送 `safe_search`
   （非企业套餐返 403），`TAVILY_SAFE_SEARCH=1` 可显式开启；隔离栈 8092 用真实
   Tavily Key 实跑验证检索成功（中/英文均 200，来源列表与
   providers_attempted 正确；serper 无 key 时正确跳过并记录）。
8. 门禁：black/flake8/pylint（含 R0911 重构）全绿；creator backend 专项测试、
   UI vitest 278 条、tsc 全绿；共享机高负载（load≈39）下全量后端偶发超时型
   失败，已在未改动基线复现同样失败，非本分支引入。
9. **修订后待补齐项（不是当前实现）**：当前分支仍只有 OSS 本地图通路，尚缺
   Uguu 自动 fallback、bbox、Serper `/scrape`、Lens 候选驱动的二次检索/抽取确认和
   上述有界重试；这些项目已由可选增强提升为 WT2 必须完成项，完成并通过修订后的
   真实验收前，WT2 不得标记完成。

---

### WT3 · 多格式文档读取并融入素材理解（`feat/creator-doc-reader`）🔵

**现状事实**：Source Intelligence 目前仅接受 image/video/audio；visual 模态由外层
VLM 按动态 fps 采样（`native_media.py::_source_video_sampling_fps`：≤120s→2fps、
120–600s→1fps、>600s→0.5fps）；index 定义在 `schemas/assets.py::SourceIntelligenceIndex`
（media / coverage / shots / transcript / semantic_entries…），持久化为 index.txt /
summary.md / shots/ 子目录。专家工具返回 JSON 文本 + `resultRef`，**图像内容进 VLM
上下文走 runtime 的多模态消息机制**（native_media payload），不走工具返回体。

**移植范围（手法 B，vendoring 样板）**：落点
`backend/vendor/mm_plugins/`，`NOTICE.md` + 文件头版权/修改标注：
- `visualize/renderers/`：首批 `pdf.py`（pypdfium2 光栅化+文本层）、`office.py`
  （libreoffice 中转 PDF）、`data.py`（pandas/matplotlib 表格）、`subtitle.py`、
  `code.py`；次批 `svg.py`（resvg-py）、`notebook.py`；`web.py`（playwright）默认
  不装、配置化开启；**不引入** latex / model3d / geo / drawio / _blender_render。
- `image_budget.py`：移植 `shared/image.py` 的 `budget_to_pixels()` /
  `smart_resize()`（32px 对齐 + token 预算），供本 WT 与 WT4 共用。

**改动**：
1. `services/document_reader.py`（新）：
   - `async def read_document(file_path: Path, *, pages: str | None, budget:
     Literal["small","normal","large"]) -> DocumentReadResult`；
   - `DocumentReadResult{format, page_count, pages_rendered: list[int],
     page_images: list[Path]（落盘到项目 runtime 的 doc-pages/ 目录）,
     text_excerpt: str, notes: list[str]}`；
   - 扩展名分派 vendored 渲染器；缺依赖（如 libreoffice）→ 可读错误说明安装方式。
2. **图像入上下文的适配**（与上游最大差异，必须遵循）：渲染页图**落盘为 runtime
   文件并返回 fileRef**，由 file agent runtime 以既有多模态消息机制注入 VLM 上下文；
   不在工具返回体内放 base64。
3. data model：`schemas/assets.py` 增 document 资产元数据（`DocumentMetadata{format,
   pageCount}`），IndexedFile.kind 增/复用 `document`；前端 contract 同步。
4. `specialist_tools.py` 追加 ToolSpec：
   `ToolSpec(name="read_document", roles={SOURCE_INTELLIGENCE},
   requires_execution_authorization=False, long_running=False, wait=NONE,
   parameters={fileRef: required, pages?: str, budget?: enum})`；
   `invoke()` 分派到 document_reader；fileRef 限项目资产边界（复用现有边界校验）。
5. **融入素材理解**：`source_analysis/service.py` 识别文档型 source →
   document_reader 渲染 → 产出文档版 index：`media.mediaKind="document"`、页级
   `shots` 类比（每页一个 shot，keyframe.ref 指向页图）、全文进
   `semantic_entries`；`source_intelligence.py` 渲染层增 pages 支持（轻改动，
   与 WT6 错开）。coverage 复用 visual 模态（producer 标记 document_reader）。
6. 依赖：plugin.json `dependencies` 增 `pypdfium2`、`pandas`、`matplotlib`、
   `openpyxl`、`tabulate`（次批 `resvg-py`、`nbformat`）；`runtimeDependencies` 增
   libreoffice（optional，同 jq 机制）。

**测试**：每格式一个 fixture 断言页数/blocks/32px 对齐/文本层；越权 fileRef 拒绝；
libreoffice 缺失降级；文档导入 → 产出文档 index 的集成测试。人工验收：UI 上传 PDF
剧本 → Agent 读文档复述结构，肉眼核对页图。

**已定稿决策**：① `read_document` 首版仅注入 SOURCE_INTELLIGENCE；② vendor 目录
以 `backend/vendor/mm_plugins/` 定稿（全项目 vendoring 样板）。

**实现回填（feat/creator-doc-reader，2026-08-03，与定稿规格的实际差异）**：
1. 工具编排层落在 `source_analysis/service.py::read_source_document`（复用
   `_resolve_agent_source_sync` 边界解析 + `_source_module_result_ref` 机制）；
   `services/document_reader.py` 为纯渲染层，签名增了 `output_dir` 关键字
   （页图目录由调用方给定）。页图落盘于
   `<project_root>/runtime/doc-pages/<checksum前16>/page-XXXX.png`，引用格式
   `doc-page://<sourceChecksum>/<page:04d>`。
2. 页图入上下文：`driver.py` 在 read_document 工具结果后追加一条多模态 user
   消息（`native_media.document_page_content_parts` 上传页图至 DashScope 临时
   存储 → image_url parts），工具返回体内无 base64；注入失败降级为文本提示。
3. data model：`DocumentMetadata{format, pageCount}` 作为
   `SourceMediaMetadata.document` 可选嵌套字段；index.txt codec 新增
   `documentFormat`/`documentPageCount` media 行（round-trip 验证覆盖）；
   `CoverageProducer` 增 `"document_reader"`；前端 `contracts/creator/assets.ts`
   增 `DocumentMetadata`。**IndexedFile.kind 未新增值**：文档上传复用
   `source_original` + `media_kind="document"`（既有 schema 已支持），页图为
   runtime 文件不入 Asset Index。
4. 文档版 index：页伪时间线 `[(N-1)*1000, N*1000)`，有页图时 commit 强制“每渲染页
   恰好一条 shot 且区间逐字匹配”，shot 的 keyframeRef/evidence 指向页图 ref；
   字幕/纯文本/代码等无页图格式（pagesRendered 为空）提交恰好一条
   `[0,1000)` shot，keyframeRef 回退到 evidence ref；provenance 含全部页图
   ref；semanticEntries 禁时间范围（页码进 tags）；**read_document 的
   textExcerpt 由 commit 确定性写入 semanticEntries**（段落对齐分块
   ≤2000 字符，tags=["document-text","chunk-N"]，归属独立的
   document_reader 模块 model run）；coverage.visual = available /
   document_reader / ratio=已渲染页数÷总页数（文本型为 1.0）；
   `moduleResultRefs` 增 `document`（对齐 asr 的 resultRef 引用机制，
   commit 工具 JSON Schema 同步声明）；`_probe_media` 对 document/text 跳过
   ffprobe（遗留 text 源按扩展名可读性归一为 document/other）。
5. 格式范围实际交付：首批 pdf/doc/docx/ppt/pptx/vsdx/csv/xlsx/srt/vtt/ass/
   纯文本与代码全部落地；次批 svg/notebook 渲染器也已 vendored（懒加载，
   resvg-py/nbformat 未列入 dependencies，缺依赖时返回安装提示）；web.py 已
   vendored，默认关闭，`CREATOR_DOC_READER_WEB_ENABLED=1` 开启。上传入口
   `_media_kind` 按“AV MIME 前缀 → 受支持扩展名 → 文档类 MIME 标记 →
   text/* → other”分类，保证 text/csv、字幕、legacy Office MIME 均进入
   document 流程。
6. libreoffice 探测链：`CREATOR_LIBREOFFICE_PATH` → PATH（libreoffice/
   soffice）→ macOS 应用路径；无自动安装；plugin.json runtime_dependencies
   已声明（optional）。
7. `source_intelligence_agent.system.txt` 新增“文档理解内容要求”静态章节
   （无占位符变更，避开 prompt 隐藏机制措辞门禁）。
8. 验收返工回填（2026-08-03 第二轮，A3/A4/A5/B1/B2/B4/B6 修复）：
   ① vendored `smart_resize` 改为超预算分支 floor（对齐官方
   qwen_vl_utils，上游 round 对齐会超预算最多一行/列 patch），三档均
   不超预算且 32px 对齐；② 表格渲染增 `configure_matplotlib_cjk`（优先
   PingFang/Noto 等已装 CJK 字体）修复中文 tofu；③ A4 中文丢失根因为
   测试环境便携版 LibreOffice 无 CJK 支持，正式安装（brew cask）后版式
   与中文完整保留；转换超时放宽到 180s（macOS 首启 Gatekeeper 扫描
   >80s）；`resolve_libreoffice` 增补 homebrew/用户目录/Linux 常见路径探测
   （服务进程 PATH 可能极简）；④ B1/B2：资产卡标签区分“来源 · 文档”，
   详情面板新增 `DocumentUnderstanding` 组件（格式/页数/摘要/页级条目+
   页图缩略图），新增 HTTP 路由
   `GET /projects/{id}/doc-pages/{checksum}/{page}` 服务页图 PNG；
   ⑤ B6：`ATTACH_SOURCE` 上传入口拒绝 media_kind=other 的不可读格式
   （如 .glb）并返回可读提示；⑥ 新增 `tests/manual/
   test_real_document_reader.py`（`manual_real` marker，默认 addopts 排除，
   `CREATOR_DOC_FIXTURES` 指定验收素材）覆盖 A1-A6/A9。
9. 二轮 CR 修复回填（2026-08-04）：① `smart_resize` 极端长宽比（如
   1×10000）短边 clamp 后补长边收缩，参数化扫描（网格+随机 500 组×三档）
   零超预算；② 全文/摘要分离：`DocumentReadResult.full_text`（上限 50 万
   字符，code/data 渲染器提限至 2 万行/2000 行）落盘
   `runtime/doc-pages/<checksum16>/text-<ref>.txt`，commit 从落盘全文分块
   入 semanticEntries（工具返回体仍只携 20k excerpt + fullTextChars）；
   ③ UI 文档理解面板按 source version 绑定 intelligence version（未分析
   版本显示“该版本尚未理解”，不再回退到 current 版本）；④ SVG 按扩展名/
   MIME 特判为 document 进 read_document 流程；⑤ B6 上传拒绝增持久 inline
   错误横幅（antd toast 瞬时性导致验收不可见）+ mock 422 前端回归；
   ⑥ 表格空单元格不再渲为 "nan"；manual_real 纳入 A8 越界拒绝。
10. 三轮 CR 修复回填（2026-08-04）：① 全文提取与渲染范围彻底解耦：渲染器
    新增 `full_text` block 类型（pdf 文本层覆盖全部页、上限
    max_text_pages=500；csv/xlsx 覆盖全部 sheet 全部行、上限 10 万行；
    代码截断时额外发全文 block），document_reader 的 full_text 优先由
    full_text blocks 组装（回退 text blocks）；21 页 PDF 第 21 页与 2002 行
    CSV 末行均入索引（复现测试固化）；② UI intelligence 选择优先 Source
    的 current 指针、无命中时按 created_at 最新（重复分析不再命中旧记录，
    双记录前端回归覆盖）；③ test_driver `_wait_for` 默认超时 5s→30s
    （负载下全量并行抖动加固）。
11. 四轮 CR 后的正式语义修订（2026-08-04，取代定稿规格中“全文进
    semantic_entries”的表述）：文档文本索引为**有界索引（bounded
    indexed text）**而非无界全文——semantic index 是行导向的 canonical
    文本工作区文件，无界索引会被百 MB 级素材撟爆。三层上限：提取阶段
    pdf/office 文本层 500 页、表格每 sheet 10 万行；索引阶段
    `MAX_INDEXED_TEXT_CHARS=2,000,000` 字符（覆盖现实长剧本，超出部分
    截断并在 notes 中如实声明）。字段改名
    `DocumentReadResult.indexed_text` + `extracted_chars`；工具返回体携
    `textCoverage{indexedChars, extractedChars}`；**截断覆盖率持久化到
    index 的 coverage.ocr 模态**（文档有提取文本时 mode=available、
    producer=document_reader、ratio=indexed÷extracted；无文本层时
    unavailable），不再宣称“全文”。
12. 五轮 CR 修复回填（2026-08-04，coverage 完整性）：① commit 对新格式
    结果（含 textCoverage）强制校验落盘索引文本：Runtime 文件必须存在、
    长度与 indexedChars 一致、sha256 匹配（0<=indexed<=extracted 数值
    校验），否则拒绝提交并提示重新 read_document；excerpt 回退仅限无
    textCoverage 的旧结果（ratio 回退 1.0）；② 提取阶段截断进入
    coverage：渲染器发结构化 `extraction_note` block（pdf 页 cap 含已知
    total；表格行 cap total 未知），textCoverage 增
    extractionComplete/extractionFraction/sha256；ratio 保守合并 =
    字符比×提取份额，total 未知时 ratio=None（SourceCoverage 放宽为
    available 允许 ratio 未知，需 producer）；③ 内部命名机械重命名
    document_indexed_text_*；固化集成回归：文件缺失/篡改拒提交、legacy
    回退、截断 ratio<1、未知 total ratio=None。
13. 六轮 CR 修复回填（2026-08-04，fail-closed 边界）：① textCoverage
    升级为严格 data model `DocumentTextCoverage`（indexedChars/
    extractedChars/extractionComplete/extractionFraction/64 位小写 hex
    sha256 全部必填 + 一致性校验）：字段缺失或非法直接拒提交，sha256
    强制比对（同长替换攻击被拒），仅完全无 textCoverage 才走 legacy；
    ② SourceCoverage 恢复通用 invariant（available 必须 ratio∈(0,1]），
    模型级仅 producer=document_reader 可 ratio=None，且
    SourceIntelligenceIndex 上下文校验进一步限定为
    mediaKind=document + modality=ocr（其余模态的无比例 available 在
    parse/round-trip 即拒）；固化回归：缺 sha256 拒提交、模型级与
    index 级 scope 门控。补强：extractionFraction 严格 float（拒字符串
    强转），且 extractionComplete=false 时 fraction 只能为 None 或 <1.0
    （不完整提取不得声明满覆盖）；legacy 分流按“textCoverage 键是否
    存在”判定（显式 null 进严格模型被拒，不会被误判为旧结果绕过
    SHA 校验）。

---

### WT4 · 自我审阅模块（`feat/creator-self-review`）🔵

**现状事实**：compose 由 `local_execution.py::FfmpegLocalMediaRunner.render()` 处理
`COMPOSE_FINAL_VIDEO`，产物经 `_materialize_and_publish()` →
`AssetFileStore.publish()`；prompt 体系为占位符白名单机制
（`prompts/__init__.py::FILE_AGENT_PROMPT_SPECS`，`ai_editing_director.system` 现有
placeholders：project_id / workspace_schema / content_type /
target_duration_seconds / tts_guidance）；trace 走
`observability/tracing.py::trace_event()` / `@traced_async`。**现无成片回看机制。**

**总开关**：`models/config.py` 增
`SELF_REVIEW_ENABLED = _bool_env("CREATOR_SELF_REVIEW_ENABLED", False)` +
`is_self_review_enabled()`。不进 plugin.json / schemas / 前端。

**模块 `services/render_review/`**：
- `frames.py`：`extract_review_frames(video_path, *, max_frames=24) ->
  list[ReviewFrame{timestamp_ms, image_path}]`——ffmpeg 按时长均匀抽帧 + 首尾帧必采，
  分辨率经 vendored `image_budget.smart_resize` 对齐 VLM 预算；
  `probe_audio_profile(video_path) -> AudioProfile{has_audio, loudness_segments}`
  （ffmpeg ebur128 概要，供配音/静音维度）。
- `protocol.py`：六维协议常量与 prompt 模板（画面质量 / 时长匹配 / 节奏 / 配音 /
  字幕 / 工程正确性），源自上游 video-edit skill 自评协议的移植改写；输出 schema
  `RenderReviewReport{video_ref, round, findings: list[ReviewFinding{dimension,
  passed, severity: minor|major, evidence_timestamp_ms, suggestion}], verdict:
  pass|revise}`（Pydantic，落 `schemas/`；前端 contract 本期不加——不开放前端）。
- `review.py`：`async review_render(project_id, video_path, plan_context) ->
  RenderReviewReport`——抽帧 → 现有 VLM 后端（`creator_vlm_model`）多图评审 →
  报告写 `runtime/render-review/{video_id}/round-{n}.json` + trace_event
  （component="render_review"）。
- 迭代环：verdict=revise 时把 findings 以**回合 user message**（结构化 JSON 文本）
  送入 `AI_EDITING_DIRECTOR` 的下一次 specialist run（不改 prompt spec、不加
  placeholder——回合消息比模板占位更贴合「反馈」语义且零白名单变更）；至多 3 轮
  （`MAX_REVIEW_ROUNDS=3`），3 轮后 verdict 保持 revise 也不阻塞交付，报告随成片留存。

**挂钩点**：`local_execution.py` 中 COMPOSE_FINAL_VIDEO 发布成功后：
`if is_self_review_enabled(): asyncio.create_task(run_review_loop(...))`——异步，
不阻塞 compose 返回。

**Prompt 调试与实际 case 测试（重心）**：
- 评测集 `backend/tests/fixtures/render_review/`：历史成片 ≥2 + 人工构造缺陷片
  （黑帧 / 音画错位 / 字幕溢出 / 配音缺失 / 节奏拖沓各 ≥1），每例配
  `expected.json`（人工标注六维结论）；
- 回归脚本 `tests/render_review/test_eval_set.py`（真实 VLM，标记 manual/skip，
  人工触发）：以「缺陷检出零漏报、误报 ≤1/例」为 prompt 迭代准绳；
- 真实 case：隔离栈开开关，「创意生成」「素材剪辑」两链路各跑一个完整项目，人工
  核对证据帧-结论一致、建议可执行。

**自动化测试**：开关关闭零行为差异回归；打桩 VLM pass/revise 两态断言轮数与终止；
报告 schema 单测；音频概要单测。

**已定稿决策**：反馈注入采用回合消息（非 prompt placeholder）。

**实现回填（feat/creator-self-review，2026-08-03）**：
- 按方案落地：`models/config.py` 增 `SELF_REVIEW_ENABLED`/`is_self_review_enabled()`；
  `schemas/render_review.py`（六维 enum + `ReviewFinding`/`RenderReviewReport`/
  `ReviewFrame`/`AudioProfile`）；`services/render_review/{frames,protocol,review}.py`；
  报告与链状态落 `runtime/render-review/{video_id}/round-{n}.json` +
  `chain-*.json`；trace component=`render_review`；vendored
  `vendor/mm_plugins/image_budget.py`（与 WT3 规范副本逐字同源，另以纯增量块
  追加 video 预算常量；NOTICE 记 commit `077aea6`）。
- 与方案的实际差异：① 挂钩收敛到 `_result_from_task()` 单点——所有成功收敛路径
  （新渲染 / 幂等重放 / 指纹复用 / 崩溃恢复）均经过它，开关、命令过滤与已审
  去重全部下沉 review 侧（废弃/隔离结果不会到达该点）；② “下一次
  specialist run”通过现有 `admit_user_request`（source=`render_review_feedback`，
  channel=runtime，幂等 client_message_id）投递回合消息，零 driver/prompt 改动；
  轮次由 per-timeline 链状态文件在文件锁下**原子占用**（claim 带「进程+事件循环」
  lease：崩溃/loop shutdown 残留的 claim 令牌无法再被出示，下次调度立即回收，
  不依赖任何 asyncio 清理回调能否执行；取消路径另有 shield+done-callback 尽力
  及时释放；较新成片可替代在飞 claim；
  反馈投递经 session store 新增的 admission_guard 在 Project 生命周期边界内
  重校 slot selected 成片，无法证明新鲜度则 fail-closed 不发指令；陈旧
  findings 只留报告不进剪辑回合、不耗轮次），verdict=pass 或满 3 轮关链；
  ③ VLM 调用失败（含瞬时网络错误）与解析失败各重试一次，失败/取消均释放
  claim 并仅记 `render_review.failed` trace，不阻塞交付。
- 评测集（`backend/tests/fixtures/render_review/`，历史成片 2 + 构造缺陷片 5，均
  人工逐帧标注）：标注仅用于构造合成的真实 timeline（TTS 语义音源/字幕 overlay/
  settings），评审上下文经与生产 compose 路径同一 `derive_plan_context`
  （expects_voiceover 以音源 metadata.sourceKind=tts_generation/角色标签为权威，
  BGM 不计；expects_subtitles 由文字 overlay 推导；target_ref 双形态解析对齐
  `_target_timeline`；project_brief 喷入供判“要求旁白但计划未落”的漏报）。真实
  VLM（qwen3.7-plus）经该生产路径独立复跑达标——**7/7 缺陷检出零漏报；误报
  总计 2 例（分散两例，各 ≤1/例）**。关键 prompt 经验：ebur128 前 400ms 窗口未
  填满会读出 -120 LUFS 假静音（需丢弃 warm-up 采样）；静音判缺陷必须要求同时段
  证据帧“画面在说话”；expects_voiceover/expects_subtitles 计划上下文显式喷入可
  大幅降误报。
- 隔离栈（8094）真实 case：素材剪辑链路完整跑通 2 轮循环——round-1 检出字幕黑块
  （10145ms）与中段黑帧（7101ms），证据帧人工核实一致；剪辑专家据反馈修字幕
  CSS、裁掉黑帧区间并重新合成；round-2 继续检出竖排字幕错行。创意生成链路
  （资产图→分镜图→视频→合成全流程）round-1 检出整片响度 -38 LUFS 过低
  （ffmpeg 实测一致），反馈后剪辑专家新增 +23dB Audio Element 完成修订；
  重合成被基线能力阻断（compose 复用指纹不感知 Audio Element，属 TTS 分支
  先行合入后自然消除的集成项，非本模块范围）。开关关闭时全量回归零行为差异。
  CR 收口后 UI 复测：缺旁白场景 round-1 即检出 voiceover major fail
  （-43.2 LUFS，suggestion 引用 project_brief 语义）且反馈以
  source=render_review_feedback 进入 AI_EDITING_DIRECTOR 回合；三轮上限后
  chain 正确关闭（rounds_completed=3、claim 清空、不发第三条反馈）；人工编辑
  与合成并发竞争场景被平台既有项目文件锁 10s 超时阻断（未到达准入守卫），
  待锁问题解除后复测。

---

### WT5 · 生成侧扩展三件套（`feat/creator-gen-providers`）🔵

同一 worktree 内按 commit 串行：5a → 5b → 5c。协议对照 §1.2-9。

**5a · qwen_image 编辑与翻译模式**
- **现状事实**：`models/image/dashscope_provider.py` 已经用 multimodal-generation
  endpoint（默认模型 `qwen-image-2.0-pro`），且已支持参考图（本地图经 DashScope
  临时存储转 `oss://` + resolve header）——**与上游 image_edit 的 payload 同构**，
  缺的是工具层的显式模式语义与 translate 模型。
- 改动：
  1. `image_generation` 工具参数增 `mode: enum[generate, edit, translate]`
     （缺省 generate 保兼容）与 `referenceImageRefs: list[fileRef] (≤3)`；
     `specialist_tools.py` 的 `_arguments_schema` 与前端 api-contract 测试同步。
  2. `dashscope_provider.py`：edit 模式复用现有参考图通路（校验 1–3 张）；
     translate 模式切模型 `qwen-mt-image`（`get_image_translate_model_name()`，
     config 缺省 `qwen-mt-image`，不强制新配置树——挂在 image 配置树下加一个可选
     字段 `translate_model`）；messages 组装图前文后。
  3. plugin.json `creator_image_model.config_fields` 追加可选 `translate_model`；
     schemas / contracts / ModelConfigModal image 区块同步。
- OpenAI provider 不支持 edit/translate → 能力矩阵校验给出可读错误。

**5b · 视频生成模式矩阵（t2v / i2v / video_edit）**
- **现状事实**：`submit_video_task(prompt, reference_image_url,
  reference_image_url_list, ratio, duration, resolution, watermark,
  generate_audio)`；backend 判定 `_uses_seedance_protocol()` /
  `is_happyhorse_model()`；wan/happyhorse 提交
  `services/aigc/video-generation/video-synthesis`、seedance 走 Ark
  `/api/v3/contents/generations/tasks`；happyhorse 现约束 r2v（1–9 图、720P/1080P、
  3–15s、9 比例、无 prompt_extend）；`r2v_generation` 工具 wait=TASK + 执行授权。
- 改动：
  1. data model：视频请求增 `mode: enum[r2v, t2v, i2v, video_edit]`（默认 r2v）、
     `first_frame_ref: fileRef|None`、`video_ref: fileRef|None`；
     `r2v_generation` 工具 `_arguments` 同步增 `mode` / `firstFrameRef` /
     `videoRef`（工具名不变，描述改为「视频生成（多模式）」——改名影响面大，先不改）。
  2. `models/video_model.py` 按 (backend, mode) 组装 payload：
     - happyhorse：模型名按模式派生——**决策点**：用户配置基名（如
       `happyhorse-1.1`）+ 按 mode 拼 `-t2v/-i2v/-r2v/-video-edit` 后缀（推荐，
       与上游模型族命名一致），或要求配完整名。t2v `input={"prompt"}`；i2v
       `input.media=[{"type":"first_frame","url"}]`；video_edit
       `input.media=[{"type":"video","url"}]`（时长跟随输入，≤15s，>15s 截断的
       上游行为在工具描述中告知 Agent）；r2v 现状不动。参考媒体传输沿用
       DashScope 临时上传通路（video_ref 同样支持）。
     - wan：t2v / i2v 对照上游 `wan_t2v.py` payload（i2v 首帧 `img_url` 字段，
       实现时以上游源码与百炼文档双确认）；**无 video_edit**。
     - seedance2：t2v/i2v 支持性在细化实测后定格（先在矩阵中标 needs-verify，
       校验层暂拒绝）。
  3. 能力矩阵常量 `models/video_capabilities.py::VIDEO_MODE_MATRIX:
     dict[backend, frozenset[mode]]`，校验函数
     `validate_video_mode(backend, model_name, mode)` 拒绝不支持组合并提示替代；
     `video_model_guidance`（R2V_GENERATION_DIRECTOR prompt 占位符已存在）追加
     矩阵说明。

     | mode | happyhorse | wan | seedance2 |
     |---|---|---|---|
     | r2v | ✅ 1–9 图 | ✅ | ✅（现状，不新增验证） |
     | t2v | ✅ | ✅ | ❌ 本期不开放（非百炼不做真实验证，校验层拒绝） |
     | i2v | ✅ | ✅ | ❌ 同上 |
     | video_edit | ✅（输入 3–60s，>15s 截前 15s） | ❌ | ❌ |

**5c · wan_s2v 数字人 provider**
- `models/s2v_model.py`（新，TTS 范式）：
  - `async detect_face(image_url) -> FaceDetectResult{passed, reason}`——POST
    `{base}/services/aigc/image2video/face-detect`，
    `{"model": vc_detect_model, "input": {"image_url"}}`（免费；失败原因透传：多人/
    侧脸/模糊/遮挡/风格不支持）；
  - `async submit_s2v_task(image_url, audio_url, resolution) -> task_id`——模型
    `wan2.2-s2v`，`input={"image_url","audio_url"}`，
    `parameters={"resolution":"480P"|"720P"}`，异步提交走现有 poller；
  - 入参校验：人像图单边 400–7000px；audio 必填。
- 配置树 `creator_s2v_model`：config.py 常量 + getter
  （`get_s2v_api_key/base_url/model_name/detect_model_name/timeout_seconds`、
  `is_s2v_configured`）；plugin.json config block（`requires_config: false`，字段
  api_key/base_url/model 默认 `wan2.2-s2v`）；schemas `S2vConfig extends
  ModelConfigItem`；contracts + ModelConfigModal 增区块 + ModelBadges。
- 工具 `s2v_generation`：
  `ToolSpec(roles={R2V_GENERATION_DIRECTOR}, requires_execution_authorization=True,
  long_running=True, wait=TASK, provider_kind="s2v",
  parameters={characterImageRef, audioAssetRef, resolution?})`；注册 gating 与
  dispatch 对照 `_TTS_TOOL_NAMES` 模式新增 `_S2V_TOOL_NAMES`；执行顺序：detect
  （免费）→ 失败即返回可读错误（**不创建执行授权**）→ 通过才走授权 + 提交。
  audioAssetRef 直接消费 TTS 产出的 audio 资产。

**测试**：5a/5b/5c 各配 respx 协议单测（payload 形状、模式矩阵校验、模型名派生、
detect 免费短路、任务状态机）；打桩 poller 集成；api-contract 测试扩展。人工验收：
真实 Key 各跑一例（含 happyhorse t2v 与 video_edit），查看实际生成内容确认语义正确，
高消费事先确认。

**已定稿决策**：① happyhorse 模型名采用基名 + 按 mode 拼后缀派生；② seedance2
t2v/i2v **本期不开放不验证**（真实验证范围限百炼模型，非百炼不做真实调用；
矩阵标 ❌，校验层拒绝，后续单独授权后再实测开放）；③ `r2v_generation`
不改名只扩参。

---

### WT6 · 长素材记忆并入 Source Intelligence（`feat/creator-source-memory`）🔵

**现状事实**：Source Intelligence 的 visual 由外层 VLM 动态 fps 采样直读，>600s 仅降
到 0.5fps，无结构化长素材机制；`media.durationMs` 可用作触发判据；Task 机制为
`ProjectExecutionStore`（SpecialistRun/Task 状态机 QUEUED→RUNNING→SUCCEEDED/FAILED）。

**移植范围（手法 B，落点 `backend/vendor/mm_plugins/video_memory/`）**——上游
`skill/script/build_memory/` 逐文件处置：

| 上游文件 | 处置 |
|---|---|
| `schema.py`（图谱节点/边 schema） | 移植（作为 vendored 领域模型） |
| `build_graph.py` + `pipeline_worker.py`（P1/P2/P3 编排与并发） | 移植改造：编排改 async + Creator 并发控制 |
| `prompts.py`（P2 子图抽取 / P3 聚合 prompt） | 移植（进 Creator prompt 常量，不走占位符白名单——非 agent prompt） |
| `embeddings.py`（qwen3-vl-embedding 客户端 + npz 索引） | 索引/余弦检索逻辑移植；HTTP 客户端**重写**为 Creator 薄客户端（手法 A） |
| `llm_client.py` / `env_config.py` | **不移植**——VLM 调用改走 `creator_vlm_model` 后端、配置走 Creator 配置树 |
| `merge_memories.py` | 本期不引入 |
| MCP 查询工具 9 个 + `loader.py` | 查询逻辑移植进 `services/media/source_memory.py`，带图谱内存缓存 |

**data model 与配置**：
- `schemas/assets.py`：SourceIntelligenceIndex 增可选
  `memory_ref: SourceMemoryRef{graphPath, embeddingsPath, builtAt, macroCount} | None`；
  前端 contract 同步（只读展示，本期 UI 仅显示「记忆已构建」徽标，范围细化时可再砍）。
- 产物 `runtime/source-intelligence/<index-id>/memory/{graph_memory.json,
  embeddings.npz}`，随 `sourceChecksum` 失效。
- **embedding 配置（决策点，推荐方案 B）**：
  A. 挂 `creator_vlm_model` 树下加 `embedding_model` 字段；
  B. 新建 `creator_embedding_model` 独立树（api_key 可选 reuse vlm，model 默认
  `qwen3-vl-embedding`，endpoint 为 DashScope 原生 multimodal-embedding，注意
  batch 上限）——独立树与「按用途分 Key」治理一致，推荐。
  `models/embedding_model.py`（新，薄客户端）：`async embed(inputs: list) ->
  list[vector]`，带 batch 切分与 Throttling 退避。

**构建（写路径）**：`services/media/source_memory.py::build_source_memory(asset, index)`
- 触发：`source_analysis` 完成常规 index 后，`durationMs > MEMORY_BUILD_THRESHOLD_MS
  (20min)` 且 embedding 已配置 → 创建后台 Task（不阻塞 index 产出）；
- 计费入口走执行授权：费用预估 = f(时长)（P2 VLM 次数 ≈ macro 数 ≈ 时长/3–8min +
  embedding 节点数），按现有费用预估版本匹配规则展示；
- P1 帧差分切割（ffmpeg，`resolve_ffmpeg`）→ P2 每 macro 一次 VLM 子图抽取
  （`creator_vlm_model`，并发度常量 4–8，帧采样复用现有 native_media 通路）∥ ASR
  音轨转写（Creator ASR 模块，WT1 后即 qwen3-asr-flash，转写入图为 ASR 节点）→
  P3 纯文本聚合 + 全节点 embedding。

**查询（读路径）**：ToolSpec
`query_source_memory(roles={SOURCE_INTELLIGENCE},
requires_execution_authorization=False, wait=NONE, parameters={assetRef,
query_type: enum[summary, super_events, macro_events, subgraph, search_nodes,
search_ocr, search_asr, by_time, enumerate], query?: str, node_types?: list,
macro_id?: str, start_ms?/end_ms?: int, top_k?: int})`；search_* 现场调 embedding
（单条，费用忽略）；返回 JSON 文本 + 命中 macro 的时间窗。Prompt 侧在
`source_intelligence_agent.system` 增记忆使用规则（占位符 `memory_guidance`，按
资产是否有 memory_ref 注入）：定位 → `subgraph` 下钻 → **回原片窄窗核验**（现有
关键帧通路）。

**投影**：P3 Root/SuperEvent 摘要写入 index 的 `summary` / `semantic_entries` 草稿
（producer 标记 source_memory），外层 VLM 只审校。

**测试（重心）**：
- 单测：fixtures 预构建小型 graph_memory.json + npz 锁定 9 类查询分派与归一化；
  触发阈值 / 授权 / checksum 失效打桩；投影 schema。
- **指定测试素材**（真实构建 + 检索端到端，人工验收，UI 操作）：
  1. 猫视角法国之旅（自然场景、少台词——验视觉图谱与场景切割）：
     `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/CAT%20with%20CAMERA%20Explores%20FRANCE%20%F0%9F%87%AB%F0%9F%87%B7%20%20(%20Calming%20CAT%20POV%20).mp4`
  2. KPL 2026 夏季赛 成都AG超玩会 vs 重庆狼队 Game 5（解说密集、屏幕文字多——验
     ASR 台词检索 + OCR + 时间定位）：
     `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/%E3%80%90KPL%20Summer%202026%E3%80%91%E6%88%90%E9%83%BDAG%E8%B6%85%E7%8E%A9%E4%BC%9A%20vs%20%E9%87%8D%E5%BA%86%E7%8B%BC%E9%98%9F%20%EF%BD%9C%20Game%205%20%EF%BD%9C%20Stage%201%20-%20Jul%2003%20%EF%BD%9C%20%23honorofkings%20%23hokchannel.mp4`
- 验收口径：UI 导入 → 构建完成 → 会话内「台词检索定位团战片段」（素材 2）与
  「语义检索定位场景转换」（素材 1），**读帧核对**定位时间窗与原片一致。

**已定稿决策**：① 新建独立 `creator_embedding_model` 配置树；② 记忆 UI 仅展示
「记忆已构建」徽标。

---

### WT7 · 外置 Skill 接入机制 + edu-agent（`feat/creator-external-skills`）🔵

**目标**：Creator 支持**后端手动配置外置 skill**——编辑配置文件指定本地 skill 目录
（SKILL.md + 脚本资产）即可让 Creator Agent 获得该技能；edu-agent 作为首个接入
用例。核心要求：① **纯后端配置，前端零改动**；② **skill 方式而非 agent 方式**
——不新增任何 subagent 角色，SKILL.md 知识注入主 Agent 上下文、脚本由主 Agent 经
沙箱工具执行；③ **Creator 本身不受影响**（skill 缺陷/缺依赖不得破坏既有链路）；
④ 能被成功调用出正确结果。

**现状事实**：prompt 注入机制为占位符白名单（`prompts/__init__.py`），
`render_creator_system_prompt()` 已示范条件注入（tts_guidance）；宿主 Agent 层有
`execute_shell_command`（`src/qwenpaw/agents/tools/shell.py`，带 sandbox/工作目录/
超时），但 **Creator file agent runtime 无 shell 工具**；模型配置读写为
`model_config.json`（`_get_user_config()` 读 + `api/model_routes.py::
mutate_model_config` 原子写、加密字段、幂等键）。上游 edu-agent skill 目录：
`SKILL.md` + `design-system.md` + `assets/`（学科组件 HTML 库 / gsap / katex /
fonts / backgrounds）+ `references/` + `scripts/`。

**机制设计**：
1. **配置面（data model 先行，纯后端）**：`<CREATOR_DATA_ROOT>/config/skills_config.json`
   （与 model_config.json 同级、同原子写规范；**仅手动编辑文件，无任何前端 UI、
   无 plugin.json config block、无前端 contract 变更**）。schema（`schemas/skills.py`
   新，仅后端内部使用）：
   ```
   SkillEntry{name: str, path: str, enabled: bool, description?: str,
              env?: list[str]（需传给脚本子进程的 env 变量名，值从宿主环境取）,
              requirements?: list[SkillRequirement{kind: binary|node_min|env,
              value: str}]}
   ```
   `models/config.py` 增 `load_skills_config() -> list[SkillEntry]`（带缓存 +
   失效，不加密——skill 配置无密钥，密钥类走 env 声明间接引用）。
2. **加载与隔离**：`services/external_skills.py`（新）：
   - `load_skills() -> list[LoadedSkill{entry, status: available|unavailable,
     reason: str|None, skill_md: str, root: Path}]`——解析 SKILL.md（front matter +
     正文），逐项探测 requirements（`shutil.which` / `node --version` 比对 /
     env 存在性）；任何失败 → unavailable + 原因，**不抛异常不影响会话建立**；
   - 注入：`creator_agent.system` prompt spec 增占位符 `external_skills`；
     `render_creator_system_prompt()` 拼装可用 skill 区块（name + 触发时机 +
     调用方式摘要），**token 预算上限 `SKILL_CONTEXT_MAX_CHARS`（初值 8000）**，
     超限按 skill 顺序截断并 trace 警告；无可用 skill 时注入空串（占位符校验兼容）。
3. **执行通道（skill 方式，非 agent 方式）**：新工具注入**主 Agent**（不新增角色）
   `ToolSpec(name="run_skill_script",
   requires_execution_authorization=True（脚本执行属敏感操作）, long_running=True,
   wait=NONE, parameters={skill: str, script: str（限 skill 根目录内相对路径）,
   args?: list[str], timeout_seconds?: int ≤1800})`：
   - `subprocess` 执行，`cwd=<workspace>/skills-runtime/<name>/`（首次运行从 skill
     root 拷贝工作副本，产物落此沙箱）；
   - 子进程 env = 最小基础 env + entry.env 声明的变量（显式白名单传递——这是外部
     子进程的受控参数传递，不同于 §2.2 禁止的「Creator 进程内 env 注入」）；
   - stdout/stderr 截断（各 64KB）后返回；产物文件由 Agent 经现有资产导入通路
     入库。
4. **edu-agent 接入**：skills_config.json 配置
   `{name:"edu-agent", path:"<上游本地路径>/src/capabilities/edu-agent/skill",
   enabled:true, env:["DASHSCOPE_API_KEY"], requirements:[{binary:"ffmpeg"},
   {node_min:"18"}, {env:"DASHSCOPE_API_KEY"}]}`；hyperframes 依赖由 skill 脚本内
   npx 解决（uvx/npx 不会代装的事项在 unavailable 原因中提示）；Apache-2.0 随
   NOTICE 归属说明（若拷贝进用户目录）。

**测试**：
- 单测：SkillEntry schema；SKILL.md 解析；注入截断；坏 skill（路径不存在/解析失败/
  依赖缺失）unavailable 隔离且会话建立成功；`run_skill_script` 路径越界拒绝、超时、
  输出截断；env 白名单传递。
- 集成（重心，UI 操作）：a) 配置 edu-agent → 会话提一道数学题 → Agent 按 skill
  产出讲解视频，**查看实际视频内容**确认讲解正确、配音字幕正常；b) 配置故意损坏的
  skill → Creator 全链路（建项目/生成/剪辑）回归零影响；c) disable 后上下文不再注入。

**2026-08-04 真实调用验收（隔离栈 8097）**：结论为**未达到全通过标准**。
实际被测分支为 `feat/creator-external-skills@10af91d3`，但派发单给出的
`.worktrees/creator-external-skills` 不存在；本机对应 worktree 位于
`/Users/linxuanrui/.qoder/worktree/QwenPaw/ed77o4`。`dev-isolated.sh verify`
比对 159 个后端文件通过，8097 listener、Creator、health、version 均返回 200，
数据根保持为 `~/.qwenpaw-skills`，未触碰 8088 / `~/.qwenpaw-poc`。

- **A1 PASS**：edu-agent 为 available；SKILL.md 67,227 字符，注入摘要 457 字符，
  包含触发时机且低于 8,000 字符上限。
- **A2 FAIL**：真机 node v25.9.0、ffmpeg 8.0、env 探测通过；抹掉 PATH 中 node 后
  正确变为 unavailable，但 reason 仅为 `required binary not found on PATH: node`，
  缺少验收要求的安装提示。
- **A3 PASS**：真实上游 `scripts/precheck.py` 在项目隔离的
  `skills-runtime/wt7-a-latest-20260804/edu-agent` 沙箱 cwd 执行；stdout 正常，白名单
  `DASHSCOPE_API_KEY` 可见、白名单外变量不可见。
- **A4 PASS**：`../../etc/passwd` 越界被拒绝；sleep 脚本约 1.0 秒后超时终止，
  两类错误信息可读。
- **A5 PASS**：不存在路径的坏 skill 被标记 unavailable，edu-agent 仍可用，
  prompt 构建与会话建立不受影响。
- **B1 PASS / B2 PASS**：新会话明确按 edu-agent 的 SKILL.md 完整流程推进；
  首次 `generate_tts.py` 在执行前弹授权，拒绝后事件记录
  `execution authorization rejected`，没有执行，二次授权后才继续。
- **B3 部分通过**：口播源 transcript 的 22 句数学内容正确（AB=5、勾股定理
  表述正确），AAC 音轨连续非静音；但独立 ASR 外传审批被拒，未完成独立听辨复核。
- **B4 FAIL**：实际抽取并目检 12 帧。标题、字幕、卡片无 tofu/压框；25/30 秒帧的
  直角三角形、斜边和直角标记语义正确；但 40--55 秒持续显示
  `a2 + b2 = c2`，60--70 秒持续显示 `32 + 42 = AB2`，平方未渲染为上标。
  edu-agent 自报 precheck 34 项和 postcheck 全通过，说明现有 gate 漏检该缺陷。
- **B5 部分通过**：MP4 已入资产库（`asset-version-6fdcbb6f77505adcbf99de6924ba1157`，
  8.9 MB，88.5 秒，1920x1080@30fps）且有下载按钮；资产被归类为“来源”，详情页
  未显示视频预览控件，未满足“可预览下载”的完整要求。
- **B6 PASS**：追加不存在路径的 `broken-wt7` 并重启后，新项目仍完成
  建项目 → qwen-image-2.0-pro 生成 → 用户保留 → PNG 入库
  （最新复验产物 `artifact-version-1e4a790b023b5e078c7f0a280e58d877`）。
- **B7 PASS**：edu-agent `enabled=false` 重启后，同题新会话明确提示“当前可用的
  Specialist 中没有 edu-agent”，未引用 skill 流程、未调用 `run_skill_script`；
  测试后保持 disabled。

观测数据：项目从创建到 MP4 入库耗时 52 分 23 秒（11:03:29--11:55:51）；
TTS 共 22 次、合成语音 87.5 秒，首次高并发触发 DashScope 限流，降低并发并加入
重试后成功；正式 HyperFrames 编码约 2 分 48 秒（11:52:10--11:54:58）。宿主缺少
edu-agent Python 依赖，Agent 现场创建 venv 后才完成 TTS；模型还多次把 shell 命令
拼入 `run_skill_script.script`，被沙箱安全拒绝，并出现 `write_skill_file requires skill`
自愈重试。授权卡在若干轮次需刷新页面才出现。分支中不存在派发单指定的
`backend/tests/manual/test_real_external_skills.py`，因此原定 `pytest -m manual_real`
命令为 file-not-found；本轮 A1--A5 用同分支真实服务 API/模块等价执行。

验收期间该 worktree 出现未提交并发改动（`external_skills.py`、runtime driver、配置模型
及测试等）；为避免只报告旧构建，随后重新 build/start，并再次通过 159 文件一致性校验，
相关 external-skill 单测 36 项通过。最新代码复验中，A1、A3--A5、B1、B2、B6、B7
仍通过，A2 仍因缺少安装提示失败；但完整视频链路在组件阶段出现新的阻塞回归：同一
Project 已由 runtime 绑定时，模型仍自动向 `write_skill_file` 传入 `projectId`，运行时
累计 20 次返回 `model tool call attempted another Project`。即使在 UI 中反复明确要求
仅传 `skill/path/content`，长脚本仍会先流式生成数分钟、再于执行前被拒。该轮已完成
18 句 TTS（首次 80.9 秒；误重复后最新音频 86.1 秒）及故事板，但最终
`dist/compositions` 仅落下 `scene-title.html`，未能进入 precheck/render/MP4 入库；
因此最新代码上的 B3--B5 判为**阻塞失败**，不能用旧构建的 MP4 代替当前版本通过。
本轮共出现 20 次脚本执行授权，另有 `run_skill_script requires script`、把 shell 命令
拼进 `script`、Python 3.14/venv 依赖探测反复和一次 DashScope 限流。测试收尾已把
edu-agent 恢复为 `enabled=false`，保留 `broken-wt7` 回归夹具，并重启 8097；listener
与 Creator 页面恢复正常。

**已定稿决策**：skill 脚本执行要求执行授权；skill 方式接入，工具给主 Agent，
不新增 subagent 角色；配置纯后端。

---

### WT8 · hyperframes 动效引擎完善（`feat/motion-js-timeline`，既有 worktree）🔵

**现状事实**：hyperframes 接入已在 `.worktrees/motion-js-timeline`（分支
`feat/motion-js-timeline`，领先 dev/creator 2 commit、落后 5，**尚未合入**）实现：
- `services/media_files/motion_engine.py`（新 248 行）：html_js 动效文档的确定性
  引擎——`window.__hf = {duration, seek}` 协议（**HyperFrames 运行时契约的最小
  子集**）；确定性 prelude（冻结 Date.now/performance.now、固定 Math.random 种子，
  保证 (html, seek t) → 像素可复现）；vendor 白名单（仅 GSAP 3.15.0，内容哈希
  pin，不入库、CLI fetch + 逐次校验——GreenSock 许可证不随仓分发）；engine
  digest 盐化帧缓存键。
- `motion_design.py`：双格式契约——`motion`（纯 CSS @keyframes，无 script）与
  `html_js`（GSAP paused timeline + `__hf.seek` 逐帧拨时间截屏）；布局铁律
  （inset:8% 根容器、禁越界、退场声明式）自动拒绝；装饰动效 VLM prompt 契约
  （克制判断、取色、避主体、无缝循环 loop 字段）。
- 渲染器：`local_execution.py`（+148 行）capture worker 逐帧 seek 截屏（Playwright
  `Page.addInitScript` 注入 prelude）；专项测试 `test_motion_js_timeline.py` 332 行。
- 与 edu-agent 的关系：同源方法论（seek 协议 + 确定性渲染），但 Creator 自建引擎
  不依赖 hyperframes CLI/npx；edu-agent 在 WT7 skill 沙箱内用 `npx hyperframes`
  独立渲染，两套互不依赖、**保持隔离不混用**。

**WT8a · 分支就绪（在既有分支原地进行，不发起合并）**：
- 在既有 worktree 内确认工作区干净、全量跑 motion 专项测试 + 隔离栈（8098）人工
  验收一条带 html_js 装饰动效的合成，确认分支当前状态可用；
- **不 rebase、不合入**：与 dev/creator 的差异（落后的 5 个 commit 含 rejection
  feedback loop 等）留待最终集成阶段（§2.3 序列中 motion 排第二）统一解决；
  重点交叠预先记录：`local_execution.py` 与 `project_files/models.py`。

**WT8b · 完善项（对照 edu-agent 完整 hyperframes 实践的差距，在既有分支上继续
提交，与其他 WT 并行）**：
1. **渲染真值自查（最大差距）**：edu-agent 有成体系的 post-render gates
   （overlap 真值检测、线/曲线漏画检测、抽帧肉眼自查循环），Creator 动效渲染后
   目前无任何回看。落地：动效渲染完成后抽 2–3 关键帧（首/中/尾）做确定性规则
   检查（越界像素检测——透明盒外沿 alpha 采样；空帧检测），不合格拒绝入库并回
   馈重生；语义级检查（遮挡主体/美观）不在此层重复建设，留给 WT4 自评环的六维
   检查统一覆盖（动效属于画面质量维度）。
2. **vendor 注册表扩展机制**：现仅 GSAP；保持白名单 + 哈希 pin 机制不变，本期
   **不新增库**（KaTeX/字体是教学排版需求，装饰动效用不到）；在 motion_engine
   文档化新增 vendor 的流程（哈希、许可证审查、digest 升版）。
3. **loop 语义闭环**：prompt 契约已有 loop 字段，确认渲染器按周期拨时间的实现与
   帧缓存键对 loop 的盐化覆盖，补齐测试。
4. **`__hf` 契约文档化**：将 seek 安全规则（禁 rAF/随机/时钟、回调抑制、末尾同步）
   从 prompt 文本提炼为 `docs/`内部契约说明，供后续动效能力（如转场、字幕动效）
   复用同一引擎时对齐。

**测试**：WT8a 以既有 332 行专项测试 + 全量回归为门禁；WT8b 新增渲染真值自查
单测（构造越界/空帧样本）与 loop 盐化测试。人工验收：隔离栈跑一条含装饰动效的
完整合成，**抽帧查看实际画面**确认动效不越界、不遮主体、循环无缝。

**实际交付回填（2026-08-03，commit `faf3255c`，已推送未合入）**：
- WT8a 完成：工作区干净；专项测试 24 例全绿；Creator 后端全量 715 例全绿；
  根仓 pytest 除 8 例 `dev/creator` 上同样失败的遗留用例（chrome NM host /
  service worker / coding project，与 motion 无关）外全绿。交叠点已写入分支内
  `plugins/apps/qwenpaw-creator/docs/WT8_INTEGRATION_NOTES.md`：上游 5 个新
  commit **未触碰** `local_execution.py`（真正交叠方是 WT4）；硬冲突在
  `project_files/models.py` + `migrations.py`：双方都从 schema v2 分叉且都注册
  `PROJECT_MIGRATIONS[2]`（本分支 2→3 去 overlay_kind；上游已到 v4），集成时
  motion 迁移须重排到上游链尾（v4→v5）。
- WT8b 实际差异：
  1. 渲染真值自查落地为**两道门**：设计期 probe 新增关键帧空帧规则（入场完成点
     0.3/中点 0.5/末态 1.0，声明托管退场的文档末态改判 0.9），不合格回馈 VLM
     重生；合成期 capture 帧序列入缓存前抽首/中/尾（尾=窗口 80% 处避开托管退场）
     做空帧+越界（外沿 alpha 采样，阈值 10%）检查，不合格拒绝入库并回退固定样式。
  2. vendor 流程已文档化在 `motion_engine.py` 模块 docstring（本期未新增库）。
  3. loop 闭环时发现并修复一处真 bug：渲染器托管退场的 progress 按已取模的时间线
     时间计算，导致 loop 装饰永不退场；改按真实播放头 `playheadMs` 计算，并因此
     bump `MOTION_ENGINE_PROTOCOL_VERSION=2`、帧缓存命名空间升 v2；新增
     `frame_timestamp_ms`/`frame_cache_identity` 可测镜像与盐化/防漂移测试。
  4. `__hf` 契约文档化为 `plugins/apps/qwenpaw-creator/docs/motion_hf_contract.md`。
  专项测试扩至 46 例；pre-commit 全量全绿。人工验收：隔离栈（8098，
  `~/.qwenpaw-motion`）UI 操作完成含 html_js 循环装饰（速度线，周期 0.5s，15s
  窗口多周期）与 4 张 html_js 字幕卡的完整合成，抽帧确认不越界、不遮主体、
  循环相位随播放头推进、托管退场在片段末正确淡出。

**整改后真实验收回填（2026-08-04，commit `b17c03b5`，已推送未合入）**：
- 后端真实用例 A1–A8 全过：seek 异常传播、精确 t=0 空帧门、静止文档门、loop
  首尾 seam gate、有限次数长 loop 铺帧、缓存和 vendor 篡改守护均有实际渲染证据；
  motion 专项测试 57 例通过。A1 首版被门拒后第二版通过，一次通过率 1/2（50%）；
  A2 克制判断 1/1；人为 A4/A5 坏样本拦截率 2/2。
- UI 真实链路在 8098 完成 30s 导入、选段、真实 VLM 设计、预览 seek、compose 和
  八帧抽查；B3/B4/B5/B6 通过。最终 1.5s loop 的 t=0/t=1.5s 裸帧字节完全一致，
  seek 往返同一时刻的视频视口像素一致；不适合动效的片段返回 `needed=false`；
  html_js 后端海报端点在线返回 640×360 PNG。
- **严格结论仍为 FAIL（12/14）**：B1/B2 未过。最终细白双弧不越界、无文字，
  但压在中央道路/主体视觉区域，并非稳定留白区，“不遮主体”不成立，风格高级感也
  不足。说明底层真值门已经能守结构/像素/时间确定性，留白识别、遮挡主体和美观仍须
  WT4 语义自评或人工抽帧兜底。完整证据见
  `acceptance/WT8-motion-real-test.md` 的 2026-08-04 实测报告。

---

## 四、风险与守护清单（全局）

| 风险 | 影响 WT | 对策 |
|---|---|---|
| vendored 代码许可证合规疏漏 | WT3/WT4/WT6 | vendor/NOTICE.md + 文件头版权与修改标注；PR checklist |
| vendored 代码与上游漂移 | WT3/WT6 | NOTICE 记录 commit `077aea6`；升级人工 diff 回灌 |
| 本地素材在 OSS 不可用时需上传第三方免费图床（uguu.se） | WT2 | 有完整 OSS 配置时优先走私有 OSS + 15min 签名 URL；OSS 未配置、配置无效或上传/签名失败时自动使用 Uguu，并保留 OSS issue；上传前强制目录、光栅解码、bbox 与 8MB 校验，trace 明确记录 `uguu`，UI/日志可见第三方托管事实 |
| oss:// URL 在 multimodal 端点的可解析性未证实 | WT1 | Step 1 实测；不成立回退公网 URL 通路 |
| qwen3-asr-flash 时间戳为均摊估算 | WT1/WT6 | confidence=0 标记；剪辑选段以块级窗口回原片核验 |
| 视频模式矩阵与真实模型能力不符 | WT5 | 矩阵常量化；百炼模型逐格实测（含零成本健康检查）；非百炼格一律 ❌ 不开放 |
| happyhorse 模型名派生规则与 token-portal 双层名冲突 | WT5 | 细化决策 + 零成本健康检查逐模式验证 |
| memory 构建成本失控 | WT6 | 阈值 + 执行授权 + 时长线性费用预估 |
| embedding batch 上限与限流 | WT6 | 薄客户端 batch 切分 + Throttling 指数退避 |
| 自评环误报/漏报导致无效迭代 | WT4 | 评测集驱动 prompt 迭代；3 轮上限；仅建议不门禁 |
| 外置 skill 破坏 Creator 稳定性 | WT7 | unavailable 隔离 + 沙箱 cwd + 路径越界拒绝 + 坏 skill 回归 |
| skill 脚本执行安全 | WT7 | 执行授权 + env 白名单 + 超时 + 输出截断 |
| motion-js-timeline rebase 冲突引入回归 | WT8 | 332 行专项测试 + 全量回归门禁；GSAP vendor 不入库的许可证约束维持 |
| data model 三处不同步（schema/contract/UI） | 全部 | api-contract 测试强制；变更独立 commit |

## 五、文档维护约定

- 各 WT 评审通过后由 🔵 升级 ✅；实现合入 `dev/creator` 后回填实际差异。
- 「待定决策」条目须在该 WT 动工前定稿并回写本文档。
- 上游事实（§1.2）如复核发现变化，先改本节再动实现。
