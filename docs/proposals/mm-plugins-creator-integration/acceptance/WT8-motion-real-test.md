# 真实调用测试项目 WT8 · hyperframes 动效引擎（隔离栈 8098，既有分支）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT8 · hyperframes 动效引擎完善（开发派发单 `dispatch/WT8-motion-engine.md`） |
| 分支 / worktree | `feat/motion-js-timeline`（**既有分支**）· `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/motion-js-timeline` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/media_files/`（motion_engine.py / motion_design.py / local_execution.py） |
| 测试实例 | 浏览器 `http://127.0.0.1:8098/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 前置步骤 | GSAP vendor 不入库：首次需在 backend 目录执行 `python -m services.media_files.motion_engine fetch` 拉取并校验 vendor 库 |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-motion`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-motion/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；需 VLM 已配置（动效设计用） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_motion_engine.py -v`；既有专项测试 `pytest tests/media_files/test_motion_js_timeline.py` |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT8 节 |

> 真实模型调用：`design_motion_overlays` 的 VLM 动效设计（`creator_vlm_model`，
> 看帧 + 产 html_js 代码）；渲染本身本地（Playwright 截屏 + ffmpeg），零 API 费用。
> 全套约 10–15 次 VLM 调用。

## 全局测试准则（每个 case 强制）
动效质量必须**抽帧读实际画面**（不越界、不遮主体、循环无缝）；UI 只经前端；
发现 bug 才下钻。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_motion_engine.py`（`@pytest.mark.manual_real`）。

| # | case | 输入 | 断言 |
|---|---|---|---|
| A1 | VLM 真实设计 | 一段有留白的素材片段帧序列 + 剪辑意图 | 产出合法 html_js（vendor 白名单校验过、`__hf` 协议完整）或 needed=false；产出后真实渲染成功 |
| A2 | 克制判断 | 一段画面很满/安静唯美的片段 | VLM 返回 needed=false（不硬凑动效） |
| A3 | seek 确定性 | 同一 html_js 对 t=1.0s 渲染两次 | 两次帧像素一致（哈希相同，确定性 prelude 生效） |
| A4 | 渲染真值自查·越界样本 | 手写一个把元素移出视口的 html_js | 自查检出越界、拒绝入库并给出重生反馈 |
| A5 | 渲染真值自查·空帧样本 | 首帧无可见内容的 html_js | 检出空帧拒绝 |
| A6 | loop 无缝 | A1 产物 loop=true | 首帧与周期末帧像素差异极小（读图对比）；缓存键含 loop 盐 |
| A7 | 帧缓存 | A1 同参数二次渲染 | 命中缓存（耗时显著下降、trace 可证） |
| A8 | vendor 守护 | 篡改本地 gsap.min.js 一个字节 | 哈希校验失败拒绝渲染，错误可读 |

## B. 前端真实使用测试（UI）
1. 既有 worktree 隔离栈启动，新建「素材剪辑」项目导入一段 30s 素材。
2. 时间轴选中一个片段 → 添加到对话 →「给这段加一个高级感的开场描边动效」。
3. Agent 走 design_motion_overlays（VLM 真实设计）→ 渲染 → 预览。
4. 预览中拖动 seek 往返多次 → 动效表现与位置稳定一致。
5. compose 成片 → 抽帧验收。

## C. UI Case 清单
| # | 期望 | 验证方法 |
|---|---|---|
| B1 | 动效风格现代、从画面取色、位于留白区 | 预览 + 抽帧目检 |
| B2 | 全程不越界（含描边/阴影/粒子）、不遮主体、无可见文字 | compose 后抽首/中/尾帧逐一目检 |
| B3 | seek 往返画面一致（无跳变/漂移） | 预览操作目视 |
| B4 | 对不适合加动效的片段请求 → Agent 解释并拒绝硬加 | 会话观察 |
| B5 | 循环动效在片段时长内无缝重复 | 播放目视 + 抽循环边界两帧对比 |
| B6 | 被真值自查拒绝的动效自动重生成且第二版合格 | 观察重生回合（可用 A4 方式人为诱发） |

## 通过标准
A1–A8、B1–B6 全过；VLM 动效设计一次通过率、真值自查拦截率观察回填总方案 WT8 节。

---

## 实测报告（2026-08-04，整改提交 `b17c03b5`）

### 结论

**严格口径不通过：A1–A8 全部通过，B1–B6 中 B1、B2 失败，其余通过，合计
12/14。** 动效引擎的确定性、loop 闭环、真值门、缓存、vendor 守护和 UI seek
均已通过；最终成片的描边虽不越界、无文字，但落在画面中央道路/主体区域，未满足
“位于留白区”和“不遮主体”的强制视觉标准，不能以技术门全绿代替目检通过。

### 环境与样本

- 被测分支/远端：`feat/motion-js-timeline`，HEAD `b17c03b5`，未 rebase、未合并。
- 隔离实例：`127.0.0.1:8098`，数据根 `~/.qwenpaw-motion`；未触碰 8088/
  `~/.qwenpaw-poc`。`dev-isolated.sh verify` 的 158 个 backend 文件一致。
- UI 项目：`project-984f9b16720d53c08c9a058ab09b01e5`（`WT8 动效真实验收
  20260804`），导入 `/private/tmp/wt8-motion-real-30s.mp4`。
- 最终成片：`assets/artifacts/file-f7b97b7bcd505e1ebd33586d2398898a.mp4`；抽取
  `t=0/0.75/1.49/1.51/2.25/2.99/15/29.5s` 八帧逐一目检。
- GSAP vendor fetch/哈希校验通过；motion 专项测试 **57 passed**。整改提交自带的
  完整验证记录为 pre-commit 全绿、Creator backend 749 passed、UI 279/279；本轮
  另以真实 VLM、Playwright、ffmpeg 和实际成片独立覆盖 A/B 用例。

### A 组结果

| Case | 结果 | 真实证据 |
|---|---|---|
| A1 | PASS | VLM 看帧生成 html_js；首版被真值门拒绝后自动重生，第二版通过并真实渲染为 `/private/tmp/wt8-motion-manual/a1-render.mp4`。最终方案取青色/暖白色、无文字，coverage=2.22%，边沿像素=0。 |
| A2 | PASS | 满画面、安静森林样本首次返回 `needed=false`，明确拒绝硬加。 |
| A3 | PASS | 同一文档 `t=1.0s` 两次 PNG SHA-256 均为 `3986d8848673af2bd31cf933cbfe0ee2ace8250b2b95870a04129acae7db0214`。 |
| A4 | PASS | 手写越界样本被拒，边沿占比 63.33%，返回可用于重生的错误反馈。 |
| A5 | PASS | 精确 `t=0` 空帧样本被拒。 |
| A6 | PASS | premultiplied RGBA 首尾均差 0.764、变化像素 1.15%，低于 seam gate；缓存键含 loop 盐。 |
| A7 | PASS | 相同参数第二次渲染命中缓存；24.943s 降至 4.920s，约快 80.3%，trace key `4610f12ceb6d5540`。 |
| A8 | PASS | 本地 `gsap.min.js` 改动一字节后哈希校验失败并拒绝渲染，错误可读；测试后恢复原文件。 |

VLM 一次通过率：A1 为 **1/2（50%）**，A2 克制判断为 **1/1（100%）**；UI
两次正式动效设计也都观察到首版被拒、第二版成功。确定性真值门对人为 A4/A5
坏样本的拦截率为 **2/2（100%）**；另真实拦截了 A1 首版和两次 UI 首版。

### B 组结果

| Case | 结果 | 真实证据 |
|---|---|---|
| B1 | **FAIL** | 最终描边为细白双弧/柔光，颜色与画面协调且无文字，但视觉较简单，且主要位于中央道路/主体区域，不是 VLM 所述的顶部/右侧留白区；不满足“现代高级感 + 位于留白区”的完整要求。 |
| B2 | **FAIL** | 八帧确认无越界、无可见文字，尾段正常消失；但首 3 秒双弧压在中央道路/主体视觉区域，故“不遮主体”不成立。 |
| B3 | PASS | 预览 seek 从 1s 往返片尾再回 1s，裁切后视频视口两次逐字节一致，SHA-256 均为 `9d0a575cd4981e89fda823d3a971d1af6526fd2a63799e71ea4b6e20872eeadb`。 |
| B4 | PASS | 对 5–10s 苔藓嗅探片段真实调用 VLM；Agent 返回 `needed=false`，解释胡须占据上半、苔藓/地面占据下半且无稳定留白，未新增 overlay。 |
| B5 | PASS | 初版周期 6s 大于 3s overlay 窗口，被本轮明确指出后经 UI 重新设计为 1.5s 闭环；裸渲染 `t=0` 与 `t=1.5s` PNG 字节完全一致，SHA-256 均为 `d0eb2b662875e2652026e02d47d232d6aa51e67a6ccb2f4467a07d23e14f4264`；1.49/1.51s 成片边界无可见跳变。 |
| B6 | PASS | 主设计和 B5 重生均在会话中观察到真值门拒绝首版、自动发起第二次 VLM 设计并产出合格协议文档。 |

html_js 预览附加验证：浏览器直接打开
`GET /api/qwenpaw-creator/media/motion-documents/file-motion-fff87d8405cd585f9510a53d163452b0/poster?format=html_js&width=640&height=360`，
8098 返回并显示 `poster (640×360)` PNG；前端以 `data-live-motion-poster=true` 的
`<img>` 加载该后端确定性帧，不在 UI sandbox 中执行文档脚本。

### 额外发现

1. **视觉语义门仍不够**：像素级门能拦空帧、越界、静止和坏 seam，但无法保证
   “位于留白区/不遮主体/足够高级”；B1、B2 证明必须由 WT4 语义自评或人工抽帧兜底。
2. **Agent 审阅恢复不可靠**：界面提示“审阅通过后自动继续”，本轮多次停在 standby，
   需要用户追加指令；自动 compose 首次也出现“重试合成”，经 UI 点击后成功。
3. **目标约束容易漂移**：初始宽泛指令曾生成 5 个宠物 OS 文字卡和 CSS motion，并
   伴随 schema/JQ 错误；删除错误 overlay、明确仅允许 html_js 描边后才收敛。
4. **周期/窗口一致性应前置**：首个 UI 合格文档的 `__hf.duration=6s`，但 overlay
   仅 3s，虽然可渲染却不构成片段内完整循环；本轮人工发现并重生为 1.5s。建议将
   `period <= overlay span` 加入设计契约或结构化检查。

### 最终判定与后续门槛

本轮不满足“A1–A8、B1–B6 全过”的通过标准。修复或重新设计最终描边，使其稳定落在
真实留白区并避开主体后，应仅重跑 B1/B2（同时保留 B3/B5 回归）；重新 compose 并
抽首/中/尾及循环边界帧，全部通过后方可把 WT8 改判为 PASS。
