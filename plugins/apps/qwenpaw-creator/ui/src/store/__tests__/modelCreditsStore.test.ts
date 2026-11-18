import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import {
  MODEL_QUOTA_ERROR_CODE,
  useModelCreditsStore,
} from "@/store/modelCreditsStore";
import { installMockFetch } from "@/test/mockFetch";
import { bootstrapRoutes, ev, sessionView } from "@/test/sessionEventBuilders";

const STORAGE_KEY = "qwenpaw-creator:model-credits:v1";

const quotaFailure = {
  code: MODEL_QUOTA_ERROR_CODE,
  message:
    "Creator AgentScope model request failed: Error code: 403 - " +
    "{'error': {'code': 'ASP.BIZ.CREDITS_INSUFFICIENT'}}",
  retryable: false,
};

const session = () => useCreatorSessionStore.getState();
const notice = () => useModelCreditsStore.getState().notice;

describe("cross-project Credits notice", () => {
  let written: Map<string, string>;

  beforeEach(() => {
    written = new Map();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => written.get(key) ?? null,
        setItem: (key: string, value: string) => void written.set(key, value),
        removeItem: (key: string) => void written.delete(key),
        clear: () => written.clear(),
      },
    });
    useModelCreditsStore.setState({ notice: null });
    session().reset();
    useCreatorSessionStore.setState({
      projectId: "p1",
      lastEventSeq: 0,
      session: sessionView(),
    });
  });

  it("raises on a Credits refusal and mirrors it to storage", () => {
    // The failure tray only exists inside the project that failed, so without
    // this the project list kept offering 生成 while no model call anywhere
    // could still succeed.
    act(() =>
      session().ingestEvent(
        ev(1, "agent.run.failed", { runId: "r1", error: quotaFailure }),
      ),
    );

    expect(notice()?.projectId).toBe("p1");
    expect(written.get(STORAGE_KEY)).toContain("p1");
  });

  it("stays silent for a failure the Agent can act on", () => {
    act(() =>
      session().ingestEvent(
        ev(1, "agent.run.failed", {
          runId: "r1",
          error: { ...quotaFailure, code: "MODEL_REQUEST_FAILED" },
        }),
      ),
    );

    expect(notice()).toBeNull();
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it("drops once a run reaches its end, which proves the balance recovered", () => {
    act(() =>
      session().ingestEvent(
        ev(1, "agent.run.failed", { runId: "r1", error: quotaFailure }),
      ),
    );
    act(() =>
      session().ingestEvent(ev(2, "agent.run.completed", { runId: "r1" })),
    );

    expect(notice()).toBeNull();
    // Removing from storage too, or the next reload would resurrect it.
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it("re-raises when a snapshot still carries the refusal after a reload", async () => {
    // The notice lives across projects while the server keeps the error on one
    // session; reopening that project has to bring the pill back.
    installMockFetch(
      bootstrapRoutes({
        session: {
          status: "ERROR",
          lastEventSeq: 4,
          error: quotaFailure,
        },
      }),
    );

    await session().bootstrap("p1");

    expect(notice()?.projectId).toBe("p1");
  });

  it("keeps the original timestamp while the same project keeps failing", () => {
    useModelCreditsStore.getState().markExhausted("p1");
    const raisedAt = notice()?.at;

    // A run that keeps dying must not make the notice look freshly raised.
    useModelCreditsStore.getState().markExhausted("p1");
    expect(notice()).toEqual({ projectId: "p1", at: raisedAt });

    // Another project proving the same condition moves the jump target.
    useModelCreditsStore.getState().markExhausted("p2");
    expect(notice()?.projectId).toBe("p2");
  });
});
