# 任务派发 WT9 · 最终集成：按功能线先行合并 + 功能级验收（修订版 v3）

> v3 修订（2026-08-04）：合并组织方式从「冲突面分批」改为**功能维度分线**——
> 每条功能线合并完成后立即做该功能的完整真实验收（对应 acceptance 文件的 UI
> case），使每次集成增量都是一个**可独立交付、可独立归因的用户能力**；最后的
> 端到端只验跨功能交叉链路。归因机制不变：集成分支 + 逐分支 `--no-ff` merge +
> tag + 快门禁 + 30 分钟定位不了即 revert，dev/creator 全程不动。

## 必读引用
- 总方案 `docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
  §2.3 冲突热点 + 各 WT 节回填；各 `dispatch/WT1-8` 与 `acceptance/WT1-8`。
- 主仓 `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw`。

## 阶段 0 · 基线就绪（不变）
1. 处理本地 dev/creator 孤立 commit `ac073503 "update plugin.json"`（推 origin
   或 drop），与 origin/dev/creator 完全同步（含 #64/#68/#69/#73）。
2. 基线门禁：干净基线跑 pre-commit 全量 + 双 pytest + 前端测试 + UI 冒烟
   （建项目 → 一张资产图），记录基线快照排除"本来就有的问题"。
3. `git checkout -b integration/mm-plugins origin/dev/creator`；独立集成隔离栈
   （`QWENPAW_WORKING_DIR=~/.qwenpaw-integ` + 空闲端口，凭据自 `~/.qwenpaw-poc`
   复制）。全程不触碰 dev/creator。

## 逐分支合并操作规范（每条线内相同）
`git merge --no-ff feat/xxx` + tag `integ/<wt>-merged` → 立即跑该分支**快门禁**
（域测试子集，分钟级）→ 绿才合线内下一个；失败 30 分钟定位不了 →
`git revert -m 1`，退回分支负责人，继续后续无依赖工作。文档
MM_PLUGINS_INTEGRATION_PLAN.md 自身冲突取并集（各分支回填章节不重叠）。

## 功能线计划（顺序即执行顺序）

> 执行顺序调整（2026-08-04，用户决策）：**F4 素材理解线先行**（任务书
> `dispatch/F4-perception-line-integration.md`，阶段 0 并入其中），后续线序
> 待 F4 完成后再定（建议 F2 → F3 → F1 → F5）。下述 F1–F5 的线内定义与验收
> 标准不变。

### 线 F1 · 检索增强（热身线，验证集成机制）
| 合并 | 快门禁 |
|---|---|
| `feat/creator-grounding-serper` | web_grounding 域测试 + api-contract |

**功能验收**（acceptance/WT2 的 UI 部分 B1–B4 + 代码层 A1/A3/A4 抽样）：
Serper 进入 provider 序列、真实检索来源可用、Lens OSS 通路、无 Key 回退。
✅ 交付物：grounding 能力增强，独立可用。

### 线 F2 · 内容生成（核心创作能力，含 TTS 全家）
| 合并 | 快门禁 |
|---|---|
| `feat/creator-gen-providers` | models 域测试（image/video/s2v/tts）+ 能力矩阵校验 + api-contract |

**合并后 · TTS 分支冗余处置**：
`git diff integration/mm-plugins feat/creator-tts-voice -- plugins/apps/qwenpaw-creator`
预期为空（gen-providers 已重放 TTS commits + CosyVoice 扩展）→ 归档
`feat/creator-tts-voice`；有实质差异则补独立 commit 并记录。

**功能验收**（acceptance/WT5 全套：A 组百炼真实调用 + 5t TTS 基线回归 + B 组
UI 完整数据流「剧本→资产→分镜→视频」）：图像 edit/translate、happyhorse
t2v/video_edit、wan、s2v 数字人、TTS 合成/音色设计/复刻/ducking。
> 成本提示：本线验收含视频与数字人真实生成（此前方案推迟到最后）——功能维度
> 先行的代价是费用前置，收益是生成能力问题在此线内闭环归因；阶段 E 交叉验收
> 时不再重复单功能 case。每条计费调用仍逐条确认。
✅ 交付物：创意生成链路全增强，独立可用。

### 线 F3 · 成片质量（动效 + 自评，验收可用 F2 的 TTS 配音）
| 序 | 合并 | 快门禁 |
|---|---|---|
| 1 | `feat/motion-js-timeline` | motion 专项测试 + timeline workbench 回归（与基线 #69 交叠重点核对） |
| 2 | `feat/creator-self-review` | render_review 测试 + 开关关闭零行为差异回归 |

（线内顺序不可换：两者共改 `local_execution.py`，motion 重改在先、self-review
单点挂钩在后。）

**功能验收**（acceptance/WT8 全套 + acceptance/WT4 全套）：html_js 动效设计/
渲染/真值自查/循环，六维自评评测集 + 完整链路 revise 反馈环（配音维度用 F2
已就位的 TTS 旁白构造缺陷片）。
✅ 交付物：compose 质量闭环，独立可用。

### 线 F4 · 素材理解（感知能力，线内有依赖链）
| 序 | 合并 | 快门禁 |
|---|---|---|
| 1 | `feat/creator-asr-qwen3` | asr 域测试（静音切块/去重/分派） |
| 2 | `feat/creator-doc-reader` | document/coverage 域测试（fail-closed 边界）+ api-contract |
| 3 | `feat/creator-source-memory` | source_memory 全部测试 + source_intelligence 回归（勿破坏文档入口） |

（线内顺序不可换：WT6 的 ASR 轨用 WT1 的 qwen3-asr、vendor 与
source_intelligence 结构依赖 WT3。）

**功能验收**（acceptance/WT1 B 组 + WT3 全套 + WT6 全套）：qwen3-asr 转写、
PDF/PPTX 读取与文档 index、>20min 长素材记忆构建（25min 缩样 + KPL/猫 POV
两素材全量）、台词/语义检索回原片读帧核对、检索→剪辑出片。
✅ 交付物：素材剪辑链路感知全增强，独立可用。

### 线 F5 · 生态扩展（与所有线零交集，可插在任意线间隙）
| 合并 | 快门禁 |
|---|---|
| `feat/creator-external-skills` | external_skills 测试 + 会话建立回归（坏 skill 隔离） |

**功能验收**（acceptance/WT7 全套）：edu-agent 一条讲解视频（读帧验数学与
渲染质量）、坏 skill 零影响回归、disable 生效、执行授权。
✅ 交付物：外置 skill 能力，独立可用。

## 阶段 E · 跨功能交叉验收 + 合回主干
单功能已在各线验过，本阶段只验**功能间交互**（acceptance/WT9 精简执行）：
1. 创意生成链路一次通片：PDF 参考（F4）+ grounding（F1）+ 生成/TTS/数字人
   （F2）+ compose 开自评（F3）——验各功能在同一项目内协同；
2. 素材剪辑链路一次通片：长视频记忆检索选段（F4）+ 动效（F3）+ 自评（F3）；
3. 交叉抽查 G1–G5（skill 共存、矩阵拒绝、配置面全区块、开关关闭回归）。
全过后：dev/creator merge `integration/mm-plugins`（期间主线有新合入则先 merge
进集成分支重跑全量再合回）；push 注意密钥扫描；清理全部 worktree 与特性分支
（含归档的 tts 分支）；回填总方案 §2.3（每线 merge commit 号、门禁与验收结果、
费用记录），收拢各分支文档副本的回填，主文档为唯一终稿，全 WT 升级 ✅。

## 范围边界（不变）
- 计划外分支不纳入（grounding-parity / caption-layout-safety /
  inspiration-examples / dynamic-motion），集成后另行处理；
- 真实验证仅限阿里系百炼模型；每次计费调用事先确认；
- UI 验收三准则：读实际内容、不跳步、只经前端。

## 完成标准
- 五条功能线各自合并 + 功能验收全过（每线有 tag 与验收证据）；
- 阶段 E 交叉验收过；dev/creator 合回且 CI 绿；清理与文档回填收拢完成。
