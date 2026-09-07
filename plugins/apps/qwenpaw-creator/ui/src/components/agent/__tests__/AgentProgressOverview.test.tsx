import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentProgressOverview from "@/components/agent/AgentProgressOverview";
import * as workGraphApi from "@/api/creator/workGraph";
import type { ProjectDocument, TaskView } from "@/contracts/creator";
import type {
  WorkGraphNode,
  WorkGraphView,
} from "@/contracts/creator/workGraph";
import i18n from "@/i18n";
import { navigateToLocator } from "@/routing/locators";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import {
  makeReviewOperation,
  makeReviewRecord,
  makeRun,
  makePendingAuthorization,
} from "@/test/agentFixtures";

vi.mock("@/routing/locators", () => ({ navigateToLocator: vi.fn() }));

type Phase = "all" | "running" | "attention" | "preparing" | "completed";

function project(projectId = "p1", groupCount = 2): ProjectDocument {
  return {
    project_id: projectId,
    name: "城市旅行系列",
    timelines: {
      order: Array.from({ length: groupCount }, (_, index) => `t${index + 1}`),
      items: Object.fromEntries(
        Array.from({ length: groupCount }, (_, index) => {
          const id = `t${index + 1}`;
          return [
            id,
            {
              timeline_id: id,
              title: `第${index + 1}集 · 城市旅行`,
              elements_by_id: Object.fromEntries(
                [1, 2, 3].map((scene) => [
                  `${id}-scene-${scene}`,
                  {
                    label: `第${index + 1}集镜头${scene}`,
                    creation: {
                      type: "r2v",
                      storyboard_prompt: "分镜",
                      video_prompt: "视频",
                    },
                  },
                ]),
              ),
            },
          ];
        }),
      ),
    },
    visual: {
      entities: { order: [], items: {} },
      cast_lineups: { order: [], items: {} },
    },
    assets: {
      source_versions_by_id: {
        "source-version": {
          name: "城市参考.mp4",
          logical_asset_id: "source-1",
        },
      },
      artifact_versions_by_id: {},
    },
    sources: { sources: { order: [], items: {} } },
  } as unknown as ProjectDocument;
}

function node(
  timelineId: string,
  scene: number,
  status: WorkGraphNode["status"],
): WorkGraphNode {
  const elementId = `${timelineId}-scene-${scene}`;
  return {
    id: `video:${elementId}`,
    kind: "video",
    label: `${elementId} · video`,
    status,
    deps: [],
    lane: `element:${elementId}`,
    taskId: status === "running" ? `task-${elementId}` : null,
    timelineId,
    progress: null,
    error: null,
    missing: [],
    locator: { page: "plan", timelineId, elementId },
    dispatchable: false,
  };
}

function graph(
  nodes = [
    node("t1", 1, "running"),
    node("t1", 2, "done"),
    node("t2", 1, "ready"),
    node("t2", 2, "failed"),
  ],
  projectId = "p1",
): WorkGraphView {
  const counts: Record<string, number> = { total: nodes.length };
  for (const item of nodes)
    counts[item.status] = (counts[item.status] ?? 0) + 1;
  return {
    projectId,
    generation: 1,
    counts,
    mediaCalls: 1,
    mediaCallBudget: 200,
    nodes,
  };
}

function seedGraph(value = graph(), snapshot = project(value.projectId)) {
  useWorkGraphStore.setState({ projectId: value.projectId, graph: value });
  useProjectSnapshotStore.setState({
    projectId: value.projectId,
    project: snapshot,
  });
}

function overview(): HTMLElement {
  const section = document.querySelector<HTMLElement>(
    "[data-agent-progress-overview]",
  );
  expect(section).not.toBeNull();
  return section!;
}

function filterButton(scope: HTMLElement, phase: Phase, count: number) {
  return within(scope).getByRole("button", {
    name: `${i18n.t(`progressOverview.${phase}`)} ${count}`,
  });
}

