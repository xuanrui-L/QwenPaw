import {
  cloneProject,
  seedProject,
  singleProject,
} from "@/test/projectSnapshotFixtures";
import { filenameFromDisposition } from "@/components/creator/ProjectExportActions";
import { waitFor } from "@testing-library/react";
import { ProjectComposer } from "@/components/creator/ProjectComposer";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";
import { useModelConfigStore } from "@/store/modelConfigStore";
import { act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import BlueprintPage from "@/pages/BlueprintPage";
import { NavigationRuntime } from "@/routing/navigation";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import type { ProjectDocument } from "@/contracts/creator";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BlueprintStructureArea from "../BlueprintStructureArea";
import { projectDocument } from "@/test/creatorFixtures";
import { selectTimelineSummaries } from "@/selectors/blueprintSelectors";
import {
  readStoryPositions,
  storyMapStorageKey,
} from "../useNarrativeGraphViewport";
import type { NarrativeEdgeDocument } from "@/contracts/creator";
import {
  applySavedPositions,
  freeNodePosition,
  GRAPH_NODE_HEIGHT,
  GRAPH_NODE_WIDTH,
  layoutStoryNodes,
  nodesOverlap,
  routeStoryEdges,
  type GraphPoint,
} from "../narrativeGraphLayout";

function props() {
  const project = structuredClone(projectDocument);
  project.narrative_edges = [
    {
      edge_id: "a",
      source_timeline_id: "timeline:main",
      target_timeline_id: "timeline:ep2",
      label: "公开真相",
      prompt: "如何选择",
    },
  ];
  return {
    project,
    summaries: selectTimelineSummaries(project),
    edges: project.narrative_edges,
    shape: "branching" as const,
    selectedTimelineId: null,
    onSelectTimeline: vi.fn(),
    onOpenTimeline: vi.fn(),
    onOpenElement: vi.fn(),
    onOpenVisualEntity: vi.fn(),
    onOpenResearch: vi.fn(),
    onOpenSource: vi.fn(),
  };
}

function pointer(target: Element, type: string, x: number, y: number) {
  const event = new MouseEvent(type, {
    bubbles: true,
    button: 0,
    clientX: x,
    clientY: y,
  });
  Object.defineProperties(event, {
    pointerId: { value: 1 },
    isPrimary: { value: true },
  });
  fireEvent(target, event);
}

describe("story map viewport", () => {
  beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows the choice label on its curve before focus and groups navigation controls with the map title", () => {
    const input = props();
    const { container } = render(<BlueprintStructureArea {...input} />);
    const label = container.querySelector('[data-graph-edge-label="a"]')!;
    expect(label).toHaveTextContent("公开真相");
    expect(label).toBeVisible();
    const toolbar = container.querySelector("[data-graph-toolbar]")!;
    expect(toolbar).toHaveTextContent("剧情地图");
    expect(toolbar).toContainElement(
      screen.getByRole("combobox", { name: "定位剧情节点" }),
    );
    expect(toolbar).toContainElement(
      screen.getByRole("button", { name: "放大剧情地图" }),
    );
    fireEvent.click(label);
    expect(screen.getByRole("region", { name: "节点分支去向" })).toBeVisible();
    expect(input.onSelectTimeline).not.toHaveBeenCalled();
    expect(readStoryPositions(input.project.project_id)).toEqual({});
  });

  it("maps Shift-wheel to horizontal scroll, including native deltaX and line mode", () => {
    const { container } = render(<BlueprintStructureArea {...props()} />);
    const viewport = container.querySelector<HTMLElement>(
      "[data-graph-viewport]",
    )!;
    const wheel = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: 120,
      shiftKey: true,
    });
    fireEvent(viewport, wheel);
    expect(wheel.defaultPrevented).toBe(true);
    expect(viewport.scrollLeft).toBe(120);
    expect(viewport.scrollTop).toBe(0);
    fireEvent.wheel(viewport, { deltaX: 60, shiftKey: true });
    expect(viewport.scrollLeft).toBe(180);
    fireEvent.wheel(viewport, { deltaY: -2, deltaMode: 1, shiftKey: true });
    expect(viewport.scrollLeft).toBe(148);
    const normal = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: 80,
    });
    fireEvent(viewport, normal);
    expect(normal.defaultPrevented).toBe(false);
  });

  it("drags nodes at zoom scale, suppresses accidental activation, and survives progress, new nodes and remount", () => {
    const input = props();
    const { container, rerender, unmount } = render(
      <BlueprintStructureArea {...input} />,
    );
    const node = container.querySelector<HTMLElement>(
      '[data-blueprint-node="timeline:main"]',
    )!;
    fireEvent.click(screen.getByRole("button", { name: "缩小剧情地图" }));
    const before = {
      x: parseFloat(node.style.left),
      y: parseFloat(node.style.top),
    };
    pointer(node, "pointerdown", 100, 100);
    pointer(node, "pointermove", 140, 160);
    pointer(node, "pointerup", 140, 160);
    fireEvent.click(node);
    expect(input.onSelectTimeline).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("region", { name: "节点分支去向" }),
    ).not.toBeInTheDocument();
    expect(parseFloat(node.style.left)).toBeCloseTo(before.x + 50);
    expect(parseFloat(node.style.top)).toBeCloseTo(before.y + 75);
    const saved = readStoryPositions(input.project.project_id);
    expect(Object.keys(saved)).toHaveLength(2);
    rerender(
      <BlueprintStructureArea
        {...input}
        summaries={input.summaries.map((s) => ({
          ...s,
          title: "更新后的长标题",
          hasScript: true,
        }))}
      />,
    );
    expect(parseFloat(node.style.left)).toBe(saved["timeline:main"].x);
    expect(parseFloat(node.style.top)).toBe(saved["timeline:main"].y);
    rerender(
      <BlueprintStructureArea
        {...input}
        summaries={[
          ...input.summaries,
          { ...input.summaries[0], timelineId: "new", index: 2 },
        ]}
      />,
    );
    expect(parseFloat(node.style.top)).toBe(saved["timeline:main"].y);
    expect(container.querySelectorAll("[data-blueprint-node]")).toHaveLength(3);
    unmount();
    const restored = render(<BlueprintStructureArea {...input} />);
    const again = restored.container.querySelector<HTMLElement>(
      '[data-blueprint-node="timeline:main"]',
    )!;
    expect(parseFloat(again.style.top)).toBe(saved["timeline:main"].y);
    fireEvent.click(screen.getByRole("button", { name: "自动整理" }));
    expect(readStoryPositions(input.project.project_id)).toEqual({});
    expect(parseFloat(again.style.top)).toBe(before.y);
  });

  it("pans the background, cancels unfinished drags and keeps script actions explicit", () => {
    const input = props();
    const { container } = render(<BlueprintStructureArea {...input} />);
    const viewport = container.querySelector<HTMLElement>(
      "[data-graph-viewport]",
    )!;
    viewport.scrollLeft = 200;
    viewport.scrollTop = 200;
    pointer(viewport, "pointerdown", 100, 100);
    pointer(viewport, "pointermove", 150, 130);
    pointer(viewport, "pointerup", 150, 130);
    expect(viewport.scrollLeft).toBe(150);
    expect(viewport.scrollTop).toBe(170);
    const node = container.querySelector<HTMLElement>(
      '[data-blueprint-node="timeline:main"]',
    )!;
    const left = node.style.left;
    pointer(node, "pointerdown", 100, 100);
    pointer(node, "pointermove", 160, 100);
    pointer(node, "pointercancel", 160, 100);
    expect(node.style.left).toBe(left);
    expect(readStoryPositions(input.project.project_id)).toEqual({});
    pointer(node, "pointerdown", 100, 100);
    pointer(node, "pointerup", 100, 100);
    fireEvent.click(node);
    expect(screen.getByRole("region", { name: "节点分支去向" })).toBeVisible();
    expect(
      screen
        .getAllByText("公开真相")
        .every((item) => item.textContent === "公开真相"),
    ).toBe(true);
    expect(input.onSelectTimeline).not.toHaveBeenCalled();
    fireEvent.click(node.querySelector("[data-graph-action]")!);
    expect(input.onSelectTimeline).toHaveBeenCalledWith("timeline:main");
  });

  it("isolates browser layouts per project and ignores invalid saved positions", () => {
    const input = props();
    window.localStorage.setItem(
      storyMapStorageKey(input.project.project_id),
      JSON.stringify({
        "timeline:main": { x: 100, y: 400 },
        invalid: { x: "bad", y: 0 },
      }),
    );
    expect(readStoryPositions(input.project.project_id)).toEqual({
      "timeline:main": { x: 100, y: 400 },
    });
    const { container, rerender } = render(
      <BlueprintStructureArea {...input} />,
    );
    expect(
      container.querySelector<HTMLElement>(
        '[data-blueprint-node="timeline:main"]',
      )!.style.top,
    ).toBe("400px");
    rerender(
      <BlueprintStructureArea
        {...input}
        project={{ ...input.project, project_id: "another" }}
      />,
    );
    expect(
      container.querySelector<HTMLElement>(
        '[data-blueprint-node="timeline:main"]',
      )!.style.top,
    ).not.toBe("400px");
  });

  it("expands outside the workspace transform while preserving pan and wheel handlers", () => {
    const input = props();
    const { container } = render(<BlueprintStructureArea {...input} />);
    const viewport = container.querySelector<HTMLElement>(
      "[data-graph-viewport]",
    )!;
    viewport.scrollLeft = 220;
    viewport.scrollTop = 100;
    fireEvent.click(screen.getByRole("button", { name: "展开剧情地图" }));
    const expanded = document.querySelector<HTMLElement>(
      '[data-graph-expanded="true"]',
    )!;
    expect(expanded.parentElement).toBe(document.body);
    const fullViewport = expanded.querySelector<HTMLElement>(
      "[data-graph-viewport]",
    )!;
    expect(fullViewport.scrollLeft).toBe(220);
    expect(fullViewport.scrollTop).toBe(100);
    fireEvent.wheel(fullViewport, { deltaY: 120, shiftKey: true });
    expect(fullViewport.scrollLeft).toBe(340);
    fireEvent.keyDown(expanded, { key: "Escape" });
    const restored = container.querySelector<HTMLElement>(
      "[data-graph-viewport]",
    )!;
    expect(restored.scrollLeft).toBe(340);
    fireEvent.wheel(restored, { deltaY: -40, shiftKey: true });
    expect(restored.scrollLeft).toBe(300);
    fireEvent.click(screen.getByRole("button", { name: "展开剧情地图" }));
    fireEvent.click(
      document.querySelector(
        '[data-blueprint-node="timeline:main"] [data-graph-action]',
      )!,
    );
    expect(document.querySelector('[data-graph-expanded="true"]')).toBeNull();
    expect(input.onSelectTimeline).toHaveBeenCalledWith("timeline:main");
  });
});

