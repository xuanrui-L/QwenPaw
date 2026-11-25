import { act, waitFor } from "@testing-library/react";
import type { CreatorEvent } from "@/contracts/creator";
import { installMockFetch } from "@/test/mockFetch";
import {
  bootstrapRoutes,
  ev,
  msg,
  sessionView,
  testEventSources,
} from "@/test/sessionEventBuilders";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CreatorSessionView, TaskView } from "@/contracts/creator";
import { useCreatorEditBufferStore } from "@/store/creatorEditBufferStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { makeRun } from "@/test/agentFixtures";
import { jsonResponse } from "@/test/projectSnapshotFixtures";

const response = (body: unknown) => jsonResponse(200, body);

afterEach(() => vi.unstubAllGlobals());

describe("cancelled media task reconciliation", () => {
  beforeEach(() => useCreatorTaskViewStore.getState().reset());

  it.each([
    { taskStatus: "CANCELLED", delegated: false, expected: "CANCELLED" },
    { taskStatus: "RUNNING", delegated: false, expected: "WAITING_RUNTIME" },
    { taskStatus: "CANCELLED", delegated: true, expected: "WAITING_RUNTIME" },
  ] as const)(
    "reconciles cancelled media without hiding live Tasks or chat delegations: %j",
    async ({ taskStatus, delegated, expected }) => {
      const run = makeRun({
        status: "WAITING_RUNTIME",
        taskRefs: ["task-1"],
        metadata: {
          commandType: "GENERATE_STORYBOARD_IMAGE",
          ...(delegated ? { parentActionId: "delegate-action" } : {}),
        },
      });
      const task: TaskView = {
        id: "task-1",
        projectId: "p1",
        transactionId: null,
        specialistRunId: run.id,
        kind: "image_generation",
        targetRef: "element:one",
        status: taskStatus,
        progress: null,
        resultRefs: [],
      };
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string) =>
          response({
            items: url.endsWith("/tasks") ? [task] : [run],
          }),
        ),
      );
      await useCreatorTaskViewStore.getState().refresh("p1");
      expect(useCreatorTaskViewStore.getState().runs[0].status).toBe(expected);
    },
  );
});

