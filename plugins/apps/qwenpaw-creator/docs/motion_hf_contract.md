# `window.__hf` 动效引擎内部契约（html_js 文档 × 确定性渲染器）

> 本文是 Creator 自建 html_js 动效引擎的**内部契约说明**，从
> `motion_design.py` 的 prompt 文本中提炼而来，供后续复用同一引擎的动效能力
> （转场动效、字幕动效等）对齐。运行时实现见
> `backend/services/media_files/motion_engine.py`（协议资产）与
> `motion_overlay.py`（probe / capture 渲染器）。
>
> 方法论与 HyperFrames 同源（seek 协议 + 确定性渲染），但 Creator 引擎
> **不依赖 hyperframes CLI/npx**，与 edu-agent skill 沙箱内的独立渲染
> 保持隔离、互不混用。

## 1. 协议形态

一个 `html_js` 动效文档是完整独立的 HTML 页面，必须注册：

```js
window.__hf = {
  duration: <总秒数>,          // 一个动画周期的长度（loop 文档为一个循环周期）
  seek: function (t, opts) {}  // 把视觉状态拨到绝对时间 t（秒）
};
```

这是 HyperFrames 运行时契约的**最小子集**：渲染器唯一的驱动入口是
`seek`，绝不实时播放。GSAP 样板（必须 paused，双 totalTime 规避
lazy-render 首帧空白）：

```js
var tl = gsap.timeline({ paused: true });
/* 在这里编排动画 */
window.__hf = { duration: <总秒数>, seek: function (t, o) { tl.pause();
  tl.totalTime(Math.max(0, t) + 0.001, true);
  tl.totalTime(Math.max(0, t), o && o.suppressEvents === true); } };
```

**提交边界铁律**：html_js 文档只能经设计管线
（`design_motion_overlays`：契约校验 → probe 真值门 → 内容寻址外置）
进入已提交 Project：Project 提交校验拒绝任何内联 html_js 文档，并要求
`html_file_id` 引用存在且为内容寻址的 motion_document（file id 必须
由索引 checksum 派生，见 `motion_document_file_id`；悬空/伪造/非
 motion 文件引用均拒绝）。最后一道防线在渲染器：每次 html_js
渲染前都重新执行完整的 loop-aware probe（结果按 (html, box, loop)
缓存），因此复用既有文档并篡改 loop 等标志也无法绕过接缝/静止/空帧门。

## 2. seek 安全规则（渲染器逐帧拨时间截屏）

确定性目标：**同一 (html, seek t) 必须永远画出同样的像素**。因此：

1. **禁 rAF/定时器驱动视觉**：不得用 `requestAnimationFrame` /
   `setTimeout` / `setInterval` 推进任何视觉状态。截屏之间没有真实
   时间流逝，这些回调的执行时机不进入确定性输入。
2. **禁随机/时钟决定视觉**：不得用 `Math.random()`、`Date.now()`、
   `performance.now()` 决定视觉状态。渲染器通过 prelude
   （`MOTION_PRELUDE_SCRIPT`，经 `Page.addInitScript` 在任何文档脚本
   之前注入）冻结时钟并固定随机种子：`Date.now`/`performance.now`
   返回 `__qpMotionClock` 拨入的帧时间戳，`Math.random` 是固定种子
   xorshift。文档若依赖真实时钟，冻结后行为即坏。
3. **回调抑制**：seek 时补间回调被抑制（`seek(t, { suppressEvents:
   true })`）。不得依赖 `onUpdate` / `onComplete` 回调写 DOM。
4. **末尾同步**：若确需根据补间状态写 DOM（如数字滚动文本），必须在
   `__hf.seek` 函数体末尾显式同步一次，保证任意乱序 seek 后 DOM
   与时间线状态一致。
5. **seek 不得抛异常**：`__hf.seek` 抛出的异常会被渲染器捕获并携带
   时间戳上报，整次 probe/capture 直接判失败（拒绝入库、回馈重生），
   绝不会把静态兼容画面当作成功渲染。

## 3. 每帧驱动顺序（capture worker）

对第 `i` 帧（输出帧率 `fps`，见 `frame_timestamp_ms`）：

1. `playheadMs = i * 1000 / fps`（真实播放头，用于渲染器托管退场）；
2. `timestampMs = loop ? playheadMs % totalMs : min(playheadMs, totalMs)`
   （时间线时间；loop 文档按周期取模，非 loop 文档保持末态）；
3. `window.__qpMotionClock(timestampMs)` 把冻结时钟钉到帧时间；
4. `__hf.seek(timestampMs / 1000, { suppressEvents: true })`；
5. 同步所有 CSS `document.getAnimations()` 的 `currentTime`（html_js
   文档中如有 CSS 动画则与时间线共用同一时钟）；
6. 应用渲染器托管退场（§5），随后透明截屏。

## 4. loop 语义

- `__hf.duration` 填**一个周期**的秒数（入场 + 持续微动一个周期，
  建议 2~4 秒），不要填整个片段时长。
- `loop: true`：渲染器按 `playheadMs % (duration*1000)` 循环拨时间。
  时间线首尾状态必须严格一致：probe 会额外采样 t=0 与 t=duration
  两帧做**无缝接缝比对**（预乘 RGBA 均差、变化像素占比双阈值），
  接缝跳变直接拒绝并回馈重生；t=0 必须非空。
