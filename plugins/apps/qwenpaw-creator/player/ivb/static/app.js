/* IVB 放映端状态机。
 *
 * 三条刻意区别于 demo 的实现:
 * 1. 入口取 bundle.entry_timeline_id,不靠 JSON 键序;
 * 2. 选项文案/色调由服务端 /api/bundle 预 join,前端不再手写 edges[ref];
 * 3. 观看秒数在离开节点时回写,choice_edge 在走过边时回写 —— 两列都是活的。
 */
(function (global) {
  "use strict";

  const el = (id) => document.getElementById(id);
  const dom = {};
  [
    "screen-library", "lib-grid", "lib-empty", "lib-scope-all", "lib-scope-mine",
    "screen-title", "screen-play", "screen-map", "screen-ending",
    "title-name", "title-tagline", "title-synopsis", "title-stats",
    "btn-start", "btn-resume", "btn-map-from-title", "btn-library", "btn-reset",
    "foot-bundle", "foot-warnings",
    "stage", "hud-node", "btn-map", "btn-title",
    "choice-overlay", "choice-question", "choice-timer", "choice-count",
    "timer-fill", "choice-cards", "choice-hint",
    "map-progress", "map-canvas", "map-links", "map-nodes", "btn-map-back",
    "lg-tone",
    "ending-name", "ending-synopsis", "ending-unlock-note", "ending-review",
    "ending-review-head", "ending-coverage", "btn-replay", "btn-ending-map",
    "btn-ending-title", "toast", "fatal",
  ].forEach((id) => { dom[id] = el(id); });

  const S = {
    pid: null,
    bundle: null,
    progress: { visited: [], endings: [], path: [], current_timeline: "" },
    current: null,
    answered: new Set(),
    pendingTarget: null,
    watchSince: 0,
    watched: 0,
    timer: null,
    countdownLeft: 0,
    mapReturnTo: "title",
  };

  // ---------- 基础工具 ----------

  function showScreen(name) {
    document.body.dataset.screen = name;
    // 离开放映屏(map / title / ending)时暂停视频。包内只有 #stage 这一路内嵌
    // 音轨,不暂停的话切到地图或标题后声音还在放;之前只有返回库靠整页刷新才
    // 停。回到 play 由各自调用方重新 play(),这里只管暂停出口。
    if (name !== "play" && dom.stage && !dom.stage.paused) dom.stage.pause();
  }

  let toastTimer = null;
  function toast(text) {
    dom.toast.textContent = text;
    dom.toast.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => dom.toast.classList.add("hidden"), 2600);
  }

  function fail(title, items) {
    dom.fatal.innerHTML = "";
    const box = document.createElement("div");
    box.className = "box";
    const head = document.createElement("h2");
    head.textContent = title;
    const list = document.createElement("ul");
    items.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      list.appendChild(li);
    });
    box.append(head, list);
    dom.fatal.appendChild(box);
    dom.fatal.classList.remove("hidden");
  }

  function segmentUrl(node) {
    if (!node.segment) return "";
    return global.Api.segmentUrl(S.pid, node.segment.split("/").pop());
  }

  function interactionsOf(timelineId) {
    return S.bundle.interactions.filter(
      (point) => point.source_timeline_id === timelineId,
    );
  }

  // ---------- 表现层注入 ----------

  async function applyPresentation(bundle) {
    const root = document.documentElement.style;
    Object.entries(bundle.theme_css_vars || {}).forEach(([key, value]) => {
      root.setProperty(key, value);
    });
    for (const href of bundle.stylesheets || []) {
      const css = await global.Api.stylesheet(href);
      if (!css) continue;
      const tag = document.createElement("style");
      tag.dataset.ivbStylesheet = href;
      tag.textContent = css;
      document.head.appendChild(tag);
    }
    const title = (bundle.screens || {}).title || {};
    if (title.cta_label) dom["btn-start"].textContent = title.cta_label;
    if (title.secondary_label) {
      dom["btn-resume"].textContent = title.secondary_label;
    }
    document.title = bundle.meta.title || "IVB 放映";
  }

  // ---------- ① 标题屏 ----------

  function renderTitle() {
    const { meta, totals } = S.bundle;
    dom["title-name"].textContent = meta.title || "(未命名)";
    // tagline 在 Creator 侧常被填成完整生成 prompt(几百字),放标题屏既难看
    // 又泄露提示词。 synopsis 才是给人看的简介, tagline 不渲染。
    dom["title-synopsis"].textContent = meta.synopsis || "";
    dom["foot-bundle"].textContent =
      meta.bundle_id + " · schema v" + S.bundle.schema_version;
    const seen = S.progress.visited.length;
    const unlocked = S.progress.endings.length;
    dom["title-stats"].innerHTML = "";
    [
      ["分段", totals.nodes],
      ["抉择点", totals.interactions],
      ["结局", unlocked + " / " + totals.endings],
      ["已看节点", seen + " / " + totals.nodes],
    ].forEach(([label, value]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = String(value);
      wrap.append(dt, dd);
      dom["title-stats"].appendChild(wrap);
    });
    const hasHistory = seen > 0;
    // 有进度时"开始游戏"改叫"从头开始",否则观众不知道这会丢掉进度。
    dom["btn-start"].textContent = hasHistory ? "从头开始" : "开始游戏";
    dom["btn-resume"].classList.toggle("hidden", !hasHistory);
    dom["btn-reset"].classList.toggle("hidden", !hasHistory);
  }

  async function refreshProgress() {
    S.progress = await global.Api.progress(S.pid) || S.progress;
  }

  // ---------- ② 播放屏 ----------

  async function commitWatch() {
    if (!S.current || S.watched <= 0.05) return;
    const seconds = S.watched;
    S.watched = 0;
    try {
      await global.Api.watch(S.pid, S.current, seconds);
    } catch (error) {
      console.warn("回写观看秒数失败", error);
    }
  }

  async function playNode(timelineId, viaEdge) {
    const node = S.bundle.nodes[timelineId];
    if (!node) {
      fail("内容层缺节点", [timelineId]);
      return;
    }
    await commitWatch();
    hideChoice();
    S.current = timelineId;
    S.answered = new Set();
    S.pendingTarget = null;
    S.watchSince = 0;
    showScreen("play");
    dom["hud-node"].textContent = node.title;
    try {
      await global.Api.visit(S.pid, timelineId, viaEdge || null);
      S.progress = await global.Api.progress(S.pid);
    } catch (error) {
      console.warn("进度未落库", error);
    }
    const video = dom.stage;
    video.src = segmentUrl(node);
    video.currentTime = 0;
    video.load();
    try {
      await video.play();
    } catch (error) {
      toast("浏览器拦住了自动播放,点画面继续");
    }
  }

  function onTimeUpdate() {
    const video = dom.stage;
    if (S.current === null) return;
    const delta = video.currentTime - S.watchSince;
    if (delta > 0) S.watched += delta;
    S.watchSince = video.currentTime;
    if (isChoiceOpen()) return;
    const pending = interactionsOf(S.current).find(
      (point) => !S.answered.has(pointKey(point)) && video.currentTime >= point.at_seconds,
    );
    if (pending) openChoice(pending);
  }

  function pointKey(point) {
    return point.source_timeline_id + "@" + point.at_seconds;
  }

  function isChoiceOpen() {
    return !dom["choice-overlay"].classList.contains("hidden");
  }

  function hideChoice() {
    dom["choice-overlay"].classList.add("hidden");
    dom["choice-hint"].classList.add("hidden");
    // 卡片抽掉再隐藏。只加 .hidden 的话旧卡还在 DOM 里,程序化点击能拿它
    // 把同一个抉择点再记一次账(S.answered 进节点时会重置)。
    dom["choice-cards"].innerHTML = "";
    stopCountdown();
  }

  function openChoice(point) {
    dom.stage.pause();
    dom["choice-question"].textContent = point.question;
    dom["choice-cards"].innerHTML = "";
    point.options.forEach((option, index) => {
      dom["choice-cards"].appendChild(makeCard(point, option, index));
    });
    dom["choice-overlay"].classList.remove("hidden");
    if (point.countdown_seconds) startCountdown(point);
    else stopCountdown();
    const first = dom["choice-cards"].querySelector("button");
    if (first) first.focus();
  }

  // tone 刻意不在这里出现。抉择那一刻就给观众"这条危险"的预期,等于替观众
  // 做了判断(红色 = 别选),分支内容反而没人走 —— 那互动结构就白搭了。
  // 它只在事后渲染:结局回顾 (renderReview) 与地图上真正走过的边 (renderMap)。
  function makeCard(point, option, index) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "choice-card";

    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = String(index + 1);

    const body = document.createElement("span");
    body.className = "body";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = option.label || option.edge_ref;
    body.appendChild(label);
    if (option.prompt) {
      const prompt = document.createElement("span");
      prompt.className = "prompt";
      prompt.textContent = option.prompt;
      body.appendChild(prompt);
    }

    card.append(idx, body);
    card.addEventListener("click", () => pickChoice(point, option.edge_ref));
    return card;
  }

  function startCountdown(point) {
    stopCountdown();
    S.countdownLeft = point.countdown_seconds;
    dom["choice-timer"].classList.remove("hidden");
    dom["choice-timer"].classList.remove("expiring");
    const total = point.countdown_seconds;
    const tick = () => {
      S.countdownLeft = Math.max(0, S.countdownLeft - 0.1);
      dom["choice-count"].textContent = Math.ceil(S.countdownLeft) + "s";
      dom["timer-fill"].style.width =
        ((S.countdownLeft / total) * 100).toFixed(1) + "%";
      dom["choice-timer"].classList.toggle("expiring", S.countdownLeft <= 3);
      if (S.countdownLeft > 0.001) return;
      stopCountdown();
      if (point.default_edge_ref) {
        pickChoice(point, point.default_edge_ref);
      } else {
        dom["choice-hint"].classList.remove("hidden");
      }
    };
    tick();
    S.timer = setInterval(tick, 100);
  }

  function stopCountdown() {
    if (S.timer) clearInterval(S.timer);
    S.timer = null;
    dom["choice-timer"].classList.add("hidden");
  }

  async function pickChoice(point, edgeRef) {
    if (S.answered.has(pointKey(point))) return;
    S.answered.add(pointKey(point));
    hideChoice();
    const edge = S.bundle.edges[edgeRef];
    if (!edge || !edge.target_timeline_id) {
      fail("边指向不明", [edgeRef]);
      return;
    }
    try {
      await global.Api.choice(S.pid, point.source_timeline_id, edgeRef);
    } catch (error) {
      console.warn("抉择未落库", error);
    }
    await playNode(edge.target_timeline_id, edgeRef);
  }

  async function onSegmentEnded() {
    if (S.current === null) return;
    const node = S.bundle.nodes[S.current];
    const unanswered = interactionsOf(S.current).filter(
      (point) => !S.answered.has(pointKey(point)),
    );
    if (unanswered.length) {
      openChoice(unanswered[0]);
      return;
    }
    if (node.is_ending) {
      await showEnding();
      return;
    }
    if (node.children.length === 1) {
      const edge = Object.values(S.bundle.edges).find(
        (item) => item.target_timeline_id === node.children[0],
      );
      await playNode(node.children[0], edge ? edge.edge_ref : null);
      return;
    }
    fail("分岔节点没有可选的抉择点", [
      S.current + " → " + node.children.join(" | "),
      "这是包结构问题,请跑 ivb validate 看诊断",
    ]);
  }

  // ---------- ④ 结局屏 ----------

  async function showEnding() {
    const timelineId = S.current;
    const node = S.bundle.nodes[timelineId];
    await commitWatch();
    let firstTime = false;
    try {
      const result = await global.Api.ending(S.pid, timelineId);
      firstTime = !!(result && result.first_time);
      await refreshProgress();
    } catch (error) {
      console.warn("结局未落库", error);
    }
    showScreen("ending");
    dom["ending-name"].textContent = node.title;
    dom["ending-synopsis"].textContent = node.synopsis || "";
    dom["ending-unlock-note"].classList.toggle("hidden", !firstTime);
    renderReview();
    const total = S.bundle.totals.endings || 1;
    const got = S.progress.endings.length;
    const coverage = S.bundle.totals.nodes
      ? Math.round((S.progress.visited.length / S.bundle.totals.nodes) * 100)
      : 0;
    dom["ending-coverage"].textContent =
      "结局 " + got + " / " + total + " · 节点覆盖 " + coverage + "%";
  }

  // path 是全历史的,而"你的路径"只该讲这一次。故事图是 DAG ⇒ 入口节点不可能
  // 在一局内被二次访问,所以最后一次落在入口的那行就是本局起点。
  function currentRunPath() {
    const path = S.progress.path || [];
    const entry = S.bundle.entry_timeline_id;
    let start = 0;
    path.forEach((row, index) => {
      if (row.timeline_id === entry) start = index;
    });
    return path.slice(start);
  }

  function renderReview() {
    dom["ending-review"].innerHTML = "";
    // show_review=false is an explicit "no recap" request from the bundle
    // (suspense endings keep the last beat clean); absent = default on.
    const endingScreen = (S.bundle.screens || {}).ending || {};
    if (endingScreen.show_review === false) {
      dom["ending-review-head"].classList.add("hidden");
      dom["ending-review"].classList.add("hidden");
      return;
    }
    dom["ending-review"].classList.remove("hidden");
    const path = currentRunPath().slice(-12);
    const visible = path.filter((row) => row.choice_edge);
    dom["ending-review-head"].classList.toggle("hidden", visible.length === 0);
    visible.forEach((row) => {
      const edge = S.bundle.edges[row.choice_edge] || {};
      const li = document.createElement("li");
      const via = document.createElement("span");
      via.className = "via";
      via.textContent = "→";
      const text = document.createElement("span");
      text.textContent = (edge.label || row.choice_edge) + " · "
        + ((S.bundle.nodes[row.timeline_id] || {}).title || row.timeline_id);
      li.append(via, text);
      if (edge.tone) {
        const tone = document.createElement("span");
        tone.className = "tone tone-" + edge.tone;
        tone.textContent = S.bundle.badge_labels[edge.tone] || edge.tone;
        li.appendChild(tone);
      }
      dom["ending-review"].appendChild(li);
    });
  }

  // ---------- ③ 地图屏 ----------

  function layout() {
    const entry = S.bundle.entry_timeline_id;
    const nodes = S.bundle.nodes;
    const depths = {};
    const queue = [entry];
    depths[entry] = 0;
    while (queue.length) {
      const current = queue.shift();
      ((nodes[current] || {}).children || []).forEach((child) => {
        if (depths[child] === undefined) {
          depths[child] = depths[current] + 1;
          queue.push(child);
        }
      });
    }
    const columns = {};
    Object.keys(nodes).forEach((id) => {
      const depth = depths[id] === undefined ? -1 : depths[id];
      const key = depth < 0 ? "detached" : String(depth);
      (columns[key] = columns[key] || []).push(id);
    });
    Object.values(columns).forEach((group) => group.sort());

    const order = Object.keys(columns)
      .filter((key) => key !== "detached")
      .map(Number).sort((a, b) => a - b);
    const width = Math.max(order.length, 1);
    const positions = {};
    order.forEach((depth) => {
      const group = columns[String(depth)];
      group.forEach((id, index) => {
        positions[id] = {
          x: width === 1 ? 50 : 8 + (depth / (width - 1)) * 84,
          y: ((index + 1) / (group.length + 1)) * 100,
        };
      });
    });
    const detached = columns.detached || [];
    detached.forEach((id, index) => {
      positions[id] = { x: 50, y: ((index + 1) / (detached.length + 1)) * 100 };
    });
    return { positions, depths };
  }

  function revealSet() {
    const visited = new Set(S.progress.visited);
    // 方案 B:地图只长在观众踩过的地方。入口无条件在集合里(零进度时至少有个
    // 起点,否则整张图是空白页);其余只有真看过才揭示。不再向下游展开。
    const revealed = new Set([S.bundle.entry_timeline_id]);
    visited.forEach((id) => revealed.add(id));
    return { revealed, visited };
  }

  function renderMap() {
    const { positions, depths } = layout();
    const { revealed, visited } = revealSet();
    const current = S.current;
    dom["map-nodes"].innerHTML = "";
    dom["map-links"].innerHTML = "";
    dom["map-links"].setAttribute("viewBox", "0 0 1000 560");

    // 走过的边才允许上风险色:地图会露出"可去未看"的分支,在那里给风险色就是
    // 事前剧透。choice_edge 只在观众真的点过时才落库,拿它当"走过"的凭据。
    const walked = new Set(
      (S.progress.path || []).map((row) => row.choice_edge).filter(Boolean),
    );
    // 一条边也没标的包(Creator 旧版导出)不该挂着一排永远用不上的三色图例。
    dom["lg-tone"].classList.toggle(
      "hidden", !Object.values(S.bundle.edges).some((item) => item.tone),
    );

    const seenCount = visited.size;
    const total = S.bundle.totals.nodes;
    const endings = S.progress.endings.length + " / " + S.bundle.totals.endings;
    dom["map-progress"].textContent =
      "已走 " + seenCount + " / " + total + " · 结局 " + endings;

    Object.keys(S.bundle.nodes).forEach((id) => {
      if (depths[id] === undefined) return;  // 不可达节点不进图
      ((S.bundle.nodes[id].children) || []).forEach((child) => {
        const from = positions[id];
        const to = positions[child];
        if (!from || !to) return;
        const shown = revealed.has(id) && revealed.has(child);
        if (!shown) return;  // 方案 B:两端都没揭示的边不画,地图只长在踩过的地方
        // 按 (来源, 目标) 精确认边。以前只判"child 在不在 id.children 里",于是
        // 汇流节点的两个父节点会匹配到同一条已走过的边,没走过的那条也染绿。
        const links = Object.values(S.bundle.edges).filter(
          (item) => item.source_timeline_id === id
            && item.target_timeline_id === child,
        );
        const walkedEdge = links.find((item) => walked.has(item.edge_ref));
        // 线性跳转在包里可能根本没有边(走查包 5 条边只登了 3 条)。这时
        // "父已看 + 子已看 + 父只有一个下游" 等价于这条路走过了。
        const implied = !links.length
          && (S.bundle.nodes[id].children || []).length === 1
          && visited.has(id) && visited.has(child);
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const mid = (from.x + to.x) / 2;
        path.setAttribute("d",
          "M" + (from.x * 10) + " " + (from.y * 5.6)
          + " C" + (mid * 10) + " " + (from.y * 5.6)
          + "," + (mid * 10) + " " + (to.y * 5.6)
          + "," + (to.x * 10) + " " + (to.y * 5.6));
        path.setAttribute("vector-effect", "non-scaling-stroke");
        // seen = 这条边真走过;reachable = 两端已揭示但没走过。两者以前共用
        // seen,于是从未走过的边也是荧光绿,"走过的路"在图上根本看不出来。
        let linkClass = "map-link";
        if (walkedEdge || implied) {
          linkClass += " seen";
          if (walkedEdge && walkedEdge.tone) linkClass += " tone-" + walkedEdge.tone;
        } else if (shown) {
          linkClass += " reachable";
        }
        path.setAttribute("class", linkClass);
        dom["map-links"].appendChild(path);
      });
    });

    Object.keys(S.bundle.nodes).forEach((id) => {
      const node = S.bundle.nodes[id];
      const place = positions[id];
      if (!place || depths[id] === undefined) return;
      const has = visited.has(id);
      const open = revealed.has(id);
      if (!open) return;  // 方案 B:fog 节点不画,地图只含入口+已看节点
      const isEntry = id === S.bundle.entry_timeline_id;
      const box = document.createElement("div");
      box.className = "map-node"
        + (has ? " seen" : " locked")
        + (node.is_ending && has ? " ending" : "")
        + (id === current ? " current" : "")
        + " clickable";
      box.style.left = place.x + "%";
      box.style.top = place.y + "%";
      const tag = document.createElement("span");
      tag.className = "tag";
      const label = document.createElement("span");
      label.textContent = node.title;
      // 入口没看过时显示"起点"而非"可去";其他已揭示未看的节点显示"可去"。
      if (isEntry && !has) {
        tag.textContent = "起点";
      } else if (has) {
        tag.textContent = node.is_ending ? "结局 · 已看" : "已看";
      } else {
        tag.textContent = node.is_ending ? "结局 · 可去" : "可去";
      }
      box.append(label, tag);
      // 入口即使没看过也能点(从头开始);其他节点只有看过才能跳回去。
      box.addEventListener("click", () => {
        S.mapReturnTo = "play";
        playNode(id, null);
      });
      dom["map-nodes"].appendChild(box);
    });
  }

  // ---------- 事件装配 ----------

  function wire() {
    dom["btn-start"].addEventListener("click", () => {
      playNode(S.bundle.entry_timeline_id, null);
    });
    dom["btn-resume"].addEventListener("click", () => {
      const target = S.progress.current_timeline || S.bundle.entry_timeline_id;
      playNode(target, null);
    });
    dom["btn-reset"].addEventListener("click", async () => {
      if (!global.confirm("清空全部进度、结局与抉择统计?")) return;
      const result = await global.Api.reset(S.pid);
      await refreshProgress();
      renderTitle();
      const deleted = (result && result.deleted) || {};
      toast("已清空(删除 " + (deleted.visits || 0) + " 条访问记录)");
    });
    dom["btn-library"].addEventListener("click", () => {
      global.location.search = "";
    });
    [["btn-map-from-title", "title"], ["btn-map", "play"], ["btn-ending-map", "ending"]]
      .forEach(([id, returnTo]) => {
        dom[id].addEventListener("click", () => {
          S.mapReturnTo = returnTo;
          renderMap();
          showScreen("map");
        });
      });
    dom["btn-map-back"].addEventListener("click", async () => {
      if (S.mapReturnTo === "play" && S.current) {
        showScreen("play");
        await dom.stage.play().catch(() => {});
      } else if (S.mapReturnTo === "ending") {
        showScreen("ending");
      } else {
        renderTitle();
        showScreen("title");
      }
    });
    [["btn-title", null], ["btn-ending-title", "title"], ["btn-replay", "replay"]]
      .forEach(([id, action]) => {
        dom[id].addEventListener("click", async () => {
          await commitWatch();
          if (action === "replay") {
            await playNode(S.bundle.entry_timeline_id, null);
          } else {
            await refreshProgress();
            renderTitle();
            showScreen("title");
          }
        });
      });
    dom.stage.addEventListener("timeupdate", onTimeUpdate);
    dom.stage.addEventListener("ended", onSegmentEnded);
    dom["choice-cards"].addEventListener("keydown", (event) => {
      const digits = { "1": 0, "2": 1, "3": 2, "4": 3, "5": 4 };
      const index = digits[event.key];
      if (index === undefined) return;
      const card = dom["choice-cards"].children[index];
      if (card) card.click();
    });
    global.addEventListener("resize", () => {
      if (document.body.dataset.screen === "map") renderMap();
    });
  }

  // ---------- ⓪ 库页 ----------

  function currentPid() {
    return new URLSearchParams(global.location.search).get("p");
  }

  function projectCard(project) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "lib-card";
    const title = document.createElement("h2");
    title.className = "lib-card-title";
    title.textContent = project.title || project.project_id;
    const synopsis = document.createElement("p");
    synopsis.className = "lib-card-synopsis";
    synopsis.textContent = project.synopsis || "";
    const meta = document.createElement("p");
    meta.className = "lib-card-meta";
    meta.textContent = "节点 " + project.node_count
      + " · 结局 " + project.ending_count
      + " · 抉择 " + project.interaction_count;
    const owner = document.createElement("span");
    owner.className = "lib-card-owner";
    owner.textContent = "上传者 " + project.owner_user_id;
    card.append(title, synopsis, meta, owner);
    // 点卡片 = 带 ?p={pid} 重新载入,进该项目放映页(可分享、可后退)。
    card.addEventListener("click", () => {
      global.location.search = "?p=" + encodeURIComponent(project.project_id);
    });
    return card;
  }

  async function renderLibrary(scope) {
    dom["lib-scope-all"].classList.toggle("active", scope !== "mine");
    dom["lib-scope-mine"].classList.toggle("active", scope === "mine");
    let payload;
    try {
      payload = await global.Api.projects(scope);
    } catch (error) {
      fail("库列表加载失败", [String(error && error.message ? error.message : error)]);
      return;
    }
    const projects = (payload && payload.projects) || [];
    dom["lib-grid"].innerHTML = "";
    dom["lib-empty"].classList.toggle("hidden", projects.length > 0);
    projects.forEach((project) => {
      dom["lib-grid"].appendChild(projectCard(project));
    });
  }

  function wireLibrary() {
    dom["lib-scope-all"].addEventListener("click", () => renderLibrary("all"));
    dom["lib-scope-mine"].addEventListener("click", () => renderLibrary("mine"));
  }

  async function bootLibrary() {
    showScreen("library");
    wireLibrary();
    await renderLibrary("all");
  }

  async function bootPlayer(pid) {
    try {
      const bundle = await global.Api.bundle(pid);
      S.bundle = bundle;
      await applyPresentation(bundle);
      await refreshProgress();
      renderTitle();
      wire();
      showScreen("title");
    } catch (error) {
      console.error(error);
      fail("包无法放映", [String(error && error.message ? error.message : error),
        "服务端可能没找到包,或包未通过结构校验。试试 ivb validate <包路径>"]);
    }
  }

  async function boot() {
    const pid = currentPid();
    if (pid) {
      S.pid = pid;
      await bootPlayer(pid);
    } else {
      await bootLibrary();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window);
