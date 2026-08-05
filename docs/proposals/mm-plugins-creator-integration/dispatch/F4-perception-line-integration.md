# 集成任务书 F4 · 素材理解功能线先行合并与验收（WT1+WT3+WT6）

> 本任务是 `dispatch/WT9-final-integration.md`（v3 功能线方案）中 **F4 线的先行
> 执行**——经决策，执行顺序调整为 F4 第一条（原 F1 热身角色由线内最小的 WT1
> 承担）。因是首条执行线，**阶段 0 基线就绪并入本任务**。
> 交付定义：三个分支合入集成分支 + 素材理解功能整体真实验收通过，成为一个可
> 独立交付、可独立归因的用户能力（导入文档/长视频 → 转写、阅读、记忆检索、
> 选段剪辑）。

## 工作信息（无背景也能执行）

| 项 | 值 |
|---|---|
| 主仓 | `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw` |
| 集成分支 | `integration/mm-plugins`（本任务创建，自 origin/dev/creator 拉出） |
| 待合分支（顺序固定） | ① `feat/creator-asr-qwen3`（+10）② `feat/creator-doc-reader`（+16）③ `feat/creator-source-memory`（+5） |
| 集成隔离栈 | `QWENPAW_WORKING_DIR=~/.qwenpaw-integ`、空闲端口（建议 8099），浏览器 `http://127.0.0.1:8099/` → Apps → QwenPaw Creator |
| 凭据 | 自 `~/.qwenpaw-poc/creator-runtime/config/model_config.json` 复制；解密设 `QWENPAW_KEYRING_ACCOUNT` |
| 系统依赖 | ffmpeg（已有）、**libreoffice**（WT3 PPTX/DOCX 验收需要，`brew install --cask libreoffice`） |
| 验收依据 | `acceptance/WT1-asr-real-test.md`（B 组）、`acceptance/WT3-doc-reader-real-test.md`（全套）、`acceptance/WT6-source-memory-real-test.md`（全套） |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §2.3/§三 WT1/WT3/WT6 节及其回填 |

## 全局硬约束
1. **dev/creator 全程不动**；只在 integration/mm-plugins 上操作。
2. 真实模型验证仅限阿里系百炼（qwen3-asr / VLM / qwen3-vl-embedding）；每次计费
   调用（尤其 WT6 构建）事先确认费用。
3. UI 验收三准则：读实际内容（读帧/读图/听音）、不跳步、只经前端。
4. 每个 merge `--no-ff` + tag；快门禁失败 30 分钟定位不了 → `git revert -m 1`
   退回分支负责人，记录失败日志。

## 步骤 0 · 基线就绪（首条线专属，约半天）
1. 处理本地 dev/creator 孤立 commit `ac073503 "update plugin.json"`：查看内容
   （`git show ac073503`），确认应保留则推 origin、属误提交则 drop——**向用户
   确认后执行**；然后本地与 origin/dev/creator 完全同步（含 #64/#68/#69/#73）。
2. **基线门禁**：干净基线上 pre-commit 全量 + 后端双 pytest + 前端测试全绿；
   起隔离栈做一次 UI 冒烟（建项目 → 生成一张资产图，读图确认）。记录基线快照
   （测试通过数、冒烟截图）——之后任何失败先对照基线。
3. `git checkout -b integration/mm-plugins origin/dev/creator` 并推 origin；
   集成隔离栈就绪（dev-isolated.sh 入 `.git/info/exclude`）。

## 步骤 1 · 合并 WT1（qwen3-asr，最小增量，兼作机制热身）
1. `git merge --no-ff feat/creator-asr-qwen3` → tag `integ/wt1-merged`。
   预期冲突：≈零（asr_model.py 独占域）。
2. 快门禁：`cd plugins/apps/qwenpaw-creator/backend && pytest tests/ -k "asr" -v`
   + api-contract 前端测试；全绿进下一步。

## 步骤 2 · 合并 WT3（文档读取）
1. `git merge --no-ff feat/creator-doc-reader` → tag `integ/wt3-merged`。
   预期冲突：`specialist_tools.py`（追加式，机械合并）；总方案文档回填章节
   （取并集）。
2. 快门禁：backend pytest -k "document or coverage or doc_reader" + fail-closed
   边界测试 + api-contract；全绿进下一步。

