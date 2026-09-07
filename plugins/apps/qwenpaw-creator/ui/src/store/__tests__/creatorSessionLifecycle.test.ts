import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { openCreatorEvents } from "@/api/creator/events";
import { toolCallPresentations } from "@/lib/creatorMessagePresentation";
import { msg } from "@/test/agentFixtures";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { installMockFetch } from "@/test/mockFetch";
import {
  bootstrapRoutes,
  ev,
  sessionView,
  testEventSources,
} from "@/test/sessionEventBuilders";

const store = () => useCreatorSessionStore.getState();
const sourceAt = (index: number) =>
  testEventSources()[index] as ReturnType<typeof testEventSources>[number] & {
    onopen: () => void;
    onerror: () => void;
  };
const sub = {
  parentActionId: "delegate-1",
  runId: "specialist-1",
  role: "source_intelligence_agent",
};

beforeEach(() => {
  store().reset();
  vi.useFakeTimers();
});
afterEach(() => {
  store().reset();
  vi.useRealTimers();
});

describe("Creator event transport lifecycle", () => {
  it("retains a superseded tool's terminal fact after more than 500 stream events and clears it across projects", async () => {
    installMockFetch(bootstrapRoutes());
    await store().bootstrap("p1");
    const source = sourceAt(0);
    source.onopen();
    source.emit(
      "agent.run.cancelled",
      ev(1, "agent.run.cancelled", { runId: "old-run", superseded: true }),
    );
    for (let seq = 2; seq <= 710; seq += 1) {
      source.emit("agent.tool_progress", ev(seq, "agent.tool_progress"));
    }
    await vi.advanceTimersByTimeAsync(20);
    const oldTool = msg({
      role: "assistant",
      source: "creator_agent",
      messageSeq: 1,
      metadata: {
        actionId: "old-call",
        runId: "old-run",
        toolCall: { name: "request_workgraph_execution", arguments: {} },
      },
    });
    expect(toolCallPresentations([oldTool], store().events)[0]).toMatchObject({
      status: "cancelled",
      superseded: true,
    });
    expect(store().events.length).toBeLessThan(710);
    installMockFetch(bootstrapRoutes({ session: { projectId: "p2" } }));
    await store().bootstrap("p2");
    expect(
      store().events.some((event) => event.type === "agent.run.cancelled"),
    ).toBe(false);
  });

  it("delivers named retry/resumed events and ignores queued callbacks after close", () => {
    const onEvent = vi.fn();
    const onOpen = vi.fn();
    const onError = vi.fn();
    const stream = openCreatorEvents("p1", 0, onEvent, onError, onOpen);
    const source = sourceAt(0);
    source.onopen();
    source.emit(
      "agent.model.rate_limit_retry",
      ev(1, "agent.model.rate_limit_retry"),
    );
    source.emit("subagent.resumed", ev(2, "subagent.resumed", sub));
    source.emit(
      "creation.checkpoint_required",
      ev(3, "creation.checkpoint_required"),
    );
    source.emit(
      "creation.checkpoint_decided",
      ev(4, "creation.checkpoint_decided"),
    );
    source.emit(
      "execution.authorization_decided",
      ev(5, "execution.authorization_decided"),
    );
    source.onerror();
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledTimes(5);
    stream.close();
    source.onopen();
    source.onerror();
    source.emit("subagent.resumed", ev(3, "subagent.resumed", sub));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledTimes(5);
  });

  it("connects only on open, preserves reconnecting across buffered events, and recovers without a new event", async () => {
    installMockFetch(bootstrapRoutes());
    await store().bootstrap("p1");
    const source = sourceAt(0);
    expect(store()).toMatchObject({
      connected: false,
      connectionState: "connecting",
    });
    source.onopen();
    expect(store()).toMatchObject({
      connected: true,
      connectionState: "connected",
      isReplaying: false,
    });
    source.emit("subagent.started", ev(1, "subagent.started", sub));
    source.onerror();
    await vi.advanceTimersByTimeAsync(16);
    expect(store().subagentActivities["delegate-1"].completed).toBe(false);
    expect(store()).toMatchObject({
      connected: false,
      connectionState: "reconnecting",
    });
    source.onopen();
    expect(store()).toMatchObject({
      connected: true,
      connectionState: "connected",
    });
  });

  it("ignores replaced stream callbacks and pending batches, including a same-project remount", async () => {
    installMockFetch(bootstrapRoutes());
    await store().bootstrap("p1");
    const oldSource = sourceAt(0);
    oldSource.onopen();
    oldSource.emit("subagent.started", ev(1, "subagent.started", sub));
    await store().bootstrap("p1");
    const nextSource = sourceAt(1);
    nextSource.onopen();
    oldSource.onerror();
    oldSource.onopen();
    await vi.advanceTimersByTimeAsync(20);
    expect(store()).toMatchObject({
      connected: true,
      connectionState: "connected",
      lastEventSeq: 0,
    });
    expect(store().subagentActivities).toEqual({});

    installMockFetch(bootstrapRoutes({ session: { projectId: "p2" } }));
    await store().bootstrap("p2");
    nextSource.onerror();
    nextSource.onopen();
    nextSource.emit("subagent.started", ev(2, "subagent.started", sub));
    await vi.advanceTimersByTimeAsync(20);
    expect(store()).toMatchObject({
      projectId: "p2",
      connected: false,
      connectionState: "connecting",
      lastEventSeq: 0,
    });
    expect(store().subagentActivities).toEqual({});
  });

  it("replays an idle mainline to restore detached work and finishes replay only at the durable cursor", async () => {
    installMockFetch(
      bootstrapRoutes({ session: { status: "IDLE", lastEventSeq: 3 } }),
    );
    await store().bootstrap("p1");
    const source = sourceAt(0);
    expect(source.url).toContain("after=0");
    source.onopen();
    source.emit("subagent.started", ev(1, "subagent.started", sub));
    await vi.advanceTimersByTimeAsync(16);
    expect(store().isReplaying).toBe(true);
    source.emit(
      "subagent.tool_started",
      ev(2, "subagent.tool_started", {
        ...sub,
        toolCallId: "tool-1",
        tool: "read_source_video",
      }),
    );
    source.emit(
      "session.status_changed",
      ev(3, "session.status_changed", { status: "IDLE" }),
    );
    await vi.advanceTimersByTimeAsync(16);
    expect(store().isReplaying).toBe(false);
    expect(store().subagentActivities["delegate-1"]).toMatchObject({
      completed: false,
      status: "RUNNING_MODEL",
    });
    expect(
      store().subagentActivities["delegate-1"].tools["specialist-1:tool-1"],
    ).toMatchObject({ status: "started", executing: true });
  });
});

