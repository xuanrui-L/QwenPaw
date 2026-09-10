/* System library UI and the adapter for the Agent-authored work player. */
(function (global) {
  "use strict";
  const dom = Object.fromEntries(["screen-library", "lib-grid", "lib-empty", "lib-scope-all", "lib-scope-mine", "fatal"].map(id => [id, document.getElementById(id)]));
  function fail(title, items) { dom.fatal.hidden = false; dom.fatal.textContent = title + "：" + items.join("；"); }
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


  async function boot() {
    const pid = new URLSearchParams(location.search).get("p");
    if (!pid) { dom["screen-library"].hidden = false; wireLibrary(); await renderLibrary("all"); return; }
    try {
      const bundle = await global.Api.bundle(pid);
      const progress = await global.Api.progress(pid);
      document.title = bundle.meta.title;
      global.IVBAuthoredPlayer.mount(document.getElementById("player"), bundle, {
        progress,
        segmentUrl(id, node) { return global.Api.segmentUrl(pid, node.segment.split("/").pop()); },
        visit(id, edge) { return global.Api.visit(pid, id, edge); },
        watch(id, seconds) { return global.Api.watch(pid, id, seconds); },
        choice(id, edge) { return global.Api.choice(pid, id, edge); },
        ending(id) { return global.Api.ending(pid, id); },
        reset() { return global.Api.reset(pid); },
      });
    } catch (error) { fail("包无法放映", [error.message]); }
  }
  void boot();
})(window);
