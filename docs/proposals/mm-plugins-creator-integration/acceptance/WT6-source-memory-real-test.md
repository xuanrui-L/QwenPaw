# 真实调用测试项目 WT6 · 长素材记忆（隔离栈 8096）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT6 · video-memory 移植并入 Source Intelligence（开发派发单 `dispatch/WT6-source-memory.md`） |
| 分支 / worktree | `feat/creator-source-memory` · `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-source-memory` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/media/source_memory.py`、`backend/vendor/mm_plugins/video_memory/`、`backend/models/embedding_model.py`、专家工具 `query_source_memory` |
| 测试实例 | 浏览器 `http://127.0.0.1:8096/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-memory`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-memory/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；需 VLM + ASR + **Embedding（UI 新增 creator_embedding_model 区块）**已配置 |
| 产物位置 | `runtime/source-intelligence/<index-id>/memory/{graph_memory.json, embeddings.npz}`（在数据根的项目目录下，A2 核对用） |
| ❗ 费用 | 构建是本套最贵操作：先缩样（A2）再全量；每次构建经授权确认费用预估 |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_source_memory.py -v` |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT6 节 |

> 真实模型调用：构建期 P2 VLM（次数≈macro 数）+ 全节点 embedding + ASR 全片转写；
> 查询期单条 embedding。**构建是本套最贵操作**——先用「短素材缩样」校准管线，再对
> 两个指定长素材各构建一次，费用预估经执行授权逐条确认。

## 全局测试准则（每个 case 强制）
定位结果必须**回原片读帧核对**（不以工具返回文本为准）；UI 只经前端；发现 bug 才下钻。

## 指定测试素材
1. 猫视角法国之旅（自然场景、少台词——验视觉图谱与场景切割）：
   `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/CAT%20with%20CAMERA%20Explores%20FRANCE%20%F0%9F%87%AB%F0%9F%87%B7%20%20(%20Calming%20CAT%20POV%20).mp4`
2. KPL 2026 夏季赛 成都AG超玩会 vs 重庆狼队 Game 5（解说密集、屏幕文字多——验
   ASR 台词检索 + OCR + 时间定位）：
   `https://creator-store2.oss-cn-beijing.aliyuncs.com/upload_videos/%E3%80%90KPL%20Summer%202026%E3%80%91%E6%88%90%E9%83%BDAG%E8%B6%85%E7%8E%A9%E4%BC%9A%20vs%20%E9%87%8D%E5%BA%86%E7%8B%BC%E9%98%9F%20%EF%BD%9C%20Game%205%20%EF%BD%9C%20Stage%201%20-%20Jul%2003%20%EF%BD%9C%20%23honorofkings%20%23hokchannel.mp4`

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_source_memory.py`（`@pytest.mark.manual_real`）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | embedding 薄客户端 | 3 条文本（含 1 条长文）真实 embed | 返回向量维度一致；batch 切分正确；两次同文本余弦≈1 |
| A2 | 缩样构建校准 | 从素材 2 截 25min 片段 | 构建完成产出 graph_memory.json + embeddings.npz；macro 数与片长量级合理（~5–8 个）；**打开 graph JSON 抽 2 个 macro 的描述文本，回原片对应时间窗读帧核对描述属实** |
| A3 | ASR 轨入图 | A2 产物 | 图中含 ASR 节点，抽 3 条台词回原片听核对 |
| A4 | 全量构建 ×2 | 素材 1、2 完整构建（费用预估先行确认） | 完成无中断；耗时与费用记录 |
| A5 | 查询全类型 | 对素材 2 产物跑 9 种 query_type 各一次 | 每种返回结构合法；search_asr("五杀"或实际出现的解说词) 命中时间窗回原片读帧核对确为该事件 |
| A6 | 投影 | 构建后 index | summary/semantic_entries 出现 producer=source_memory 草稿，内容与全片主题相符 |
| A7 | 失效 | 替换素材文件（改 checksum） | memory_ref 失效，查询给出需重建的可读提示 |
| A8 | 阈值 | <20min 素材 | 不触发构建 |

## B. 前端真实使用测试（UI）
1. 新建「素材剪辑」项目 → UI 导入素材 2（KPL）→ Source Intelligence 完成后出现
   memory 构建授权（费用预估显示）→ 确认 → 等待构建完成 → 资产出现「记忆已构建」徽标。
2. 会话检索：「找到 AG 超玩会拿下关键团战的片段」→ Agent 用 query_source_memory
   定位 → **回原片读帧核对**时间窗内确为团战。
3. 追问下钻：「这段团战里双方用了哪些英雄」→ Agent 经 subgraph/OCR 检索回答 →
   读帧核对英雄名与画面一致。
4. 导入素材 1（猫）重复构建 → 检索「猫走进咖啡馆/室内的场景」（按实际内容调整）→
   读帧核对场景转换定位。
5. 剪辑落地：让 Agent 基于检索结果把该团战剪成 15s 高光片段 → 预览确认片段正确。

## C. UI Case 清单
| # | 期望 | 验证方法 |
|---|---|---|
| B1 | 构建授权与费用预估正确展示、可拒绝 | 目视（先拒绝一次验证不扣费不构建） |
| B2 | 台词检索定位准确（素材 2） | 回原片读帧+听音核对 |
| B3 | OCR/语义下钻回答与画面一致 | 读帧核对 |
| B4 | 语义检索定位场景转换（素材 1，无台词依赖） | 读帧核对 |
| B5 | 检索→剪辑端到端出片 | 播放高光片段确认内容 |
| B6 | 构建期间不阻塞常规 index 与其他项目操作 | 构建中并行浏览/编辑 |

## 通过标准
A1–A8、B1–B6 全过；两个素材的构建耗时/费用、macro 数、检索命中质量记录回填总方案
WT6 节。
