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
    "screen-title", "screen-play", "screen-map", "screen-ending",
    "title-name", "title-tagline", "title-synopsis", "title-stats",
    "btn-start", "btn-resume", "btn-map-from-title", "btn-reset",
    "foot-bundle", "foot-warnings",
    "stage", "hud-node", "btn-map", "btn-title",
    "choice-overlay", "choice-question", "choice-timer", "choice-count",
    "timer-fill", "choice-cards", "choice-hint",
    "map-progress", "map-canvas", "map-links", "map-nodes", "btn-map-back",
    "ending-name", "ending-synopsis", "ending-unlock-note", "ending-review",
    "ending-review-head", "ending-coverage", "btn-replay", "btn-ending-map",
    "btn-ending-title", "toast", "fatal",
  ].forEach((id) => { dom[id] = el(id); });

  const S = {
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
    return "/api/bundle/segments/" + node.segment.split("/").pop();
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
    dom["title-tagline"].textContent = meta.tagline || "";
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
    dom["btn-resume"].classList.toggle("hidden", !hasHistory);
    dom["btn-reset"].classList.toggle("hidden", !hasHistory);
  }

  async function refreshProgress() {
    S.progress = await global.Api.progress() || S.progress;
  }

  // ---------- ② 播放屏 ----------

  async function commitWatch() {
    if (!S.current || S.watched <= 0.05) return;
    const seconds = S.watched;
    S.watched = 0;
    try {
      await global.Api.watch(S.current, seconds);
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
      await global.Api.visit(timelineId, viaEdge || null);
      S.progress = await global.Api.progress();
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

  function makeCard(point, option, index) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "choice-card" + (option.tone ? " tone-" + option.tone : "");
    const badge = S.bundle.badge_labels[option.tone] || "";

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
    if (badge) {
      const tag = document.createElement("span");
      tag.className = "badge";
      tag.textContent = badge;
      card.appendChild(tag);
    }
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
      await global.Api.choice(point.source_timeline_id, edgeRef);
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
      const result = await global.Api.ending(timelineId);
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
    const path = (S.progress.path || []).slice(-12);
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
    const revealed = new Set(visited);
    const depth = S.bundle.screens && S.bundle.screens.map
      ? (S.bundle.screens.map.reveal_depth ?? 1) : 1;
    let frontier = new Set(visited);
    for (let step = 0; step < depth; step += 1) {
      const next = new Set();
      frontier.forEach((id) => {
        ((S.bundle.nodes[id] || {}).children || []).forEach((child) => {
          if (!revealed.has(child)) { revealed.add(child); next.add(child); }
        });
      });
      frontier = next;
    }
    return { revealed, visited };
  }

  function renderMap() {
    const { positions, depths } = layout();
    const { revealed, visited } = revealSet();
    const current = S.current;
    dom["map-nodes"].innerHTML = "";
    dom["map-links"].innerHTML = "";
    dom["map-links"].setAttribute("viewBox", "0 0 1000 560");

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
        const edge = Object.values(S.bundle.edges)
          .find((item) => item.target_timeline_id === child
            && isChildOf(item, id));
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const mid = (from.x + to.x) / 2;
        path.setAttribute("d",
          "M" + (from.x * 10) + " " + (from.y * 5.6)
          + " C" + (mid * 10) + " " + (from.y * 5.6)
          + "," + (mid * 10) + " " + (to.y * 5.6)
          + "," + (to.x * 10) + " " + (to.y * 5.6));
        path.setAttribute("vector-effect", "non-scaling-stroke");
        path.setAttribute("class",
          "map-link" + (shown ? " seen" : "") + (edge && edge.tone === "danger" ? " danger" : ""));
        dom["map-links"].appendChild(path);
      });
    });

    Object.keys(S.bundle.nodes).forEach((id) => {
      const node = S.bundle.nodes[id];
      const place = positions[id];
      if (!place || depths[id] === undefined) return;
      const box = document.createElement("div");
      const has = visited.has(id);
      const open = revealed.has(id);
      box.className = "map-node"
        + (has ? " seen" : open ? " locked" : " fog")
        + (node.is_ending && has ? " ending" : "")
        + (id === current ? " current" : "")
        + (open ? " clickable" : "");
      box.style.left = place.x + "%";
      box.style.top = place.y + "%";
      const tag = document.createElement("span");
      tag.className = "tag";
      const label = document.createElement("span");
      if (open) {
        label.textContent = node.title;
        tag.textContent = node.is_ending ? "结局" : has ? "已看" : "可去";
      } else {
        label.textContent = "？";
        tag.textContent = "未解锁";
      }
      box.append(label, tag);
      if (open && !has) {
        box.addEventListener("click", () => {
          S.mapReturnTo = "play";
          playNode(id, null);
        });
      }
      dom["map-nodes"].appendChild(box);
    });
  }

  function isChildOf(edge, timelineId) {
    const parent = S.bundle.nodes[timelineId];
    return !!parent && parent.children.indexOf(edge.target_timeline_id) >= 0;
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
      const result = await global.Api.reset();
      await refreshProgress();
      renderTitle();
      const deleted = (result && result.deleted) || {};
      toast("已清空(删除 " + (deleted.visits || 0) + " 条访问记录)");
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

  async function boot() {
    try {
      const bundle = await global.Api.bundle();
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window);
