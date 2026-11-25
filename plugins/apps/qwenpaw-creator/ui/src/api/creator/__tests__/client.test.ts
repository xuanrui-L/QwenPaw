import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CreatorHttpError, creatorRequest } from "@/api/creator/client";
import i18n from "@/i18n";
import {
  reportBackendAvailable,
  reportBackendUnavailable,
} from "@/lib/backendHealthSignal";
import { useCreatorBackendHealthStore } from "@/store/creatorBackendHealthStore";
import { copyProject, listProjects } from "@/api/creator/projects";

/**
 * v2.0.6 problem 3: after an upgrade the host SPA catch-all answered Creator
 * requests with a plain-text 405, and users were shown the raw English status.
 * The client now translates that (and a non-envelope 404) into an actionable
 * BACKEND_UNAVAILABLE diagnostic and flags the backend-health store.
 */

function installFetch(make: () => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(make())),
  );
}

function resetHealth(): void {
  reportBackendAvailable();
  useCreatorBackendHealthStore.setState({
    phase: "checking",
    probing: false,
    everReady: false,
    error: null,
    checkedAt: null,
  });
}

describe("creator client backend-unavailable translation", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    resetHealth();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetHealth();
  });

  it("translates a plain-text 405 into BACKEND_UNAVAILABLE and flags the store", async () => {
    installFetch(
      () =>
        new Response("Method Not Allowed", {
          status: 405,
          statusText: "Method Not Allowed",
        }),
    );

    const settled = creatorRequest("/projects/p1/project");
    await expect(settled).rejects.toBeInstanceOf(CreatorHttpError);
    await expect(settled).rejects.toMatchObject({
      status: 405,
      code: "BACKEND_UNAVAILABLE",
      retryable: true,
      userMessage: i18n.t("api.backendUnavailable"),
      // The raw host status text never reaches the user.
      message: i18n.t("api.backendUnavailable"),
    });
    expect(useCreatorBackendHealthStore.getState().phase).toBe("unavailable");
  });

  it("keeps a real JSON 405 from the plugin backend and leaves health alone", async () => {
    installFetch(
      () =>
        new Response(JSON.stringify({ detail: "Method Not Allowed" }), {
          status: 405,
          headers: { "content-type": "application/json" },
        }),
    );

    await expect(creatorRequest("/projects/p1/project")).rejects.toMatchObject({
      status: 405,
      message: "Method Not Allowed",
    });
    expect(useCreatorBackendHealthStore.getState().phase).toBe("checking");
  });

  it("translates a non-Creator 404 the same way", async () => {
    installFetch(
      () =>
        new Response("<html>Not Found</html>", {
          status: 404,
          headers: { "content-type": "text/html" },
        }),
    );

    await expect(creatorRequest("/health")).rejects.toMatchObject({
      status: 404,
      code: "BACKEND_UNAVAILABLE",
      retryable: true,
    });
    expect(useCreatorBackendHealthStore.getState().phase).toBe("unavailable");
  });

  it("keeps a real Creator 404 envelope verbatim and leaves health alone", async () => {
    installFetch(
      () =>
        new Response(
          JSON.stringify({ code: "NOT_FOUND", message: "项目不存在" }),
          { status: 404, headers: { "content-type": "application/json" } },
        ),
    );

    await expect(creatorRequest("/projects/missing")).rejects.toMatchObject({
      status: 404,
      code: "NOT_FOUND",
      userMessage: "项目不存在",
    });
    expect(useCreatorBackendHealthStore.getState().phase).toBe("checking");
  });

  it("clears the unavailable phase as soon as a real response arrives", async () => {
    reportBackendUnavailable();
    expect(useCreatorBackendHealthStore.getState().phase).toBe("unavailable");

    installFetch(
      () =>
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );

    await expect(creatorRequest("/health")).resolves.toEqual({ ok: true });
    const state = useCreatorBackendHealthStore.getState();
    expect(state.phase).toBe("ready");
    expect(state.everReady).toBe(true);
    expect(state.error).toBeNull();
  });
});

/**
 * The measured duplication this guards against: one Project page issued the
 * execution-authorization lookup five times, the active-review lookup three
 * and the Project document three, while the backend's GIL sat at 96% busy.
 */
describe("creatorRequest in-flight dedupe", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  function installFetch(payload: unknown) {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        urls.push(String(input));
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );
    return urls;
  }

  it("shares one GET while it is still outstanding", async () => {
    const urls = installFetch({ ok: true });
    const path = "/projects/p-1/execution-authorizations?status=PENDING";

    const [first, second, third] = await Promise.all([
      creatorRequest(path),
      creatorRequest(path),
      creatorRequest(path),
    ]);

    expect(urls).toHaveLength(1);
    expect(second).toEqual(first);
    expect(third).toEqual(first);
  });

  it("does a fresh list read when returning from a successful copy", async () => {
    let finishList!: (response: Response) => void;
    const oldList = new Promise<Response>((resolve) => {
      finishList = resolve;
    });
    const fetch = vi
      .fn()
      .mockReturnValueOnce(oldList)
      .mockResolvedValueOnce(Response.json({ projectId: "copy" }))
      .mockResolvedValueOnce(Response.json({ items: [{ projectId: "copy" }] }));
    vi.stubGlobal("fetch", fetch);
    const original = listProjects();
    await copyProject("source", "retry-key");
    const afterCopy = listProjects();
    finishList(Response.json({ items: [] }));
    expect((await original).items).toEqual([]);
    expect((await afterCopy).items).toEqual([{ projectId: "copy" }]);
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("issues a fresh request once the previous one settles", async () => {
    const urls = installFetch({ ok: true });

    await creatorRequest("/models/tts-capabilities");
    await creatorRequest("/models/tts-capabilities");

    expect(urls).toHaveLength(2);
  });

  it("never dedupes a different path, a body, or a caller's own signal", async () => {
    const urls = installFetch({ ok: true });

    await Promise.all([
      creatorRequest("/a"),
      creatorRequest("/b"),
      creatorRequest("/a", { method: "POST", body: "{}" }),
      creatorRequest("/a", { signal: new AbortController().signal }),
    ]);

    expect(urls).toHaveLength(4);
  });

  it("shares the failure too, so a 500 cannot stampede the server", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        urls.push(String(input));
        return Promise.resolve(
          new Response(JSON.stringify({ code: "STORAGE_INTEGRITY_ERROR" }), {
            status: 503,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );

    const settled = await Promise.allSettled([
      creatorRequest("/media/artifacts/artifact-version-1"),
      creatorRequest("/media/artifacts/artifact-version-1"),
    ]);

    expect(urls).toHaveLength(1);
    expect(settled.every((entry) => entry.status === "rejected")).toBe(true);
  });
});
