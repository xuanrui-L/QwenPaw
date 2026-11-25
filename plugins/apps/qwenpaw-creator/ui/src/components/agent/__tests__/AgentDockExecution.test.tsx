import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentDock, { simplifyErrorMessage } from "@/components/agent/AgentDock";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { projectDocument } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";
import {
  evt,
  makePendingAuthorization,
  makeReviewRecord,
  makeRun,
  msg,
  seedCreatorSession,
} from "@/test/agentFixtures";

function renderDock(switchProjects = false) {
  return render(
    <MemoryRouter initialEntries={["/project/p1/plan"]}>
      {switchProjects && <Link to="/project/p2/plan">切到第二项目</Link>}
      <Routes>
        <Route path="/project/:id/plan" element={<AgentDock />} />
      </Routes>
    </MemoryRouter>,
  );
}

const composerBox = () =>
  screen.getByRole("textbox", { name: "输入修改意图，@ 可引用对象…" });

const asst = (overrides: Parameters<typeof msg>[0]) =>
  msg({
    messageSeq: 2,
    role: "assistant",
    source: "creator_agent",
    ...overrides,
  });

const executionNotice = (overrides: Parameters<typeof msg>[0] = {}) =>
  asst({
    messageId: "execution-pause-1",
    source: "creator_execution_notice",
    text: "自动创作已暂停，作品尚未完成。已连续自动处理 5 轮，仍待处理序章剧本。",
    metadata: { executionPause: { reason: "consecutive_resume_limit" } },
    ...overrides,
  });

const ACCEPTED = {
  messageSeq: 2,
  eventSeq: 20,
  classification: "mutation_instruction",
  appendState: "queued_until_message_boundary",
  creatorSessionId: "session-1",
  conversationId: "conversation-1",
};

const seedSession = (status: string, patch: Record<string, unknown> = {}) =>
  useCreatorSessionStore.setState(
    (state) =>
      ({
        session: { ...state.session!, status },
        ...patch,
      }) as never,
  );

