# 真实调用测试项目 WT7 · 外置 Skill + edu-agent（隔离栈 8097）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT7 · 外置 Skill 接入机制 + edu-agent（开发派发单 `dispatch/WT7-external-skills.md`） |
| 分支 / worktree | `feat/creator-external-skills` · `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-external-skills` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/external_skills.py`、工具 `run_skill_script`；**配置面纯后端，无任何前端入口** |
| skill 配置文件 | `~/.qwenpaw-skills/creator-runtime/config/skills_config.json`（手动编辑，改后重启生效） |
| edu-agent 源目录 | `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/edu-agent/skill`（配置条目的 path 填此绝对路径） |
| 测试实例 | 浏览器 `http://127.0.0.1:8097/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 系统依赖 | Node ≥18（`node -v` 自查）、ffmpeg、可联网（npx hyperframes 首次拉取） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-skills`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-skills/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；宿主环境另需 `DASHSCOPE_API_KEY`（edu-agent TTS 用，经 env 白名单传入） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_external_skills.py -v` |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT7 节 |

> 真实模型调用：主 Agent LLM（既有）+ edu-agent 内部的 DashScope TTS（经 env 白名单
> 传入 `DASHSCOPE_API_KEY`）。edu-agent 单条讲解视频含多次 TTS 调用 + hyperframes
> 渲染（本地），费用低但耗时长（10–30min/条），预留时间窗。
> 环境前置：Node ≥18、ffmpeg、可联网（npx hyperframes 首次拉取）。

## 全局测试准则（每个 case 强制）
产出视频必须**读帧**核对（公式渲染、文字不重叠、图形语义正确）；UI 只经前端；
发现 bug 才下钻。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_external_skills.py`（`@pytest.mark.manual_real`）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | skill 加载 | skills_config.json 配置 edu-agent（真实上游路径） | status=available；SKILL.md 注入内容含触发时机摘要且 ≤ 上限 |
| A2 | 依赖探测真跑 | 真机探测 node/ffmpeg/env | 全过；随后临时抹掉 PATH 中 node → unavailable + 原因含安装提示 |
| A3 | run_skill_script 真执行 | 执行 edu-agent `scripts/precheck.py`（对一个最小 dist 样例） | 子进程在 skills-runtime 沙箱 cwd 运行；stdout 正常回传；env 白名单外变量不可见（脚本内 `env` 采样断言） |
| A4 | 越界与超时 | script 传 `../../etc/passwd`；一个 sleep 超时脚本 | 均拒绝/终止且信息可读 |
| A5 | 坏 skill 隔离 | 追加一个指向不存在路径的 skill 条目 | load_skills 不抛异常；会话建立成功；其余 skill 不受影响 |

## B. 前端真实使用测试（UI）
1. 后端编辑 `skills_config.json` 配置 edu-agent 并重启隔离栈（这是唯一允许的
   非 UI 操作——本 WT 配置面即后端文件，属功能本身）。
2. 打开 Creator 会话，提问：
   「请用 edu-agent 技能制作一条讲解视频：直角三角形 ABC 中，∠C=90°，AC=3，
   BC=4，求 AB 并讲解勾股定理的思路」。
3. 过程观察：Agent 按 SKILL.md 流程推进（问题分析 → 讲稿 → TTS → 组件搭建 →
   渲染 → 自检）；`run_skill_script` 触发执行授权弹窗。
4. 产出讲解视频 → 逐段读帧验收。

## C. UI Case 清单
| # | 期望 | 验证方法 |
|---|---|---|
| B1 | Agent 识别该请求应使用 edu-agent skill（上下文注入生效） | 会话内可见其引用 skill 流程 |
| B2 | 执行授权在脚本执行前弹出、可拒绝 | 先拒绝一次确认不执行 |
| B3 | 讲解视频数学内容正确（AB=5、勾股定理表述无误） | **读帧** + 听配音核对 |
| B4 | 视觉质量：公式渲染完整（无 tofu）、文字不压框、图形（直角三角形/直角标记）语义正确、配音与画面同步 | 抽 ≥5 帧逐一目检 |
| B5 | 产物入库 | 视频作为资产出现在项目资产库，可预览下载 |
| B6 | 坏 skill 回归 | 配置损坏 skill 后，跑一遍最小创作链路（建项目→生成一张资产图） | 全链路零影响 |
| B7 | disable 生效 | enabled=false 重启后，同 B2 请求 Agent 不再引用该 skill | 会话观察 |

## 通过标准
A1–A5、B1–B7 全过；edu-agent 单条视频实际耗时、TTS 次数、渲染稳定性观察回填总方案
WT7 节。
