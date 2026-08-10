# 真实调用测试项目 WT1 · qwen3-asr-flash（隔离栈 8091）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT1 · qwen3-asr-flash 支持（开发派发单 `docs/proposals/mm-plugins-creator-integration/dispatch/WT1-asr-qwen3.md`） |
| 分支 / worktree | `feat/creator-asr-qwen3` · `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-asr-qwen3` |
| 被测代码 | 上述目录下 `plugins/apps/qwenpaw-creator/`（backend + ui），核心改动 `backend/models/asr_model.py` |
| 测试实例 | 独立隔离栈，浏览器访问 `http://127.0.0.1:8091/` → 左侧 Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（另有 status / stop / verify；脚本在 worktree 根，由开发者交付，缺失先索取） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-asr`（与主实例 `~/.qwenpaw-poc`/8088 完全隔离，**勿动主实例**） |
| 模型凭据 | `~/.qwenpaw-asr/creator-runtime/config/model_config.json`（自 `~/.qwenpaw-poc/creator-runtime/config/model_config.json` 复制；解密需设 env `QWENPAW_KEYRING_ACCOUNT`，值向环境负责人索取） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_asr_qwen3.py -v` |
| 背景文档 | 总方案 `docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT1 节 |

> 与开发期打桩单测互补：本文件全部 case 使用**真实 DashScope Key 与真实模型调用**。
> **验证范围仅限阿里系百炼模型**（fun-asr / qwen3-asr-flash 均属百炼）；
> Whisper（OpenAI）provider **不做真实验证**，仅保留打桩单测。
> 凭据：复制主实例 `~/.qwenpaw-poc/creator-runtime/config/model_config.json`。
> 成本量级：ASR 按音频时长计费，全套 case 预计消耗 <30min 音频转写额度，执行前口头确认。

## 全局测试准则（每个 case 强制）
1. 质量验证必须查看/收听实际内容，不允许仅以 HTTP 200 / 字段非空判定；
2. UI 层测试只经前端操作，不直调 API、不改 JSON 数据文件；
3. 发现 bug 才可下钻代码与数据层定位。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_asr_qwen3.py`，标记
`@pytest.mark.manual_real`（默认 skip，`pytest -m manual_real` 人工触发），Key 从
runtime config 读取（不写死）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | 短音频直转 | <5min 中文语音 wav/mp3 fixture | segments 非空；文本与人工听写抽查 3 句一致；start/end 单调递增 |
| A2 | 长音频分块 | >6min 音频（可用 ffmpeg 拼接 A1 素材） | 产生 ≥2 块；跨块处 end_ms/start_ms 连续无回退；全文无重复段/丢段（对照原文） |
| A3 | 视频容器输入 | 含语音的 mp4 | ffmpeg 抽音轨路径走通，结果同 A1 标准 |
| A4 | 无声输入 | 无音轨视频 | 返回空 segments 或可读错误，不抛未捕获异常 |
| A5 | 语言参数 | 英文音频 + `ASR_LANGUAGE=en` | asr_options.language 生效（trace/请求日志核对），文本为英文 |
| A6 | oss:// 可解析性 | A1 素材经 `upload_local_file_to_dashscope_temp` | multimodal 端点接受 oss:// URL；若拒绝，记录报错并验证公网 URL 回退路径 |

前端代码层：无 UI 改动，仅跑既有 api-contract 测试确认零回归。

## B. 前端真实使用测试（UI）
隔离栈启动后全程浏览器操作：
1. Model Configuration → ASR 区块：model 填 `qwen3-tts-flash` 同款文本框改为
   `qwen3-asr-flash`，保存；ModelBadges 显示 ASR 已配置。
2. 新建「素材剪辑」项目 → 导入一段 2–3min 含台词的视频素材。
3. 触发素材理解（Source Intelligence）→ 面板查看 transcript。
4. 再导入一段 >6min 素材重复步骤 3（覆盖 UI 侧长音频路径）。

## C. UI Case 清单
| # | 操作 | 期望 | 验证方法 |
|---|---|---|---|
| B1 | 配置保存后刷新页面 | 配置持久化，model 名回显 | 目视 |
| B2 | 短素材转写 | 面板出现 transcript 且分句合理 | **播放素材原声**对照抽查 5 句文本一致 |
| B3 | 长素材转写 | 转写覆盖全片（末段有内容） | 拖到素材末 1min 播放，对照末段 transcript |
| B4 | 时间定位 | 点击 transcript 条目跳转的时间窗与台词大致对齐（qwen3-asr 时间戳为块内均摊估算，容差 ±块长） | 点击 → 播放核对 |
| B5 | 错误呈现 | 填一个不存在的 model 名 → 转写失败 | UI 给出可读错误而非静默/白屏 |

## 通过标准
A1–A6 全过（A6 允许「回退路径通过」）；B1–B5 全过；实测结论（尤其 A6 与 B4 的
时间戳精度表现）回填总方案 WT1 节。