const edge = (
  source: string,
  target: string,
  i = 0,
): NarrativeEdgeDocument => ({
  edge_id: `${source}-${target}-${i}`,
  source_timeline_id: source,
  target_timeline_id: target,
  label: `选择 ${i}`,
  prompt: "下一步？",
});

// Same 37-node / 62-link branching and merging density as the reported project.
const adjacency = [
  [1, 2],
  [3, 4],
  [3, 4],
  [5, 6],
  [5, 6],
  [7, 8],
  [7, 8],
  [9, 10],
  [9, 10],
  [11, 12],
  [13, 14],
  [15, 16],
  [17, 18],
  [19, 20],
  [21, 22],
  [23, 24],
  [23, 24],
  [23, 24],
  [23, 24],
  [23, 26],
  [23, 26],
  [23, 24, 25],
  [23, 24, 25],
  [27, 34, 32],
  [30, 29, 31],
  [28, 29, 32],
  [28, 33, 35],
  [],
  [],
  [],
  [],
  [36],
  [36],
  [],
  [],
  [],
  [],
];
const ids = adjacency.map((_, i) => `n${i}`);
const edges = adjacency.flatMap((targets, source) =>
  targets.map((target, i) => edge(`n${source}`, `n${target}`, i)),
);

function expectNoOverlap(positions: Map<string, GraphPoint>) {
  const points = [...positions.values()];
  points.forEach((a, i) =>
    points
      .slice(i + 1)
      .forEach((b) => expect(nodesOverlap(a, b, 0)).toBe(false)),
  );
}

