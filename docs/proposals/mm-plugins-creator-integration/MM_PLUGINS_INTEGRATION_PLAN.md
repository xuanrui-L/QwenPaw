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
| 8 | search 并入 grounding，新增 Serper | web_grounding providers 插入 serper adapter | WT2 |

统一取舍：blender / freecad 不引入；video-edit 剪辑工作流不引入（只吸收自评协议）；
qwen_tts 已被 TTS 分支覆盖且超出，不再接。

### 2.2 统一接入原则：一切封装为 Creator 原生工具

**硬性约束：不引入 `qwen-mm-plugins` 为运行时依赖；不做任何 env 注入；不做任何
进程内直调 handle。** 两种落地手法：

- **手法 A · 协议对齐薄客户端**（远程 API 类）：对照 §1.2-9 的协议细节在
  `backend/models/` 或 provider 层自建 httpx 客户端，Key 走 `creator_*_model` 配置树。
- **手法 B · 算法移植（Apache-2.0 合规 vendoring）**（本地计算类）：移植为 Creator
  内部代码。合规义务：移植文件头保留上游版权声明并标注修改（Apache-2.0 §4b）；
  `backend/vendor/NOTICE.md` 集中声明来源仓库、commit `077aea6`、许可证与模块清单；
  移植代码集中放 `backend/vendor/mm_plugins/`（WT3 定稿目录样板，全项目沿用）。

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

**最终集成阶段（独立任务，见 dispatch/WT9）**：所有分支开发完成后，按以下序逐个
合入 `dev/creator` 并在每步跑全量回归：
```
feat/creator-tts-voice → feat/motion-js-timeline → feat/creator-asr-qwen3
→ feat/creator-grounding-serper → feat/creator-external-skills
→ feat/creator-doc-reader → feat/creator-self-review
→ feat/creator-gen-providers → feat/creator-source-memory
```
依据：TTS 是 WT5 的基底；motion 先于 self-review（local_execution.py）；
doc-reader 先于 source-memory（vendor 样板与 source_intelligence 轻/重改动）；
source-memory 改动面最大排最后。每步合并后 pre-commit + 双 pytest +
api-contract 全绿才进下一个；全部合入后做一轮端到端 UI 验收，再统一清理全部
worktree 与特性分支。

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
6. **Serper Lens 以图搜图（独立第二 commit，可选交付）**——这是新能力而非现有链路
   插桩。前提约束（§1.2-7）：Lens API 只收 `{"url"}`，无 base64/文件上传形态；
   DashScope 临时存储的 `oss://` 非公网不可用；uguu.se 公共图床禁止。因此本地
   素材的唯一合规通路是：`creator_media_oss` 凭据 + oss2 `bucket.sign_url()` 生成
   **短时效 presigned 公网 URL（15min）**，用完即过期；`_search_serper_lens(client,
   image_url)` POST `/lens`。OSS 未配置时仅支持输入本身已是 http(s) URL 的图，
   本地图路径返回可读错误说明原因。首版仅暴露给 grounding pipeline 的
   visual_jobs 使用，不单独开专家工具。

**测试**：respx 打桩 Serper 三端点响应；fallback 顺序单测（serper key 有/无两态）；
Lens 无 OSS 降级单测；隔离栈真实 Key 跑一次 grounding 检索核对来源列表与
providers_attempted。

**已定稿决策**：① Serper 顺位定在 Tavily 之后、DashScope 之前；② Lens 以图搜图
本期作为第二 commit 交付。

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
     | r2v | ✅ 1–9 图 | ✅ | ✅ |
     | t2v | ✅ | ✅ | needs-verify |
     | i2v | ✅ | ✅ | needs-verify |
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
t2v/i2v 维持 needs-verify，实测后补格；③ `r2v_generation` 不改名只扩参。

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
  定位精度正式口径（定稿）：产品承诺 **macro 级时间窗**（帧差分切割的场景段，
  典型 30–300s）命中与原片一致；micro-event 级子窗口来自 VLM 对片段内相对
  时间的估计，为 best-effort（秒级–十秒级偏移），精确剪辑窗口必须经
  `memory_guidance` 要求的回原片窄窗核验后使用，不作为检索命中的判定标准。

**已定稿决策**：① 新建独立 `creator_embedding_model` 配置树；② 记忆 UI 仅展示
「记忆已构建」徽标。

**交付纪要（2026-08-04，分支 `feat/creator-source-memory` 已推送）**：
- 提交：`b0ef87ae`（vendoring + 全链路集成 + 43 项新单测）、`3617e9f9`（真实素材
  验收暴露问题的加固）。质量门禁全绿：pre-commit、Creator 后端 pytest 733/733、
  前端 typecheck + vitest 278/278；root 套件失败项经基线对照均为并行环境噪声
  （0 个 creator 用例）。