describe("agent stop acknowledgement and session polling", () => {
  const store = () => useCreatorSessionStore.getState();

  function session(projectId: string): CreatorSessionView {
    return {
      id: `session-${projectId}`,
      projectId,
      status: "IDLE",
      lastMessageSeq: 0,
      lastConsumedMessageSeq: 0,
      lastEventSeq: 0,
    };
  }

  function bind(projectId: string, conversationId: string) {
    useCreatorSessionStore.setState({
      projectId,
      session: session(projectId),
      activeConversationId: conversationId,
      messages: [],
      queuedUi: [],
    });
  }

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

  beforeEach(() => {
    store().reset();
    useCreatorEditBufferStore.getState().reset();
  });

  it("applies the stop acknowledgement and ignores a pre-stop session poll", async () => {
    bind("p1", "conversation-p1");
    useCreatorSessionStore.setState({
      session: { ...session("p1"), status: "RUNNING" },
    });
    const pendingPoll = stubPending();
    const poll = store().refreshSession();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response({
          creatorSessionId: "session-p1",
          status: "CANCELLED",
          stopRequested: true,
        }),
      ),
    );

    await store().stopAllAgents();
    expect(store().session?.status).toBe("CANCELLED");
    expect(store().stopping).toBe(false);
    pendingPoll.resolve(
      response({ session: { ...session("p1"), status: "RUNNING" } }),
    );
    await poll;
    expect(store().session?.status).toBe("CANCELLED");
  });

  it("keeps the stop pending when a poll races cancellation cleanup", async () => {
    bind("p1", "conversation-p1");
    const pendingStop = stubPending();
    const stop = store().stopAllAgents();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response({
          session: { ...session("p1"), status: "RUNNING" },
          agentStatusBar: {
            progress: { sourceEventSeq: 0 },
            activity: { runningTaskCount: 0 },
            badges: [],
          },
        }),
      ),
    );
    await store().refreshSession();
    expect(store().session?.status).toBe("INTERRUPT_REQUESTED");
    pendingStop.resolve(
      response({
        creatorSessionId: "session-p1",
        status: "CANCELLED",
        stopRequested: true,
      }),
    );
    await stop;
    expect(store().session?.status).toBe("CANCELLED");
  });

  it("fences pending polls even when CANCELLED arrived before the stop acknowledgement", async () => {
    bind("p1", "conversation-p1");
    const pendingStop = stubPending();
    const stop = store().stopAllAgents();
    useCreatorSessionStore.setState({
      session: { ...session("p1"), status: "CANCELLED" },
    });
    const pendingPoll = stubPending();
    const poll = store().refreshSession();
    pendingStop.resolve(
      response({
        creatorSessionId: "session-p1",
        status: "CANCELLED",
        stopRequested: true,
      }),
    );
    await stop;
    pendingPoll.resolve(
      response({ session: { ...session("p1"), status: "RUNNING" } }),
    );
    await poll;
    expect(store().session?.status).toBe("CANCELLED");
  });

  it.each(["resumed", "switched"])(
    "does not apply a late stop after the project is %s",
    async (change) => {
      bind("p1", "conversation-p1");
      const pending = stubPending();
      const stop = store().stopAllAgents();
      if (change === "switched") {
        store().reset();
        bind("p2", "conversation-p2");
      }
      useCreatorSessionStore.setState({
        session: { ...store().session!, status: "RUNNING" },
      });
      pending.resolve(
        response({
          creatorSessionId: "session-p1",
          status: "CANCELLED",
          stopRequested: true,
        }),
      );
      await stop;
      expect(store().session?.status).toBe("RUNNING");
    },
  );
});

const store = () => useCreatorSessionStore.getState();
const ingest = (...events: CreatorEvent[]) =>
  act(() => store().ingestEvents(events));
const seed = (state: Record<string, unknown> = {}) =>
  useCreatorSessionStore.setState({
    projectId: "p1",
    lastEventSeq: 0,
    ...state,
  });

