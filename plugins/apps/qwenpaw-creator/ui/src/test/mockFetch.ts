import { vi } from "vitest";

export interface MockFetchCall {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

export interface MockRoute {
  ok?: boolean;
  status?: number;
  json?: unknown;
  headers?: Record<string, string>;
}

/**
 * 安装一个可断言的 fetch mock。routes 以「路径包含子串」匹配，返回对应响应。
 * 记录每次调用的 url/method/body，供断言「按钮触发了哪个请求」。
 */
export function installMockFetch(
  routes: Array<{ match: string; method?: string; response: MockRoute }>,
) {
  const calls: MockFetchCall[] = [];

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init.method || "GET").toUpperCase();
      let body: unknown = undefined;
      if (typeof init.body === "string") {
        try {
          body = JSON.parse(init.body);
        } catch {
          body = init.body;
        }
      } else if (init.body instanceof FormData) {
        const obj: Record<string, unknown> = {};
        (init.body as FormData).forEach((v, k) => {
          obj[k] = v;
        });
        body = obj;
      }
      const headers: Record<string, string> = {};
      new Headers(init.headers).forEach((v, k) => {
        headers[k] = v;
      });
      calls.push({ url, method, body, headers });

      const route = routes.find(
        (r) =>
          url.includes(r.match) &&
          (!r.method || r.method.toUpperCase() === method),
      );
      const resp = route?.response ?? { ok: true, status: 200, json: {} };
      return {
        ok: resp.ok ?? true,
        status: resp.status ?? 200,
        statusText: "OK",
        headers: new Headers(resp.headers),
        json: async () => resp.json ?? {},
        blob: async () => new Blob(),
      } as Response;
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}
