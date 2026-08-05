# 任务派发 WT2 · Serper 并入 Web Grounding（`feat/creator-grounding-serper`）

## 你的任务
grounding 管线新增 Serper 为文本搜索与图片搜索 provider（第一 commit），并新增
Serper Lens 以图搜图能力（第二 commit）；继续补齐 Qwen-MM-Plugins 已有的完整
Grounding 能力基线（第三 commit 起），包括 bbox、网页抽取、候选确认闭环、重试与
OSS→免费图床自动路由。按 Key/OSS 配置自动选择 provider 与托管通路，零破坏。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT2 节 + §1.2 事实 7 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/services/web_grounding/providers/`
  （adapters.py / config.py / search.py / tavily.py 为同构样板）、
  `backend/models/config.py`（web_grounding getter 区）、`plugin.json`
  `creator_web_grounding` block、`ui/src/contracts/creator/models.ts`
  GroundingConfig、`ui/src/components/creator/ModelConfigModal.tsx` grounding 区块。
- 上游协议参考（只读）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/core/qwen_mm_plugins_core/apis/web_search.py`、
  `apis/image_search.py`、`apis/web_extractor.py`、`qwen_mm_plugins_core/serper.py`
  （post_serper 封装）以及 `src/capabilities/core/skill/references/video_search.md`。

## 全局硬约束（引自总方案 §2.2 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 env 注入；不做进程直调。手法 A：
   httpx adapter 与 `_search_tavily*` 同构。
2. data model 先行：GroundingConfig（Pydantic + TS contract）增字段须同步
   api-contract 测试。
3. **能力基线（§2.2）**：Qwen-MM-Plugins 已有 Grounding 能力 Creator 必须全部
   覆盖；不得遗漏 `web_search` 多 query、`image_search` 本地/URL + bbox、
   `web_extractor`、Lens 候选二次检索确认与重试降级。
4. **图片托管路由（§1.2-7）**：Serper Lens 只收 `{"url"}` 公网 URL，不支持
   base64，DashScope 临时存储的 `oss://` 也不能直接使用。本地或裁剪后图片：
   - `creator_media_oss` 完整可用 → 只走 OSS 私有上传 + 15min presigned URL；
   - 未配置 `creator_media_oss` → 自动 POST `https://uguu.se/upload`，multipart
     `files[]`，使用返回的免费临时 URL；
   - 已配置 OSS 但上传/签名失败 → 保留可读 issue，并继续切换 Uguu。
   OSS readiness 必须为三态：全部字段为空是 `absent`，完整有效是 `ready`，部分填写或
   无效是 `invalid`；`absent` 直接走 Uguu，`invalid` 保留配置 issue 后走 Uguu。Uguu 不新增用户
   配置项或密钥，响应读取 `files[0].url` 并校验为公网 HTTPS URL。托管通路记录为
   `direct_url / creator_oss / uguu`，临时 URL 不进入长期持久化来源。
   两条通路都必须先通过 `CREATOR_DATA_ROOT` 边界、防 symlink、光栅解码、bbox 与
   8MB 上限校验；公网输入继续执行 SSRF 校验。
5. pre-commit + 双 pytest 全绿；注释英文；真实 Key/真实上传调用进入人工验收。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-grounding-serper -b feat/creator-grounding-serper dev/creator
```
基线为**当前 dev/creator**（无前置合并）。隔离栈：`dev-isolated.sh`（入 `.git/info/exclude`）、
`QWENPAW_WORKING_DIR=~/.qwenpaw-serper`、端口 **8092**；凭据复制自主实例
`~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 实现规格（引自总方案 §三 WT2，已定稿）
> **现状**：文本链 `search.py::search_web()`：Tavily（有 key）→ 无结果且
> native_search_enabled 时 DashScope，返回 `{query, sources, issues, provider,
> providers, providers_attempted}`；视觉链 `search_visual_refs()` 按
> `visual_search_provider_order()`（默认 `("tavily","dashscope_web_search_image")`
> 过滤可用项）**以文搜图**；现状没有以图搜图路径。
>
> **Commit 1 · Serper 文本 + 图片搜索**：
> 1. `providers/adapters.py` 增 `_search_serper(client, query, limit)`（POST
>    `https://google.serper.dev/search`，header `X-API-KEY`，organic →
>    `{title,url,snippet,provider:"serper",query,score:None}`）与
>    `_search_serper_visuals(...)`（POST `/images`，映射视觉 source 结构）。
> 2. `providers/serper.py`（新）：URL 常量（对齐 tavily.py 分层）。
> 3. `providers/config.py`：`serper_api_key()`；
>    `DEFAULT_VISUAL_SEARCH_PROVIDERS = ("tavily","serper","dashscope_web_search_image")`；
>    文本链顺位同理 **Tavily → Serper → DashScope**（已定稿）。
> 4. `providers/search.py`：两条链插入 serper 尝试，providers_attempted 照记。
> 5. 配置贯通：config.py `get_web_grounding_serper_api_key()`（grounding config
>    `serper_api_key` → env `SERPER_API_KEY`/`WEB_GROUNDING_SERPER_API_KEY`）；
>    plugin.json 追加 password 字段；schemas + contracts + ModelConfigModal
>    grounding 区块加输入框（本 WT 唯一前端改动）。
>
> **Commit 2 · Serper Lens 以图搜图（已定稿本期交付）**：
> `_search_serper_lens(client, image_url)` POST `/lens`；支持公网 URL、本地图片与
> 可选 0–1000 归一化 bbox。本地/裁剪后图片有完整 OSS 配置时走
> creator_media_oss + 15min 签名 URL，没有 OSS 配置时自动走 Uguu 免费临时图床。
> 首版暴露给 grounding pipeline 的 visual_jobs 使用，不单独开上游 MCP 工具。
>
> **Commit 3 · Qwen-MM Grounding 能力补齐（修订后必交付）**：
> 1. 新增 Serper `/scrape` adapter，提供 Creator 原生 `web_extractor` 等价能力：
>    URL 列表 + goal、`includeMarkdown:true`、每 URL 最多 8000 字符。
> 2. 文本搜索支持多 query 并保留每条结果的 query/date/title/snippet/url。
> 3. 新增 Lens bbox 裁剪与托管路由：OSS ready → OSS；OSS absent → Uguu；
>    OSS invalid 或上传/签名失败 → 保留明确 issue 后 fallback 到 Uguu。
> 4. 对具体身份/事实形成 `Lens candidate → web search → extract best URL → confirm`
>    闭环；证据薄弱或冲突时只输出不确定结论和 issues。
> 5. 重试上限对齐上游：search/Lens 10、Uguu 5、scrape 3，指数退避 cap=10s；
>    只重试 transport/timeout/429/5xx，所有尝试进入 trace。

## 测试与验收
- respx 打桩 Serper `/search`、`/images`、`/lens`、`/scrape` 与 Uguu `/upload`；
  覆盖多 query、bbox、fallback 顺序、四种图片通路（公网、OSS、无 OSS→Uguu、
  OSS configured-but-failed）和重试分类。
- 增加候选实体必须经过 web search/extractor 确认的 pipeline 测试。
- 隔离栈用真实 Key 分别跑“有 OSS”和“无 OSS”两态，核对实际内容、托管 provider、
  来源列表、providers_attempted 与 issues。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9）。
- 热点：plugin.json 与 ModelConfigModal 只动 grounding 区块，只追加。
- 完成后回填总方案 WT2 节实际差异。