describe("Interactive session replay and history", () => {
  beforeEach(() => store().reset());

  it("keeps replay within the loaded tail while preserving live messages and backward paging", async () => {
    const tail = Array.from({ length: 50 }, (_, index) =>
      msg({ messageId: `m${951 + index}`, messageSeq: 951 + index }),
    );
    const routes = bootstrapRoutes({
      messages: tail,
      session: { lastMessageSeq: 1000, lastEventSeq: 10_000, status: "IDLE" },
    });
    routes[0].response.json = { items: tail, nextBefore: 951 } as never;
    const live = msg({ messageId: "m1001", messageSeq: 1001 });
    const older = msg({ messageId: "m950", messageSeq: 950 });
    const { calls } = installMockFetch([
      { match: "messages?after=1000", response: { json: { items: [live] } } },
      { match: "messages?before=951", response: { json: { items: [older] } } },
      ...routes,
    ]);
    await store().bootstrap("p1");
    ingest(
      ev(1, "agent.message_delta", {
        messageId: "old",
        deltaIndex: 0,
        delta: "历史内容",
      }),
      ev(2, "message.completed", { messageId: "old", messageSeq: 2 }),
      ev(3, "message.appended", {
        message: msg({ messageId: "old2", messageSeq: 3 }),
      }),
      ev(10_000, "subagent.started", {
        parentActionId: "detached",
        runId: "still-running",
        role: "visual_development_agent",
      }),
    );
    expect(store().messages).toEqual(tail);
    expect(store().streamingAssistantMessages).toEqual({});
    expect(store().hasMoreMessages).toBe(true);
    expect(store().isReplaying).toBe(false);
    expect(store().subagentActivities.detached.completed).toBe(false);
    expect(calls.filter((call) => call.url.includes("/messages"))).toHaveLength(
      1,
    );

    ingest(
      ev(10_001, "message.completed", { messageId: "m1001", messageSeq: 1001 }),
    );
    await waitFor(() => expect(store().messages.at(-1)).toEqual(live));
    await store().loadOlderMessages();
    expect(store().messages[0]).toEqual(older);
    expect(store().messages).toHaveLength(52);
    expect(calls.some((call) => /[?&]after=(?:0|1)&/u.test(call.url))).toBe(
      false,
    );
  });

  it("keeps a replayed delta prefix for a run whose message is not yet durable", async () => {
    // Review #243 P2: the backend deliberately keeps an active run's
    // historical deltas because those messages have no durable projection
    // yet. The frontend's replay filter was discarding them anyway, so a
    // returning user saw the reply missing its opening stretch -- replay
    // delta0 "Hello " was dropped and only live delta1 "world" survived.
    const routes = bootstrapRoutes({
      messages: [],
      session: { lastMessageSeq: 0, lastEventSeq: 2, status: "RUNNING" },
    });
    routes[0].response.json = { items: [], nextBefore: null } as never;
    installMockFetch(routes);
    await store().bootstrap("p1");
    expect(store().isReplaying).toBe(true);
    expect(store().lastEventSeq).toBe(0);

    ingest(
      ev(1, "agent.run.started", { runId: "run-1" }),
      ev(2, "agent.message_delta", {
        messageId: "assistant-partial",
        runId: "run-1",
        deltaIndex: 0,
        delta: "Hello ",
      }),
    );
    expect(store().isReplaying).toBe(false);

    ingest(
      ev(3, "agent.message_delta", {
        messageId: "assistant-partial",
        runId: "run-1",
        deltaIndex: 1,
        delta: "world",
      }),
    );
    const streaming = store().streamingAssistantMessages["assistant-partial"];
    expect(streaming.deltas[0]).toBe("Hello ");
    expect(streaming.deltas[1]).toBe("world");
  });

  it("still folds a replayed delta whose message is already durable", () => {
    // The historicalReplay filter had a real job: not build a streaming
    // bubble on top of a message the tail load already contains. That
    // path is guarded here so removing the broader replay filter (above)
    // does not let duplication back in.
    seed({
      messages: [
        msg({
          messageId: "already-durable",
          messageSeq: 1,
          role: "assistant",
        }),
      ],
      isReplaying: true,
      replayUntilSeq: 10,
      events: [],
    });
    ingest(
      ev(1, "agent.message_delta", {
        messageId: "already-durable",
        deltaIndex: 0,
        delta: "duplicate tail",
      }),
    );
    expect(
      store().streamingAssistantMessages["already-durable"],
    ).toBeUndefined();
  });

  it("does not let replayed status history flip the live session status", () => {
    // The flicker this guards: a first view replays the run's own
    // session.status_changed events. Writing them into the live session made
    // the failure notice under the conversation -- whose condition is
    // status === ERROR -- unmount and remount 8 times inside 830 ms on a real
    // Project, which users report as "this part keeps refreshing". The
    // authoritative status already arrived with the snapshot the stream was
    // opened from, so in-boundary transitions are history, not news.
    seed({
      isReplaying: true,
      replayUntilSeq: 10,
      session: sessionView({ status: "ERROR" }),
    });

    ingest(
      ev(1, "session.status_changed", { status: "RUNNING" }),
      ev(2, "session.status_changed", { status: "WAITING_EXECUTION_AUTH" }),
    );

    expect(store().session?.status).toBe("ERROR");

    // Past the boundary a transition is news again.
    ingest(ev(11, "session.status_changed", { status: "IDLE" }));
    expect(store().session?.status).toBe("IDLE");
  });
});
