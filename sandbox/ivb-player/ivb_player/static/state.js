/* 状态层客户端。所有进度落在服务端 SQLite,浏览器本地只缓存 project_id
 * 用于换包后失效判断 —— 不再像 demo 那样把进度整个塞 localStorage。
 *
 * 多包路由后每个端点都带 pid;挂载前缀 BASE 从 <base href>(document.baseURI)
 * 反推,使服务挂在子路径(如 /ivb/)时 API 与分段 URL 都带正确前缀。 */
(function (global) {
  "use strict";

  // 服务端按 root_path 注入 <base href>;去掉结尾斜杠得到挂载前缀。
  // 根挂载时 pathname="/" → BASE="";子路径 "/ivb/" → BASE="/ivb"。
  const BASE = (function () {
    const url = new URL(document.baseURI || global.location.href);
    return url.pathname.replace(/\/+$/, "");
  })();

  function projectPath(pid) {
    return "/api/projects/" + encodeURIComponent(pid);
  }

  async function request(method, path, body) {
    const options = { method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(BASE + path, options);
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.message);
      throw new Error(path + " → " + response.status + (detail ? " " + JSON.stringify(detail) : ""));
    }
    return payload;
  }

  const Api = {
    base: BASE,
    health: () => request("GET", "/api/health"),
    // 库页:scope=mine|all
    projects: (scope) =>
      request("GET", "/api/projects?scope=" + encodeURIComponent(scope || "all")),
    bundle: (pid) => request("GET", projectPath(pid) + "/bundle"),
    progress: (pid) => request("GET", projectPath(pid) + "/state/progress"),
    stats: (pid) => request("GET", projectPath(pid) + "/state/stats"),
    visit: (pid, timelineId, choiceEdge) =>
      request("POST", projectPath(pid) + "/state/visit", {
        timeline_id: timelineId,
        choice_edge: choiceEdge || null,
        watched_seconds: 0,
      }),
    watch: (pid, timelineId, seconds) =>
      request("POST", projectPath(pid) + "/state/watch", {
        timeline_id: timelineId,
        watched_seconds: Math.round(seconds * 10) / 10,
      }),
    choice: (pid, source, edgeRef) =>
      request("POST", projectPath(pid) + "/state/choice", {
        interaction_source: source,
        edge_ref: edgeRef,
      }),
    ending: (pid, timelineId) =>
      request("POST", projectPath(pid) + "/state/ending", { timeline_id: timelineId }),
    reset: (pid) => request("POST", projectPath(pid) + "/state/reset", {}),
    // stylesheets 由服务端返回绝对路径(/api/projects/{pid}/styles/x.css),
    // 这里只补挂载前缀。
    stylesheet: (href) => fetch(BASE + href).then((r) => (r.ok ? r.text() : "")),
    segmentUrl: (pid, name) =>
      BASE + projectPath(pid) + "/segments/" + encodeURIComponent(name),
  };

  global.Api = Api;
})(window);
