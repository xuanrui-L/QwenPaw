# 任务派发 WT5 · 生成侧扩展三件套（`feat/creator-gen-providers`）

## 你的任务
同一 worktree 内按 commit 串行交付三件：5a qwen_image 编辑与翻译模式 → 5b 视频
生成模式矩阵（t2v/i2v/video_edit）→ 5c wan_s2v 数字人 provider。全部复刻 TTS 范式。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT5 节 + §1.2 事实 9 + §1.3 TTS 范式 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/models/`（image/、video_model.py、
  video_capabilities.py、config.py、media_transport.py、tts_model.py 为范式样板）、
  `backend/services/specialist_tools.py`、`plugin.json`、`backend/schemas/models.py`、
  `ui/src/contracts/creator/models.ts`、`ModelConfigModal.tsx`、`ModelBadges.tsx`。
- 上游协议参考（只读）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/video-edit/qwen_mm_plugins_video_edit/tools/`
  （qwen_image.py / happyhorse.py / wan_t2v.py / wan_s2v.py）。

## 全局硬约束（引自总方案 §2.2 / §1.3 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 env 注入；不做进程直调。手法 A。
2. TTS 范式六要素：Creator 原生工具/服务；data model 先行；`creator_*_model`
   三级配置树；按 Key 动态注册；计费 `requires_execution_authorization=True` +
   现有 poller（wait=TASK）；产物 AssetFileStore 落盘（远端 URL 24h 过期）。
3. 计费单测一律 respx 打桩；真实调用仅人工验收且**每次高消费事先确认成本**。
4. 人工验收必须**查看实际生成的图/视频内容**判断语义正确（读帧），走前端 UI。
5. pre-commit + 双 pytest 全绿；注释英文。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-gen-providers -b feat/creator-gen-providers feat/creator-tts-voice
```
**拉取基线为 `feat/creator-tts-voice`（已定稿，非 dev/creator）**：5c 消费 TTS 的
audio 资产与动态注册范式（`_TTS_TOOL_NAMES` / `is_tts_configured()` /
`audio_execution.py`），而 TTS 分支不预先合入主干。注意：基线中 TTS 分支领先
dev/creator 的 1 个 commit 会随你的分支一起在集成阶段处理（集成序：TTS 最先合，
你的分支在其之后，到时 diff 自然收敛为纯增量）。隔离栈：`dev-isolated.sh`、
`QWENPAW_WORKING_DIR=~/.qwenpaw-gen`、端口 **8095**；凭据复制自主实例
`~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 基线自带的 TTS 能力（已完成，须保持并消费）

你的分支基于 `feat/creator-tts-voice`，以下 TTS 能力**已实现并自带测试**（实证自
commit fb4ad9bf），是本 WT 的既成事实而非待开发项：

1. **合成**：`tts_generation` 工具（roles=VISUAL_DEVELOPMENT +
   AI_EDITING_DIRECTOR，执行授权）——text ≤800 字符（长旁白按镜头/语义分段多次
   调用）、voice 可选系统音色、`characterRef` 传角色实体时自动改用其绑定的复刻
   音色；产物为不可变 audio SourceAssetVersion，返回 version id +
   durationSeconds。模型 qwen3-tts-flash（默认音色 Cherry，8 个系统音色）。
2. **音色复刻/设计**：`create_character_voice`（模型 qwen3-tts-vc-2026-01-22），
   音色来源**三选一**：`voicePrompt`（纯文字描述设计音色，无需样本）/
   `sampleSourceVersionId`（10–20s 已有音频复刻）/ `sampleText`（先用系统音色
   试音再复刻）；绑定到 character 实体，重复复刻替换旧绑定。
3. **音频后处理**：`audio_execution.py` 含 WAV header 时长校正（qwen3-tts 返回的
   WAV header 帧数为占位值，靠 `_wav_duration_seconds` + ffprobe 兜底）；
   `local_execution.py` 含旁白自动 ducking（旁白窗口内原片音量降至 0.35，
   重叠窗口合并）。
4. **UI**：ModelConfigModal TTS 区块、ModelBadges、ElementDetail 音频元素详情
   （音色/模型/时长/文本预览）、AssetsPage 音频类目。
5. **动态注册**：无 TTS Key 时两工具不注册（`_TTS_TOOL_NAMES` gate，你 5c 的
   `_S2V_TOOL_NAMES` 照此模式）。
6. **既有测试**（你的每个 commit 都必须保持全绿）：`tests/models/test_tts_model.py`、
   `tests/media/test_audio_mixing.py`、`tests/services/test_tts_specialist_tools.py`、
   `tests/prompts/test_tts_prompt_guidance.py`。

**对本 WT 的 TTS 相关要求**：
- 5c 的 `audioAssetRef` 必须能直接消费 `tts_generation` 产出的 audio version
  （含复刻音色合成的），时长取校正后的 durationSeconds；
- 不得修改 TTS 协议/配置树/工具参数（DashScope 百炼协议强制绑定是既定规范）；
  如发现 TTS bug 单独 commit 修复并在 PR 描述中标注；
- 验收时 TTS 链路作为基线能力一并回归（见 acceptance/WT5 的 5t 组 case）。

## 实现规格（引自总方案 §三 WT5，已定稿）

