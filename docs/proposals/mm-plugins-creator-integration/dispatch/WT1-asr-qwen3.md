# 任务派发 WT1 · qwen3-asr-flash 支持（`feat/creator-asr-qwen3`）

## 你的任务
让 `creator_asr_model` 配置 `qwen3-asr-flash` 后，Source Intelligence 的
`transcribe_source_audio` 全链路可用。先零代码实测，再按需补协议分支。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT1 节 + §1.2 事实 3/9 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/models/asr_model.py`、
  `backend/models/config.py`（ASR getter 区）、
  `backend/models/media_transport.py`（`upload_local_file_to_dashscope_temp`）。
- 上游协议参考（只读，不引入依赖）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/core/qwen_mm_plugins_core/apis/asr.py`
  （`_transcribe_dashscope` 的 MultiModalConversation 调用形态、≤5min 分块）、
  `src/shared/api_dashscope.py`（重试参数）。

## 全局硬约束（引自总方案 §2.2 / §1.3 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 env 注入；不做进程内直调 handle。
   上游仅作协议参考（手法 A：httpx 自建薄客户端）。
2. data model 变更必须同步 Pydantic schema + 前端 contract + api-contract 测试
   （本 WT 预期零 data model 变更）。
3. pre-commit + 双 pytest 全绿；注释精简且英文；计费 API 单测一律 respx 打桩，
   真实调用仅人工验收且事先确认成本。
4. 验收走前端 UI、查看实际转写内容，不以 HTTP 200 判定。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-asr-qwen3 -b feat/creator-asr-qwen3 dev/creator
```
基线为**当前 dev/creator**（无前置合并）。隔离栈：worktree 根放 `dev-isolated.sh`（记入
`.git/info/exclude`），`QWENPAW_WORKING_DIR=~/.qwenpaw-asr`，端口 **8091**；模型凭据
复制主实例 `~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 实现规格（引自总方案 §三 WT1，已定稿）
> **现状事实**：入口 `transcribe(media_url) -> ASRResult{provider, model,
> segments: tuple[ASRSegment{start_ms, end_ms, text, confidence=1.0, speaker}]}`；
> `get_asr_provider()` 为 "whisper" 走 `_whisper()`，否则 `_fun_asr()`；
> `_fun_asr()` 经 `_fun_asr_file_url()`（视频 ffmpeg 抽音轨 → DashScope 临时上传得
> 48h `oss://` URL）→ POST `{base}/services/audio/asr/transcription`（异步）→ 轮询
> → 解析 transcription.json。现有实现无重试机制。
>
> **Step 1 · 零代码实测**：隔离栈 UI 配置 ASR model=`qwen3-asr-flash`（provider
> 不动），走一次 `transcribe_source_audio`。预期失败（qwen3-asr-flash 只在
> MultiModalConversation endpoint 服务，与 fun-asr 文件转写 API 不同）。记录确切
> 报错回填总方案 WT1 节。
>
> **Step 2 · 协议分支**：
> 1. `transcribe()` fun-asr 分支前插入
>    `if model.casefold().startswith("qwen3-asr"): return await _qwen3_asr(media_url)`；
>    不新增 provider 枚举/配置项。
> 2. `_qwen3_asr()`：复用 `_fun_asr_file_url()` 取 `oss://` URL（多模态端点对
>    resolve header 的支持在 Step 1 一并实测；不成立则回退公网 http URL 通路）；
>    ffprobe 取时长，>270s 时 ffmpeg `-f segment -segment_time 270` 切块逐块转写；
>    POST `{asr_base 的 host}/api/v1/services/aigc/multimodal-generation/generation`，
>    body `{"model": model, "input": {"messages": [{"role":"user","content":
>    [{"audio": url}]}]}, "parameters": {"result_format":"message",
>    "asr_options": {"language": get_asr_language() 或省略}}}`；
>    解析 `output.choices[0].message.content[*].text`；块内句子按时长均摊
>    start/end（`confidence=0.0` 标记估算），跨块加偏移；返回
>    `ASRResult(provider="fun-asr", model=model, ...)`（不改 schema）。
> 3. 重试：新增 `_post_with_retry(...)`——瞬断线性退避（×3，2s 起）、
>    `Throttling.*` code 指数退避（×4，base 2s + jitter）；仅新分支使用。

## 测试与验收
- 单测（respx）：model 名分派、分块偏移回填、Throttling 重试、空音轨、language 透传。
- 集成（真实 Key，人工触发）：<5min 与 >5min 素材各一段，抽查 segments 与原音一致。
- 验收：前端 ModelConfigModal 填 `qwen3-asr-flash` 即可用（零 UI 改动），
  Source Intelligence 面板可见转写产出。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9，本分支集成序列中排第三）。开发完成后在分支上保持
  可合并状态（测试全绿）即可。
- 热点：`models/config.py` 仅允许极轻改动（如无必要不动）。
- WT6 开发期通过 `transcribe()` 接口解耦，不直接依赖你的代码；你的分支不需要
  为它做任何适配。
- 完成后回填总方案 WT1 节实际差异（尤其 Step 1 实测结论、oss:// 可解析性）。
