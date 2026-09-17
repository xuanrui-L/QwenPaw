/* Behavior-only controller. Every work-specific element and style comes from
 * the reviewed Agent HTML. No page/choice templates or visual fallbacks. */
(function (global) {
  "use strict";
  function mount(container, bundle, adapter = {}) {
    const owner = container.ownerDocument;
    container.replaceChildren();
    if (!bundle.authored_html) {
      container.textContent = "作品页面尚未生成，请在 Creator 生成并审阅后重新导出。";
      return { dispose() {} };
    }
    const frame = owner.createElement("iframe");
    frame.title = bundle.meta.title;
    frame.allowFullscreen = true;
    frame.setAttribute("sandbox", "allow-same-origin");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.style.cssText = "width:100%;height:100%;border:0;display:block";
    container.append(frame);
    let doc, video, slot, choice, screen = "title", returnScreen = "title";
    // Returning from the map must respect a viewer's manual pause.
    let returnWasPlaying = false;
    let current = null, closed = false, busy = false, answered = false, watched = 0;
    let authoredTitle = "", authoredTeaser = "", mapNodes = [], choiceOpening = null, transition = 0;
    const edges = bundle.edges || bundle.edge_index || {};
    const progress = Object.assign({visited: [], endings: [], current_timeline: ""}, adapter.progress || {});
    const report = error => {
      adapter.onError?.(error);
      const alert = owner.createElement("p");
      alert.setAttribute("role", "alert");
      alert.textContent = "播放失败：" + String(error.message || error);
      frame.style.height = "85%";
      container.prepend(alert);
    };
    const call = async (name, ...args) => { if (!closed) return adapter[name]?.(...args); };
    const persist = () => call("save", {...progress});
    const all = selector => [...doc.querySelectorAll(selector)];
    function shortName(value) {
      const name = String(value || "片段").trim().split(/[：:。\n]/)[0];
      return /^结局[\s\d一二三四五六七八九十A-Z]/i.test(name) ? "结局" : [...name].slice(0, 12).join("");
    }
    const hiddenMapStyles = new WeakMap();
    function setMapHidden(el, hidden) {
      el.toggleAttribute("data-host-hidden", hidden);
      if (hidden) {
        if (!hiddenMapStyles.has(el)) hiddenMapStyles.set(el, [el.style.getPropertyValue("display"), el.style.getPropertyPriority("display")]);
        // Authored !important layout rules must not expose future nodes.
        el.style.setProperty("display", "none", "important");
      } else if (hiddenMapStyles.has(el)) {
        const [value, priority] = hiddenMapStyles.get(el);
        if (value) el.style.setProperty("display", value, priority);
        else el.style.removeProperty("display");
        hiddenMapStyles.delete(el);
      }
    }
    function bindMap() {
      const visited = new Set(progress.visited.filter(id => bundle.nodes[id]));
      const visible = new Set(visited);
      if (!visited.size) visible.add(bundle.entry_timeline_id);
      visited.forEach(id => (bundle.nodes[id].children || []).forEach(child => visible.add(child)));
      mapNodes.forEach(({el, id, label, template, jumpLabel}) => {
        const known = visited.has(id);
        setMapHidden(el, !visible.has(id));
        el.toggleAttribute("data-visited", known);
        el.toggleAttribute("data-current", id === current);
        el.toggleAttribute("data-unknown", !known);
        el.removeAttribute("title");
        el.setAttribute("aria-label", known ? label.textContent : "未探索");
        // Preserve the author's layout/chips/connectors, but disclose only
        // the short label and the navigation control. No synopsis or tooltip
        // survives, even when old HTML mixed them into the node container.
        const copy = template.cloneNode(true);
        const scrub = node => {
          if (node.nodeType === 3) { node.textContent = ""; return; }
          if (node.nodeType === 1) {
            node.removeAttribute("title"); node.removeAttribute("aria-label");
            node.removeAttribute("data-bind");
          }
          [...node.childNodes].forEach(scrub);
        };
        scrub(copy);
        let text = copy.querySelector("[data-node-label]");
        if (!text) { text = label.cloneNode(false); copy.prepend(text); }
        // Legacy templates put the node name inside their jump button. Keep
        // the name independently visible when that control is removed or
        // rewritten, without flattening the rest of the authored layout.
        const labelButton = text.closest("button[data-action]");
        if (labelButton && labelButton !== copy) labelButton.before(text);
        text.textContent = known ? label.textContent : "？";
        if (!known) {
          // Authors sometimes hide real labels and draw a separate question
          // mark. The host owns this placeholder and must keep it visible.
          text.style.setProperty("display", "inline-block", "important");
          text.style.setProperty("visibility", "visible", "important");
          text.style.setProperty("opacity", "1", "important");
        }
        copy.querySelectorAll("button[data-action]").forEach(button => {
          if (!known) button.remove();
          else { button.disabled = false; button.textContent = jumpLabel; }
        });
        el.replaceChildren(...copy.childNodes);
        if (el.matches('button[data-action="jump"]')) el.disabled = !known;
      });
      all('[data-screen="map"] [data-map-from], [data-screen="map"] [data-map-to]').forEach(el => {
        setMapHidden(el, !(visited.has(el.dataset.mapFrom) && visible.has(el.dataset.mapTo)));
      });
    }
    function bind() {
      const node = bundle.nodes[current] || {};
      const values = {"project.title": bundle.meta.title_source === "user" ? bundle.meta.title : authoredTitle || bundle.meta.title,
        "project.synopsis": authoredTeaser, "node.title": current && !(node.children || []).length ? "结局" : node.title,
        "node.synopsis": node.synopsis, "progress.visited": progress.visited.length,
        "progress.endings": ""};
      all("[data-bind]").forEach(el => {
        if (el.closest('[data-screen="map"] [data-node-ref]')) return;
        if (!el.querySelector("button,video,[data-slot]"))
          el.textContent = el.dataset.bind === "node.title" && el.closest('[data-screen="ending"]')
            ? "结局" : String(values[el.dataset.bind] ?? "");
      });
      bindMap();
      all('[data-action="resume"]').forEach(el => {
        el.disabled = !bundle.nodes[progress.current_timeline];
        el.toggleAttribute("data-host-hidden", el.disabled);
      });
    }
    async function exitVideoFullscreen() {
      // Standard fullscreen can belong to the child document or its host.
      for (const surface of new Set([doc, owner])) {
        const full = surface.fullscreenElement || surface.webkitFullscreenElement;
        if (full && (full === video || full === frame || full.contains?.(video) || full.contains?.(frame))) {
          const exit = surface.exitFullscreen || surface.webkitExitFullscreen;
          if (exit) await exit.call(surface);
        }
      }
      // iPhone/Safari native media fullscreen uses a separate presentation API.
      if (video.webkitDisplayingFullscreen || video.webkitPresentationMode === "fullscreen") {
        await new Promise((resolve, reject) => {
          let timer;
          const cleanup = () => {
            clearTimeout(timer);
            video.removeEventListener("webkitendfullscreen", finish);
            video.removeEventListener("webkitpresentationmodechanged", changed);
          };
          const finish = () => { cleanup(); resolve(); };
          const changed = () => { if (video.webkitPresentationMode !== "fullscreen") finish(); };
          video.addEventListener("webkitendfullscreen", finish, {once: true});
          video.addEventListener("webkitpresentationmodechanged", changed);
          timer = setTimeout(() => {
            if (!video.webkitDisplayingFullscreen && video.webkitPresentationMode !== "fullscreen") finish();
            else { cleanup(); reject(new Error("请退出视频全屏以显示抉择按钮")); }
          }, 1500);
          try {
            if (video.webkitExitFullscreen) video.webkitExitFullscreen();
            else video.webkitSetPresentationMode?.("inline");
          } catch (error) { cleanup(); reject(error); }
        });
      }
    }
    function matchVideoBackground() {
      if (!video.videoWidth || !video.videoHeight) return;
      try {
        const canvas = doc.createElement("canvas"); canvas.width = 16; canvas.height = 9;
        const ctx = canvas.getContext("2d", {willReadFrequently: true});
        if (!ctx) return;
        ctx.drawImage(video, 0, 0, 16, 9);
        const pixels = ctx.getImageData(0, 0, 16, 9).data, rgb = [0, 0, 0];
        for (let i = 0; i < pixels.length; i += 4) for (let c = 0; c < 3; c++) rgb[c] += pixels[i + c];
        const color = "rgb(" + rgb.map(v => Math.round(v / 144 * 0.22)).join(",") + ")";
        doc.documentElement.style.setProperty("--player-video-background", color);
        doc.querySelector('[data-screen="play"]')?.style.setProperty("background-color", color);
      } catch (_) { /* Cross-origin media keeps the authored palette. */ }
    }
    function show(name) {
      screen = name;
      all("[data-screen]").forEach(el => el.toggleAttribute("data-host-hidden", el.dataset.screen !== name));
      doc.documentElement.dataset.currentScreen = name;
      choice?.pause(name !== "play" || owner.hidden);
      if (name !== "play") video.pause();
      bind();
      adapter.onScreen?.(name);
    }
    function play() { if (screen === "play" && !choice && !choiceOpening && !owner.hidden) video.play().catch(() => {}); }
    async function flush() {
      if (current && watched > 0) { const seconds = watched; watched = 0; await call("watch", current, seconds); }
    }
    function clearChoice() { transition++; choiceOpening = null; choice?.dispose(); choice = null; slot.toggleAttribute("data-host-hidden", true); }
    async function go(id, edgeRef) {
      if (!bundle.nodes[id]) throw new Error("未知剧情节点");
      await flush();
      clearChoice(); answered = false; current = id;
      await call("visit", id, edgeRef);
      if (closed) return;
      if (!progress.visited.includes(id)) progress.visited.push(id);
      progress.current_timeline = id;
      await persist();
      video.src = adapter.segmentUrl ? adapter.segmentUrl(id, bundle.nodes[id])
        : bundle.segments[id];
      video.load();
      show("play"); play();
    }
    function pointHere() { return (bundle.interactions || []).find(p => p.source_timeline_id === current); }
    async function openChoice(point) {
      if (choice || answered || choiceOpening || closed) return;
      const token = ++transition;
      choiceOpening = token;
      video.pause();
      try {
        await exitVideoFullscreen();
        if (closed || token !== transition) return;
        slot.removeAttribute("data-host-hidden");
        choice = global.IVBInteraction.mount(slot, point, edges, ref => run(async () => {
          const edge = edges[ref];
          if (!point.options.some(o => o.edge_ref === ref) || !bundle.nodes[edge?.target_timeline_id]) throw new Error("无效分支");
          await call("choice", current, ref);
          answered = true;
          await go(edge.target_timeline_id, ref);
        }));
        choice.pause(screen !== "play" || owner.hidden);
      } finally { if (choiceOpening === token) choiceOpening = null; }
    }
    async function end() {
      if (adapter.review || busy || screen !== "play") return;
      const point = pointHere();
      if (point && !answered) { await openChoice(point); return; }
      const children = bundle.nodes[current].children || [];
      if (children.length === 1) { await run(() => go(children[0])); return; }
      if (children.length > 1) throw new Error("分支缺少交互设计");
      await run(async () => {
        await exitVideoFullscreen();
        if (closed) return;
        await flush(); await call("ending", current);
        if (!progress.endings.includes(current)) progress.endings.push(current);
        await persist(); show("ending");
      });
    }
    async function run(fn) {
      if (closed || busy) return;
      busy = true;
      try { await fn(); } catch (error) { report(error); }
      finally { busy = false; }
    }
    async function action(button) {
      switch (button.dataset.action) {
        case "start": case "replay": await go(bundle.entry_timeline_id); break;
        case "resume": await go(progress.current_timeline); break;
        case "jump": if (progress.visited.includes(button.dataset.nodeRef)) await go(button.dataset.nodeRef); break;
        case "map":
          returnScreen = screen;
          returnWasPlaying = screen === "play" && !video.paused;
          show("map"); await flush(); break;
        case "map_back": show(returnScreen); if (returnWasPlaying) play(); break;
        case "title": show("title"); await flush(); break;
        case "toggle_play": if (video.paused) play(); else video.pause(); break;
        case "reset":
          video.pause(); clearChoice(); await flush(); await call("reset");
          Object.assign(progress, {visited: [], endings: [], current_timeline: ""});
          current = null; await persist(); show("title"); break;
      }
    }
    let previousTime = 0;
    function visibility() { choice?.pause(screen !== "play" || owner.hidden); if (owner.hidden) video?.pause(); }
    frame.onload = () => {
      if (closed) return;
      doc = frame.contentDocument;
      if (adapter.review) {
        const selectionStyle = doc.createElement("style");
        selectionStyle.textContent = "[data-editor-selected]{outline:2px solid #171717!important;outline-offset:3px!important;box-shadow:0 0 0 3px #fff!important}";
        doc.head.append(selectionStyle);
      }
      video = doc.querySelector("video[data-player-video]");
      slot = doc.querySelector('[data-slot="interaction"]');
      if (!video || !slot) { report(new Error("作品 HTML 播放接口缺失")); return; }
      video.playsInline = true;
      if (adapter.review) {
        // Design review consumes the same selected media as final playback.
        // Authored buttons still select their editor instead of navigating.
        video.controls = true;
      }
      authoredTitle = doc.querySelector('[data-screen="title"] [data-bind="project.title"]')?.textContent.trim() || "";
      authoredTeaser = doc.querySelector('[data-screen="title"] [data-bind="project.synopsis"]')?.textContent.trim() || "由你决定故事的下一步。";
      mapNodes = all('[data-screen="map"] [data-node-ref]').filter(el => !el.parentElement.closest("[data-node-ref]")).map(el => {
        const label = (el.querySelector("[data-node-label]") || doc.createElement("span")).cloneNode(false);
        label.setAttribute("data-node-label", "");
        label.textContent = shortName(el.querySelector("[data-node-label]")?.textContent || bundle.nodes[el.dataset.nodeRef]?.title);
        const jump = el.querySelector('button[data-action="jump"]');
        const jumpLabel = jump?.querySelector("[data-node-label]") ? "回看" : shortName(jump?.textContent || "回看");
        return {el, id: el.dataset.nodeRef, label, template: el.cloneNode(true), jumpLabel};
      });
      // Old graphs can draw all routes as one SVG. Hide unbound paths rather
      // than revealing the shape or number of unexplored descendants.
      all('[data-screen="map"] svg, [data-screen="map"] path, [data-screen="map"] line, [data-screen="map"] polyline').forEach(el => {
        if (!el.closest("[data-node-ref], [data-map-from]") && !el.querySelector("[data-map-from]")) setMapHidden(el, true);
      });
      const visibilityStyle = doc.createElement("style");
      visibilityStyle.textContent = '[data-host-hidden]{display:none!important}[data-screen="map"] [data-node-ref]::before,[data-screen="map"] [data-node-ref]::after,[data-screen="map"] [data-node-ref] *::before,[data-screen="map"] [data-node-ref] *::after{content:none!important}';
      doc.head.append(visibilityStyle);
      all("button[data-action]").forEach(button => { button.type = "button"; });
      doc.addEventListener("click", e => {
          const button = e.target.closest?.("button[data-action]");
          if (!button || button.disabled) return;
          e.preventDefault();
          if (adapter.review) {
            adapter.onInspect?.({screen: button.closest("[data-screen]").dataset.screen, action: button.dataset.action});
          } else void run(() => action(button));
      });
      video.addEventListener("loadeddata", matchVideoBackground);
      video.addEventListener("seeked", matchVideoBackground);
      video.addEventListener("play", () => { if (choice || choiceOpening || screen !== "play") video.pause(); });
      video.addEventListener("loadedmetadata", () => {
        previousTime = 0;
        if (adapter.review) video.currentTime = Math.max(0, Math.min(
          adapter.reviewPoint?.at_seconds || 0.04, video.duration - 0.04));
      });
      video.addEventListener("timeupdate", () => {
        const t = video.currentTime, delta = t - previousTime; previousTime = t;
        if (adapter.review || screen !== "play") return;
        if (!video.paused && delta > 0 && delta < 1) watched += delta;
        const point = pointHere();
        if (point && !answered && t >= point.at_seconds) void openChoice(point).catch(report);
      });
      video.addEventListener("ended", () => { void end().catch(report); });
      video.addEventListener("error", () => report(new Error("视频载入失败")));
      owner.addEventListener("visibilitychange", visibility);
      clearChoice(); show("title");
      adapter.onControls?.(all("button[data-action]").map(button => ({
        screen: button.closest("[data-screen]").dataset.screen,
        action: button.dataset.action, label: button.textContent.trim(),
      })));
      adapter.onReady?.();
    };
    // These rules implement visibility/isolation only. They contain no palette,
    // font, layout, card, navigation or animation decisions.
    const guard = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; media-src 'self' file: blob: http: https:; img-src data:; frame-src 'self' about:; form-action 'none'; base-uri 'none'"><style>[data-host-hidden]{display:none!important}</style>`;
    frame.srcdoc = bundle.authored_html.replace(/<head(?:\s[^>]*)?>/i, match => match + guard);
    return { dispose() { closed = true; transition++; choice?.dispose(); video?.pause(); owner.removeEventListener("visibilitychange", visibility); frame.remove(); },
      show(name) { if (doc && ["title", "play", "map", "ending"].includes(name)) {
        if (adapter.review) {
          current = name === "ending" ? Object.keys(bundle.nodes).find(id => !(bundle.nodes[id].children || []).length) : adapter.reviewPoint?.source_timeline_id || bundle.entry_timeline_id;
          if (name === "play") {
            const url = adapter.segmentUrl?.(current, bundle.nodes[current]) || "";
            const previousUrl = video.getAttribute("src") || "";
            if (url !== previousUrl) {
              if (url) video.setAttribute("src", url);
              else video.removeAttribute("src");
              video.load();
            }
            let missing = doc.querySelector("[data-editor-missing-media]");
            if (!url && !missing) {
              missing = doc.createElement("p");
              missing.setAttribute("data-editor-missing-media", "");
              missing.setAttribute("role", "status");
              missing.textContent = "当前节点尚未合成视频";
              video.after(missing);
            }
            if (url) missing?.remove();
          }
          if (name === "play" && adapter.reviewPoint && !choice) {
            slot.removeAttribute("data-host-hidden");
            choice = global.IVBInteraction.mount(slot, {...adapter.reviewPoint, review: true}, edges,
              ref => adapter.onChoiceInspect?.(ref));
          } else if (name !== "play" && choice) clearChoice();
        } else if (name === "ending" && !current) current = Object.keys(bundle.nodes).find(id => !(bundle.nodes[id].children || []).length);
        show(name);
      } },
      inspect(name, actionName) {
        if (!doc || !adapter.review) return;
        all("button[data-action]").forEach(button => {
          // Editor-only selection outline, never part of exported artwork.
          const selected = button.closest("[data-screen]").dataset.screen === name && button.dataset.action === actionName;
          button.toggleAttribute("data-editor-selected", selected);
          if (selected) {
            button.scrollIntoView({block: "nearest", inline: "nearest"});
          }
        });
      } };
  }
  function offline(bundle) {
    const key = "ivb:" + bundle.meta.bundle_id + ":" + bundle.content_revision;
    let progress;
    try { progress = JSON.parse(localStorage.getItem(key)); } catch (_) {}
    return mount(document.getElementById("player"), bundle, {progress,
      save(value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {} },
      segmentUrl(id) { return new URL(bundle.segments[id], document.baseURI).href; },
    });
  }
  global.IVBAuthoredPlayer = { mount, offline };
})(globalThis);