### Commit 1 · 5a qwen_image 编辑与翻译
> **现状**：`models/image/dashscope_provider.py` 已用 multimodal-generation 端点
> （默认 `qwen-image-2.0-pro`）且已支持参考图（本地图经 DashScope 临时存储转
> `oss://` + resolve header）——与上游 image_edit payload 同构，缺的是工具层模式
> 语义与 translate 模型。
> 1. `image_generation` 工具参数增 `mode: enum[generate, edit, translate]`
>    （缺省 generate 保兼容）与 `referenceImageRefs: list[fileRef] (≤3)`。
> 2. dashscope_provider：edit 复用现有参考图通路（校验 1–3 张）；translate 切模型
>    `qwen-mt-image`（image 配置树下加可选 `translate_model` 字段，缺省
>    qwen-mt-image）；messages 图前文后（对照上游 qwen_image.py：t2i/edit 用
>    qwen-image-2.0-pro，parameters 含 size(W*H) 与 watermark:false）。
> 3. plugin.json `creator_image_model` 追加可选 translate_model；schemas /
>    contracts / ModelConfigModal image 区块同步。OpenAI provider 不支持
>    edit/translate → 校验给可读错误。

### Commit 2 · 5b 视频生成模式矩阵
> **现状**：`submit_video_task(prompt, reference_image_url,
> reference_image_url_list, ratio, duration, resolution, watermark,
> generate_audio)`；happyhorse 已支持但全按 r2v 契约（1–9 图、720P/1080P、3–15s）。
> 1. data model：视频请求增 `mode: enum[r2v, t2v, i2v, video_edit]`（默认 r2v）、
>    `first_frame_ref` / `video_ref`；`r2v_generation` 工具 `_arguments` 增
>    mode / firstFrameRef / videoRef（**已定稿：不改名只扩参**）。
> 2. video_model.py 按 (backend, mode) 组装 payload（对照上游 happyhorse.py）：
>    - happyhorse 模型名**已定稿：配基名（如 happyhorse-1.1）+ 按 mode 拼后缀**
>      `-t2v/-i2v/-r2v/-video-edit`（与上游模型族命名一致；token-portal 双层模型名
>      经 extendParams 的现状规则维持）；t2v `input={"prompt"}`；i2v
>      `input.media=[{"type":"first_frame","url"}]`；video_edit
>      `input.media=[{"type":"video","url"}]`（输入 3–60s，>15s 上游自动截前 15s，
>      工具描述告知 Agent）；r2v 现状不动。
>    - wan：t2v/i2v 对照上游 wan_t2v.py 并以百炼文档双确认；**无 video_edit**。
>    - seedance2（火山引擎，非百炼）：t2v/i2v **本期不开放**（已定稿：真实验证
>      范围限百炼模型，非百炼不做真实调用），矩阵标 ❌、校验层拒绝；r2v 维持
>      现状不新增验证。
> 3. 能力矩阵常量 `video_capabilities.py::VIDEO_MODE_MATRIX:
>    dict[backend, frozenset[mode]]` + `validate_video_mode()` 拒绝不支持组合并
>    提示替代；`video_model_guidance` prompt 占位符追加矩阵说明。
>    矩阵：r2v=happyhorse/wan ✅、seedance2 ✅（现状不新增验证）；t2v/i2v=
>    happyhorse/wan ✅、seedance2 ❌（非百炼不开放）；video_edit=仅 happyhorse ✅。

### Commit 3 · 5c wan_s2v 数字人
> 1. `models/s2v_model.py`（新）：`detect_face(image_url) ->
>    FaceDetectResult{passed, reason}`——POST
>    `{base}/services/aigc/image2video/face-detect`，
>    `{"model":"wan2.2-s2v-detect","input":{"image_url"}}`（**免费**；失败原因
>    透传：多人/侧脸/模糊/遮挡/风格不支持）；`submit_s2v_task(image_url,
>    audio_url, resolution) -> task_id`——模型 `wan2.2-s2v`，
>    `input={"image_url","audio_url"}`，`parameters={"resolution":"480P"|"720P"}`，
>    走现有 poller。校验：人像图单边 400–7000px；audio 必填。
> 2. 配置树 `creator_s2v_model`：config.py getter（get_s2v_api_key/base_url/
>    model_name/detect_model_name/timeout_seconds、is_s2v_configured）；
>    plugin.json block（requires_config:false）；schemas S2vConfig；contracts +
>    ModelConfigModal + ModelBadges 增区块。
> 3. 工具 `s2v_generation`：ToolSpec(roles={R2V_GENERATION_DIRECTOR},
>    requires_execution_authorization=True, long_running=True, wait=TASK,
>    provider_kind="s2v", parameters={characterImageRef, audioAssetRef,
>    resolution?})；gating 对照 `_TTS_TOOL_NAMES` 模式新增 `_S2V_TOOL_NAMES`；
>    执行顺序：detect（免费）→ 失败即返回可读错误（**不创建执行授权**）→ 通过才走
>    授权 + 提交。audioAssetRef 直接消费 TTS 产出的 audio 资产。

## 测试与验收
- 三件各配 respx 协议单测（payload 形状、模式矩阵校验、模型名派生、detect 免费
  短路、任务状态机、错误路径）；打桩 poller 集成；api-contract 测试扩展。
- 人工验收（隔离栈、真实 Key、UI 操作、读实际内容）：**真实调用仅限百炼模型**
  （happyhorse / wan / qwen-image / qwen3-tts / wan2.2-s2v；seedance2 与 OpenAI
  image 不做真实调用，只测校验拒绝与配置回显）。5a 编辑/翻译各一例；5b 含
  happyhorse t2v 与 video_edit 各一次（先零成本健康检查端点验证模型名可用）；
  5c 数字人一例（角色图 + TTS 音频）；TTS 基线回归见 acceptance/WT5 的 5t 组。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9，集成序：TTS 最先、本分支倒数第二）。三件按 commit
  串行提交在同一分支上。
- 热点：specialist_tools.py 只追加/扩参；config.py 只追加配置树；plugin.json /
  ModelConfigModal 只动 image/video/s2v 区块。
- 完成后回填总方案 WT5 节（happyhorse 各模式健康检查与模型名派生实测结论；
seedance2 本期不验证，无需补格）。