describe("Independent activity state", () => {
  beforeEach(() =>
    useCreatorSessionStore.setState({
      projectId: "p1",
      session: sessionView(),
    }),
  );

  it.each(["IDLE", "ERROR", "CANCELLED"])(
    "does not terminate a detached specialist from mainline %s",
    (status) => {
      store().ingestEvents([
        ev(1, "subagent.started", sub),
        ev(2, "session.status_changed", { status }),
      ]);
      expect(store().subagentActivities["delegate-1"]).toMatchObject({
        completed: false,
        status: "RUNNING_MODEL",
      });
      expect(
        store().subagentActivities["delegate-1"].terminalKind,
      ).toBeUndefined();
    },
  );

  it("preserves queued, runtime waiting, resumed, review, and cancelled semantics", () => {
    store().ingestEvent(ev(1, "subagent.accepted", sub));
    expect(store().subagentActivities["delegate-1"].status).toBe("QUEUED");
    store().ingestEvent(ev(2, "subagent.waiting_runtime", sub));
    expect(store().subagentActivities["delegate-1"].status).toBe(
      "WAITING_RUNTIME",
    );
    store().ingestEvent(ev(3, "subagent.resumed", sub));
    expect(store().subagentActivities["delegate-1"].status).toBe(
      "RUNNING_MODEL",
    );
    store().ingestEvent(
      ev(4, "subagent.blocked", { ...sub, waitingReview: true }),
    );
    expect(store().subagentActivities["delegate-1"]).toMatchObject({
      completed: true,
      status: "BLOCKED",
      waitingReview: true,
    });
    store().ingestEvent(
      ev(5, "subagent.started", { ...sub, runId: "specialist-2" }),
    );
    store().ingestEvent(
      ev(6, "subagent.failed", {
        ...sub,
        runId: "specialist-2",
        marker: "FAILED",
        cancelled: true,
      }),
    );
    expect(store().subagentActivities["delegate-1"]).toMatchObject({
      completed: true,
      status: "CANCELLED",
      terminalKind: "CANCELLED",
    });
  });

  it("distinguishes argument preparation from execution and ignores late progress after completion", () => {
    const tool = { ...sub, toolCallId: "tool-1", tool: "read_source_video" };
    const readTool = () =>
      store().subagentActivities["delegate-1"].tools["specialist-1:tool-1"];
    store().ingestEvent(
      ev(1, "subagent.tool_progress", {
        ...tool,
        complete: true,
        receivedBytes: 1024,
      }),
    );
    expect(readTool()).toMatchObject({ status: "started", executing: false });
    store().ingestEvent(ev(2, "subagent.tool_started", tool));
    store().ingestEvent(ev(3, "subagent.tool_progress", tool));
    expect(readTool()).toMatchObject({ status: "started", executing: true });
    store().ingestEvent(
      ev(4, "subagent.tool_completed", { ...tool, state: "succeeded" }),
    );
    store().ingestEvent(ev(5, "subagent.tool_progress", tool));
    expect(readTool()).toMatchObject({ status: "succeeded", executing: false });
  });

  it.each(["resume", "failure"])(
    "clears a retry only on the matching run's %s",
    (outcome) => {
      useCreatorSessionStore.setState({
        projectId: "p1",
        session: sessionView(),
      });
      store().ingestEvent(
        ev(1, "agent.model.rate_limit_retry", {
          runId: "main-1",
          attempt: 2,
          maxAttempts: 5,
          delaySeconds: 2,
        }),
      );
      store().ingestEvent(
        ev(2, "agent.message_delta", {
          runId: "main-other",
          messageId: "m-other",
          deltaIndex: 0,
          delta: "ok",
        }),
      );
      store().ingestEvent(
        ev(3, "agent.run.completed", { runId: "main-other" }),
      );
      expect(store().rateLimitRetry).toMatchObject({
        runId: "main-1",
        attempt: 2,
        maxAttempts: 5,
        delaySeconds: 2,
        reason: "rate_limit",
      });
      if (outcome === "failure") {
        const error = {
          code: "MODEL_RATE_LIMITED",
          message: "模型遭遇限流，已重试5次仍无法访问",
          retryable: true,
          details: { retryCount: 5 },
        };
        store().ingestEvent(
          ev(4, "agent.run.failed", { runId: "main-1", error }),
        );
        expect(store().session?.error).toEqual(error);
      } else {
        store().ingestEvent(
          ev(4, "agent.message_delta", {
            runId: "main-1",
            messageId: "m1",
            deltaIndex: 0,
            delta: "ok",
          }),
        );
      }
      expect(store().rateLimitRetry).toBeNull();
    },
  );

  it("marks cancelled tools as cancelled and settles only the cancelled specialist", () => {
    store().ingestEvents([
      ev(1, "subagent.tool_started", {
        ...sub,
        toolCallId: "t1",
        tool: "read_source_video",
      }),
      ev(2, "subagent.tool_started", {
        ...sub,
        toolCallId: "t2",
        tool: "read_source_video",
      }),
      ev(3, "subagent.tool_completed", {
        ...sub,
        toolCallId: "t1",
        tool: "read_source_video",
        state: "cancelled",
      }),
      ev(4, "subagent.started", {
        ...sub,
        parentActionId: "delegate-2",
        runId: "specialist-2",
      }),
      ev(5, "subagent.failed", { ...sub, cancelled: true }),
    ]);
    const activity = store().subagentActivities["delegate-1"];
    expect(activity.tools["specialist-1:t1"].status).toBe("cancelled");
    expect(activity.tools["specialist-1:t2"]).toMatchObject({
      status: "cancelled",
      executing: false,
    });
    expect(store().subagentActivities["delegate-2"].completed).toBe(false);
  });

  it("clears retry when the same run recovers with a tool-only answer", () => {
    store().ingestEvent(
      ev(1, "agent.model.rate_limit_retry", {
        runId: "main-1",
        attempt: 1,
        maxAttempts: 5,
      }),
    );
    store().ingestEvent(
      ev(2, "agent.tool_progress", {
        runId: "main-other",
        toolCallId: "t2",
        messageId: "m2",
        tool: "read_project",
      }),
    );
    expect(store().rateLimitRetry?.runId).toBe("main-1");
    store().ingestEvent(
      ev(3, "agent.tool_progress", {
        runId: "main-1",
        toolCallId: "t1",
        messageId: "m1",
        tool: "read_project",
      }),
    );
    expect(store().rateLimitRetry).toBeNull();
  });

  it("rejects invalid retry counts instead of showing invented progress", () => {
    store().ingestEvents([
      ev(1, "agent.model.rate_limit_retry", {
        runId: "main-1",
        attempt: 0,
        maxAttempts: 5,
      }),
      ev(2, "agent.model.rate_limit_retry", {
        runId: "main-1",
        attempt: 9,
        maxAttempts: 5,
      }),
      ev(3, "agent.model.rate_limit_retry", {
        runId: "main-1",
        attempt: 1.5,
        maxAttempts: 5,
      }),
    ]);
    expect(store().rateLimitRetry).toBeNull();
  });
});

