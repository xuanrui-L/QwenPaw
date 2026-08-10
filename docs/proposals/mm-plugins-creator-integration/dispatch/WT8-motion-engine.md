# 任务派发 WT8 · hyperframes 动效引擎完善（`feat/motion-js-timeline`，既有 worktree）

## 你的任务
在**既有分支** `feat/motion-js-timeline` 上继续完善 Creator 的 html_js 动效引擎
（HyperFrames 同源方法论）：先确认分支当前状态可用（WT8a），再补齐对照 edu-agent
完整实践的差距项（WT8b）。**不 rebase、不合入**——合并留待最终集成阶段。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT8 节 + §2.3）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 工作位置（既有 worktree，勿新建）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/motion-js-timeline`
  —— 关键文件：`plugins/apps/qwenpaw-creator/backend/services/media_files/
  motion_engine.py`（248 行：`__hf` 协议、确定性 prelude、GSAP vendor 白名单、
  engine digest）、`motion_design.py`（双格式契约与布局铁律）、
  `local_execution.py`（capture worker 逐帧 seek 截屏）、
  `tests/media_files/test_motion_js_timeline.py`（332 行专项测试）。
- 方法论对照（只读）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/edu-agent/skill/`
  的 post-render gates（SKILL.md Step 6：check_render_overlap / postcheck.py
  线条漏画检测 / 抽帧肉眼自查循环）——吸收思想，不引其代码。

## 全局硬约束（违反即返工）
1. **不 rebase 到 dev/creator、不发起合并**：与主干的差异（落后 5 个 commit）
   留待最终集成阶段（集成序中 motion 排第二，见 dispatch/WT9）；只在既有分支上
   追加 commit 并推送。
2. vendor 白名单 + 内容哈希 pin 机制不变；本期**不新增 vendor 库**（KaTeX/字体
   是教学排版需求，装饰动效用不到）；GSAP 文件不入库（GreenSock 许可证约束）。
3. 与 edu-agent 的边界：Creator 自建引擎不依赖 hyperframes CLI/npx；edu-agent 在
   WT7 skill 沙箱内独立渲染，两套**保持隔离不混用**。
4. pre-commit + 双 pytest 全绿；注释英文；人工验收抽帧查看实际画面。

## Worktree 准备
既有 worktree 原地继续：
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/motion-js-timeline
```
隔离栈：`dev-isolated.sh`（入 `.git/info/exclude`）、
`QWENPAW_WORKING_DIR=~/.qwenpaw-motion`、端口 **8098**；凭据复制自主实例
`~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 实现规格（引自总方案 §三 WT8，已定稿）

### WT8a · 分支就绪（先做）
> - 确认工作区干净、全量跑 motion 专项测试 + 隔离栈人工验收一条带 html_js 装饰
>   动效的合成，确认分支当前状态可用；
> - **不 rebase、不合入**；重点交叠预先记录（写入本分支一个 NOTES 或 PR 描述）：
>   `local_execution.py` 与 `project_files/models.py` 与主干新 commit（rejection
>   feedback loop 等）的交叠点，供集成阶段参考。

### WT8b · 完善项（在既有分支继续提交）
> 1. **渲染真值自查（最大差距）**：动效渲染完成后抽 2–3 关键帧（首/中/尾）做
>    确定性规则检查——越界像素检测（透明盒外沿 alpha 采样）、空帧检测；不合格
>    拒绝入库并回馈重生。语义级检查（遮挡主体/美观）不在此层重复建设，留给 WT4
>    自评环六维检查统一覆盖（动效属于画面质量维度）。
> 2. **vendor 注册表扩展机制**：保持白名单 + 哈希 pin 不变；在 motion_engine
>    文档化新增 vendor 的流程（哈希、许可证审查、digest 升版）。
> 3. **loop 语义闭环**：prompt 契约已有 loop 字段——确认渲染器按周期拨时间的
>    实现与帧缓存键对 loop 的盐化覆盖，补齐测试。
> 4. **`__hf` 契约文档化**：将 seek 安全规则（禁 rAF/随机/时钟、回调抑制、末尾
>    同步）从 prompt 文本提炼为 docs/ 内部契约说明，供后续动效能力（转场、字幕
>    动效）复用同一引擎时对齐。

## 测试与验收
- WT8a：既有 332 行专项测试 + 全量回归为门禁。
- WT8b：渲染真值自查单测（构造越界/空帧样本）与 loop 盐化测试。
- 人工验收：隔离栈跑一条含装饰动效的完整合成，**抽帧查看实际画面**确认动效
  不越界、不遮主体、循环无缝（按用户测试三准则：UI 操作、读帧看实际内容）。

## 交付与协作边界
- **只推 `feat/motion-js-timeline`，不发起合并**；最终集成时本分支第二个合入
  （TTS 之后、其余之前），与 WT4 在 `local_execution.py` 的交叠由集成阶段解决
  （你先合，WT4 的单点挂钩后合，冲突可机械解决）。
- 完成后回填总方案 WT8 节实际差异。
