import { describe, expect, it } from "vitest";
import type {
  AgentStatusBarView,
  CreatorSessionStatus,
  CreatorSessionView,
  ProjectDocument,
  TaskView,
} from "@/contracts/creator";
import type { SubagentActivity } from "@/store/creatorSessionStore";
import type { ToolCallPresentation } from "@/lib/creatorMessagePresentation";
import { creatorStatusLabel } from "@/lib/creatorPresentation";
import {
  deriveAgentLiveStatus,
  toolActivityPhase,
  type AgentLiveStatusInput,
} from "@/lib/agentLiveStatus";
import i18n from "@/i18n";

const project = {
  timelines: {
    items: {
      t1: {
        elements_by_id: {
          e2: { element_id: "e2", label: "在厨房准备早餐的特写镜头" },
        },
      },
    },
  },
  assets: {
    source_versions_by_id: {
      v1: { logical_asset_id: "la1", name: "主角小狐" },
    },
  },
} as unknown as ProjectDocument;

function session(status: CreatorSessionStatus): CreatorSessionView {
  return {
    id: "session-1",
    projectId: "p1",
    status,
    lastMessageSeq: 0,
    lastConsumedMessageSeq: 0,
    lastEventSeq: 0,
  };
}

function statusBar(
  overrides: Partial<AgentStatusBarView["progress"]> = {},
): AgentStatusBarView {
  return {
    progress: {
      phase: "visual_development",
      label: "正在制作",
      sourceEventSeq: 1,
      updatedAt: "now",
      ...overrides,
    },
    badges: [],
  };
}

function subagentActivity(
  tool: string,
  args?: Record<string, unknown>,
): SubagentActivity {
  return {
    parentActionId: "action-1",
    runId: "run-1",
    role: "visual_development_agent",
    targetRefs: [],
    firstEventSeq: 1,
    completed: false,
    status: "RUNNING_MODEL",
    messages: {},
    tools: {
      "tc-1": {
        toolCallId: "tc-1",
        runId: "run-1",
        tool,
        firstEventSeq: 2,
        status: "started",
        executing: true,
        arguments: args,
        outputEvents: [],
      },
    },
  };
}

const delegateCall: ToolCallPresentation = {
  actionId: "action-1",
  order: 1,
  status: "started",
  executing: true,
  tool: "delegate_to_agent",
  arguments: { role: "visual_development_agent" },
};

const task = (
  kind: TaskView["kind"],
  progress: number | null,
  targetRef = "asset:la1",
) =>
  ({
    id: "task-1",
    projectId: "p1",
    kind,
    targetRef,
    status: "RUNNING",
    progress,
    resultRefs: [],
  }) as TaskView;

const live = (overrides: Partial<AgentLiveStatusInput> = {}) =>
  deriveAgentLiveStatus({
    session: session("RUNNING"),
    agentStatusBar: statusBar(),
    stopping: false,
    hasQueuedInput: false,
    isReplaying: false,
    subagentActivities: {},
    toolCalls: [],
    tasks: [],
    project,
    ...overrides,
  });

const withActivity = (
  activity: SubagentActivity,
  extra: Partial<AgentLiveStatusInput> = {},
) => live({ subagentActivities: { "action-1": activity }, ...extra });