describe("Stop request lifecycle ownership", () => {
  function bind(projectId: string) {
    useCreatorSessionStore.setState({
      projectId,
      session: sessionView({ projectId }),
      stopping: false,
    });
  }

  function pendingResponse() {
    let resolve!: (value: Response) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<Response>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    return { promise, resolve, reject };
  }

  function acceptedStop(): Response {
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    } as Response;
  }

  it.each([
    ["success", "different project"],
    ["failure", "different project"],
    ["success", "same project reopened"],
    ["failure", "same project reopened"],
  ] as const)(
    "does not let an old stop %s alter the %s stop",
    async (outcome, destination) => {
      bind("p1");
      const oldRequest = pendingResponse();
      const newRequest = pendingResponse();
      const fetchMock = vi
        .fn()
        .mockReturnValueOnce(oldRequest.promise)
        .mockReturnValueOnce(newRequest.promise);
      vi.stubGlobal("fetch", fetchMock);
      const oldStop = store().stopAllAgents();
      expect(store()).toMatchObject({
        stopping: true,
        session: { status: "INTERRUPT_REQUESTED" },
      });
      if (destination === "same project reopened") store().reset();
      const nextProject = destination === "different project" ? "p2" : "p1";
      bind(nextProject);
      const newStop = store().stopAllAgents();
      if (outcome === "success") oldRequest.resolve(acceptedStop());
      else oldRequest.reject(new Error("obsolete stop error"));
      await expect(oldStop).resolves.toBeUndefined();
      expect(store()).toMatchObject({
        projectId: nextProject,
        stopping: true,
        session: { status: "INTERRUPT_REQUESTED" },
      });
      newRequest.resolve(acceptedStop());
      await newStop;
      expect(store()).toMatchObject({
        projectId: nextProject,
        stopping: false,
        session: { status: "INTERRUPT_REQUESTED" },
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it("does not revive state after reset while a stop is pending", async () => {
    bind("p1");
    const request = pendingResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => request.promise),
    );
    const stop = store().stopAllAgents();
    store().reset();
    request.resolve(acceptedStop());
    await stop;
    expect(store()).toMatchObject({
      projectId: null,
      session: null,
      stopping: false,
    });
  });

  it("rolls back only the current optimistic stop when its request fails", async () => {
    bind("p1");
    const request = pendingResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => request.promise),
    );
    const stop = store().stopAllAgents();
    request.reject(new Error("current stop failure"));
    await expect(stop).rejects.toThrow("current stop failure");
    expect(store()).toMatchObject({
      stopping: false,
      session: { status: "RUNNING" },
    });
  });

  it("preserves a newer SSE terminal state when the stop request later fails", async () => {
    bind("p1");
    const request = pendingResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => request.promise),
    );
    const stop = store().stopAllAgents();
    store().ingestEvent(
      ev(1, "session.status_changed", { status: "CANCELLED" }),
    );
    request.reject(new Error("late transport failure"));
    await expect(stop).rejects.toThrow("late transport failure");
    expect(store()).toMatchObject({
      stopping: false,
      session: { status: "CANCELLED" },
    });
  });

  it("sends only one stop while that lifecycle's request is pending", async () => {
    bind("p1");
    const request = pendingResponse();
    const fetchMock = vi.fn(() => request.promise);
    vi.stubGlobal("fetch", fetchMock);
    const stop = store().stopAllAgents();
    await store().stopAllAgents();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    request.resolve(acceptedStop());
    await stop;
    expect(store().stopping).toBe(false);
    // The transport acknowledgement alone does not prove terminal cancellation.
    expect(store().session?.status).toBe("INTERRUPT_REQUESTED");
  });
});

