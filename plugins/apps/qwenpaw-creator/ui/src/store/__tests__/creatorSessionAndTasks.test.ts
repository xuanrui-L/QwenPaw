import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  SpecialistRunListResponse,
  TaskListResponse,
  TaskView,
  CreatorEvent,
  CreatorMessage,
  CreatorSessionView,
} from "@/contracts/creator";
import { listTasks } from "@/api/creator";
import {
  ev,
  bootstrapRoutes,
  testEventSources,
} from "@/test/sessionEventBuilders";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { installMockFetch } from "@/test/mockFetch";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { act, waitFor } from "@testing-library/react";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorEditBufferStore } from "@/store/creatorEditBufferStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function response<T>(body: T): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

describe("creator task view refresh ordering", () => {
  beforeEach(() => {
    useCreatorTaskViewStore.getState().reset();
  });

  it("keeps a task admitted during a shared GET, then accepts a later authoritative absence", async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn((input: RequestInfo | URL) =>
      String(input).endsWith("/tasks")
        ? pending.promise
        : Promise.resolve(response({ items: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useCreatorTaskViewStore.getState();
    useCreatorTaskViewStore.setState({ projectId: "p1" });
    // The first caller can be outside this store. The later refreshes must use
    // the actual task GET's boundary even when the run GET starts later.
    const first = listTasks("p1");
    store.consumeEvent(
      ev(1, "task.created", {
        task: { id: "t1", projectId: "p1", status: "RUNNING" },
      }),
    );
    const joined = store.refresh("p1");
    pending.resolve(response({ items: [] }));
    await Promise.all([first, joined, store.refresh("p1")]);
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/tasks")),
    ).toHaveLength(1);
    expect(
      useCreatorTaskViewStore.getState().tasks.map((task) => task.id),
    ).toEqual(["t1"]);
    await store.refresh("p1");
    expect(useCreatorTaskViewStore.getState().tasks).toEqual([]);
  });

  it("retains a task from an independent read when a later caller joins an older GET", async () => {
    const slow = deferred<Response>();
    const task = { id: "new-task", status: "RUNNING" };
    let taskReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        if (!String(input).endsWith("/tasks"))
          return Promise.resolve(response({ items: [] }));
        return ++taskReads === 1
          ? slow.promise
          : Promise.resolve(response({ items: [task] }));
      }),
    );
    const first = listTasks("p1");
    const store = useCreatorTaskViewStore.getState();
    await store.refresh("p1", { dedupe: false });
    const joined = store.refresh("p1");
    slow.resolve(response({ items: [] }));
    await Promise.all([first, joined]);
    expect(taskReads).toBe(2);
    expect(useCreatorTaskViewStore.getState().tasks).toEqual([task]);
  });

  it("orders events within a millisecond across UTC offsets", () => {
    useCreatorTaskViewStore.setState({ projectId: "p1" });
    const store = useCreatorTaskViewStore.getState();
    const apply = (seq: number, status: string, updatedAt: string) =>
      store.consumeEvent(
        ev(seq, "task.updated", {
          task: { id: "t1", projectId: "p1", status, updatedAt },
        }),
      );
    apply(1, "SUCCEEDED", "2026-10-01T09:00:01.123999+00:00");
    apply(2, "RUNNING", "2026-10-01T17:00:01.123001+08:00");
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe(
      "SUCCEEDED",
    );
    apply(3, "CANCELLED", "2026-10-01T17:00:01.124000+08:00");
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe(
      "CANCELLED",
    );
  });

  it("does not let an older pending snapshot overwrite a newer terminal refresh", async () => {
    const oldRuns = deferred<Response>();
    const oldTasks = deferred<Response>();
    const newRuns = deferred<Response>();
    const newTasks = deferred<Response>();
    const runResponses = [oldRuns, newRuns];
    const taskResponses = [oldTasks, newTasks];
    let runRequest = 0;
    let taskRequest = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/specialist-runs"))
          return runResponses[runRequest++].promise;
        if (url.endsWith("/tasks")) return taskResponses[taskRequest++].promise;
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    // dedupe: false because this case needs two genuinely separate responses
    // to arrive out of order. Production cannot produce that shape -- two
    // overlapping reads of one path are served by one shared request now --
    // but refreshes seconds apart still race, which is what the generation
    // guard below protects.
    const olderRefresh = useCreatorTaskViewStore
      .getState()
      .refresh("p1", { dedupe: false });
    const newerRefresh = useCreatorTaskViewStore
      .getState()
      .refresh("p1", { dedupe: false });

    const terminalRuns: SpecialistRunListResponse = {
      items: [
        {
          id: "run-1",
          role: "ai_editing_director",
          displayName: "一致性质检 Agent",
          status: "SUCCEEDED",
          targetRefs: ["project:p1"],
          finalMarker: "SUCCESS",
          finalSummaryText: "质检完成。",
          taskRefs: [],
          metadata: {},
        },
      ],
    };
    const terminalTasks: TaskListResponse = { items: [] };
    newRuns.resolve(response(terminalRuns));
    newTasks.resolve(response(terminalTasks));
    await newerRefresh;
    expect(useCreatorTaskViewStore.getState().runs[0].status).toBe("SUCCEEDED");

    const staleRuns: SpecialistRunListResponse = {
      items: [
        {
          ...terminalRuns.items[0],
          status: "WAITING_AUTHORIZATION",
          finalMarker: undefined,
        },
      ],
    };
    oldRuns.resolve(response(staleRuns));
    oldTasks.resolve(response({ items: [] } satisfies TaskListResponse));
    await olderRefresh;

    expect(useCreatorTaskViewStore.getState().runs[0]).toMatchObject({
      id: "run-1",
      status: "SUCCEEDED",
      finalMarker: "SUCCESS",
    });
    expect(useCreatorTaskViewStore.getState().loading).toBe(false);
  });

  it.each([
    { scenario: "reopened project", expected: "RUNNING" },
    { scenario: "newer task update", expected: "SUCCEEDED" },
    { scenario: "same project polled", expected: "CANCELLED" },
  ])(
    "applies a delayed cancellation to the correct task state: $scenario",
    async ({ scenario, expected }) => {
      const cancelled = deferred<Response>();
      const task = {
        id: "t1",
        projectId: "p1",
        status: "RUNNING",
        updatedAt: "2026-10-01T09:00:01.100Z",
      };
      vi.stubGlobal(
        "fetch",
        vi.fn((url: string) => {
          if (url.endsWith("/cancel")) return cancelled.promise;
          const items = url.endsWith("/p1/tasks") ? [task] : [];
          return Promise.resolve(response({ items }));
        }),
      );
      const store = useCreatorTaskViewStore.getState();
      await store.refresh("p1");
      const pending = store.cancel("t1");
      if (scenario === "reopened project") {
        await store.refresh("p2");
        await store.refresh("p1");
      } else if (scenario === "newer task update") {
        store.consumeEvent(
          ev(1, "task.updated", {
            task: {
              ...task,
              status: "SUCCEEDED",
              updatedAt: "2026-10-01T09:00:01.300Z",
            },
          }),
        );
      } else {
        // Polling the same project preserves its in-flight user action.
        await store.refresh("p1");
      }
      cancelled.resolve(
        response({
          ...task,
          status: "CANCELLED",
          updatedAt: "2026-10-01T09:00:01.200Z",
        }),
      );
      await pending;
      expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe(expected);
    },
  );

  it("drops a cancel response after switching projects", async () => {
    const cancelled = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => cancelled.promise),
    );
    const task = (projectId: string, status: TaskView["status"]): TaskView => ({
      id: "reused-task-id",
      projectId,
      transactionId: null,
      specialistRunId: null,
      kind: "compose",
      targetRef: `project:${projectId}`,
      status,
      progress: null,
      resultRefs: [],
    });
    useCreatorTaskViewStore.setState({
      projectId: "p1",
      tasks: [task("p1", "RUNNING")],
    });

    const cancel = useCreatorTaskViewStore.getState().cancel("reused-task-id");
    useCreatorTaskViewStore.setState({
      projectId: "p2",
      tasks: [task("p2", "RUNNING")],
    });
    cancelled.resolve(response(task("p1", "CANCELLED")));
    await cancel;

    expect(useCreatorTaskViewStore.getState()).toMatchObject({
      projectId: "p2",
      tasks: [task("p2", "RUNNING")],
    });
  });

  it("ignores the string delegation brief that subagent.accepted carries on `task`", () => {
    const existing: TaskView = {
      id: "t1",
      projectId: "p1",
      transactionId: null,
      specialistRunId: null,
      kind: "compose",
      targetRef: "project:p1",
      status: "RUNNING",
      progress: null,
      resultRefs: [],
    };
    useCreatorTaskViewStore.setState({ projectId: "p1", tasks: [existing] });
    const store = useCreatorTaskViewStore.getState();
    // Replayed history written before the backend renamed the field keeps the
    // brief under `task`; a WeakMap keyed by rows must never see a string.
    expect(() =>
      store.consumeEvent(
        ev(1, "subagent.accepted", {
          runId: "run-1",
          role: "source_intelligence_agent",
          task: "分析这段小猫视角影片（cat_camera_1.mp4）",
        }),
      ),
    ).not.toThrow();
    expect(useCreatorTaskViewStore.getState().tasks).toEqual([existing]);
    expect(useCreatorTaskViewStore.getState().runs).toEqual([]);
  });
});

