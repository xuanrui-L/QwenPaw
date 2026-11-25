import { fireEvent, render, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ConfigProvider } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentProgressOverview from "@/components/agent/AgentProgressOverview";
import R2VWorkbenchPage from "@/pages/R2VWorkbenchPage";
import AssetsPage from "@/pages/AssetsPage";
import PlanPage from "@/pages/PlanPage";
import { NavigationRuntime } from "@/routing/navigation";
import { findCreatorFieldElement } from "@/routing/reviewFocus";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useNavigationStore } from "@/store/navigationStore";
import { projectDocument } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";
import type {
  WorkGraphNode,
  AgentStatusBarView,
  CreatorSessionStatus,
  CreatorSessionView,
  ProjectDocument,
  TaskView,
} from "@/contracts/creator";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import type { SubagentActivity } from "@/store/creatorSessionStore";
import type { ToolCallPresentation } from "@/lib/creatorMessagePresentation";
import {
  deriveAgentLiveStatus,
  type AgentLiveStatusInput,
} from "@/lib/agentLiveStatus";
import { nodeGenerating } from "@/lib/generationActivity";

const mediaBudget = { mediaCallBudget: 10 };

const timelineId = "timeline:second";
const elementId = "r2v-window";
const base = `/timelines/items/${timelineId}/elements_by_id/${elementId}/creation`;
function LocationProbe() {
  const location = useLocation();
  return (
    <output data-location>
      {location.pathname}
      {location.search}
    </output>
  );
}
function operation(
  kind: WorkGraphNode["kind"],
  id = `${kind}:${elementId}`,
): WorkGraphNode {
  return {
    id,
    kind,
    timelineId,
    label: "private label",
    lane: "private lane",
    status: "done",
    deps: [],
    missing: [],
    taskId: null,
    progress: null,
    error: null,
    dispatchable: false,
    locator: { page: "plan", elementId },
  };
}

