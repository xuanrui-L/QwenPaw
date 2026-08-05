# 真实调用测试项目 WT2 · Serper Grounding（隔离栈 8092）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT2 · Serper 并入 Web Grounding（开发派发单 `docs/proposals/mm-plugins-creator-integration/dispatch/WT2-grounding-serper.md`） |
| 分支 / worktree | `feat/creator-grounding-serper` · `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-grounding-serper` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/web_grounding/`（Serper search/images/lens/scrape、bbox、确认闭环）+ `services/media_transport.py`（OSS/Uguu 托管路由）+ ModelConfigModal grounding 区块 |
| 测试实例 | 浏览器 `http://127.0.0.1:8092/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-serper`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-serper/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；另需 `SERPER_API_KEY`（向负责人索取，在 UI Grounding 区块填入） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_serper_grounding.py -v` |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT2 节 |

> 真实 Key：`SERPER_API_KEY`（新增）、Tavily key（既有，用于顺位对照）。
> `creator_media_oss` 是 Lens 的优先托管通路但不再是必需配置：配置时走 OSS；未配置时
> 自动走 Uguu 免费临时图床。Serper 按查询计费，修订后的全套预计 <100 次查询。

## 全局测试准则（每个 case 强制）
质量验证看实际内容；UI 层只经前端操作；发现 bug 才下钻代码。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_serper_grounding.py`（`@pytest.mark.manual_real`）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | 文本搜索 | query="2026 KPL 夏季赛 成都AG超玩会"（中文）与英文 query 各一 | sources 非空；**逐条打开 top3 url** 确认可访问且与 query 相关（不是仅断言字段存在） |
| A2 | 图片搜索 | query="埃菲尔铁塔 黄昏" | 返回 image_url 可下载；**读图确认**内容为铁塔 |
| A3 | 顺位验证 | 同一 query 分三态跑：仅 Tavily key / 仅 Serper key / 双 key | providers_attempted 分别为 [tavily] / [serper] / [tavily]（tavily 有结果时不落到 serper） |
| A4 | Lens 反查（OSS 通路） | 本地素材图（如埃菲尔铁塔帧）→ OSS 15min 签名 URL → /lens | organic 非空且 top 结果与图内容相关（人工读结果 title）；签名 URL 过期后（等 16min 或缩短时效重签）再调返回失败——确认无长期暴露 |
| A5 | Lens 无 OSS 自动免费图床 | 临时清空全部 OSS 配置 + 本地图路径 | 自动 POST Uguu `files[]` 并取得临时 URL，随后 `/lens` 成功；trace 标记 `uguu`，全过程不要求用户补 OSS 配置 |
| A6 | Lens URL 直通 | 输入本身是公网图 URL | 不走 OSS/Uguu 上传直接反查成功 |
| A7 | OSS 路由优先且可观测降级 | 完整配置 OSS、只填部分 OSS 字段、制造 OSS 上传失败三态各跑一次 | 完整正常态只走 OSS；部分配置保留 `creator_oss_invalid` 后请求 Uguu；上传失败保留 `creator_oss_upload_failed` 后请求 Uguu；后两态 trace 的 transport 均为 `uguu` |
| A8 | bbox 局部反查 | 一张含多个不同地标/物体的图片，分别传两个 0–1000 bbox | 两次结果分别对应框内主体；非法/越界/空 bbox 返回可读 issue，裁剪失败不得静默搜索整图 |
| A9 | 多 query 文本搜索 | 一次提交至少两个中英文 query | 每个 query 都有独立结果归属；结果保留 title/snippet/date/url，编号与来源不串组 |
| A10 | 网页正文抽取 | 对 A1 最相关 URL 调 `/scrape`，goal 指向一个可核验事实 | 返回 markdown/text，保留 URL 与 goal，正文不超过每 URL 8000 字符且包含目标事实；失败 URL 不影响其他 URL |
| A11 | 身份/事实确认闭环 | 使用仅凭画面无法可靠确认的地标、人物或产品帧 | trace 可见 Lens 候选 → 候选 web search → 最佳 URL extractor；最终结论有来源支撑，证据冲突时明确不确定而非直接采信视觉猜测 |

## B. 前端真实使用测试（UI）
1. Model Configuration → Grounding 区块：填入 `serper_api_key`，保存。
2. 新建「创意生成」项目，创作简报写一个**需要外部事实**的主题（如
   「制作一支介绍 2026 KPL 夏季赛成都AG超玩会 vs 重庆狼队 Game 5 的 30 秒资讯短片」）。
3. Agent 规划过程中触发 web grounding → 查看 grounding 结果面板/上下文。

## C. UI Case 清单
| # | 操作 | 期望 | 验证方法 |
|---|---|---|---|
| B1 | 触发 grounding | 来源列表含 provider=serper 的条目（把 tavily key 临时置空以强制走 serper） | 查看来源 provider 标记 + 打开来源链接核实内容相关 |
| B2 | 视觉参考 | grounding 拉到的参考图与主题相关 | **读图**确认（比赛/战队相关而非无关图） |
| B3 | 事实进入产出 | 后续生成的剧本/文案包含检索到的真实事实（如比赛日期/队名） | 对照 A1 打开的来源页人工核对 |
| B4 | 无 Key 回退 | 清空 serper+tavily key 后再触发 | 走 DashScope 兜底或给出可读降级信息，不阻塞创作流程 |
| B5 | 无 OSS 本地图 Lens | 保持 OSS 为空，在 UI 添加本地参考图并触发具体身份识别 | 无需再配置 OSS；自动使用免费图床完成 Lens，UI/trace 可见 `uguu` 托管通路 |
| B6 | bbox 目标识别 | 对包含多个主体的参考图指定其中一个区域 | 返回的是选中区域主体的候选与确认结果，不被整图其他主体干扰 |
| B7 | 证据进入产出 | 用需要识别并核实的参考图触发创作 | 产出中的具体身份/事实可回溯到 web search/extractor 来源；没有足够证据时保留不确定表达 |

## D. 自动化可靠性门禁

- search/Lens 最多 10 次、Uguu upload 最多 5 次、scrape 最多 3 次，指数退避上限
  10 秒；用 fake clock/respx 验证次数，测试不得真实等待。
- timeout、transport error、429、5xx 会重试；400/401/403/404 等非 429 的 4xx
  立即失败。
- 每次尝试、最终托管通路与失败原因进入 providers_attempted/issues；不得把本地路径、
  OSS 签名 URL、Uguu 临时 URL 泄漏进面向模型的长期持久化来源字段。
- 公网 URL SSRF、本地 `CREATOR_DATA_ROOT` containment、防 symlink、真实光栅解码和
  8MB 上限回归必须继续通过。

## 通过标准
A1–A11、B1–B7 与 D 节门禁全过；必须同时完成“有 OSS 走 OSS”和“无 OSS 自动走
免费图床”两条真实 Lens 通路。Serper 实际召回质量、网页抽取质量、身份确认闭环与
两种托管通路结论回填总方案 WT2 节；缺任一 Qwen-MM Grounding 基线能力不得通过。