- 落点与规格与上文一致；补充实现细节：P2 片段经本地 ffmpeg 编码后以 file://
  video_url 交 `creator_vlm_model`，非 DashScope 网关走 base64 内联，编码按
  `(max_dim, crf, fps)` 阶梯（720p/28/8fps → 256p/38/3fps）降档至 6MB 传输预算；
  构建期优先复用 index 已发布 transcript（单次计费），仅索引无 ASR 模态时才
  全片转写；P2 全部失败时构建 fail-close 不落盘；启动恢复将 RUNNING 中断的构建
  经 FAILED→QUEUED 重排队（已批准授权直接续跑）。
- **真实构建成本实测**：
  - 素材 2（KPL Game5，32:37 / 995MB）：授权卡预估 **¥1.47**（11 macros）；
    P2 11 次 VLM（video 输入，单段推理 39–84s）+ fun-asr 378 段 + 302 节点
    embedding；构建墙钟 ≈15 分钟。图谱 11 macro 全含子图，5 个 super event
    正确切出「BP→开局→胜利→采访→回放」叙事结构。
  - 素材 1（猫视角法国，116:45 / 1.1GB）：授权卡预估 **¥4.83**（45 macros）；
    45/45 子图全提取、459 节点、9 super events；构建墙钟 ≈15 分钟（含一次
    Throttling 429 自动退避重试）。
- **检索命中实测（全程 UI 会话，读帧核对通过）**：
  - KPL 台词检索（search_asr 多轮变体）：命中「大获全胜的一波团战。0换3」
    （macro_0005，870–1077s）与「一换三」（macro_0006，1077–1284s），并正确
    区分赛后采访中引用 Game 1 的「0换3」（macro_0008，1538–1792s）；本场未出现
    「团灭/零换五」字面词并如实报告。读帧核对：950s 帧为决胜局 4:3 推进画面、
    1150s 帧为「关键群控 x3」高地团战，与台词依据一致。
  - 猫视角语义检索（search_nodes + 因果链下钻）：森林→公路转换命中
    `macro_0028:me_001`（4192–4197s，实体 Asphalt Road），读帧 4230s 证实铺装
    路面；进入货车命中 `macro_0034:me_003`（5337–5347s，CAUSAL 链 approaches→
    enters），读帧 5340s（货车外观）/5352s（车内登山靴、水瓶与命中实体一致）。
- 真实环境额外修复：状态栏投影 `_TASK_PRESENTATION` 补 `SOURCE_MEMORY_BUILD`
  （否则 GET /session 500）；恢复扫描改用 `services.projects.list()`。
- 已知残留（非本 WT 引入）：qwen3.7-plus 偶发抄写截断专家工具参数中的 40 字符
  projectId，被运行时 fail-close 正确拦截，重委派即可恢复；后续可评估将
  projectId 从专家工具参数移除、改由运行时上下文绑定。

**CR 整改纪要（2026-08-04 第二轮，提交 `5b229066`）**：
- 两个 P1 + 三个 P2 全部修复并补回归测试（10 个新用例）：
  ① vendored 分词器对 CJK 连续串改出字符 bigram，短台词获得 BM25 精确命中；
  BM25 零分候选不再获得稀疏 RRF 排名（真实产物验证：「一换三」Top1 命中
  `asr_macro_0006_000`）；② 恢复不再重放计费调用——RUNNING 中断仅在完整产物
  已落盘时重排队收敛（_execute 开头产物收敛，零新增调用），否则 fail-close
  保持 FAILED 需新 commit/授权；③ P3 projection 经 `load()` 并回 SI 表面：
  Root/SuperEvent 草稿以 `modelRunId=source_memory` 进入 `semanticEntries`
  （内存合并，canonical 字节不变；真实 KPL 产物 29 条含 6 条 source_memory）；
  ④ ASR 复用改用 `coverage.asr.mode` 判定，静音素材不重复计费；⑤ 编码预算由
  `get_vlm_max_inline_bytes` 派生（含 Base64 膨胀与 headroom）。
