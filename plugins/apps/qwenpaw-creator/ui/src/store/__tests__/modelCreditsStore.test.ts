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
    useModelCreditsStore.setState({ notice: null, recovery: null });
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

  it("keeps the current Credits notice through historical failures and successes", () => {
    useCreatorSessionStore.setState({
      isReplaying: true,
      replayUntilSeq: 10,
      session: sessionView({ lastEventSeq: 10 }),
    });
    useModelCreditsStore.getState().markExhausted("p2");
    act(() =>
      session().ingestEvent(ev(9, "agent.run.failed", { error: quotaFailure })),
    );
    expect(notice()?.projectId).toBe("p2");
    act(() => session().ingestEvent(ev(10, "agent.run.completed")));
    expect(notice()?.projectId).toBe("p2");
    act(() => session().ingestEvent(ev(11, "agent.run.completed")));
    expect(notice()).toBeNull();
  });

  it("clears a persisted refusal when replay reaches a later success in the same session", () => {
    session().ingestEvent(ev(4, "agent.run.failed", { error: quotaFailure }));
    expect(JSON.parse(written.get(STORAGE_KEY)!)).toMatchObject({
      source: { seq: 4 },
    });
    session().reset();
    useCreatorSessionStore.setState({
      projectId: "p1",
      session: sessionView({ lastEventSeq: 7 }),
      isReplaying: true,
      replayUntilSeq: 7,
    });
    session().ingestEvent(ev(3, "agent.run.completed"));
    expect(notice()?.projectId).toBe("p1");
    session().ingestEvent(ev(7, "agent.run.completed"));
    expect(notice()).toBeNull();
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it.each(["2000-01-01", "2099-01-01"])(
    "ignores a legacy notice's browser clock (%s)",
    (at) => {
      const legacy = { projectId: "p2", at };
      written.set(STORAGE_KEY, JSON.stringify(legacy));
      useModelCreditsStore.setState({ notice: legacy });
      useCreatorSessionStore.setState({
        session: sessionView({ lastEventSeq: 3 }),
        isReplaying: true,
        replayUntilSeq: 3,
      });
      const completed = (seq: number) => ({
        ...ev(seq, "agent.run.completed"),
        at: "2026-11-18T10:00:00Z",
      });
      session().ingestEvent(completed(3));
      expect(notice()).toEqual(legacy);
      session().ingestEvent(completed(4));
      expect(notice()).toBeNull();
      expect(written.has(STORAGE_KEY)).toBe(false);
    },
  );

  it("accepts a live completion even if polling has already seen its sequence", async () => {
    const legacy = { projectId: "another", at: "2099-01-01T00:00:00Z" };
    useModelCreditsStore.setState({ notice: legacy });
    written.set(STORAGE_KEY, JSON.stringify(legacy));
    installMockFetch(bootstrapRoutes({ session: { lastEventSeq: 4 } }));
    await session().bootstrap("p1");
    installMockFetch(
      bootstrapRoutes({ session: { lastEventSeq: 5, status: "IDLE" } }),
    );
    await session().refreshSession();
    session().ingestEvent({
      ...ev(5, "agent.run.completed"),
      at: "2026-11-18T10:00:00Z",
    });
    expect(notice()).toBeNull();
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it.each([
    ["2026-11-18T09:00:00Z", false],
    ["2026-11-18T11:00:00Z", true],
  ] as const)(
    "reconciles an old notice once its own refusal replays at %s",
    async (refusedAt, remains) => {
      const legacy = { projectId: "p1", at: "2099-01-01T00:00:00Z" };
      useModelCreditsStore.setState({ notice: legacy });
      written.set(STORAGE_KEY, JSON.stringify(legacy));
      useCreatorSessionStore.setState({
        projectId: "p2",
        session: sessionView({ id: "s2", projectId: "p2", lastEventSeq: 2 }),
        replayUntilSeq: 2,
      });
      session().ingestEvent({
        ...ev(2, "agent.run.completed"),
        projectId: "p2",
        creatorSessionId: "s2",
        at: "2026-11-18T10:00:00Z",
      });
      expect(notice()).toEqual(legacy);
      session().reset();
      const error = { ...quotaFailure, details: { runId: "current" } };
      installMockFetch(
        bootstrapRoutes({
          session: { lastEventSeq: 3, status: "ERROR", error },
        }),
      );
      await session().bootstrap("p1");
      session().ingestEvent({
        ...ev(1, "agent.run.failed", { runId: "older", error: quotaFailure }),
        at: "2026-11-18T08:00:00Z",
      });
      expect(notice()).toEqual(legacy);
      session().ingestEvent({
        ...ev(2, "agent.run.failed", { runId: "current", error }),
        at: refusedAt,
      });
      session().ingestEvent(
        ev(3, "session.status_changed", { status: "ERROR" }),
      );
      expect(Boolean(notice())).toBe(remains);
      expect(written.has(STORAGE_KEY)).toBe(remains);
      if (remains) expect(notice()?.source?.at).toBe(refusedAt);
    },
  );

  it("reconciles a legacy notice from the original project's completed history", () => {
    const legacy = { projectId: "p1", at: "2099-01-01T00:00:00Z" };
    useModelCreditsStore.setState({ notice: legacy });
    written.set(STORAGE_KEY, JSON.stringify(legacy));
    useCreatorSessionStore.setState({
      session: sessionView({ status: "IDLE", lastEventSeq: 3 }),
      replayUntilSeq: 3,
    });
    session().ingestEvent({
      ...ev(1, "agent.run.failed", { error: quotaFailure }),
      at: "2026-11-18T09:00:00Z",
    });
    expect(notice()).toEqual(legacy);
    session().ingestEvent({
      ...ev(3, "agent.run.completed"),
      at: "2026-11-18T11:00:00Z",
    });
    expect(notice()).toBeNull();
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it.each([false, true])(
    "retains the last refusal after a stopped retry across replay batches (bound=%s)",
    (bound) => {
      const legacy = {
        projectId: "p1",
        at: "2099-01-01T00:00:00Z",
        ...(bound && {
          source: {
            sessionId: sessionView().id,
            seq: 1,
            at: "2026-11-18T09:00:00Z",
          },
        }),
      };
      useModelCreditsStore.setState({ notice: legacy });
      written.set(STORAGE_KEY, JSON.stringify(legacy));
      useCreatorSessionStore.setState({
        session: sessionView({ status: "CANCELLED", lastEventSeq: 5 }),
        replayUntilSeq: 5,
      });
      const refusal = (seq: number, hour: number) => ({
        ...ev(seq, "agent.run.failed", { error: quotaFailure }),
        at: new Date(Date.UTC(2026, 10, 18, hour)).toISOString(),
      });
      session().ingestEvent(refusal(1, 9));
      session().ingestEvent({
        ...ev(2, "agent.run.completed"),
        at: "2026-11-18T10:00:00Z",
      });
      expect(notice()).toEqual(legacy);
      session().ingestEvent(refusal(3, 11));
      session().ingestEvent(ev(5, "agent.run.cancelled", { runId: "retry" }));
      expect(notice()?.source?.seq).toBe(3);
      expect(JSON.parse(written.get(STORAGE_KEY)!).source.seq).toBe(3);
    },
  );

  it("reopens a source-bound project's history after another project's success", () => {
    const failed = (seq: number, hour: number) => ({
      ...ev(seq, "agent.run.failed", { error: quotaFailure }),
      at: new Date(Date.UTC(2026, 10, 18, hour)).toISOString(),
    });
    session().ingestEvent(failed(1, 8));
    session().reset();
    useCreatorSessionStore.setState({
      projectId: "p2",
      session: sessionView({ projectId: "p2", id: "s2", lastEventSeq: 1 }),
      replayUntilSeq: 1,
    });
    session().ingestEvent({
      ...ev(1, "agent.run.completed"),
      projectId: "p2",
      creatorSessionId: "s2",
      at: "2026-11-18T09:00:00Z",
    });
    expect(notice()).toBeNull();
    const receipt = JSON.parse(
      written.get("qwenpaw-creator:model-credits:last-success")!,
    );
    expect(receipt.clearedNotice).toBe(true);
    useModelCreditsStore.setState({ notice: null, recovery: receipt });

    const reopen = () => {
      session().reset();
      useCreatorSessionStore.setState({
        projectId: "p1",
        session: sessionView({ lastEventSeq: 5, status: "CANCELLED" }),
        replayUntilSeq: 5,
      });
      session().ingestEvent(failed(1, 8));
      session().ingestEvent({
        ...ev(2, "agent.run.completed"),
        at: "2026-11-18T10:00:00Z",
      });
      session().ingestEvent(failed(4, 11));
      session().ingestEvent(ev(5, "agent.run.cancelled", { runId: "retry" }));
    };
    reopen();
    expect(notice()?.source?.seq).toBe(4);
    useModelCreditsStore.getState().clear();
    reopen();
    expect(notice()).toBeNull();
    expect(written.has(STORAGE_KEY)).toBe(false);
  });

  it("orders another project's replay by server time", () => {
    const time = (microsecond: number) =>
      `2026-10-01T12:00:00.${String(microsecond).padStart(6, "0")}+00:00`;
    session().ingestEvent({
      ...ev(10, "agent.run.failed", { error: quotaFailure }),
      at: time(2),
    });
    session().reset();
    useCreatorSessionStore.setState({
      projectId: "p2",
      session: sessionView({ id: "s2", projectId: "p2", lastEventSeq: 2 }),
      isReplaying: true,
      replayUntilSeq: 2,
    });
    const completed = (seq: number, minute: number) => ({
      ...ev(seq, "agent.run.completed"),
      projectId: "p2",
      creatorSessionId: "s2",
      at: time(minute),
    });
    session().ingestEvent(completed(1, 1));
    expect(notice()?.projectId).toBe("p1");
    session().ingestEvent(completed(2, 3));
    expect(notice()).toBeNull();
  });

  it("keeps an older refusal snapshot from reviving a recovered balance", async () => {
    const at = (minute: number) =>
      new Date(Date.UTC(2026, 9, 1, 12, minute)).toISOString();
    const old = { ...quotaFailure, details: { refusedAt: at(1) } };
    session().ingestEvent({
      ...ev(1, "agent.run.failed", { error: old }),
      at: at(1),
    });
    session().reset();
    useCreatorSessionStore.setState({
      projectId: "p2",
      session: sessionView({ projectId: "p2", id: "s2" }),
    });
    session().ingestEvent({
      ...ev(1, "agent.run.completed"),
      projectId: "p2",
      creatorSessionId: "s2",
      at: at(2),
    });
    expect(notice()).toBeNull();
    expect(
      JSON.parse(written.get("qwenpaw-creator:model-credits:last-success")!)
        .projectId,
    ).toBe("p2");
    session().reset();
    installMockFetch(
      bootstrapRoutes({
        session: { lastEventSeq: 100, status: "ERROR", error: old },
      }),
    );
    await session().bootstrap("p1");
    expect(notice()).toBeNull();
    session().ingestEvent({
      ...ev(1, "agent.run.failed", { error: old }),
      at: at(1),
    });
    expect(notice()).toBeNull();
    session().ingestEvent({
      ...ev(101, "agent.run.failed", { error: quotaFailure }),
      at: at(3),
    });
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