describe("AgentDock public output and interactions", () => {
  beforeEach(() => {
    seedCreatorSession();
  });

  it("pins and opens a pause buried by runtime notifications and assistant output only once", async () => {
    const { calls } = installMockFetch([]);
    useAgentDockUiStore.getState().setOpen(false);
    renderDock();
    const notice = executionNotice();
    const laterMessages = [
      msg({
        messageId: "runtime-notification",
        messageSeq: 3,
        source: "runtime_notification",
        text: "[RUNTIME_EVENT:completed] 后台任务已完成",
      }),
      asst({
        messageId: "later-tool",
        messageSeq: 4,
        text: "正在整理后台结果。",
        metadata: {
          actionId: "read-after-pause",
          parsedAction: {
            action: "tool_call",
            tool: "read_project_file",
            arguments: { path: "plan.json" },
          },
        },
      }),
      asst({
        messageId: "later-summary",
        messageSeq: 5,
        text: "已有素材已整理，剧本仍待处理。",
      }),
    ];
    act(() =>
      useCreatorSessionStore.setState({
        messages: [notice, ...laterMessages],
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("自动创作已暂停，作品尚未完成");
    expect(alert).toHaveTextContent("仍待处理序章剧本");
    expect(alert.closest(".agent-conversation-feed")).toBeNull();
    expect(alert).toHaveClass("max-h-36", "overflow-y-auto");
    expect(alert).toHaveAttribute("tabindex", "0");
    expect(
      within(
        document.querySelector(".agent-conversation-feed") as HTMLElement,
      ).getByText(/自动创作已暂停，作品尚未完成/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("已有素材已整理，剧本仍待处理。"),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("RUNTIME_EVENT");
    expect(useExecutionAuthorizationStore.getState().items).toHaveLength(0);
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);

    fireEvent.keyDown(window, { key: "Escape" });
    act(() =>
      useCreatorSessionStore.setState({
        messages: [{ ...notice }, ...laterMessages],
      }),
    );
    expect(document.querySelector("[data-agent-dock]")).not.toBeInTheDocument();
    act(() =>
      useCreatorSessionStore.setState({
        messages: [
          notice,
          ...laterMessages,
          executionNotice({
            messageId: "execution-pause-2",
            messageSeq: 6,
            text: "自动创作已暂停，本轮未提交有效进展。",
            metadata: { executionPause: { reason: "no_committed_progress" } },
          }),
        ],
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "本轮未提交有效进展",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(
      "已连续自动处理 5 轮",
    );
  });

  it.each(["initial_goal", "agent_dock", "review_revise", "frontend_editor"])(
    "clears the pinned pause on a new %s user instruction, keeping its history",
    (source) => {
      const notice = executionNotice();
      useCreatorSessionStore.setState({ messages: [notice] });
      renderDock();
      expect(screen.getByRole("alert")).toBeInTheDocument();
      act(() =>
        useCreatorSessionStore.setState({
          messages: [
            notice,
            msg({
              messageId: "new-instruction",
              messageSeq: 3,
              source,
              text: "请继续修改剧本",
            }),
            asst({
              messageId: "reply",
              messageSeq: 4,
              text: "收到新的修改指令。",
            }),
          ],
        }),
      );
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByText(/自动创作已暂停，作品尚未完成/),
      ).toBeInTheDocument();
      fireEvent.keyDown(window, { key: "Escape" });
      act(() =>
        useCreatorSessionStore.setState((state) => ({
          messages: [...state.messages],
        })),
      );
      expect(
        document.querySelector("[data-agent-dock]"),
      ).not.toBeInTheDocument();
    },
  );

  it("clears the pause as soon as a new instruction is submitted", async () => {
    installMockFetch([
      { match: "/messages", method: "POST", response: { json: ACCEPTED } },
    ]);
    useCreatorSessionStore.setState({ messages: [executionNotice()] });
    renderDock();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    composerBox().textContent = "请继续修改剧本";
    fireEvent.input(composerBox());
    fireEvent.keyDown(composerBox(), { key: "Enter" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(useCreatorSessionStore.getState().queuedUi[0]?.state).toBe(
        "queued",
      ),
    );
  });

  it.each(["RUNNING", "RESUMING", "WAITING_RUNTIME", "INTERRUPT_REQUESTED"])(
    "does not pin or auto-open pause history while the session is %s",
    (status) => {
      seedSession(status, { messages: [executionNotice()] });
      useAgentDockUiStore.getState().setOpen(false);
      renderDock();
      expect(
        document.querySelector("[data-agent-dock]"),
      ).not.toBeInTheDocument();
      act(() => useAgentDockUiStore.getState().setOpen(true));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByText(/自动创作已暂停，作品尚未完成/),
      ).toBeInTheDocument();
    },
  );

  it.each(["RUNNING", "RESUMING"])(
    "clears a visible pause when execution becomes %s",
    (status) => {
      useCreatorSessionStore.setState({ messages: [executionNotice()] });
      renderDock();
      expect(screen.getByRole("alert")).toBeInTheDocument();
      act(() => seedSession(status));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByText(/自动创作已暂停，作品尚未完成/),
      ).toBeInTheDocument();
    },
  );

  it("excludes the previous project's pause while its session store is still loaded", () => {
    useCreatorSessionStore.setState({ messages: [executionNotice()] });
    renderDock(true);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "切到第二项目" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    act(() =>
      useCreatorSessionStore.setState({
        messages: [executionNotice({ messageId: "old-project-pause" })],
      }),
    );
    expect(document.querySelector("[data-agent-dock]")).not.toBeInTheDocument();
  });

  it("excludes a previous conversation and respects manual close when returning to it", async () => {
    const notice = executionNotice();
    installMockFetch([
      {
        match: "/conversations/conversation-other/messages",
        response: { json: { items: [] } },
      },
      {
        match: "/conversations/conversation-1/messages",
        response: { json: { items: [notice] } },
      },
    ]);
    useCreatorSessionStore.setState({ messages: [notice] });
    renderDock();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await act(async () =>
      useCreatorSessionStore.getState().setConversation("conversation-other"),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/自动创作已暂停，作品尚未完成/),
    ).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    await act(async () =>
      useCreatorSessionStore.getState().setConversation("conversation-1"),
    );
    expect(document.querySelector("[data-agent-dock]")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创作助手" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "自动创作已暂停，作品尚未完成",
    );
  });

  it("keeps removed media-budget pauses only as historical bubbles", () => {
    const text = "自动创作已暂停，媒体调用预算已用完。";
    useCreatorSessionStore.setState({
      messages: [
        executionNotice({
          text,
          metadata: { executionPause: { reason: "media_budget_exhausted" } },
        }),
      ],
    });
    useAgentDockUiStore.getState().setOpen(false);
    renderDock();
    expect(document.querySelector("[data-agent-dock]")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创作助手" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.getByText(text).closest("[data-agent-message]"),
    ).toBeInTheDocument();
  });

  it("keeps transient thinking out of the conversation through tool-result persistence", () => {
    useAgentDockUiStore.getState().setOpen(true);
    const narration = asst({
      messageId: "narration",
      text: "我先检查当前剧本。",
    });
    seedSession("RUNNING", {
      messages: [narration],
      streamingAssistantMessages: {
        reasoning: {
          messageId: "reasoning",
          firstEventSeq: 10,
          deltas: {},
          thinkingDeltas: { 0: "PRIVATE_REASONING" },
          createdAt: "now",
        },
      },
    });
    renderDock();
    const flow = document.querySelector("[data-agent-response-flow]")!;
    const liveStatus = document.querySelector("[data-agent-live-status]")!;
    expect(flow.children).toHaveLength(1);
    expect(liveStatus).toHaveTextContent("正在思考");

    const progress = evt("agent.tool_progress", 11, {
      toolCallId: "read-plan",
      tool: "read_project",
    });
    const started = evt("agent.tool_started", 12, {
      toolCallId: "read-plan",
      tool: "read_project",
    });
    act(() => useCreatorSessionStore.setState({ events: [progress, started] }));
    expect(flow.children).toHaveLength(2);
    expect(flow).not.toHaveTextContent("正在思考");
    expect(liveStatus).toHaveTextContent("处理中");

    // The durable envelope arrives later, with the reasoning placeholder
    // cleared in the same publication. No temporary row may disappear.
    act(() =>
      useCreatorSessionStore.setState({
        streamingAssistantMessages: {},
        messages: [
          narration,
          asst({
            messageId: "read-plan-message",
            messageSeq: 3,
            text: "",
            metadata: {
              actionId: "read-plan",
              toolCall: { id: "read-plan", name: "read_project" },
            },
          }),
        ],
        events: [
          progress,
          started,
          evt("agent.tool_completed", 13, {
            toolCallId: "read-plan",
            tool: "read_project",
          }),
        ],
      }),
    );
    expect(flow.children).toHaveLength(2);
    expect(flow.querySelectorAll("[data-agent-tool]")).toHaveLength(1);
    expect(flow).toHaveTextContent("已完成");
    expect(document.body).not.toHaveTextContent("PRIVATE_REASONING");
  });

  const seedFailedRun = (error: Record<string, unknown>) =>
    useCreatorSessionStore.setState(
      (state) =>
        ({
          session: { ...state.session!, status: "ERROR", error },
        }) as never,
    );

  it("tells the user to redeem Credits when the model run is refused for balance", async () => {
    // MODEL_QUOTA_EXCEEDED means the provider declined before reaching any
    // model, so no further Agent turn can succeed. The generic tray copy
    // claimed the failure "went back to the Agent", which is what let one
    // empty balance produce repeated failures within two seconds.
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/messages",
        method: "POST",
        response: { json: ACCEPTED },
      },
    ]);
    useAgentDockUiStore.getState().setOpen(true);
    seedFailedRun({
      code: "MODEL_QUOTA_EXCEEDED",
      message:
        "Creator AgentScope model request failed: Error code: 403 - " +
        "{'error': {'code': 'ASP.BIZ.CREDITS_INSUFFICIENT'}}",
      retryable: false,
    });

    renderDock();

    expect(
      screen.getByText(/模型 Credits 不足，已停止自动重试/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/本次执行未完成/)).not.toBeInTheDocument();
    // The raw provider repr stays out of the tray.
    expect(document.body).not.toHaveTextContent("ASP.BIZ.CREDITS_INSUFFICIENT");

    fireEvent.click(screen.getByRole("button", { name: "已兑换，继续" }));
    await waitFor(() =>
      expect(
        calls.find((call) => call.url.includes("/projects/p1/messages"))?.body,
      ).toMatchObject({ message: "我已兑换 Credits，请继续之前中断的任务" }),
    );
  });

  it("keeps the throttle tray on its own copy and continue message", () => {
    // Both causes share one resume handler; this pins the throttle side so the
    // Credits branch cannot quietly take over its wording.
    installMockFetch([
      {
        match: "/projects/p1/messages",
        method: "POST",
        response: { json: ACCEPTED },
      },
    ]);
    useAgentDockUiStore.getState().setOpen(true);
    seedFailedRun({
      code: "MODEL_RATE_LIMITED",
      message: "model rate limited",
      retryable: true,
      details: { retryCount: 5 },
    });

    renderDock();

    expect(screen.getByText(/已重试 5 次仍无法访问/)).toBeInTheDocument();
    // antd pads a two-character button label, so match the word loosely.
    expect(
      screen.getByRole("button", { name: /^\s*继\s*续\s*$/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/模型 Credits 不足/)).not.toBeInTheDocument();
  });

  it("settles the stop button with cancelled media Tasks and still allows stopping newly started work", async () => {
    const task = {
      id: "task-stopped-image",
      projectId: "p1",
      transactionId: null,
      specialistRunId: "run-1",
      kind: "image_generation" as const,
      targetRef: "element:one",
      status: "CANCELLED" as const,
      progress: null,
      resultRefs: [],
    };
    const staleRun = makeRun({
      status: "WAITING_RUNTIME",
      taskRefs: [task.id],
      metadata: { commandType: "GENERATE_STORYBOARD_IMAGE" },
    });
    installMockFetch([
      {
        match: "/interrupt",
        method: "POST",
        response: {
          json: {
            creatorSessionId: "session-1",
            status: "CANCELLED",
            stopRequested: true,
          },
        },
      },
      { match: "/specialist-runs", response: { json: { items: [staleRun] } } },
      { match: "/tasks", response: { json: { items: [task] } } },
    ]);
    seedSession("RUNNING");
    useCreatorTaskViewStore.setState({ projectId: "p1", runs: [staleRun] });
    useAgentDockUiStore.getState().setOpen(true);
    renderDock();
    fireEvent.click(screen.getByRole("button", { name: "停止所有 Agent" }));
    await waitFor(() =>
      expect(useCreatorSessionStore.getState().session?.status).toBe(
        "CANCELLED",
      ),
    );
    await act(() => useCreatorTaskViewStore.getState().refresh("p1"));
    expect(
      screen.queryByRole("button", { name: "停止所有 Agent" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();

    // A later manual generation can run independently of the stopped chat.
    act(() =>
      useCreatorTaskViewStore.setState({
        tasks: [
          task,
          {
            ...task,
            id: "new-manual-task",
            specialistRunId: null,
            status: "RUNNING",
          },
        ],
      }),
    );
    expect(
      screen.getByRole("button", { name: "停止所有 Agent" }),
    ).toBeEnabled();
  });
});

describe("AgentDock older-history paging while replay is suppressed", () => {
  beforeEach(() => {
    seedCreatorSession();
    useAgentDockUiStore.getState().setOpen(true);
  });

  const historyCalls = (calls: Array<{ url: string }>) =>
    calls.filter((call) => call.url.includes("messages?before=")).length;

  const routes: Parameters<typeof installMockFetch>[0] = [
    {
      match: "messages?before=",
      response: {
        json: {
          items: [
            msg({ messageId: "tail-4", messageSeq: 4, text: "更早的消息" }),
          ],
          nextBefore: null,
        },
      },
    },
    { match: "/specialist-runs", response: { json: { items: [] } } },
    { match: "/tasks", response: { json: { items: [] } } },
  ];

  /** The state a dying EventSource leaves behind: replay never finished. */
  const seedStuckReplay = (connectionState: "reconnecting" | "connected") =>
    useCreatorSessionStore.setState({
      messages: [msg({ messageId: "tail-5", messageSeq: 5 })],
      hasMoreMessages: true,
      isReplaying: true,
      loadingOlder: false,
      connectionState,
    });

  it("holds the composer button at 发送 while a replay is still catching up", () => {
    // The measured defect: a Project whose session had already ended in ERROR
    // flipped this button to 停止所有 Agent three times inside 1.5s, because
    // the four sources behind `stoppable` land at different moments and the
    // replay walks through the run's own WAITING_EXECUTION_AUTH history.
    useCreatorSessionStore.setState({
      isReplaying: true,
      session: {
        id: "session-1",
        projectId: "p1",
        status: "WAITING_EXECUTION_AUTH",
        activeGoalId: null,
        lastMessageSeq: 3,
        lastConsumedMessageSeq: 3,
        lastEventSeq: 10,
        error: null,
      } as never,
    });
    renderDock();

    expect(screen.getByRole("button", { name: "发送" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停止所有 Agent" }),
    ).not.toBeInTheDocument();
  });

  it("re-reads the authoritative session the moment a replay catches up", async () => {
    // Replay replays events, not the session row. Without this the corrected
    // status only arrives with the next ~10s poll batch.
    const refresh = vi.fn(async () => undefined);
    useCreatorSessionStore.setState({
      isReplaying: true,
      refreshSession: refresh,
    });
    renderDock();

    useCreatorSessionStore.setState({ isReplaying: false });

    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });
});