function expectRoutesClear(
  links: NarrativeEdgeDocument[],
  positions: Map<string, GraphPoint>,
) {
  const routes = routeStoryEdges(links, positions);
  expect(routes).toHaveLength(links.length);
  for (const route of routes) {
    expect(route.path).not.toMatch(/NaN|Infinity/);
    for (const sample of route.samples) {
      for (const p of positions.values()) {
        expect(
          sample.x > p.x + 0.001 &&
            sample.x < p.x + GRAPH_NODE_WIDTH - 0.001 &&
            sample.y > p.y + 0.001 &&
            sample.y < p.y + GRAPH_NODE_HEIGHT - 0.001,
          `${route.edge.edge_id} curve passes through a card`,
        ).toBe(false);
      }
    }
    for (let i = 1; i < route.points.length; i++) {
      const a = route.points[i - 1],
        b = route.points[i];
      expect(a.x === b.x || a.y === b.y).toBe(true);
      for (const p of positions.values()) {
        const through =
          a.x === b.x
            ? a.x > p.x &&
              a.x < p.x + GRAPH_NODE_WIDTH &&
              Math.max(a.y, b.y) > p.y &&
              Math.min(a.y, b.y) < p.y + GRAPH_NODE_HEIGHT
            : a.y > p.y &&
              a.y < p.y + GRAPH_NODE_HEIGHT &&
              Math.max(a.x, b.x) > p.x &&
              Math.min(a.x, b.x) < p.x + GRAPH_NODE_WIDTH;
        expect(through, `${route.edge.edge_id} passes through a card`).toBe(
          false,
        );
      }
    }
  }
  return routes;
}