describe("overview operation clicks reach the actual generation field", () => {
  beforeEach(() => {
    useProjectSnapshotStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useWorkGraphStore.getState().reset();
    useExecutionAuthorizationStore.getState().reset();
    useNavigationStore.getState().clear();
    Element.prototype.scrollIntoView = vi.fn();
    const project = structuredClone(projectDocument);
    const timeline = structuredClone(project.timelines.items["timeline:main"]);
    timeline.timeline_id = timelineId;
    timeline.title = "第二集";
    const creation = timeline.elements_by_id[elementId].creation;
    if (creation.type === "r2v") {
      creation.storyboard_prompt = "第二集专用分镜提示词";
      creation.video_prompt = "第二集专用视频提示词";
    }
    project.timelines.items[timelineId] = timeline;
    project.timelines.order.push(timelineId);
    useProjectSnapshotStore.setState({
      projectId: "p1",
      project,
      generation: project.generation,
      syncStatus: "healthy",
      etag: "test",
    });
    useWorkGraphStore.setState({
      projectId: "p1",
      graph: {
        projectId: "p1",
        generation: project.generation,
        nodes: [operation("storyboard"), operation("video")],
        counts: { done: 2 },
        mediaCalls: 0,
        ...mediaBudget,
      },
    });
    installMockFetch([
      {
        match: "/models/resolved",
        response: {
          json: { video: { provider: "wan", model: "wan3.0-video-prime" } },
        },
      },
    ]);
  });

  function renderOverview(entry = "/project/p1/assets") {
    return render(
      <ConfigProvider theme={{ token: { motion: false } }}>
        <MemoryRouter initialEntries={[entry]}>
          <NavigationRuntime />
          <LocationProbe />
          <aside>
            <AgentProgressOverview projectId="p1" />
          </aside>
          <main data-creator-workspace-root>
            <Routes>
              <Route path="/project/:id/assets" element={<AssetsPage />} />
              <Route
                path="/project/:id/t/:timelineId/plan"
                element={<PlanPage />}
              />
              <Route
                path="/project/:id/t/:timelineId/plan/element/:elementId"
                element={<R2VWorkbenchPage />}
              />
            </Routes>
          </main>
        </MemoryRouter>
      </ConfigProvider>,
    );
  }

  function clickOperation(container: HTMLElement, kind: string) {
    const row = Array.from(
      container.querySelectorAll<HTMLElement>("[data-node-id]"),
    ).find((item) => item.dataset.nodeId === `${kind}:${elementId}`)!;
    fireEvent.click(within(row).getByRole("button"));
  }

  it("clicks storyboard/video/storyboard on the second episode, switches the real tab and scrolls its exact prompt without entering review", async () => {
    const { container } = renderOverview();
    for (const kind of ["storyboard", "video", "storyboard"]) {
      clickOperation(container, kind);
      const field = `${base}/${kind}_prompt`;
      await waitFor(() => {
        const target = findCreatorFieldElement(
          field,
          container.querySelector("main")!,
        );
        expect(target).not.toBeNull();
        expect(target).toHaveClass("review-flash");
        expect(target).toHaveTextContent(
          kind === "storyboard"
            ? "第二集专用分镜提示词"
            : "第二集专用视频提示词",
        );
      });
      const location = container.querySelector("[data-location]")!.textContent!;
      expect(location).toContain(
        `/t/${encodeURIComponent(timelineId)}/plan/element/${elementId}`,
      );
      const query = new URLSearchParams(location.split("?")[1]);
      expect(query.get("field")).toBe(field);
      expect(query.get("focusField")).toBe("1");
      expect(query.has("review")).toBe(false);
      expect(useCreatorInteractionStore.getState().selectedRef).toBe(
        `element:${elementId}`,
      );
    }
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
});

function rect(height: number): DOMRect {
  return {
    height,
    width: 300,
    top: 0,
    left: 0,
    bottom: height,
    right: 300,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

describe("creation overview drag-to-resize", () => {
  beforeEach(() => {
    useAgentDockUiStore.getState().reset();
  });

  it("drags within [floor, feed slack], nudges by keyboard, survives collapse and resets on double click", () => {
    const { container } = render(
      <div data-agent-dock>
        <AgentProgressOverview projectId="p1" />
        <div data-feed style={{ flexGrow: 1 }} />
      </div>,
    );
    const dock = container.querySelector<HTMLElement>("[data-agent-dock]")!;
    const section = container.querySelector<HTMLElement>(
      "[data-agent-progress-overview]",
    )!;
    Object.defineProperty(dock, "clientHeight", { value: 600 });
    vi.spyOn(section, "getBoundingClientRect").mockReturnValue(rect(160));
    vi.spyOn(
      container.querySelector<HTMLElement>("[data-feed]")!,
      "getBoundingClientRect",
    ).mockReturnValue(rect(300));
    const handle = () =>
      container.querySelector<HTMLElement>("[data-agent-overview-resize]");
    const header = () =>
      section.querySelector<HTMLButtonElement>(".agent-progress-header")!;
    const stored = () => useAgentDockUiStore.getState().overviewHeight;

    expect(section).not.toHaveAttribute("data-resized");
    expect(handle()).toHaveAttribute("role", "separator");

    fireEvent.pointerDown(handle()!, { button: 0, clientY: 100 });
    fireEvent.pointerMove(window, { clientY: 180 });
    expect(stored()).toBe(240);
    expect(section).toHaveAttribute("data-resized", "true");
    expect(section.getAttribute("style")).toContain(
      "--agent-overview-height: 240px",
    );
    fireEvent.pointerMove(window, { clientY: -500 });
    expect(stored()).toBe(96);
    // 160 (current) + 300 (feed slack) - 120 (feed floor)
    fireEvent.pointerMove(window, { clientY: 900 });
    expect(stored()).toBe(340);
    fireEvent.pointerUp(window);
    fireEvent.pointerMove(window, { clientY: 400 });
    expect(stored()).toBe(340);

    fireEvent.keyDown(handle()!, { key: "ArrowDown" });
    expect(stored()).toBe(176);
    fireEvent.keyDown(handle()!, { key: "ArrowUp" });
    expect(stored()).toBe(144);

    fireEvent.click(header());
    expect(section).toHaveAttribute("data-expanded", "false");
    expect(section).not.toHaveAttribute("data-resized");
    expect(handle()).toBeNull();
    expect(stored()).toBe(144);
    fireEvent.click(header());
    expect(section).toHaveAttribute("data-resized", "true");

    fireEvent.doubleClick(handle()!);
    expect(stored()).toBeNull();
    expect(section).not.toHaveAttribute("data-resized");
  });
});

describe("Live status projection", () => {
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
        live({ tasks: [task("r2v_generation", 0.42, "element:e2")] })
          .progressPercent,
      ).toBeNull();
      expect(
        live({ agentStatusBar: statusBar({ completed: 3, total: 10 }) })
          .progressPercent,
      ).toBeNull();
    });
  });

  const runningTask = (kind: TaskView["kind"], targetRef: string): TaskView =>
    ({
      id: "task-1",
      kind,
      targetRef,
      status: "RUNNING",
      progress: null,
      resultRefs: [],
    }) as unknown as TaskView;

  describe("nodeGenerating", () => {
    it("matches visual nodes whose entity ids contain colons", () => {
      // CR 2026-09-11: char:hero was truncated to char, so the pill never
      // locked while asset:char:hero was rendering.
      const tasks = [runningTask("image_generation", "asset:char:hero")];
      expect(nodeGenerating(tasks, "visual:char:hero:var:x")).toBe(true);
      expect(nodeGenerating(tasks, "visual:char:heroine:var:x")).toBe(false);
    });

    it("matches element and lineup targets without splitting their ids", () => {
      expect(
        nodeGenerating(
          [runningTask("r2v_generation", "element:elem:shot3")],
          "video:elem:shot3",
        ),
      ).toBe(true);
      expect(
        nodeGenerating(
          [runningTask("image_generation", "lineup:cast:main")],
          "lineup:cast:main",
        ),
      ).toBe(true);
    });
  });
});
