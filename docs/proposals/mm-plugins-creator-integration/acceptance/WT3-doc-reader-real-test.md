# 真实调用测试项目 WT3 · 多格式文档读取（隔离栈 8093）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT3 · 多格式文档读取并融入素材理解（开发派发单 `dispatch/WT3-doc-reader.md`） |
| 分支 / worktree | `feat/creator-doc-reader` · `/Users/linxuanrui/.qoder/worktree/QwenPaw/JqFVdX`（harness 托管 worktree；`dev-isolated.sh` 在其根目录，不入库） |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/document_reader.py`、`backend/vendor/mm_plugins/visualize/`、specialist 工具 `read_document` |
| 测试实例 | 浏览器 `http://127.0.0.1:8093/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 系统依赖 | 本机需安装 libreoffice（PPTX/DOCX 中转）；`brew install --cask libreoffice` |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-docreader`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-docreader/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；需 VLM 已配置（A7/B3 用） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && CREATOR_DOC_FIXTURES=<素材目录> pytest -m manual_real tests/manual/test_real_document_reader.py -v`（页图落在 `tests/manual/.manual-doc-reader/`，逐张打开人工核对） |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT3 节 |

> 渲染本身零 API 费用（本地 pypdfium2/libreoffice）；真实模型调用发生在
> **VLM 读页图**环节（`creator_vlm_model`），全套 <20 次 VLM 调用。

## 全局测试准则（每个 case 强制）
质量验证看实际内容（页图要人工打开看）；UI 层只经前端操作；发现 bug 才下钻。

## 测试素材准备（一次性，入 fixtures 或本地目录）
- 一份 ≥6 页中文短剧剧本 PDF（含标题/场次/对白结构）；
- 一份 ≥5 页分镜 PPTX（每页一张分镜图 + 文字说明）；
- 一份 XLSX（两个 sheet：镜头表 + 预算表）；一份 CSV；
- 一份 SRT 字幕；一个 .py 代码文件；一张 SVG（次批交付时）。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_document_reader.py`（`@pytest.mark.manual_real`）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | PDF 渲染 | 剧本 PDF 全页 | page_count 正确；**逐张打开页图**确认清晰可读、无空白页；尺寸 32px 对齐 |
| A2 | pages 范围 | pages="2-3" | 只渲染 2 张；页序正确 |
| A3 | budget 档位 | 同一页 small/normal/large 三档 | 分辨率递增且都在预算内；small 档文字仍可辨认 |
| A4 | PPTX 中转 | 分镜 PPTX | libreoffice 转换成功，页图与原 PPT 视觉一致（人工对照打开原文件） |
| A5 | XLSX/CSV | 两 sheet XLSX | 表格渲染含表头与数据，多 sheet 都覆盖 |
| A6 | 文本类 | SRT + .py | 文本直读，text_excerpt 与原文一致 |
| A7 | VLM 真读 | A1 页图喂 `creator_vlm_model`："第 2 页的场次和地点是什么" | VLM 回答与 PDF 实际内容一致（人工对照原 PDF） |
| A8 | 边界拒绝 | 项目资产边界外路径 | 拒绝且错误信息可读 |
| A9 | 依赖缺失 | 临时抹掉 PATH 中 libreoffice 后读 PPTX | 可读降级错误含安装提示；PDF 仍可用 |

## B. 前端真实使用测试（UI）
1. 新建项目 → 素材导入处**上传 PDF 剧本**。
2. 观察 Source Intelligence：文档型资产出现文档 index（页级条目 + 文本）。
3. 会话中要求：「阅读这份剧本，总结它的三幕结构和主要角色」。
4. 再上传分镜 PPTX，要求：「对照分镜稿第 3 页，描述那个镜头的构图」。

## C. UI Case 清单
| # | 操作 | 期望 | 验证方法 |
|---|---|---|---|
| B1 | PDF 导入 | 资产库出现 document 资产，含页数元数据 | 目视 |
| B2 | 文档 index | Source Intelligence 面板可浏览页级条目 | 打开若干页图人工核对与原 PDF 一致 |
| B3 | 剧本问答 | Agent 总结与 PDF 实际内容一致（幕结构/角色名不张冠李戴） | 人工通读原 PDF 对照 |
| B4 | 分镜问答 | 对第 3 页构图的描述与 PPT 该页实际画面一致 | 打开原 PPT 第 3 页对照 |
| B5 | 剧本进入创作流 | 以该 PDF 为源走「剧本 → 资产 → 分镜」一步（资产生成可只出 1 张角色图控成本） | 角色图与剧本角色设定相符（**读图**） |
| B6 | 不支持格式 | 上传 .glb 之类 | 可读提示不支持，不影响项目 |

## 通过标准
A1–A9、B1–B6 全过；libreoffice 版式还原度与 VLM 读图正确率的观察结论回填总方案
WT3 节。