describe("deriveAgentLiveStatus", () => {
  it("uses the real guide subject for a running call and clears it on completion", () => {
    const call: ToolCallPresentation = {
      actionId: "guide-call",
      order: 1,
      tool: "view_skill",
      status: "started",
      executing: true,
      arguments: { skill: "professional-media-prompts" },
    };
    expect(live({ toolCalls: [call], agentStatusBar: null })).toMatchObject({
      label: "正在查阅提示词编写指南…",
      indicatorPhase: "running",
    });
    expect(
      live({
        toolCalls: [{ ...call, status: "succeeded" }],
        agentStatusBar: null,
      }).label,
    ).not.toContain("指南");
  });

  it("shows the durable initial goal before the scheduler starts, including after reload", () => {
    const queued = {
      ...session("IDLE"),
      activeGoalId: "goal-1",
      lastMessageSeq: 1,
    };
    expect(live({ session: queued })).toMatchObject({
      state: "working",
      label: i18n.t("liveStatus.commandSent"),
      indicatorPhase: "waiting",
    });
    expect(
      live({ session: { ...queued, lastConsumedMessageSeq: 1 } }).state,
    ).toBe("idle");
    expect(live({ session: { ...queued, status: "CANCELLED" } }).state).toBe(
      "idle",
    );
    expect(
      live({ session: { ...queued, status: "PENDING_REVIEW" } }).state,
    ).toBe("waiting");
  });

  it("recognizes an unconsumed user message but never mistakes response history for queued work", () => {
    const current = {
      ...session("IDLE"),
      lastMessageSeq: 8,
      lastConsumedMessageSeq: 5,
    };
    const message = {
      messageId: "msg-8",
      messageSeq: 8,
      role: "user" as const,
      content: [],
      metadata: {},
      createdAt: "now",
    };
    expect(live({ session: current, messages: [message] }).state).toBe(
      "working",
    );
    expect(
      live({ session: current, messages: [{ ...message, role: "assistant" }] })
        .state,
    ).toBe("idle");
    expect(
      live({ session: current, messages: [{ ...message, messageSeq: 5 }] })
        .state,
    ).toBe("idle");
  });

  it.each(["IDLE", "RUNNING", "WAITING_RUNTIME"] as const)(
    "keeps an actual media review actionable while the main session is %s",
    (status) => {
      const result = live({
        session: session(status),
        pendingReviewCount: 1,
        tasks: status === "RUNNING" ? [task("image_generation", 0.4)] : [],
      });
      expect(result).toMatchObject({
        state: "waiting",
        indicatorPhase: "attention",
        label: i18n.t("liveStatus.waitingReview"),
        progressPercent: null,
      });
    },
  );

  it("does not suppress an undecided media result during the queued-input gap", () => {
    expect(
      live({
        session: session("IDLE"),
        hasQueuedInput: true,
        pendingReviewCount: 1,
      }).label,
    ).toBe(i18n.t("liveStatus.waitingReview"));
  });

  it("returns to idle after review resolution while stopping still has priority", () => {
    expect(
      live({ session: session("IDLE"), pendingReviewCount: 0 }).state,
    ).toBe("idle");
    expect(live({ stopping: true, pendingReviewCount: 1 }).state).toBe(
      "stopping",
    );
  });

  it("uses actual tool authorization events while the session snapshot still says running", () => {
    const result = live({
      toolCalls: [
        {
          actionId: "production",
          order: 1,
          status: "started",
          executing: true,
          tool: "request_workgraph_execution",
          waitingAuthorization: true,
        },
      ],
    });
    expect(result.state).toBe("waiting");
    expect(result.indicatorPhase).toBe("attention");
    expect(result.label).toBe(i18n.t("liveStatus.waitingExecAuth"));
  });

  it.each([
    ["IDLE", false, "idle", "随时可以继续创作"],
    ["WAITING_USER_INPUT", false, "waiting", "等待补充信息，请继续输入。"],
    ["IDLE", true, "working", "已发送，等待助手响应…"],
  ] as const)(
    "maps session %s (queued=%s) to %s",
    (status, hasQueuedInput, state, label) => {
      const result = live({
        session: session(status),
        agentStatusBar: null,
        hasQueuedInput,
      });
      expect(result.state).toBe(state);
      expect(result.label).toBe(label);
    },
  );

  it("prioritises stopping over any running work", () => {
    const result = live({ stopping: true, toolCalls: [delegateCall] });
    expect(result.state).toBe("stopping");
  });

  it("shows the rate-limit retry notice above any other working label", () => {
    const result = withActivity(
      subagentActivity("image_generation", { targetRef: "element:e2" }),
      { rateLimitRetry: { attempt: 2, maxAttempts: 5 } },
    );
    expect(result.label).toBe("遇到限流，正在重试（2/5）…");
  });

  it.each([
    ["element:e2", "正在生成「在厨房准备早餐的特…」分镜图…"],
    ["asset:la1", "正在生成「主角小狐」画面…"],
  ])("labels image generation for %s", (targetRef, label) => {
    expect(
      withActivity(subagentActivity("image_generation", { targetRef })).label,
    ).toBe(label);
  });

  it("does not show '正在安排' once the delegated subagent has completed (e.g. cancelled)", () => {
    // Incident regression: delegate still "started" but the specialist already
    // terminated — show a neutral current-session label, not a milestone.
    const result = withActivity(
      {
        ...subagentActivity("unknown_tool"),
        tools: {},
        completed: true,
        terminalKind: "CANCELLED",
      },
      {
        toolCalls: [delegateCall],
        agentStatusBar: statusBar({ label: "后端进度" }),
      },
    );
    expect(result.label).not.toBe("正在安排「视觉开发」…");
    expect(result.label).toBe("处理中");
  });

  it("shows a mini progress bar only for quantified task progress", () => {
    const quantified = live({ tasks: [task("asset_ingest", 0.42)] });
    expect(quantified.state).toBe("working");
    expect(quantified.label).toBe("「主角小狐」素材入库中…");
    expect(quantified.progressPercent).toBe(42);
    expect(
      live({ tasks: [task("r2v_generation", null, "element:e2")] })
        .progressPercent,
    ).toBeNull();
    expect(
      live({ agentStatusBar: statusBar({ completed: 3, total: 10 }) })
        .progressPercent,
    ).toBeNull();
  });

  it.each(["IDLE", "ERROR", "CANCELLED"] as const)(
    "shows a new queued instruction instead of the previous %s result",
    (status) => {
      const result = live({
        session: session(status),
        hasQueuedInput: true,
        agentStatusBar: statusBar({
          label: "AI 剪辑失败",
          completed: 3,
          total: 10,
        }),
      });
      expect(result).toEqual({
        state: "working",
        indicatorPhase: "waiting",
        label: "已发送，等待助手响应…",
        progressPercent: null,
      });
    },
  );

  it("does not reuse a terminal milestone label or count for the current run", () => {
    const result = live({
      agentStatusBar: statusBar({
        label: "AI 剪辑失败",
        completed: 3,
        total: 10,
        sourceEventSeq: 999,
      }),
    });
    expect(result).toEqual({
      state: "working",
      indicatorPhase: "running",
      label: "处理中",
      progressPercent: null,
    });
  });

  it("uses the current active task rather than an earlier failure milestone", () => {
    const result = live({
      agentStatusBar: statusBar({
        label: "AI 剪辑失败",
        completed: 3,
        total: 10,
      }),
      tasks: [task("asset_ingest", 0.42)],
    });
    expect(result).toEqual({
      state: "working",
      indicatorPhase: "running",
      label: "「主角小狐」素材入库中…",
      progressPercent: 42,
    });
  });

  it("does not attach another task's numeric progress to an active specialist tool", () => {
    const result = withActivity(
      subagentActivity("image_generation", { targetRef: "element:e2" }),
      {
        tasks: [task("asset_ingest", 0.42)],
      },
    );
    expect(result.label).toContain("分镜图");
    expect(result.progressPercent).toBeNull();
  });

  it("keeps queued tasks distinct from execution and suppresses stale numeric progress", () => {
    const queued = { ...task("asset_ingest", 0.42), status: "QUEUED" as const };
    const result = live({ tasks: [queued] });
    expect(result.label).toContain(creatorStatusLabel("QUEUED"));
    expect(result.progressPercent).toBeNull();
  });

  it("keeps detached work visible while the main session is idle", () => {
    const result = withActivity(
      { ...subagentActivity("read_source_video"), status: "RUNNING_MODEL" },
      { session: session("IDLE"), agentStatusBar: null },
    );
    expect(result.state).toBe("working");
    expect(result.progressPercent).toBeNull();
  });

  it.each([
    ["WAITING_EXECUTION_AUTH", "等待您确认开始制作"],
    ["PENDING_REVIEW", "改动已准备好，等待您审阅"],
    ["WAITING_USER_INPUT", "等待补充信息，请继续输入。"],
  ] as const)(
    "prioritizes %s while another task is active",
    (status, label) => {
      const result = live({
        session: session(status),
        tasks: [task("asset_ingest", 0.4)],
      });
      expect(result).toMatchObject({
        state: "waiting",
        label,
        progressPercent: null,
      });
    },
  );

  it("does not report argument preparation as tool execution", () => {
    const result = live({
      agentStatusBar: null,
      toolCalls: [
        {
          actionId: "a1",
          order: 1,
          tool: "image_generation",
          status: "started",
          executing: false,
        },
      ],
    });
    expect(result.label).toBe(i18n.t("agentActivity.preparing"));
    expect(result.indicatorPhase).toBe("waiting");
    expect(result.progressPercent).toBeNull();
  });

  it("keeps runtime waiting specific rather than falling back to thinking", () => {
    expect(
      live({ session: session("WAITING_RUNTIME"), agentStatusBar: null }).label,
    ).toBe(creatorStatusLabel("WAITING_RUNTIME"));
  });

  it("does not copy a raw backend failure into the live status row", () => {
    const result = live({
      session: {
        ...session("ERROR"),
        error: {
          code: "FAILED",
          message: '{"internalToken":"sensitive"}',
          retryable: false,
        },
      },
    });
    expect(result.label).toBe("执行失败");
  });
});