describe("story map layout", () => {
  it("uses a smooth cubic connection with a visible choice label between unobstructed nodes", () => {
    const positions = new Map([
      ["a", { x: 48, y: 300 }],
      ["b", { x: 480, y: 120 }],
    ]);
    const [route] = expectRoutesClear([edge("a", "b")], positions);
    expect(route.path).toContain(" C ");
    expect(route.label.x).toBeGreaterThan(48 + GRAPH_NODE_WIDTH);
    expect(route.label.x).toBeLessThan(480);
    expect(route.labelWidth).toBeGreaterThan(0);
  });
  it("keeps all 37 nodes separate and routes all 62 choices outside cards", () => {
    expect(ids).toHaveLength(37);
    expect(edges).toHaveLength(62);
    const positions = layoutStoryNodes(ids, edges);
    expectNoOverlap(positions);
    const routes = expectRoutesClear(edges, positions);
    // Dense merge paths have separate source ports, destination ports and lanes.
    const intoMerge = routes.filter((r) => r.edge.target_timeline_id === "n23");
    expect(new Set(intoMerge.map((r) => r.target.y)).size).toBe(
      intoMerge.length,
    );
    expect(new Set(intoMerge.map((r) => r.points[2].x)).size).toBe(
      intoMerge.length,
    );
    expect(
      layoutStoryNodes(
        ids,
        edges.map((e) => ({ ...e, label: "修改文案" })),
      ),
    ).toEqual(positions);
  });

  it("reduces a crossed diamond and centres its single root", () => {
    const positions = layoutStoryNodes(
      ["root", "a", "b", "c", "d"],
      [edge("root", "a"), edge("root", "b"), edge("a", "d"), edge("b", "c")],
    );
    expect(Math.sign(positions.get("a")!.y - positions.get("b")!.y)).toBe(
      Math.sign(positions.get("d")!.y - positions.get("c")!.y),
    );
    expect(positions.get("root")!.y).toBe(
      (positions.get("a")!.y + positions.get("b")!.y) / 2,
    );
  });

  it("handles loops, disconnected nodes and missing endpoints without runaway columns", () => {
    const links = [
      edge("a", "b"),
      edge("b", "a"),
      edge("b", "b"),
      edge("missing", "a"),
    ];
    const positions = layoutStoryNodes(["a", "b", "isolated"], links);
    expectNoOverlap(positions);
    expect(Math.max(...[...positions.values()].map((p) => p.x))).toBeLessThan(
      1200,
    );
    expectRoutesClear(links.slice(0, 3), positions);
    expect(routeStoryEdges(links, positions)).toHaveLength(3);
  });

  it("routes a skipped layer and a backwards dragged branch around intervening nodes", () => {
    const positions = new Map([
      ["a", { x: 48, y: 200 }],
      ["obstacle", { x: 450, y: 200 }],
      ["b", { x: 900, y: 200 }],
    ]);
    expectRoutesClear([edge("a", "b"), edge("b", "a")], positions);
  });

  it("preserves browser positions and places arriving nodes without collisions", () => {
    const automatic = layoutStoryNodes(ids, edges);
    const saved = Object.fromEntries(automatic);
    saved.n15 = freeNodePosition(
      { x: saved.n15.x + 80, y: saved.n15.y - 40 },
      [...automatic].filter(([id]) => id !== "n15").map(([, p]) => p),
    );
    const updated = layoutStoryNodes(
      [...ids, "new"],
      [...edges, edge("n14", "new")],
    );
    const applied = applySavedPositions(updated, saved);
    for (const id of ids) expect(applied.get(id)).toEqual(saved[id]);
    expectNoOverlap(applied);
    expectRoutesClear(edges, applied);
    const dropped = freeNodePosition(
      applied.get("n16")!,
      [...applied].filter(([id]) => id !== "n15").map(([, p]) => p),
    );
    expect(nodesOverlap(dropped, applied.get("n16")!)).toBe(false);
  });
});