- `loop: false`（字幕卡）：超过 duration 后保持末态。
- 超长窗口：当 `duration × fps` 超过单次捕获上限（240 帧）时，loop
  文档切换为**单周期捕获**：只截一个周期的帧，由 ffmpeg
  `-stream_loop` 按需重复铺满窗口，托管退场改由 ffmpeg 在窗口末 15%
  做 alpha 淡出（shrink 降级为同样的淡出），避免旧实现在 40 秒后
  冻结的问题。
- `loop` 与单周期模式都参与帧缓存键与渲染指纹
  （`frame_cache_identity`），同一文档改变 loop 不会复用旧帧。

## 5. 布局与退场铁律（自动拒绝项）

- 根容器固定 `position:absolute; inset:8%`；动画全程任何可见像素
  （含描边、阴影、发光、粒子）不得碰到视口边缘；禁止视口外滑入/滑出；
  位移幅度 ≤ 视口 5%，scale 过冲 ≤ 1.06。
- **退场不要自己做**：需要退场时在根容器声明
  `data-motion-exit="soft_fade"`（或 `"shrink"`），渲染器在输出窗口
  的最后 15%（progress ≥ 0.85，按**真实播放头**计算，loop 文档同样
  生效）自动做透明度/缩放退场。时间线末态必须保持完整可见。
- 除 `vendor/` 白名单运行时外禁止一切外部资源；vendor 注册表与
  新增流程见 `motion_engine.py` 模块 docstring。

## 6. 渲染真值自查（post-render gates）

设计期（probe，`probe_motion_document`）与合成期（capture，
`render_motion_overlay`）各有一道确定性规则门：

- **probe**：按 envelope 分数 `(0.05, 0.15, 0.3, 0.5, 0.7, 0.9, 1.0)`
  抽帧（loop 文档额外前置 t=0），拨时间时**不施加托管退场**（probe
  检验的是裸时间线状态）；用 ffmpeg `alphaextract` 采样 alpha 平面 ——
  整体可见覆盖率、透明盒外沿 alpha 接触率（越界检测）、关键帧空帧
  检测（首帧 0.05 / 入场完成点 0.3 / 中点 0.5 / 末态 1.0）、html_js
  全帧像素零变化的静止文档检测、loop 首尾接缝比对。任一 seek 抛出
  异常即整体失败并携带时间戳。不合格拒绝并把原因回馈给设计 VLM
  重生（`_design_document` 重试环）。
- **capture**：每次 html_js 渲染前强制重跑完整 loop-aware probe（上述
  全部规则，结果按 (html, box, loop) 缓存），不通过则渲染直接失败；
  帧序列入缓存前另抽首/中/尾（尾 = 窗口 80% 处，避开托管退场）做
  空帧 + 越界（`_CAPTURE_MAX_EDGE_CONTACT`）检查；不合格的帧序列
  **拒绝入库**（不进帧缓存、不进合成）。装饰动效渲染失败会
  **中止整次合成任务**（不发布缺少既定动效的成片）；字幕卡生成
  样式失败回退固定气泡模板，而固定模板也失败时同样中止合成
  （台词内容不得从成片中丢失），均由 rejection feedback loop 回馈重生。
- **前端实时预览**：html_js 文档在预览 iframe 中永不执行脚本
  （sandbox 禁 script，vendor 也无法从 srcDoc 解析）；预览唯一入口是
  后端渲染的确定性海报帧（`/media/motion-documents/{file_id}/poster`，
  采样周期 35% 处），就绪信号来自海报真实加载；无海报可用时
  fail-close（不渲染任何内容）但不阻塞整体预览（内联 html_js 在
  提交边界已被拒绝，产品链路不会产生该形态）。
- 语义级检查（遮挡主体/美观）**不在此层建设**，由自评环六维检查统一
  覆盖（动效属于画面质量维度）。

## 7. 缓存与指纹盐化

出文档之外的一切渲染输入都要参与缓存失效：

- `engine_digest(libs)` = 协议版本 + prelude 字节哈希 + 各引用 vendor
  的内容哈希；参与 probe 缓存键与帧缓存键（`frame_cache_identity`）。
- `full_engine_digest()`（全部注册 vendor）参与渲染指纹
  （`_motion_document_payload`），外置文档不加载 body 也能对引擎
  升级失效。
- 升级 prelude、seek 语义或 vendor pin 时，凡改变已有 (html, t) 的
  像素结果的行为变化必须 bump `MOTION_ENGINE_PROTOCOL_VERSION`。

## 8. 复用同一引擎的新动效能力检查单

新能力（转场、字幕动效等）接入时逐项对齐：

1. 文档产出遵守 §1 协议形态与 §2 seek 安全规则（prompt 里引用
   `_JS_TIMELINE_CODE_RULES` 或复述同等约束）；
2. 校验入口复用 `_validated_design` 同级检查（vendor 白名单、
   `window.__hf` 注册、禁外链/交互标签）；
3. 渲染走 `probe_motion_document` + `render_motion_overlay`，不绕过
   真值自查（§6）；
4. 新增缓存维度（如转场双输入）必须并入帧缓存键，且考虑是否构成
   协议版本 bump（§7）。
