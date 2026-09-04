/* 状态层客户端。所有进度落在服务端 SQLite,浏览器本地只缓存 bundle_id
 * 用于换包后失效判断 —— 不再像 demo 那样把进度整个塞 localStorage。 */
(function (global) {
  "use strict";

  const BASE = "";

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
    bundle: () => request("GET", "/api/bundle"),
    health: () => request("GET", "/api/health"),
    progress: () => request("GET", "/api/state/progress"),
    stats: () => request("GET", "/api/state/stats"),
    visit: (timelineId, choiceEdge) =>
      request("POST", "/api/state/visit", {
        timeline_id: timelineId,
        choice_edge: choiceEdge || null,
        watched_seconds: 0,
      }),
    watch: (timelineId, seconds) =>
      request("POST", "/api/state/watch", {
        timeline_id: timelineId,
        watched_seconds: Math.round(seconds * 10) / 10,
      }),
    choice: (source, edgeRef) =>
      request("POST", "/api/state/choice", {
        interaction_source: source,
        edge_ref: edgeRef,
      }),
    ending: (timelineId) =>
      request("POST", "/api/state/ending", { timeline_id: timelineId }),
    reset: () => request("POST", "/api/state/reset", {}),
    stylesheet: (href) => fetch(BASE + href).then((r) => (r.ok ? r.text() : "")),
  };

  global.Api = Api;
})(window);