## 步骤 3 · 合并 WT6（长素材记忆，线内最大件）
1. `git merge --no-ff feat/creator-source-memory` → tag `integ/wt6-merged`。
   预期冲突（总方案 §2.3 预案）：
   - `source_intelligence.py` / `schemas/assets.py`：WT3 文档入口（已在）与 WT6
     重改叠加，**以 WT6 为主体、保留 WT3 文档分支逻辑**；
   - `backend/vendor/` NOTICE.md：条目取并集；若 WT6 自带了与 WT3 同源的
     vendored 文件，逐字比对后任选其一；
   - `specialist_tools.py` 追加；prompts 占位符靠白名单校验兜底（mismatch 即
     测试失败，按测试修齐）。
2. 快门禁：backend pytest -k "source_memory or memory" 全部 + WT3 域测试回归
   （确认文档入口未被破坏）+ 全量后端 pytest 一次（三分支已齐，此处升级为全量）。

## 步骤 4 · F4 功能级真实验收（本任务重心）
前置配置（UI）：ASR model=`qwen3-asr-flash`；`creator_embedding_model` 区块配置
（model 默认 qwen3-vl-embedding，Key 可复用 VLM）；VLM 已配置。

按序执行三份验收文件的指定部分，全部在集成隔离栈、全程 UI 操作：
1. **acceptance/WT1 · B 组（B1–B5）**：短/长素材转写、时间定位、错误呈现——
   播放原声对照抽查。（成本：<30min 转写额度）
2. **acceptance/WT3 · 全套（A1–A9 + B1–B6）**：PDF/PPTX/XLSX/SRT 渲染与 32px
   对齐、VLM 读页问答对照原文、文档 index 入素材理解、越权拒绝、libreoffice
   缺失降级。（成本：<20 次 VLM 调用）
3. **acceptance/WT6 · 全套（A1–A8 + B1–B6）**：embedding 客户端、25min 缩样
   校准构建 → 两个指定素材全量构建（**每次构建授权前口头确认费用预估**）、
   9 类查询、台词/语义检索**回原片读帧核对**、检索→剪辑 15s 高光出片、
   构建期不阻塞、checksum 失效与 <20min 不触发。（成本：本任务最大项）
4. **功能线串联验收（F4 专属新增）**：同一个「素材剪辑」项目内串联三能力——
   导入 KPL 长视频 + 一份 PDF 剧本参考 → 转写（WT1）+ 文档阅读（WT3）+ 记忆
   检索（WT6）→ 会话指令「参考这份剧本的叙事结构，把素材里 AG 的关键团战剪成
   30s 高光」→ 产出片段播放核对：选段确为团战（读帧）、结构呼应 PDF（对照
   原文）。这是"功能维度合并"的最终证明。

## 步骤 5 · 收尾
1. 集成分支推 origin（注意 pre-push 密钥扫描）；三个 tag 在位。
2. 回填总方案 §2.3：三个 merge commit 号、冲突解决要点、快门禁与验收结果、
   实际费用（转写分钟数 / VLM 次数 / 两素材构建费用与耗时、macro 数）；
   同时把 WT6 分支文档副本里的交付回填收拢进主文档，补齐 WT1 缺失的回填
   （Step 1 实测结论、oss:// 可解析性、静音切块偏差说明）。
3. **不清理分支与 worktree**（F1/F2/F3/F5 仍待执行）；不合回 dev/creator。

## 失败处置
- 快门禁失败：revert 该 merge → 问题带日志退回对应 WT 负责人 → 修复后重新入队；
  线内后续分支若依赖被 revert 的分支（WT6 依赖 WT1/WT3）则一并暂停，等修复。
- 验收失败：定位属于哪个 WT 的能力（三份验收文件是分界）；单能力问题在集成
  分支上以 fix commit 修复（cherry-pick 回其特性分支保持同步），串联验收失败
  优先怀疑 WT6 与 WT3 在 source_intelligence 的合并结果。

## 完成标准
- 三分支合入 integration/mm-plugins，tag 齐备，快门禁与全量后端测试绿；
- 三份验收 + 串联验收全过，证据（截图/读帧记录）留档；
- 总方案回填完成（含费用记录）；WT1/WT3/WT6 节升级 ✅；
- 汇报后由用户决定下一条线（建议 F2 内容生成）。