const pending = {
  id: "authorization-1",
  transactionId: "round-1",
  specialistRunId: "run-1",
  executionRequestId: "request-1",
  targetRef: "element:hero",
  scope: { operation: "r2v_generation" },
  status: "PENDING" as const,
  authorizationToken: "token-1",
  provider: "creator-image",
  model: "configured-image-model",
  maxCandidates: 1,
  createdAt: "now",
};

describe("file execution authorization polling", () => {
  beforeEach(() => useExecutionAuthorizationStore.getState().reset());

  it("uses project-level file routes without a SQL transaction", async () => {
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/execution-authorizations?status=PENDING",
        response: { json: { items: [pending] } },
      },
      {
        match: "/projects/p1/execution-authorizations/authorization-1/approve",
        response: { json: { ...pending, status: "APPROVED" } },
      },
    ]);

    await useExecutionAuthorizationStore.getState().load("p1");
    await useExecutionAuthorizationStore.getState().approve("authorization-1", {
      authorizationToken: "token-1",
      provider: "creator-image",
      model: "configured-image-model",
      maxCost: 0,
      maxCandidates: 1,
    });

    expect(useExecutionAuthorizationStore.getState().items[0].status).toBe(
      "APPROVED",
    );
    expect(calls.some((call) => call.url.includes("/transactions/"))).toBe(
      false,
    );
    expect(calls.at(-1)?.body).toEqual({
      authorizationToken: "token-1",
      provider: "creator-image",
      model: "configured-image-model",
      maxCost: 0,
      maxCandidates: 1,
    });
  });
});