function expectCounts(scope: HTMLElement, counts: Record<Phase, number>) {
  for (const [phase, count] of Object.entries(counts))
    expect(filterButton(scope, phase as Phase, count)).toBeInTheDocument();
}

function task(overrides: Partial<TaskView> = {}): TaskView {
  return {
    id: "task-t1-scene-1",
    projectId: "p1",
    transactionId: null,
    specialistRunId: null,
    kind: "r2v_generation",
    status: "RUNNING",
    targetRef: "element:t1-scene-1",
    progress: null,
    resultRefs: [],
    ...overrides,
  };
}

describe("AgentProgressOverview factual progress", () => {
  beforeEach(() => {
    useWorkGraphStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useExecutionAuthorizationStore.getState().reset();
    vi.mocked(navigateToLocator).mockClear();
  });

  it("surfaces real pending production confirmations without adding fake operations or generation buttons", () => {
    seedGraph();
    useExecutionAuthorizationStore.setState({
      projectId: "p1",
      items: [makePendingAuthorization({ id: "real-confirmation" })],
    });
    render(<AgentProgressOverview projectId="p1" />);
    expect(overview()).toHaveTextContent(
      i18n.t("agent.productionConfirmPending", { count: 1 }),
    );
    expect(overview()).toHaveTextContent("可在对话下方确认");
    expect(filterButton(overview(), "all", 4)).toBeInTheDocument();
    expect(
      within(overview()).queryByRole("button", {
        name: /^(?:生成|确认生成|重新生成|重试)$/,
      }),
    ).not.toBeInTheDocument();
    act(() => useExecutionAuthorizationStore.setState({ projectId: "other" }));
    expect(overview()).not.toHaveTextContent("可在对话下方确认");
  });

  it("starts expanded inline with five factual counters, grouped work, and no modal or invented percentage", () => {
    seedGraph();
    render(<AgentProgressOverview projectId="p1" />);
    const section = overview();
    const toggle = within(section).getByRole("button", {
      name: i18n.t("progressOverview.collapse"),
    });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent(i18n.t("progressOverview.title"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expectCounts(section, {
      all: 4,
      running: 1,
      attention: 1,
      preparing: 1,
      completed: 1,
    });
    expect(filterButton(section, "all", 4)).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const groups = section.querySelectorAll(".agent-overview-group");
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveTextContent("第1集 · 城市旅行");
    expect(groups[0]).toHaveTextContent("第1集镜头1");
    expect(groups[0]).not.toHaveTextContent("第2集镜头1");
    expect(groups[1]).toHaveTextContent("第2集 · 城市旅行");
    expect(groups[1]).toHaveTextContent("第2集镜头1");
    expect(within(section).queryByRole("progressbar")).not.toBeInTheDocument();
    expect(section).not.toHaveTextContent(/\d\s*%/);
  });

  it("toggles with Enter, Space, and a button click while keeping the collapsed title and factual summary visible", async () => {
    const user = userEvent.setup();
    seedGraph();
    render(<AgentProgressOverview projectId="p1" />);
    const section = overview();
    const toggle = within(section).getByRole("button", {
      name: i18n.t("progressOverview.collapse"),
    });
    toggle.focus();
    expect(toggle).toHaveFocus();
    await user.keyboard("{Enter}");
    const collapsedToggle = within(section).getByRole("button", {
      name: i18n.t("progressOverview.expand"),
    });
    expect(collapsedToggle).toHaveAttribute("aria-expanded", "false");
    expect(collapsedToggle).toHaveTextContent(i18n.t("progressOverview.title"));
    expect(collapsedToggle).toHaveTextContent(
      i18n.t("progressOverview.runningOperations", { count: 1 }),
    );
    expect(section.querySelector(".agent-progress-metrics")).toBeNull();
    expect(section.querySelector(".agent-progress-details")).toBeNull();
    expect(
      within(section).queryByRole("button", { name: "全部 4" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.keyboard(" ");
    expect(
      within(section).getByRole("button", {
        name: i18n.t("progressOverview.collapse"),
      }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(section.querySelectorAll(".agent-overview-operation")).toHaveLength(
      4,
    );
    expectCounts(section, {
      all: 4,
      running: 1,
      attention: 1,
      preparing: 1,
      completed: 1,
    });

    fireEvent.click(
      within(section).getByRole("button", {
        name: i18n.t("progressOverview.collapse"),
      }),
    );
    expect(
      within(section).getByRole("button", {
        name: i18n.t("progressOverview.expand"),
      }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(section.querySelector(".agent-progress-details")).toBeNull();
  });

  it("updates the truthful summary for asynchronous completion and new review while remaining collapsed", () => {
    const initial = graph([node("t1", 1, "running")]);
    seedGraph(initial);
    render(<AgentProgressOverview projectId="p1" />);
    fireEvent.click(
      within(overview()).getByRole("button", {
        name: i18n.t("progressOverview.collapse"),
      }),
    );
    expect(overview()).toHaveTextContent(
      i18n.t("progressOverview.runningOperations", { count: 1 }),
    );
    act(() =>
      useWorkGraphStore.setState({
        graph: {
          ...graph([{ ...initial.nodes[0], status: "done", taskId: null }]),
          generation: 2,
        },
      }),
    );
    expect(overview()).toHaveTextContent(
      i18n.t("progressOverview.retained", { count: 1 }),
    );
    expect(overview()).not.toHaveTextContent(
      i18n.t("progressOverview.runningOperations", { count: 1 }),
    );
    expect(
      within(overview()).getByRole("button", {
        name: i18n.t("progressOverview.expand"),
      }),
    ).toHaveAttribute("aria-expanded", "false");
    act(() =>
      useFileProjectReviewStore.setState({
        projectId: "p1",
        reviews: [makeReviewRecord()],
      }),
    );
    expect(overview()).toHaveTextContent("1 项待审阅");
    expect(overview().querySelector(".agent-progress-metrics")).toBeNull();
    expect(overview().querySelector(".agent-progress-details")).toBeNull();
    expect(
      within(overview()).getByRole("button", {
        name: i18n.t("progressOverview.expand"),
      }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("filters real operations by each lifecycle phase without changing the totals", () => {
    seedGraph();
    render(<AgentProgressOverview projectId="p1" />);
    const section = overview();
    for (const phase of [
      "running",
      "attention",
      "preparing",
      "completed",
    ] as const) {
      fireEvent.click(filterButton(section, phase, 1));
      expect(filterButton(section, phase, 1)).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      const rows = section.querySelectorAll(".agent-overview-operation");
      expect(rows).toHaveLength(1);
      expect(rows[0]).toHaveAttribute("data-phase", phase);
      expectCounts(section, {
        all: 4,
        running: 1,
        attention: 1,
        preparing: 1,
        completed: 1,
      });
    }
    fireEvent.click(filterButton(section, "all", 4));
    expect(section.querySelectorAll(".agent-overview-operation")).toHaveLength(
      4,
    );
  });

  it("keeps every parallel group visible through completion and filters its operations inline", () => {
    const nodes = [1, 2, 3, 4].flatMap((group) =>
      [1, 2, 3].map((scene) =>
        node(`t${group}`, scene, scene === 1 ? "running" : "done"),
      ),
    );
    seedGraph(graph(nodes), project("p1", 4));
    render(<AgentProgressOverview projectId="p1" />);
    const section = overview();
    expect(section.querySelectorAll(".agent-overview-group")).toHaveLength(4);
    expect(section.querySelectorAll(".agent-overview-operation")).toHaveLength(
      12,
    );
    expect(section).toHaveTextContent("4 组工作正在并行");
    expect(
      section.querySelector(".agent-overview-more, .agent-group-more"),
    ).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expectCounts(section, {
      all: 12,
      running: 4,
      attention: 0,
      preparing: 0,
      completed: 8,
    });
    fireEvent.click(filterButton(section, "running", 4));
    expect(section.querySelectorAll(".agent-overview-operation")).toHaveLength(
      4,
    );
    expect(
      section.querySelectorAll(
        '.agent-overview-operation[data-phase="running"]',
      ),
    ).toHaveLength(4);
    act(() =>
      useWorkGraphStore.setState({
        graph: graph(
          nodes.map((item) => ({ ...item, status: "done", taskId: null })),
        ),
      }),
    );
    fireEvent.click(filterButton(section, "all", 12));
    expect(section.querySelectorAll(".agent-overview-group")).toHaveLength(4);
    expect(section.querySelectorAll(".agent-overview-operation")).toHaveLength(
      12,
    );
    expectCounts(section, {
      all: 12,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 12,
    });
    expect(section).toHaveTextContent("已完成 12");
    expect(section).not.toHaveTextContent("100%");
  });

  it("counts a linked operation once from queued to running before graph refresh and after completion", () => {
    const initial = graph([node("t1", 1, "ready")]);
    seedGraph(initial);
    useCreatorTaskViewStore.setState({
      projectId: "p1",
      tasks: [task({ status: "QUEUED" })],
    });
    render(<AgentProgressOverview projectId="p1" />);
    expectCounts(overview(), {
      all: 1,
      running: 0,
      attention: 0,
      preparing: 1,
      completed: 0,
    });
    act(() => useCreatorTaskViewStore.setState({ tasks: [task()] }));
    expectCounts(overview(), {
      all: 1,
      running: 1,
      attention: 0,
      preparing: 0,
      completed: 0,
    });
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(1);
    act(() => {
      useCreatorTaskViewStore.setState({
        tasks: [task({ status: "SUCCEEDED" })],
      });
      useWorkGraphStore.setState({
        graph: graph([{ ...initial.nodes[0], status: "done", taskId: null }]),
      });
    });
    expectCounts(overview(), {
      all: 1,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 1,
    });
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(1);
  });

  it.each(["ready", "failed", "stale"] as const)(
    "keeps %s work read-only while preserving precise object navigation",
    (status) => {
      const dispatch = vi
        .spyOn(workGraphApi, "dispatchWorkGraphNode")
        .mockRejectedValue(
          new Error("The progress overview must not dispatch work"),
        );
      seedGraph(graph([{ ...node("t2", 1, status), dispatchable: true }]));
      render(<AgentProgressOverview projectId="p1" />);
      const section = overview();
      expect(section.querySelector(".agent-work-action")).toBeNull();
      for (const key of ["workGraph.generate", "workGraph.retry"]) {
        expect(
          within(section).queryByRole("button", { name: i18n.t(key) }),
        ).not.toBeInTheDocument();
      }
      expect(
        filterButton(
          section,
          status === "ready" ? "preparing" : "attention",
          1,
        ),
      ).toBeInTheDocument();
      fireEvent.click(
        within(section).getByRole("button", { name: /第2集镜头1/ }),
      );
      expect(navigateToLocator).toHaveBeenLastCalledWith(
        "p1",
        expect.objectContaining({
          page: "element",
          timelineId: "t2",
          elementId: "t2-scene-1",
          field:
            "/timelines/items/t2/elements_by_id/t2-scene-1/creation/video_prompt",
        }),
        { description: i18n.t("progressOverview.title"), focusField: true },
      );
      expect(dispatch).not.toHaveBeenCalled();
      expect(useWorkGraphStore.getState().dispatching).toEqual({});
    },
  );

  it("projects public names and excludes internal labels from text and accessibility attributes", () => {
    seedGraph(
      graph([
        {
          ...node("t1", 1, "running"),
          label:
            'snapshot:PRIVATE_NODE /timelines/items/t1 {"element_id":"secret"}',
          error: "PRIVATE_STACK_TRACE",
        },
      ]),
    );
    useCreatorTaskViewStore.setState({
      projectId: "p1",
      runs: [
        makeRun({
          id: "PRIVATE_RUN_ID",
          role: "source_intelligence_agent",
          status: "WAITING_RUNTIME",
          displayName: "source_intelligence_agent /private/workspace.json",
          finalSummaryText:
            'PRIVATE_SUMMARY schema {"tool_name":"read_source_video"}',
          targetRefs: ["asset:source-1"],
          metadata: { hidden: "PRIVATE_METADATA" },
        }),
      ],
    });
    render(<AgentProgressOverview projectId="p1" />);
    const section = overview();
    expect(section).toHaveTextContent("第1集镜头1");
    expect(section).toHaveTextContent("城市参考.mp4");
    const exposedText = [
      section.textContent,
      ...Array.from(section.querySelectorAll("[title], [aria-label]")).flatMap(
        (element) => [
          element.getAttribute("title"),
          element.getAttribute("aria-label"),
        ],
      ),
    ].join(" ");
    expect(exposedText).not.toMatch(
      /PRIVATE_|snapshot:|\/timelines\/|\/private\/|element_id|tool_name|source_intelligence_agent|read_source_video/,
    );
    expect(section).not.toHaveTextContent(/\d\s*%/);
  });

  it("navigates second-episode groups and operations inline with the exact locator and overview source", () => {
    seedGraph();
    render(<AgentProgressOverview projectId="p1" />);
    fireEvent.click(
      within(overview()).getByRole("button", {
        name: "第2集 · 城市旅行",
      }),
    );
    expect(navigateToLocator).toHaveBeenLastCalledWith(
      "p1",
      expect.objectContaining({ page: "plan", timelineId: "t2" }),
      { description: i18n.t("progressOverview.title") },
    );
    fireEvent.click(
      within(overview()).getByRole("button", { name: /第2集镜头1/ }),
    );
    expect(navigateToLocator).toHaveBeenLastCalledWith(
      "p1",
      expect.objectContaining({
        page: "element",
        timelineId: "t2",
        elementId: "t2-scene-1",
        field:
          "/timelines/items/t2/elements_by_id/t2-scene-1/creation/video_prompt",
      }),
      { description: i18n.t("progressOverview.title"), focusField: true },
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(4);
  });

  it("keeps text review separate from completed operations and resolves it without changing the graph", () => {
    const completedGraph = graph([node("t1", 1, "done")]);
    seedGraph(completedGraph);
    const review = makeReviewRecord({
      operations: [
        makeReviewOperation({
          operation_id: "pending-name",
          json_pointer: "/timelines/items/t1/title",
        }),
        makeReviewOperation({
          operation_id: "accepted-name",
          decision: "ACCEPTED",
        }),
      ],
    });
    useFileProjectReviewStore.setState({ projectId: "p1", reviews: [review] });
    render(<AgentProgressOverview projectId="p1" />);
    expectCounts(overview(), {
      all: 1,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 1,
    });
    expect(
      overview().querySelector(".agent-overview-review"),
    ).toHaveTextContent("1 项待审阅");
    expect(useWorkGraphStore.getState().graph).toBe(completedGraph);
    expect(overview()).not.toHaveTextContent("/timelines/items/t1/title");
    act(() =>
      useFileProjectReviewStore.setState({
        reviews: [{ ...review, status: "RESOLVED" }],
      }),
    );
    expect(overview().querySelector(".agent-overview-review")).toBeNull();
    expect(filterButton(overview(), "completed", 1)).toBeInTheDocument();
  });

  it.each([
    ["text", 3],
    ["media", 2],
  ] as const)(
    "uses the maximum review count without adding duplicate %s units to operation totals",
    (kind, expected) => {
      seedGraph(
        graph([
          node("t1", 1, "done"),
          node("t1", 2, "waiting_review"),
          node("t2", 1, "waiting_review"),
        ]),
      );
      useFileProjectReviewStore.setState({
        projectId: "p1",
        reviews: [
          makeReviewRecord({
            operations: [0, 1, 2].map((index) =>
              makeReviewOperation({
                operation_id: `operation-${index}`,
                ui_locator: kind === "media" ? { mediaType: "video" } : {},
              }),
            ),
          }),
        ],
      });
      render(<AgentProgressOverview projectId="p1" />);
      expect(
        overview().querySelector(".agent-overview-review"),
      ).toHaveTextContent(`${expected} 项待审阅`);
      expectCounts(overview(), {
        all: 3,
        running: 0,
        attention: 2,
        preparing: 0,
        completed: 1,
      });
      expect(overview()).not.toHaveTextContent("5 项待审阅");
    },
  );

  it("scopes review-only projects independently and keeps the empty overview after review resolution", () => {
    seedGraph();
    useFileProjectReviewStore.setState({
      projectId: "p2",
      reviews: [makeReviewRecord()],
    });
    const view = render(<AgentProgressOverview projectId="p1" />);
    expect(overview().querySelector(".agent-overview-review")).toBeNull();
    view.rerender(<AgentProgressOverview projectId="p2" />);
    expect(
      overview().querySelector(".agent-overview-review"),
    ).toHaveTextContent("1 项待审阅");
    expectCounts(overview(), {
      all: 0,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 0,
    });
    expect(overview().querySelectorAll(".agent-overview-group")).toHaveLength(
      0,
    );
    act(() =>
      useFileProjectReviewStore.setState({
        reviews: [makeReviewRecord({ status: "SUPERSEDED" })],
      }),
    );
    expectCounts(overview(), {
      all: 0,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 0,
    });
    expect(overview().querySelector(".agent-overview-review")).toBeNull();
    expect(overview()).toHaveTextContent(i18n.t("progressOverview.empty"));
  });

  it("preserves the collapsed preference across project changes while resetting the operation filter", () => {
    seedGraph();
    const view = render(<AgentProgressOverview projectId="p1" />);
    fireEvent.click(filterButton(overview(), "completed", 1));
    expect(filterButton(overview(), "completed", 1)).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(1);
    fireEvent.click(
      within(overview()).getByRole("button", {
        name: i18n.t("progressOverview.collapse"),
      }),
    );
    act(() => {
      seedGraph(graph([node("t2", 1, "ready")], "p2"), project("p2"));
      view.rerender(<AgentProgressOverview projectId="p2" />);
    });
    const expand = within(overview()).getByRole("button", {
      name: i18n.t("progressOverview.expand"),
    });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(overview().querySelector(".agent-progress-details")).toBeNull();
    expect(overview().querySelector(".agent-progress-metrics")).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(expand);
    expectCounts(overview(), {
      all: 1,
      running: 0,
      attention: 0,
      preparing: 1,
      completed: 0,
    });
    expect(filterButton(overview(), "all", 1)).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(1);
    expect(overview()).not.toHaveTextContent("第1集镜头1");
  });

  it("keeps an empty overview visible while excluding stale graph and detached work from another project", () => {
    const view = render(<AgentProgressOverview projectId="p2" />);
    expectCounts(overview(), {
      all: 0,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 0,
    });
    expect(overview()).toHaveTextContent(i18n.t("progressOverview.empty"));
    act(() => {
      seedGraph();
      useCreatorTaskViewStore.setState({
        projectId: "p1",
        runs: [
          makeRun({ role: "ai_editing_director", status: "RUNNING_MODEL" }),
        ],
      });
    });
    view.rerender(<AgentProgressOverview projectId="p2" />);
    expectCounts(overview(), {
      all: 0,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 0,
    });
    expect(overview()).toHaveTextContent(i18n.t("progressOverview.empty"));
    expect(overview().querySelectorAll(".agent-overview-group")).toHaveLength(
      0,
    );
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(0);
    expect(overview()).not.toHaveTextContent(
      /第1集|第2集|城市旅行|ai_editing_director/,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("loads real work nodes after project publication when the initial graph was empty and no task event occurred", async () => {
    const empty = { ...graph([]), generation: 0 };
    seedGraph(empty);
    useProjectSnapshotStore.setState({ generation: 0 });
    const fresh = {
      ...graph([
        ...[1, 2, 3].map((index) => ({
          ...node("t1", index, "ready"),
          id: `visual:asset-${index}`,
          kind: "visual" as const,
          timelineId: null,
          locator: { page: "assets", assetId: `asset-${index}` },
        })),
        ...[1, 2].flatMap((index) => [
          {
            ...node("t1", index, "gated"),
            id: `storyboard:t1-scene-${index}`,
            kind: "storyboard" as const,
          },
          node("t1", index, "gated"),
        ]),
        {
          ...node("t1", 3, "gated"),
          id: "compose:t1",
          kind: "compose" as const,
        },
      ]),
      generation: 6,
    };
    let resolveGraph!: (value: WorkGraphView) => void;
    const getGraph = vi.spyOn(workGraphApi, "getWorkGraph").mockReturnValue(
      new Promise((resolve) => {
        resolveGraph = resolve;
      }),
    );
    render(<AgentProgressOverview projectId="p1" />);
    expect(getGraph).not.toHaveBeenCalled();
    expect(filterButton(overview(), "all", 0)).toBeInTheDocument();

    act(() => useProjectSnapshotStore.setState({ generation: 6 }));
    expect(getGraph).toHaveBeenCalledExactlyOnceWith("p1");
    // Never fabricate rows from the Project alone while the GET is pending.
    expect(filterButton(overview(), "all", 0)).toBeInTheDocument();
    await act(async () => resolveGraph(fresh));
    expectCounts(overview(), {
      all: 8,
      running: 0,
      attention: 0,
      preparing: 8,
      completed: 0,
    });
    expect(
      overview().querySelectorAll(".agent-overview-operation"),
    ).toHaveLength(8);
    expect(useWorkGraphStore.getState().graph).toBe(fresh);

    act(() => useProjectSnapshotStore.setState({ project: { ...project() } }));
    expect(getGraph).toHaveBeenCalledTimes(1);
  });

  it("keeps the newer graph when two project publications refresh it before the older response finishes", async () => {
    seedGraph({ ...graph([]), generation: 0 });
    useProjectSnapshotStore.setState({ generation: 0 });
    let resolveOld!: (value: WorkGraphView) => void;
    let resolveNew!: (value: WorkGraphView) => void;
    const getGraph = vi
      .spyOn(workGraphApi, "getWorkGraph")
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveNew = resolve;
        }),
      );
    render(<AgentProgressOverview projectId="p1" />);
    act(() => useProjectSnapshotStore.setState({ generation: 1 }));
    act(() => useProjectSnapshotStore.setState({ generation: 2 }));
    expect(getGraph).toHaveBeenCalledTimes(2);
    const newer = { ...graph([node("t1", 1, "done")]), generation: 2 };
    await act(async () => resolveNew(newer));
    await act(async () =>
      resolveOld({
        ...graph([node("t1", 1, "ready"), node("t1", 2, "ready")]),
        generation: 1,
      }),
    );
    expectCounts(overview(), {
      all: 1,
      running: 0,
      attention: 0,
      preparing: 0,
      completed: 1,
    });
    expect(useWorkGraphStore.getState().graph).toBe(newer);
  });

  it("does not request the visible project's graph from another project's publication", () => {
    seedGraph({ ...graph([]), generation: 0 });
    useProjectSnapshotStore.setState({
      projectId: "p2",
      project: project("p2"),
      generation: 6,
    });
    const getGraph = vi.spyOn(workGraphApi, "getWorkGraph");
    render(<AgentProgressOverview projectId="p1" />);
    act(() => useProjectSnapshotStore.setState({ generation: 7 }));
    expect(getGraph).not.toHaveBeenCalled();
    expect(filterButton(overview(), "all", 0)).toBeInTheDocument();
  });
});