- 遗留验收项补执行完成：
  - A2：`tests/manual/test_source_memory_real.py`（marker `manual_real`，默认排除）
    用 KPL 原片前 25 分钟真实跑通全管线（1 passed / 20:23）。
  - A3：抽 3 段（macro_0000/0005/0008）原片音频重新 fun-asr 转写，与图谱
    asr_text 逐句吻合。
  - B1：合成 2 分钟素材（临时 60s 阈值）先拒绝→授权 DECLINED、任务
    CANCELLED（零调用）；新 index 重新触发后批准→SUCCEEDED（2 macros/7 节点）。
  - B3：KPL 会话追问高地团战双方英雄，专家用 subgraph+search_ocr 给出实体/
    OCR 节点依据并主动交叉印证 BP 矛盾；读帧核对 925s（对局团战）与
    1275s（VICTORY 结算）与 OCR 命中一致。
  - B5：会话驱动剪 15 秒团战高光（源区间 915–930s）入时间轴
    `edit:teamfight-highlight-001`，预览播放器可播（render_source 直引）。
  - B6：首轮记录有误（检索实际发生在构建完成后 ≈96s，无重叠），已于
    第三轮重做并取证：合成素材第五版构建 attempt 窗口
    12:40:17Z→12:41:08Z（RUNNING→SUCCEEDED），期间猫项目专家于
    12:40:36Z/12:40:55Z/12:41:00Z/12:41:04Z/12:41:08Z 连续调用
    `query_source_memory`（河床/水边检索，命中 macro_0022/0010 带节点依据），
    构建 RUNNING 与跨项目检索真实重叠。
  - A7/A8：新 index 版本不误挂旧记忆（memoryRef=None、无 source_memory
    entries）；正常 20min 阈值下 130s 素材提交新版本不产生授权/任务。
- B4 已知限制：micro-event 级时间窗来自 VLM 对片段内相对时间的估计，存在
  秒级–十秒级偏移（森林→公路 me_001 报 4192–4197s，实际路面在 ≈ 4200–4230s
    才入画）；macro 级窗口可靠，`memory_guidance` 已要求对精确剪辑窗口回原片
  窄窗核验后再使用。

**CR 整改纪要（2026-08-04 第三轮）**：
- **[P1] projection 审校链落地**：构建期新增外层 VLM 审校步骤
  （`_review_projection`，文本调用，可改写/剔除草稿条目），审校通过后
  `projection.json` 携带 `review{status:approved, model, reviewedAt}`；
  `merge_projection_semantics` 仅合并已审校投影（未审校 fail-close 不进
  表面），且审校后的 Root 摘要以「[长素材记忆摘要 · 已审校]」标记追加进
  `index.summary`（内存层，不动 canonical 字节），满足「草稿进
  summary/semantic_entries、外层 VLM 只审校」的定稿口径。审校失败保留未审
  草稿不影响构建成功。存量 3 份产物已经真实外层 VLM 审校回填
  （全部 APPROVED，KPL 表面验证 summary 含审校标记 + 6 条 source_memory
  entries）。
- **[P2] 预算下限**：推导预算低于可工作下限（256KiB）时直接报
  ValidationError 配置错误，不再返回超出真实 transport 限制的地板值。
- 新增单测 3 项（审校门控/审校回退/summary 幂等追加）+ 预算报错断言；
  Creator 后端 746/746。B6 重叠取证见上节修正后的记录。

**CR 整改纪要（2026-08-04 第四轮，针对 A2/A7/B3/B4 遗留）**：
- **A2 闭环**：`test_source_memory_real.py` 补 macro 数断言（5–50）与双 macro
  中点帧导出（/tmp/wt6-manual-real-frames/）；增强后真实重跑
  **1 passed / 12:40**（KPL 前 25 分钟，8 macros），macro_0004（456–663s）
  中点 559s 帧为开局 01:47 河道遭遇战，窗口与原片一致。
- **A7 真实替换闭环**：UI 上传变体素材 v2（checksum b7504595…→8b0dfd16…）
  → 会话重指 ProjectSource → 重新理解发布新 index bcd6f8cd… →
  memoryRef=None、无 source_memory 投影、UI 徽标仅留在旧 v1 卡（新卡无）、
  正常阈值下无自动授权/任务；会话内 query_source_memory 原始返回：
  `{"ok":true,"available":false,"reason":"该素材尚未构建长素材记忆；构建在
  素材理解完成后自动排队，需要执行授权通过后才会生成。"}`——提示重建✓。
- **B3 收敛**：专家不再给多套答案，基于 ASR 链给出唯一阵容（AG：关羽/
  海月/杨戬/朵莉亚/一诺射手；狼队：司空震/后羿/元坦/张飞/第5人）；
  人工读帧（380s BP 定妝栏、559s 对局头像栏）印证关羽（轩染）/海月（长生）/
  朵莉亚（大帅）/杨戬（钟意）与司空震（信位）。剩余 2 个不确定项源于
  既有能力边界：SI 专家无任意时间点抽帧工具（仅关键帧通路），已作为
  平台限制记录（非 WT6 引入）。
- **B4**：定位精度正式口径已写入验收标准（macro 级承诺，micro 级
  best-effort 需回原片窄窗核验），见上方验收口径段。
- **B6 取证位置澄清**：验收方第二轮引用的是首次（无重叠）运行；真重叠
  证据在第三轮（本页上方）：构建 attempt 12:40:17Z→12:41:08Z 与猫项目
  5 次 query_source_memory（12:40:36–12:41:08Z）重叠，持久化于
  memtask-2abd…/attempts.jsonl 与服务日志。
