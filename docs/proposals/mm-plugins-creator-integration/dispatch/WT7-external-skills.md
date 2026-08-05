# 任务派发 WT7 · 外置 Skill 接入机制 + edu-agent（`feat/creator-external-skills`）

## 你的任务
为 Creator 实现**后端手动配置外置 skill** 的通用机制（skill 方式，非 agent 方式），
并以 edu-agent 为首个接入用例验证「一道题 → 讲解视频」链路。核心要求：① 纯后端
配置，前端零改动；② 不新增任何 subagent 角色；③ Creator 本身不受影响（坏 skill
不得破坏既有链路）；④ 能被成功调用出正确结果。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT7 节 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/services/file_agent_runtime/prompts/__init__.py`
  （占位符白名单机制，`render_creator_system_prompt()` 的 tts_guidance 条件注入
  为样板）、`backend/models/config.py`（`_get_user_config()` 读 model_config.json
  的模式）、`backend/services/specialist_tools.py`、`backend/schemas/`。
- 上游 skill 用例（只读，Apache-2.0）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/edu-agent/skill/`
  （SKILL.md + design-system.md + assets/ 83 个 K12 组件 + references/ + scripts/；
  运行时依赖：Node ≥18、`npx hyperframes`、headless Chromium、ffmpeg、
  python3 + dashscope/soundfile/numpy/requests、`DASHSCOPE_API_KEY`）。

## 全局硬约束（引自总方案 §2.2 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 Creator 进程内 env 注入（skill 子进程
   的 env 白名单传递是受控参数传递，允许）；不做进程直调。
2. **纯后端配置**：无任何前端 UI（含只读列表）、无 plugin.json config block、无
   前端 contract 变更。
3. **skill 方式非 agent 方式**：不新增 subagent 角色；SKILL.md 注入主 Agent 系统
   上下文；脚本由主 Agent 经沙箱工具执行。
4. 隔离铁律：任何 skill 解析失败/路径不存在/依赖缺失 → 标记 unavailable + 可读
   原因，**不抛异常、不影响会话建立与既有全链路**。
5. pre-commit + 双 pytest 全绿；注释英文；验收走前端 UI 会话、查看实际视频内容。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-external-skills -b feat/creator-external-skills dev/creator
```
基线为**当前 dev/creator**（无前置合并）。隔离栈：`dev-isolated.sh`（入
`.git/info/exclude`）、`QWENPAW_WORKING_DIR=~/.qwenpaw-skills`、端口 **8097**、
凭据复制自主实例 `~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 实现规格（引自总方案 §三 WT7，已定稿）
> 1. **配置面**：`<CREATOR_DATA_ROOT>/config/skills_config.json`（与
>    model_config.json 同级、同原子写规范；仅手动编辑文件）。schema
>    （`schemas/skills.py` 新，仅后端内部使用）：
>    `SkillEntry{name, path, enabled, description?, env?: list[str]（传给脚本
>    子进程的 env 变量名，值从宿主环境取）, requirements?:
>    list[SkillRequirement{kind: binary|node_min|env, value}]}`；
>    config.py 增 `load_skills_config()`（带缓存+失效，不加密——密钥类走 env
>    声明间接引用）。
> 2. **加载与隔离**：`services/external_skills.py`（新）：
>    `load_skills() -> list[LoadedSkill{entry, status: available|unavailable,
>    reason, skill_md, root}]`——解析 SKILL.md、逐项探测 requirements
>    （shutil.which / `node --version` 比对 / env 存在性）；任何失败 →
>    unavailable + 原因。注入：`creator_agent.system` prompt spec 增占位符
>    `external_skills`，`render_creator_system_prompt()` 拼装可用 skill 区块
>    （name + 触发时机 + 调用方式摘要），token 预算上限
>    `SKILL_CONTEXT_MAX_CHARS=8000`，超限按序截断并 trace 警告；无可用 skill
>    注入空串（占位符校验兼容）。
> 3. **执行通道（工具给主 Agent，已定稿）**：
>    `ToolSpec(name="run_skill_script",
>    requires_execution_authorization=True（已定稿：要求授权）, long_running=True,
>    wait=NONE, parameters={skill, script（限 skill 根目录内相对路径）, args?,
>    timeout_seconds? ≤1800})`：subprocess 执行，
>    `cwd=<workspace>/skills-runtime/<name>/`（首次运行从 skill root 拷贝工作
>    副本）；子进程 env = 最小基础 env + entry.env 白名单；stdout/stderr 各截断
>    64KB；产物文件由 Agent 经现有资产导入通路入库。
> 4. **edu-agent 接入**：skills_config.json 配置
>    `{name:"edu-agent", path:"<上游本地路径>/src/capabilities/edu-agent/skill",
>    enabled:true, env:["DASHSCOPE_API_KEY"], requirements:[{binary:"ffmpeg"},
>    {node_min:"18"}, {env:"DASHSCOPE_API_KEY"}]}`；hyperframes 依赖由 skill
>    脚本内 npx 解决；若拷贝进用户目录须附 Apache-2.0 归属说明。

## 测试与验收
- 单测：SkillEntry schema；SKILL.md 解析；注入截断；坏 skill（路径不存在/解析
  失败/依赖缺失）unavailable 隔离且会话建立成功；run_skill_script 路径越界拒绝、
  超时、输出截断；env 白名单传递。
- 集成（重心，UI 操作）：a) 配置 edu-agent → 会话提一道数学题 → Agent 按 skill
  产出讲解视频，**查看实际视频内容**确认讲解正确、配音字幕正常；b) 配置故意
  损坏的 skill → Creator 全链路（建项目/生成/剪辑）回归零影响；c) disable 后
  上下文不再注入。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9）。
- 热点：specialist_tools.py 只追加；config.py 只追加 skill 读取；prompts/ 只增
  `external_skills` 占位符（占位符校验机制防漏改）。
- 完成后回填总方案 WT7 节实际差异。
