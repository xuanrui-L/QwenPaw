# 真实调用测试项目 WT9 · 最终集成端到端验收（合并后的 dev/creator）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT9 · 九分支统一合并后的全链路验收（集成派发单 `dispatch/WT9-final-integration.md`） |
| 分支 / 位置 | `dev/creator`（全部合并完成后）· 主仓 `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw`（无独立 worktree） |
| 测试实例 | 主实例：浏览器 `http://127.0.0.1:8088/` → Apps → QwenPaw Creator（数据根 `~/.qwenpaw-poc`）；如不想动主实例数据，可另起干净栈（新 `QWENPAW_WORKING_DIR` + 空闲端口，凭据同样从 `~/.qwenpaw-poc` 复制） |
| 启动 | 主实例按项目常规方式启动（如已在跑则直接用）；验收前确认插件后端为合并后代码（重新构建/重启） |
| 前置条件 | dispatch/WT9 的合并序列全部完成且自动化测试全绿；GSAP vendor 已 fetch；libreoffice/Node≥18/ffmpeg 已安装 |
| 模型凭据 | 全部能力需已配置：LLM/VLM/ASR/Image/Video/TTS/S2V/Grounding（含 Serper）/Embedding/OSS；另需宿主 env `DASHSCOPE_API_KEY`（edu-agent）与启动 env `CREATOR_SELF_REVIEW_ENABLED=true`（E7/F5） |
| skill 配置 | `~/.qwenpaw-poc/creator-runtime/config/skills_config.json`（G1/G3 用，edu-agent 源目录见 WT7 验收文件） |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §2.3 + 各 WT 验收文件 `acceptance/WT1-8` |

> 前提：WT9 九分支合并完成、全量自动化测试绿。本文件是**合并后的真实全链路验收**，
> 所有模型调用真实计费——执行前汇总费用预估并确认。两条主链路 + 交叉能力矩阵。
> 全程以新手用户视角**只经前端 UI** 操作。
> **真实验证范围仅限阿里系百炼模型**（LLM/VLM、qwen3-asr、qwen-image、
> wan/happyhorse、qwen3-tts 系列含音色复刻、wan2.2-s2v、qwen3-vl-embedding）；
> seedance2（火山）/ OpenAI image / Whisper **不做真实调用**，只验配置保存回显。

## 全局测试准则（每个 case 强制）
1. 一切生成产物读图/读帧/听音验证语义正确；
2. 数据流不跳步：剧本 → 分镜文本 → 资产（角色锚点+场景基准）→ 分镜图（以资产图
   为参考）→ 视频；
3. 只经 UI，发现 bug 才下钻代码与数据。

## 链路一 · 创意生成短剧（覆盖 WT2/3/4/5 + TTS）
主题建议：「一只戴红围巾的橘猫在巴黎的一日冒险」（3 镜，30–45s）。

| # | 步骤 | 覆盖 | 验证方法 |
|---|---|---|---|
| E1 | 上传一份 PDF 创作参考（人物设定文档）随简报导入 | WT3 | Agent 引用了 PDF 中的设定（对照原文） |
| E2 | 简报含真实地标事实需求 → grounding 触发 | WT2 | 来源列表含 serper；参考图相关（读图） |
| E3 | 剧本 → 资产：角色锚点图 + 场景基准图 | 基线 | 读图：角色特征稳定、场景对应剧本 |
| E4 | 分镜图生成（资产图为参考）；其中一张用 edit 模式局部修正 | WT5a | 前后对比：仅局部变化、角色不漂移 |
| E5 | 视频：镜 1 r2v（默认）、镜 2 happyhorse t2v、镜 3 用 video_edit 改写镜 1 产物风格 | WT5b | 逐镜读帧：内容对应分镜；授权与费用每条确认 |
| E6 | 角色音色（voicePrompt 设计或样本复刻）→ TTS 旁白（characterRef 自动选用复刻音色）→ 一个数字人插镜 | TTS+WT5c | 口型同步、音色与设计/样本一致（播放对照）；旁白 durationSeconds 与实际时长一致；成片中旁白窗口 ducking 生效（听 E8 成片对应时段） |
| E7 | compose（开 `CREATOR_SELF_REVIEW_ENABLED`） | WT4 | 审阅报告结论与成片实际质量一致（抽帧对照证据帧）；若 revise 观察修订回合收敛 |
| E8 | 下载成片完整观看 | 全部 | 画面/配音/字幕/节奏整体成立 |

## 链路二 · 长素材剪辑（覆盖 WT1/3/6 + 动效 WT8）
素材：KPL Game 5 长视频（WT6 指定素材 2，构建费用先确认）。

| # | 步骤 | 覆盖 | 验证方法 |
|---|---|---|---|
| F1 | ASR 配置为 qwen3-asr-flash → 导入素材 → 转写产出 | WT1 | 播放对照抽查台词 |
| F2 | >20min 触发 memory 构建（授权+预估）→ 徽标出现 | WT6 | 目视 |
| F3 | 会话：「剪一条 30s 的 AG 超玩会关键团战高光」→ 检索定位 → 选段 | WT6 | **回原片读帧**核对每个选段确为团战 |
| F4 | 高光片段加开场动效 + 字幕 | WT8 | 抽帧：动效不越界不遮主体 |
| F5 | compose + 自评 → 成片完整观看 | WT4 | 高光内容正确、解说轨与画面同步 |

## 交叉能力抽查
| # | case | 覆盖 | 验证 |
|---|---|---|---|
| G1 | edu-agent skill 出一条几何题讲解视频 | WT7 | 读帧：数学正确、公式渲染完整 |
| G2 | 能力矩阵拒绝：wan + video_edit、seedance2 + t2v（均无真实调用） | WT5b | 错误提示给出替代 |
| G3 | 坏 skill 配置共存 | WT7 | 链路一/二全程零影响 |
| G4 | 全部模型配置区块（LLM/VLM/ASR/Image/Video/TTS/S2V/Grounding/Embedding/OSS）保存-回显-生效 | 配置面 | 逐区块目视；**仅百炼系区块各触发一次真实调用**（含 TTS 合成一段短句试听）；非百炼（seedance2/OpenAI image/Whisper）只验保存回显不触发调用 |
| G5 | 关闭自评开关重跑一次小 compose | WT4 | 行为与主干旧版一致（零回归） |

## 通过标准
- E1–E8、F1–F5、G1–G5 全过，全部验证有读帧/读图/听音证据（截图留档）；
- 两条链路成片可对外展示级完整；
- 缺陷清单（若有）逐条建 issue 并标注所属 WT；
- 费用汇总与验收结论回填总方案 §2.3（集成记录）后，本项目收口。