- 剩余开放项（平台级，非 WT6 范围）：专家工具参数 projectId 的 LLM 抄写
  截断偶发（fail-close 拦截正确，需重委派）；建议后续改为运行时上下文
  绑定；SI 专家缺任意时间点抽帧工具。

**CR 整改纪要（2026-08-04 第五轮，针对 A7/审校防篡改/B3/A2）**：
- **B3 改判 blocked（更正第四轮结论）**：会话终态实际仍存在无法读取
  指定帧的待确认项，"专家唯一阵容已收敛"表述不成立，B3 如实记为
  blocked——SI 专家缺任意时间点抽帧工具（平台级能力缺口，非 WT6 引入），
  在该能力补齐前 Agent 无法回原片核验完整阵容。同时更正第四轮的阵容
  事实错误（人工地面真值，依据原片 380s BP 帧、559s 对局帧与比赛记录/
  官方首发名单）：AG——轩染关羽、钟意杨戬、长生海月、一诺蚩奼、大帅
  朵莉亚；狼队——清清司空震、皖皖元流之子·坦克、紫幻沈梦溪、道崽后羿、
  信张飞。第四轮"司空震（信位）"为误记，司空震实为清清；该地面真值是
  人工核验结论，不代表 Agent 产出。
- **审校防篡改（服务端 fail-close）**：`_review_projection` 不再全盘
  接受模型返回——草稿条目携带不可变 entryId，审校模型只能改文案/标签/
  置信度或剔除条目；未知/重复 entryId（新增条目）或 startMs/endMs 与
  草稿不一致一律判审校失败（草稿保持 unreviewed，不并入 index 表面），
  保留条目的时间窗由服务端从草稿恢复（模型无权改写权威时间窗）。新增
  回归测试：改时间窗 fail-close、新增条目 fail-close、合法剔除+改文案
  仍 approved 且时间窗与草稿一致。
- **记忆徽标版本感知**：AssetsPage 徽标不再仅按 logical asset 匹配
  SUCCEEDED 任务——现要求 ProjectSource 的
  current_intelligence_version_id 所指 index 恰好指向当前
  selected_asset_version_id 且 source_checksum 与该版本 checksum 一致
  才显示（后端 memoryRef 本身按 checksum 门控）。新增 UI 测试覆盖
  "同 logical asset：v1 已构建有徽标；切到未构建 v2 后旧任务不得给新
  版本亮徽标"。
- **A2 断言收紧**：macro 数断言由算法边界（5–50）收紧为 7–9（固定
  KPL 前 25 分钟稳定 8 个），并补窗口有序、不重叠、间隙 ≤2s、起点
  ≤1s、覆盖率 ≥98% 断言（未重跑计费的 manual_real，断言基于既有稳定
  观测；下次真实重跑生效）。
- **A7 同 logical asset 版本替换（如实说明）**：上一轮实为"新
  logical asset + 重指"，不构成同资产版本替换。核对产品入口后确认：
  当前 ingest 通路（文件/URL/文本）每次上传都会派生新的
  logical_asset_id，不存在"给既有 logical asset 追加新版本"的用户可达
  UI/API 入口，故该数据形态在真实 UI 链路中暂不可达（未来由系统流或
  版本追加入口产生时才可真实验收）。徽标缺陷本身已按版本感知修复，
  且以确定性 UI 测试覆盖同 logical asset "v1 已构建 / v2 已选中未构建"
  形态（旧任务不得给新版本亮徽标）；真实链路 A7 版本替换验收留待
  版本追加入口落地后执行，如实记录而非宣称已完成。

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

---

## 四、风险与守护清单（全局）

| 风险 | 影响 WT | 对策 |
|---|---|---|
| vendored 代码许可证合规疏漏 | WT3/WT4/WT6 | vendor/NOTICE.md + 文件头版权与修改标注；PR checklist |
| vendored 代码与上游漂移 | WT3/WT6 | NOTICE 记录 commit `077aea6`；升级人工 diff 回灌 |
| 素材经第三方公共 host 外泄（uguu.se） | WT2 | Lens 一律走自有 OSS 短时效签名 URL；无 OSS 则不可用 |
| oss:// URL 在 multimodal 端点的可解析性未证实 | WT1 | Step 1 实测；不成立回退公网 URL 通路 |
| qwen3-asr-flash 时间戳为均摊估算 | WT1/WT6 | confidence=0 标记；剪辑选段以块级窗口回原片核验 |
| 视频模式矩阵与真实模型能力不符 | WT5 | 矩阵常量 + needs-verify 格逐一实测后定稿 |
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