describe("model recovery visibility", () => {
  beforeEach(() =>
    useCreatorSessionStore.setState({
      projectId: "p1",
      session: sessionView(),
    }),
  );
  it("keeps input recovery distinct from network or rate-limit retries", () => {
    store().ingestEvent(
      ev(1, "agent.model.retry", {
        runId: "m1",
        reason: "context_recovery",
        attempt: 1,
        maxAttempts: 1,
        delaySeconds: 0,
      }),
    );
    expect(store().rateLimitRetry).toMatchObject({
      reason: "context_recovery",
      delaySeconds: 0,
    });
  });
  it("retains actual generic retry timing and clears on explicit recovery", () => {
    const event = {
      ...ev(1, "agent.model.retry", {
        runId: "m1",
        reason: "transient",
        attempt: 1,
        maxAttempts: 3,
        delaySeconds: 2,
      }),
      at: "2026-09-07T01:00:00Z",
    };
    store().ingestEvent(event);
    expect(store().rateLimitRetry).toMatchObject({
      reason: "transient",
      delaySeconds: 2,
      createdAt: event.at,
    });
    store().ingestEvent(ev(2, "agent.model.retry_recovered", { runId: "old" }));
    expect(store().rateLimitRetry).not.toBeNull();
    store().ingestEvent(ev(3, "agent.model.retry_recovered", { runId: "m1" }));
    expect(store().rateLimitRetry).toBeNull();
  });
  it("isolates concurrent specialist retries and ignores a terminal run's late retry", () => {
    const other = { ...sub, runId: "s2", parentActionId: "d2" };
    store().ingestEvents([
      ev(1, "subagent.started", sub),
      ev(2, "subagent.started", other),
      ev(3, "subagent.model.retry", {
        ...sub,
        attempt: 1,
        maxAttempts: 5,
        reason: "rate_limit",
        delaySeconds: 8,
      }),
      ev(4, "subagent.model.retry", {
        ...other,
        attempt: 2,
        maxAttempts: 3,
        reason: "transient",
        delaySeconds: 2,
      }),
      ev(5, "session.status_changed", { status: "IDLE" }),
      ev(6, "subagent.model.retry_recovered", other),
    ]);
    expect(store().subagentActivities["delegate-1"].modelRetry?.reason).toBe(
      "rate_limit",
    );
    expect(store().subagentActivities.d2.modelRetry).toBeNull();
    store().ingestEvent(ev(7, "subagent.completed", sub));
    store().ingestEvent(
      ev(8, "subagent.model.retry", { ...sub, attempt: 2, maxAttempts: 5 }),
    );
    expect(store().subagentActivities["delegate-1"].modelRetry).toBeNull();
  });
});