describe("Session refresh and replay", () => {
  const store = () => useCreatorSessionStore.getState();

  /** Stub fetch with a manually resolvable Response. */
  function stubPending() {
    let resolve!: (value: Response) => void;
    const promise = new Promise<Response>((done) => {
      resolve = done;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => promise),
    );
    return { resolve };
  }

  function response(body: unknown, status = 200): Response {
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status >= 400 ? "Failed" : "OK",
      json: async () => body,
    } as Response;
  }

  const seqs = {
    lastMessageSeq: 0,
    lastConsumedMessageSeq: 0,
    lastEventSeq: 0,
  };
  const session = (projectId: string): CreatorSessionView => ({
    id: `session-${projectId}`,
    projectId,
    status: "IDLE",
    ...seqs,
  });

  const message = (projectId: string, messageSeq = 1): CreatorMessage => ({
    messageId: `message-${projectId}-${messageSeq}`,
    messageSeq,
    role: "user",
    content: [{ type: "text", text: projectId }],
    metadata: {},
    createdAt: "2026-07-23T00:00:00Z",
  });

  function bind(projectId: string, conversationId: string) {
    useCreatorSessionStore.setState({
      projectId,
      session: session(projectId),
      activeConversationId: conversationId,
      messages: [],
      queuedUi: [],
    });
  }

  describe("Creator Session async project/conversation isolation", () => {
    beforeEach(() => {
      store().reset();
      useCreatorEditBufferStore.getState().reset();
      vi.unstubAllGlobals();
    });

    it("keeps the refreshed error when an older failure arrives on the stream", async () => {
      bind("p1", "c1");
      const error = {
        code: "LATEST",
        message: "latest failure",
        retryable: false,
      };
      store().ingestEvent(ev(1, "agent.run.started"));
      installMockFetch(
        bootstrapRoutes({
          session: { lastEventSeq: 4, status: "ERROR", error },
        }),
      );
      await store().refreshSession();

      const delayed = ev(2, "agent.run.failed", {
        error: { ...error, message: "old failure" },
      });
      store().ingestEvent(delayed);
      expect(store().events).toContainEqual(delayed);
      expect(store().session).toMatchObject({ lastEventSeq: 4, error });
    });

    it("drops an old send acceptance after switching projects", async () => {
      const pending = stubPending();
      bind("p1", "conversation-p1");

      const send = store().sendMessage({ message: "old project message" });
      expect(store().queuedUi).toHaveLength(1);

      store().reset();
      bind("p2", "conversation-p2");
      useCreatorSessionStore.setState({ messages: [message("p2")] });
      pending.resolve(
        response(
          {
            messageSeq: 1,
            eventSeq: 1,
            classification: "mutation_instruction",
            appendState: "queued_until_message_boundary",
            creatorSessionId: "session-p1",
            conversationId: "conversation-p1",
          },
          202,
        ),
      );
      await send;

      expect(store()).toMatchObject({
        projectId: "p2",
        activeConversationId: "conversation-p2",
        queuedUi: [],
        messages: [message("p2")],
      });
    });

    it("drops an old send failure after switching conversations", async () => {
      const pending = stubPending();
      bind("p1", "conversation-old");

      const send = store().sendMessage({ message: "old conversation message" });
      useCreatorSessionStore.setState({
        activeConversationId: "conversation-new",
        queuedUi: [],
      });
      pending.resolve(
        response(
          {
            code: "MODEL_REQUEST_FAILED",
            message: "provider leaked internal details",
          },
          500,
        ),
      );

      await expect(send).rejects.toThrow("provider leaked internal details");
      expect(store()).toMatchObject({
        activeConversationId: "conversation-new",
        queuedUi: [],
      });
    });

    it("retries a failed send verbatim under its original clientMessageId", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => Promise.reject(new Error("network down"))),
      );
      bind("p1", "conversation-p1");
      // Attachments and structured selections must survive the retry — a
      // plain-text reconstruction would send selections=[] and drop refs
      // (CR 2026-09-11).
      const edits = useCreatorEditBufferStore.getState();
      edits.recordPatch({
        projectId: "p1",
        projectBefore: null,
        generation: 1,
        operations: [
          {
            op: "replace",
            path: "/settings/name",
            before: "old",
            value: "new",
          },
        ],
      });
      const userEdits = edits.consumeContext("p1")!;
      const request = {
        message: "指令 A",
        assetVersionRefs: ["asset-version:av1"],
        context: {
          selections: [{ field: "prompt", path: "/a", text: "选区" }],
          userEdits,
        },
      };

      await expect(store().sendMessage(request)).rejects.toThrow(
        "network down",
      );
      const failed = store().queuedUi[0];
      expect(failed).toMatchObject({ state: "failed", request });
      expect(edits.consumeContext("p1")).toEqual(userEdits);
      // A newer edit belongs to the next message, even when the old card retries.
      const later = {
        ...userEdits.edits[0],
        at: "2099-01-01T00:00:00.000Z",
        after: "later",
      };
      useCreatorEditBufferStore.setState({
        entries: [...userEdits.edits, later],
      });

      const bodies: Record<string, unknown>[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((_url: unknown, init?: RequestInit) => {
          bodies.push(JSON.parse(String(init?.body)));
          return Promise.resolve(
            response({
              messageSeq: 1,
              eventSeq: 1,
              classification: "mutation_instruction",
              appendState: "queued_until_message_boundary",
              creatorSessionId: "session-p1",
              conversationId: "conversation-p1",
            }),
          );
        }),
      );
      await store().retryQueuedMessage(failed.clientMessageId);

      expect(bodies).toHaveLength(1);
      expect(bodies[0]).toMatchObject({
        clientMessageId: failed.clientMessageId,
        assetVersionRefs: ["asset-version:av1"],
        context: request.context,
      });
      expect(store().queuedUi).toMatchObject([{ state: "queued" }]);
      expect(useCreatorEditBufferStore.getState().entries).toEqual([later]);
    });

    it("keeps a failed request in its original conversation when switching and retrying", async () => {
      bind("p1", "conversation-old");
      vi.stubGlobal(
        "fetch",
        vi.fn(() => Promise.reject(new Error("offline"))),
      );
      await expect(
        store().sendMessage({ message: "old instruction" }),
      ).rejects.toThrow("offline");
      const failed = store().queuedUi[0];
      const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
        Promise.resolve(
          response(
            init?.method === "POST"
              ? {
                  appendState: "queued_until_message_boundary",
                  messageSeq: 1,
                  eventSeq: 1,
                }
              : { items: [] },
          ),
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      await store().setConversation("conversation-new");
      await store().retryQueuedMessage(failed.clientMessageId);
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
      ).toHaveLength(0);
      expect(store().queuedUi[0]).toMatchObject({
        state: "failed",
        conversationId: "conversation-old",
      });
      await store().setConversation("conversation-old");
      await store().retryQueuedMessage(failed.clientMessageId);
      const [, init] = fetchMock.mock.calls.find(
        ([, options]) => options?.method === "POST",
      )!;
      expect(JSON.parse(String(init?.body))).toMatchObject({
        conversationId: "conversation-old",
        clientMessageId: failed.clientMessageId,
        message: "old instruction",
      });
    });

    it("records an in-flight failure for recovery after returning to that conversation", async () => {
      bind("p1", "conversation-old");
      const pending = stubPending();
      const send = store().sendMessage({ message: "old instruction" });
      vi.stubGlobal(
        "fetch",
        vi.fn(() => Promise.resolve(response({ items: [] }))),
      );
      await store().setConversation("conversation-new");
      pending.resolve(response({ message: "offline" }, 500));
      await expect(send).rejects.toThrow();
      await store().setConversation("conversation-old");
      expect(store().queuedUi[0]).toMatchObject({
        state: "failed",
        text: "old instruction",
      });
    });

    it("does not merge an old pagination response into a new project", async () => {
      const pending = stubPending();
      bind("p1", "conversation-p1");
      // Older-history paging needs a loaded oldest message to anchor `before`.
      useCreatorSessionStore.setState({ messages: [message("p1", 5)] });

      const load = store().loadOlderMessages();
      store().reset();
      bind("p2", "conversation-p2");
      useCreatorSessionStore.setState({ messages: [message("p2")] });
      pending.resolve(
        response({ items: [message("p1", 2)], nextBefore: null }),
      );
      await load;

      expect(store()).toMatchObject({
        projectId: "p2",
        loadingOlder: false,
        messages: [message("p2")],
      });
    });

    it("does not activate a conversation created for a previous project", async () => {
      const pending = stubPending();
      bind("p1", "conversation-p1");

      const create = store().newConversation();
      store().reset();
      bind("p2", "conversation-p2");
      pending.resolve(
        response({
          conversationId: "created-for-p1",
          creatorSessionId: "session-p1",
          title: "旧项目新对话",
          createdAt: "2026-07-23T00:00:00Z",
        }),
      );
      expect(await create).toBe("created-for-p1");

      expect(store()).toMatchObject({
        projectId: "p2",
        activeConversationId: "conversation-p2",
        conversations: [],
      });
    });
  });

  /**
   * Durable log of a session that is already IDLE: a delegation accepted near the
   * head of the log, its specialist cancelled shortly after, a superseded mainline
   * run end event, and 900 later events pushing all of them far below any window.
   */
  function terminalDelegationLog(): CreatorEvent[] {
    const delegate = {
      parentActionId: "delegate-1",
      runId: "run-specialist",
      role: "media_understanding_agent",
    };
    return [
      ev(1, "agent.tool_started", {
        actionId: "delegate-1",
        tool: "delegate_to_agent",
        role: "media_understanding_agent",
      }),
      ev(10, "subagent.accepted", {
        ...delegate,
        delegationText: "理解刚上传的素材",
      }),
      ev(90, "agent.run.cancelled", { runId: "run-mainline" }),
      ev(100, "subagent.failed", {
        ...delegate,
        cancelled: true,
        error: "specialist run cancelled",
      }),
      ...Array.from({ length: 898 }, (_, index) =>
        ev(101 + index, "task.progress_updated", {
          taskId: "task-1",
          status: "RUNNING",
          progress: 0.5,
        }),
      ),
      ev(999, "agent.tool_completed", {
        actionId: "delegate-1",
        tool: "delegate_to_agent",
        state: "accepted",
        result: { ok: true, status: "ACCEPTED" },
      }),
      ev(1000, "session.status_changed", { status: "IDLE" }),
    ];
  }

  describe("terminal projection rebuild after a first-entry replay", () => {
    beforeEach(() => {
      store().reset();
      vi.unstubAllGlobals();
    });

    it("rebuilds the delegation terminal from the replay exactly as the raw log does", async () => {
      const history = terminalDelegationLog();
      installMockFetch(
        bootstrapRoutes({
          session: {
            lastMessageSeq: 1,
            lastConsumedMessageSeq: 1,
            lastEventSeq: 1000,
            status: "IDLE",
          },
        }),
      );

      store().reset();
      useCreatorSessionStore.setState({
        projectId: "p1",
        activeConversationId: "c1",
      });
      store().ingestEvents(history);
      const reference = store().subagentActivities;
      expect(reference["delegate-1"]).toMatchObject({
        completed: true,
        terminalKind: "CANCELLED",
        terminalEventSeq: 100,
      });

      // Contrast: replaying only the last 400 events — what the bounded window
      // used to fetch — keeps the card in "素材理解中" forever, because the
      // durable snapshot holds no terminal projection to fall back on.
      store().reset();
      useCreatorSessionStore.setState({
        projectId: "p1",
        activeConversationId: "c1",
      });
      store().ingestEvents(history.slice(-400));
      expect(store().subagentActivities["delegate-1"]?.completed).toBe(false);

      store().reset();
      await store().bootstrap("p1");
      const sources = testEventSources();
      // Replay exactly what the stream the store opened can deliver, so a
      // re-introduced bounded window fails this test instead of being papered
      // over by a hand-fed event list.
      const deliveredFrom = Number(
        /after=(\d+)/u.exec(sources[0].url)?.[1] ?? "0",
      );
      act(() => {
        for (const event of history)
          if (event.seq > deliveredFrom) sources[0].emit(event.type, event);
      });
      await waitFor(() => expect(store().lastEventSeq).toBe(1000));
      expect(store().subagentActivities).toEqual(reference);
      // The superseded mainline run keeps its authoritative end event even after
      // high-volume streaming history leaves the bounded `events` cache.
      expect(store().events).toContainEqual(
        expect.objectContaining({ seq: 90, type: "agent.run.cancelled" }),
      );
    });
  });
});
