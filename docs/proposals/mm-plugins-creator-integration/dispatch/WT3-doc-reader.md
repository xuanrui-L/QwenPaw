# 任务派发 WT3 · 多格式文档读取并融入素材理解（`feat/creator-doc-reader`）

## 你的任务
把 mm-plugins visualize 的「万物皆截图」能力以 Creator 原生工具落地：新增
`read_document` 专家工具（PDF/Office/表格/字幕等多格式），并让文档型素材进入
Source Intelligence 编目体系。本 WT 同时定稿全项目 vendoring 样板。

## 必读引用
- 总方案（唯一实现依据，精读 §三 WT3 节 + §1.2 事实 6/8 + §2.2/§2.5）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
- 本仓落点：`plugins/apps/qwenpaw-creator/backend/services/specialist_tools.py`、
  `backend/services/source_analysis/service.py`、
  `backend/services/media/source_intelligence.py`、`backend/schemas/assets.py`、
  `plugin.json`、`ui/src/contracts/creator/`。
- 上游移植来源（Apache-2.0，本地路径）：
  `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins/src/capabilities/core/qwen_mm_plugins_core/renderers/`
  （pdf.py / office.py / data.py / subtitle.py / code.py / svg.py / notebook.py /
  web.py）与 `src/shared/image.py`（budget_to_pixels / smart_resize）。

## 全局硬约束（引自总方案 §2.2 / §2.5，违反即返工）
1. 不引入 qwen-mm-plugins 运行时依赖；不做 env 注入；不做进程直调。手法 B：
   **算法移植（vendoring）**——移植文件头保留上游版权声明并标注修改（Apache-2.0
   §4b）；新建 `backend/vendor/NOTICE.md` 声明来源仓库、commit `077aea6`、许可证与
   模块清单；移植代码集中放 `backend/vendor/mm_plugins/`（已定稿，作全项目样板）。
2. data model 先行：schema + 前端 contract + api-contract 测试同步。
3. pre-commit + 双 pytest 全绿；注释英文。
4. 人工验收走前端 UI，肉眼核对渲染页图内容。

## Worktree 准备
```
cd /Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw
git worktree add .worktrees/creator-doc-reader -b feat/creator-doc-reader dev/creator
```
基线为**当前 dev/creator**（无前置合并）。隔离栈：`dev-isolated.sh`（入 `.git/info/exclude`）、
`QWENPAW_WORKING_DIR=~/.qwenpaw-docreader`、端口 **8093**；凭据复制自主实例
`~/.qwenpaw-poc/creator-runtime/config/model_config.json`，解密需设
`QWENPAW_KEYRING_ACCOUNT`。

## 实现规格（引自总方案 §三 WT3，已定稿）
> **格式范围**：首批必交付 pdf（pypdfium2）、pptx/ppt/docx/xlsx 版式（libreoffice
> 中转 PDF）、csv/xlsx 数据表（pandas+matplotlib+openpyxl+tabulate）、srt/vtt/ass
> 字幕与纯文本/代码（零依赖）；次批视进度 svg（resvg-py）、ipynb（nbformat）；
> web/html（playwright）默认不装、配置化开启；不引入 latex/model3d/geo/drawio。
> 渲染器懒加载移植，装多少支持多少。
>
> **改动**：
> 1. `services/document_reader.py`（新）：
>    `async read_document(file_path, *, pages: str|None, budget:
>    Literal["small","normal","large"]) -> DocumentReadResult{format, page_count,
>    pages_rendered, page_images: list[Path], text_excerpt, notes}`；扩展名分派
>    vendored 渲染器；缺依赖给可读错误与安装方式。
> 2. **图像入上下文适配（与上游最大差异）**：渲染页图落盘为 runtime 文件
>    （doc-pages/ 目录）并返回 fileRef，由 file agent runtime 以既有多模态消息
>    机制注入 VLM 上下文；**不在工具返回体内放 base64**。
> 3. data model：schemas/assets.py 增 DocumentMetadata{format, pageCount}，
>    IndexedFile.kind 增/复用 document；前端 contract 同步。
> 4. specialist_tools.py 追加
>    `ToolSpec(name="read_document", roles={SOURCE_INTELLIGENCE}（已定稿，首版
>    不给主 Agent）, requires_execution_authorization=False, wait=NONE,
>    parameters={fileRef: required, pages?: str, budget?: enum})`；fileRef 限项目
>    资产边界（复用现有校验）。
> 5. 融入素材理解：`source_analysis/service.py` 识别文档型 source →
>    document_reader 渲染 → 产出文档版 index（media.mediaKind="document"、每页
>    一个 shot、keyframe.ref 指向页图、全文进 semantic_entries）；
>    `source_intelligence.py` 只加文档入口分支（轻改动，与 WT6 错开）。
> 6. 依赖：plugin.json dependencies 增 pypdfium2/pandas/matplotlib/openpyxl/
>    tabulate；runtimeDependencies 增 libreoffice（optional，同 jq 机制）。

## 测试与验收
- 每格式一个 fixture：页数 / blocks / 首图 32px 对齐 / 文本层断言；越权 fileRef
  拒绝；libreoffice 缺失降级；文档导入 → 产出文档 index 集成测试。
- 人工验收：UI 上传 PDF 剧本 → Agent 读文档复述结构，肉眼核对页图内容。

## 交付与协作边界
- **只推自己的特性分支，不发起合并、不 rebase 其他分支**；统一合并在最终集成
  阶段进行（见 dispatch/WT9，本分支先于 source-memory 合入）。
- 热点：specialist_tools.py 只追加 ToolSpec；source_intelligence.py 只加文档入口
  分支（WT6 会重改此文件，保持改动最小）。
- 你是 vendor 目录样板（`backend/vendor/mm_plugins/` + NOTICE.md）的定稿者：
  WT4/WT6 在各自分支上若未见到你的代码，会自行同规范移植同源文件（逐字相同），
  集成时归一——因此目录布局与文件头规范定了就不要再变，定稿后立即回写总方案 §2.2。
- 完成后回填总方案 WT3 节实际差异。
