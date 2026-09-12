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
    frame.setAttribute("sandbox", "allow-same-origin");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.style.cssText = "width:100%;height:100%;border:0;display:block";
    container.append(frame);
    let doc, video, slot, choice, screen = "title", returnScreen = "title";
    // Returning from the map must respect a viewer's manual pause.
    let returnWasPlaying = false;
    let current = null, closed = false, busy = false, answered = false, watched = 0;
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
    function bind() {
      const node = bundle.nodes[current] || {};
      const values = {"project.title": bundle.meta.title,
        "project.synopsis": bundle.meta.synopsis, "node.title": node.title,
        "node.synopsis": node.synopsis, "progress.visited": progress.visited.length,
        "progress.endings": progress.endings.length};
      all("[data-bind]").forEach(el => {
        if (!el.querySelector("button,video,[data-slot]"))
          el.textContent = String(values[el.dataset.bind] ?? "");
      });
      all("[data-node-ref]").forEach(el => {
        const visited = progress.visited.includes(el.dataset.nodeRef);
        el.toggleAttribute("data-visited", visited);
        el.toggleAttribute("data-current", el.dataset.nodeRef === current);
        // The authored map owns disclosure/appearance of unvisited nodes.
        // Keep its route structure visible; navigation still requires a visit.
        if (el.dataset.action === "jump") el.disabled = !visited;
      });
      all('[data-action="resume"]').forEach(el => {
        el.disabled = !bundle.nodes[progress.current_timeline];
        el.toggleAttribute("data-host-hidden", el.disabled);
      });
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
    function play() { if (screen === "play" && !choice && !owner.hidden) video.play().catch(() => {}); }
    async function flush() {
      if (current && watched > 0) { const seconds = watched; watched = 0; await call("watch", current, seconds); }
    }
    function clearChoice() { choice?.dispose(); choice = null; slot.toggleAttribute("data-host-hidden", true); }
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
    function openChoice(point) {
      if (choice || answered) return;
      video.pause(); slot.removeAttribute("data-host-hidden");
      choice = global.IVBInteraction.mount(slot, point, edges, ref => run(async () => {
        const edge = edges[ref];
        if (!point.options.some(o => o.edge_ref === ref) || !bundle.nodes[edge?.target_timeline_id]) throw new Error("无效分支");
        await call("choice", current, ref);
        answered = true;
        await go(edge.target_timeline_id, ref);
      }));
      choice.pause(screen !== "play");
    }
    async function end() {
      if (adapter.review || busy || screen !== "play") return;
      const point = pointHere();
      if (point && !answered) { openChoice(point); return; }
      const children = bundle.nodes[current].children || [];
      if (children.length === 1) { await run(() => go(children[0])); return; }
      if (children.length > 1) throw new Error("分支缺少交互设计");
      await run(async () => {
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
      all("button[data-action]").forEach(button => {
        button.type = "button";
        button.addEventListener("click", e => {
          e.preventDefault();
          if (adapter.review) {
            adapter.onInspect?.({screen: button.closest("[data-screen]").dataset.screen, action: button.dataset.action});
          } else void run(() => action(button));
        });
      });
      video.addEventListener("play", () => { if (choice || screen !== "play") video.pause(); });
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
        if (point && !answered && t >= point.at_seconds) openChoice(point);
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
    return { dispose() { closed = true; choice?.dispose(); video?.pause(); owner.removeEventListener("visibilitychange", visibility); frame.remove(); },
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