function renderPage(entry = "/project/p1") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <NavigationRuntime />
      <Routes>
        <Route path="/project/:id" element={<BlueprintPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Interactive projects on the Blueprint page", () => {
  beforeEach(() => {
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useWorkGraphStore.getState().reset();
  });

  it("branching projects keep the structure graph and the bundle export entry", () => {
    const project = cloneProject();
    project.narrative_edges = [
      {
        edge_id: "edge:a",
        source_timeline_id: "timeline:main",
        target_timeline_id: "timeline:ep2",
        label: "选择A · 星夜归途",
        prompt: "此刻，你决定——",
      },
    ];
    seedProject(project);
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="branching"]'),
    ).toBeInTheDocument();
    expect(container.querySelector("[data-export-bundle]")).toBeInTheDocument();
    const title = screen.getByText("视频脚本");
    const entry = screen.getByRole("button", { name: "交互设计" });
    expect(entry.parentElement).toBe(title.parentElement);
    fireEvent.click(entry);
    expect(
      screen.getByRole("region", { name: "交互设计工作台" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "返回蓝图" }));
    expect(
      container.querySelector("[data-interaction-design-panel]"),
    ).not.toBeVisible();
    expect(
      screen.getByRole("button", { name: "下载 / 导出" }),
    ).toBeInTheDocument();
  });

  it("presents an explicit interactive request before the first Agent patch or story node", () => {
    const project = singleProject();
    project.settings.content_type = null;
    project.description =
      "我想做一个互动式视频，要求和剧本如下：\n\n《七日夺嫡》";
    project.visual.entities = { order: [], items: {} };
    project.assets.artifact_slots_by_id = {};
    Object.assign(project.timelines.items["timeline:main"], {
      title: "",
      synopsis: "",
      description: "",
      elements_by_id: {},
    });
    seedProject(project);
    const { container } = renderPage();

    expect(
      container.querySelector(
        '[data-blueprint-shape="branching"] [data-graph-toolbar]',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("分支连接尚未建立")).toBeInTheDocument();
    expect(
      container.querySelector('[data-workspace-empty="interactive"]'),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("[data-blueprint-node]")).toHaveLength(0);
    expect(container.querySelector("[data-export-bundle]")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "交互设计" }));
    expect(
      screen.getByRole("region", { name: "交互设计工作台" }),
    ).toBeVisible();
    expect(screen.getByRole("tab", { name: "首页" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "剧情地图" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "生成作品页面" }),
    ).not.toBeInTheDocument();
  });

  it("keeps unconnected interactive drafts as story nodes and adds paths when a later snapshot supplies edges", () => {
    const project = cloneProject();
    project.settings.content_type = "interactive_branching_drama";
    project.description = "皇帝暴毙于御书房，七日封城。";
    seedProject(project);
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="branching"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-blueprint-shape="linear"]'),
    ).not.toBeInTheDocument();
    expect(container.querySelectorAll("[data-blueprint-node]")).toHaveLength(2);
    expect(screen.queryByText("结局")).not.toBeInTheDocument();
    expect(container.querySelector("[data-export-bundle]")).toBeDisabled();
    fireEvent.click(
      container.querySelector(
        '[data-blueprint-node="timeline:ep2"]',
      ) as HTMLElement,
    );
    expect(useCreatorInteractionStore.getState().selectedRef).toBe(
      "timeline:timeline:ep2",
    );

    const connected = structuredClone(project);
    connected.narrative_edges = [
      {
        edge_id: "edge:a",
        source_timeline_id: "timeline:main",
        target_timeline_id: "timeline:ep2",
        label: "公开保留",
        prompt: "血诏怎么办？",
      },
    ];
    act(() => seedProject(connected));
    expect(screen.queryByText("分支连接尚未建立")).not.toBeInTheDocument();
    expect(container.querySelectorAll("[data-graph-edge]")).toHaveLength(1);
    fireEvent.click(
      container.querySelector(
        '[data-blueprint-node="timeline:main"]',
      ) as HTMLElement,
    );
    expect(
      container.querySelector('[data-graph-edge-label="edge:a"]'),
    ).toHaveTextContent("公开保留");
    expect(screen.getAllByText("结局")).toHaveLength(1);
    expect(
      container.querySelector('[data-blueprint-shape="linear"]'),
    ).not.toBeInTheDocument();
  });
});