describe("truthful activity indicators", () => {
  it.each([
    ["started", true, "running"],
    ["started", false, "waiting"],
    ["started", undefined, "waiting"],
    ["succeeded", false, "completed"],
    ["failed", false, "attention"],
    ["waiting_review", false, "attention"],
    ["cancelled", false, "idle"],
    ["unknown", false, "idle"],
  ] as const)(
    "maps tool %s with executing=%s to %s",
    (status, executing, phase) => {
      expect(toolActivityPhase({ status, executing })).toBe(phase);
    },
  );

  it.each([
    ["QUEUED", "waiting"],
    ["QUEUED_CAPACITY", "waiting"],
    ["WAITING_RUNTIME", "waiting"],
    ["WAITING_AUTHORIZATION", "attention"],
    ["RUNNING_MODEL", "running"],
  ] as const)(
    "uses parent %s instead of a residual started tool",
    (status, phase) => {
      const activity = {
        ...subagentActivity("image_generation", { targetRef: "element:e2" }),
        status,
      };
      expect(
        toolActivityPhase({ status: "started", executing: true }, activity),
      ).toBe(phase);
      if (status === "RUNNING_MODEL") return;
      const result = withActivity(activity, {
        session: session("IDLE"),
        agentStatusBar: null,
        toolCalls: [delegateCall],
      });
      expect(result.indicatorPhase).toBe(phase);
      expect(result.label).toContain(creatorStatusLabel(status));
      expect(result.label).not.toContain("正在生成");
      expect(result.progressPercent).toBeNull();
    },
  );

  it("changes waiting → running only when the specialist actually resumes", () => {
    const activity = subagentActivity("read_source_video");
    const derive = (status: SubagentActivity["status"]) =>
      withActivity(
        { ...activity, status },
        {
          session: session("IDLE"),
          agentStatusBar: null,
        },
      );
    expect(derive("WAITING_RUNTIME").indicatorPhase).toBe("waiting");
    expect(derive("RUNNING_MODEL").indicatorPhase).toBe("running");
    expect(derive("WAITING_AUTHORIZATION").indicatorPhase).toBe("attention");
  });

  it("does not turn delegation acceptance into a completed specialist indicator", () => {
    const activity = {
      ...subagentActivity("read_source_video"),
      status: "QUEUED" as const,
    };
    expect(
      toolActivityPhase({ status: "succeeded", executing: false }, activity),
    ).toBe("waiting");
    expect(
      toolActivityPhase(
        { status: "succeeded" },
        {
          ...activity,
          status: "SUCCEEDED",
          completed: true,
          terminalKind: "SUCCESS",
        },
      ),
    ).toBe("completed");
  });

  it("shows an unfinished review lifecycle above leftover tools and unrelated runtime progress", () => {
    const result = withActivity(
      {
        ...subagentActivity("image_generation"),
        waitingReview: true,
      },
      { tasks: [task("asset_ingest", 0.4)] },
    );
    expect(result).toMatchObject({
      state: "waiting",
      indicatorPhase: "attention",
      progressPercent: null,
    });
    expect(result.label).toContain(creatorStatusLabel("PENDING_REVIEW"));
  });

  it("does not let completed review history hold a new mainline run in attention", () => {
    const activity = {
      ...subagentActivity("image_generation"),
      completed: true,
      waitingReview: true,
    };
    expect(toolActivityPhase({ status: "succeeded" }, activity)).toBe(
      "attention",
    );
    const current = withActivity(activity, {
      session: session("RUNNING"),
      agentStatusBar: null,
    });
    expect(current).toMatchObject({
      indicatorPhase: "running",
      label: "处理中",
    });
  });

  it("uses the actually running task while a specialist waits for its runtime result", () => {
    const result = withActivity(
      {
        ...subagentActivity("image_generation"),
        status: "WAITING_RUNTIME",
      },
      { tasks: [task("asset_ingest", 0.4)], toolCalls: [delegateCall] },
    );
    expect(result).toMatchObject({
      indicatorPhase: "running",
      progressPercent: 40,
    });
    expect(result.label).toContain("素材入库");
  });

  it.each(["image_generation", "delegate_to_agent"])(
    "keeps %s argument preparation static until execution is confirmed",
    (tool) => {
      const call: ToolCallPresentation = {
        actionId: "new-call",
        order: 1,
        tool,
        status: "started",
        executing: false,
      };
      const preparing = live({ toolCalls: [call], agentStatusBar: null });
      expect(preparing).toMatchObject({
        state: "working",
        indicatorPhase: "waiting",
        label: i18n.t("agentActivity.preparing"),
      });
      expect(
        live({
          toolCalls: [{ ...call, executing: true }],
          agentStatusBar: null,
        }).indicatorPhase,
      ).toBe("running");
    },
  );

  it("keeps queued task progress separate from actual execution", () => {
    const queued = { ...task("asset_ingest", 0.75), status: "QUEUED" as const };
    const result = live({ tasks: [queued] });
    expect(result).toMatchObject({
      state: "working",
      indicatorPhase: "waiting",
      progressPercent: null,
    });
    expect(
      live({ tasks: [{ ...queued, status: "RUNNING" }] }).indicatorPhase,
    ).toBe("running");
  });

  it("does not animate rate-limit waits, replay or the cancellation handshake as execution", () => {
    expect(
      live({ rateLimitRetry: { attempt: 2, maxAttempts: 3 } }).indicatorPhase,
    ).toBe("waiting");
    expect(live({ isReplaying: true }).indicatorPhase).toBe("waiting");
    expect(live({ stopping: true }).indicatorPhase).toBe("waiting");
    expect(live({ session: session("RESUMING") }).indicatorPhase).toBe(
      "waiting",
    );
  });

  it("does not infer success from IDLE and marks a real current error as attention", () => {
    expect(
      live({ session: session("IDLE"), agentStatusBar: null }).indicatorPhase,
    ).toBe("idle");
    expect(
      live({ session: session("CANCELLED"), agentStatusBar: null })
        .indicatorPhase,
    ).toBe("idle");
    expect(
      live({ session: session("ERROR"), agentStatusBar: null }).indicatorPhase,
    ).toBe("attention");
  });
});
