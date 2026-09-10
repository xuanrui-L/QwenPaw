/* Shared by Creator preview, exported file:// bundles and the IVB service.
 * The host owns navigation/timing. Generated CSS-only HTML gets no scripts,
 * network, forms or top navigation, and never supplies branch destinations. */
(function (global) {
  "use strict";
  function mount(container, point, edges, onSelect) {
    container.replaceChildren();
    const document = container.ownerDocument;
    const root = document.createElement("div");
    root.dataset.interactionView = "";
    root.style.cssText = "position:absolute;inset:0;overflow:hidden";
    if (point.base_frame_data_uri || point.base_frame_url) {
      const image = document.createElement("img");
      image.alt = ""; image.src = point.base_frame_data_uri || point.base_frame_url;
      image.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:cover";
      root.append(image);
    }
    container.append(root);
    let closed = false, chosen = false, paused = false, frame = null;
    let remaining = Number(point.countdown_seconds || 0), last = performance.now();
    const select = (ref) => {
      if (closed || chosen || paused || document.hidden || !point.options.some(o => o.edge_ref === ref) || !edges[ref]) return;
      chosen = !point.review;
      onSelect(ref);
    };
    function hotspot(button, option) {
      const h = option.hotspot;
      if (!h) return;
      const x = h.x ?? .5, y = h.y ?? .5, w = h.width ?? 1, hgt = h.height ?? 1;
      const ax = h.anchor_x ?? .5, ay = h.anchor_y ?? .5;
      button.style.cssText += `;position:fixed!important;left:${(x-w*ax)*100}%!important;top:${(y-hgt*ay)*100}%!important;width:${w*100}%!important;height:${hgt*100}%!important;margin:0!important;box-sizing:border-box!important;transform:rotate(${h.rotation_degrees || 0}deg)!important;opacity:${h.opacity ?? 1}!important;z-index:10!important;`;
    }
    function invalid() {
      chosen = true;
      root.replaceChildren(document.createTextNode("缺少有效的 Agent 交互页面，请返回 Creator 生成并审阅。"));
      root.setAttribute("role", "alert");
    }
    let count = null, ready = false;
    const showCount = () => { if (count) count.textContent = Math.ceil(Math.max(0, remaining)) + "s"; };
    if (point.motion_html) {
      frame = document.createElement("iframe");
      frame.title = point.question || "交互动效预览";
      frame.setAttribute("sandbox", "allow-same-origin");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:0;background:transparent";
      const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; form-action 'none'; base-uri 'none'">`;
      const reset = "<style>html,body{margin:0!important;width:100%!important;height:100%!important;overflow:hidden!important;box-sizing:border-box}*,*::before,*::after{box-sizing:border-box}button[data-edge-ref]{cursor:pointer}html[data-paused] *,html[data-paused] *::before,html[data-paused] *::after{animation-play-state:paused!important}</style>";
      frame.onload = () => {
        if (closed) return;
        const doc = frame.contentDocument;
        if (!doc) return;
        // Models sometimes annotate the entire stage with data-question. Never
        // replace a container that owns the option controls with textContent.
        const question = [...doc.querySelectorAll("[data-question], .question")]
          .find(node => !node.matches("button") && !node.querySelector("button[data-edge-ref]"));
        if (question) question.textContent = point.question;
        let valid = true;
        const buttons = [...doc.querySelectorAll("button[data-edge-ref]")];
        if (buttons.length !== point.options.length) valid = false;
        point.options.forEach(option => {
          const matches = buttons.filter(b => b.dataset.edgeRef === option.edge_ref);
          if (matches.length !== 1) { valid = false; return; }
          const button = matches[0];
          button.type = "button";
          const copy = (edges[option.edge_ref] || {}).label || option.edge_ref;
          const label = button.querySelector("[data-option-label]");
          if (label) label.textContent = copy;
          else button.textContent = copy;
          button.setAttribute("aria-label", copy);
          hotspot(button, option);
          button.addEventListener("click", e => { e.preventDefault(); select(option.edge_ref); });
        });
        count = doc.querySelector("[data-interaction-countdown]");
        if (remaining > 0 && point.default_edge_ref && !count) valid = false;
        ready = valid; showCount();
        if (!valid) { frame.remove(); frame = null; invalid(); }
        else doc.documentElement.toggleAttribute("data-paused", paused || document.hidden);
      };
      const html = point.motion_html;
      frame.srcdoc = /<head(?:\s[^>]*)?>/i.test(html)
        ? html.replace(/<head(?:\s[^>]*)?>/i, match => match + policy + reset)
        : policy + reset + html;
      root.append(frame);
    } else invalid();
    function syncPause() {
      last = performance.now();
      frame?.contentDocument?.documentElement.toggleAttribute("data-paused", paused || document.hidden);
    }
    document.addEventListener("visibilitychange", syncPause);
    const timer = setInterval(() => {
      const now = performance.now(), elapsed = (now - last) / 1000;
      last = now;
      if (point.review || closed || chosen || !ready || paused || document.hidden || remaining <= 0) return;
      remaining = Math.max(0, remaining - elapsed);
      showCount();
      if (remaining === 0 && point.default_edge_ref) select(point.default_edge_ref);
    }, 50);
    return {
      pause(value) { paused = value; syncPause(); },
      dispose() { closed = true; clearInterval(timer); document.removeEventListener("visibilitychange", syncPause); root.remove(); },
    };
  }
  global.IVBInteraction = { mount };
})(globalThis);