function installComposerMockFetch(
  routes: Parameters<typeof installMockFetch>[0],
) {
  return installMockFetch([
    {
      match: "/models/config",
      response: { json: configuredModelConfig },
    },
    ...routes,
  ]);
}

/** The project-created payload returned by POST /projects. */
const created = (
  projectId: string,
  creatorSessionId: string,
  conversationId: string,
) => ({
  projectId,
  creatorSessionId,
  conversationId,
  projectSnapshotId: `snapshot-${projectId}`,
  header: {},
});

function renderComposer(onClose = vi.fn()) {
  render(
    <MemoryRouter>
      <ProjectComposer open onClose={onClose} />
    </MemoryRouter>,
  );
  return onClose;
}

const fill = (placeholder: RegExp | string, value: string) =>
  fireEvent.change(screen.getByPlaceholderText(placeholder), {
    target: { value },
  });

describe("Interactive intent at project creation", () => {
  // The model-config snapshot is a module-level singleton; a previous test's
  // fetch must not leak into the next render's synchronous assertions.
  beforeEach(() => {
    useModelConfigStore.setState({ config: null });
  });

  it.each([
    [
      "我想做一个互动式视频，要求和剧本如下：\n\n《七日夺嫡》",
      "interactive_branching_drama",
    ],
    ["制作一个非常有趣的互动短剧", "interactive_branching_drama"],
    [
      "Create an interactive film with two endings",
      "interactive_branching_drama",
    ],
    ["制作一个线性短剧，不要互动式视频", null],
    ["Create a non-interactive video", null],
    ["制作一部短剧，人物之间需要自然的互动", null],
    ["制作线性剧集\n\n剧本对白：我想做互动式视频。", null],
  ])(
    "persists the narrative format at creation for %s",
    async (brief, contentType) => {
      const { calls } = installComposerMockFetch([
        {
          match: "/projects",
          response: { json: created("p-format", "s-format", "c-format") },
        },
      ]);
      renderComposer();
      fill(/^例：霸道总裁短剧/, brief!);
      fireEvent.click(screen.getByRole("button", { name: /启动 Agent/ }));
      await waitFor(() =>
        expect(
          calls.find((call) => call.url.endsWith("/projects"))?.body,
        ).toMatchObject({
          scenario: "short_drama",
          contentType,
          ...(contentType ? { nameSource: "auto" } : {}),
          initialGoal: brief,
        }),
      );
    },
  );
});

describe("filenameFromDisposition", () => {
  it("prefers the RFC 5987 UTF-8 name, falls back to the plain one", () => {
    expect(
      filenameFromDisposition(
        "attachment; filename=\"雾.zip\"; filename*=UTF-8''%E9%9B%BE%E5%B1%B1.zip",
      ),
    ).toBe("雾山.zip");
    expect(filenameFromDisposition('attachment; filename="plain.zip"')).toBe(
      "plain.zip",
    );
    expect(filenameFromDisposition(null)).toBeNull();
  });
});
