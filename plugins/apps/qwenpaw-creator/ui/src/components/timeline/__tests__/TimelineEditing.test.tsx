import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TimelineTracks from "@/components/timeline/TimelineTracks";
import { projectDocument } from "@/test/creatorFixtures";
import type {
  ProjectDocument,
  TimelineDocument,
  TimelineElementDocument,
  EditPlanDocument,
} from "@/contracts/creator";
import type { ElementPlaybackStatus } from "@/selectors/elementPlaybackSelectors";
import ElementDetail from "@/components/timeline/ElementDetail";
import EditPlanCard from "@/components/timeline/EditPlanCard";
import {
  buildSpanOperations,
  collectSnapTicks,
  minDurationTick,
  resolveSpanDrag,
  splitTransitionsForDisplay,
  transitionFollowChanges,
  transitionOverlapWindow,
} from "@/lib/timelineEditing";

function setup(overrides: Partial<Parameters<typeof TimelineTracks>[0]> = {}) {
  const project = structuredClone(projectDocument) as ProjectDocument;
  const timeline = project.timelines.items["timeline:main"] as TimelineDocument;
  const props = {
    project,
    timeline,
    authorityTimeline: overrides.timeline ?? timeline,
    durationTick: 20000,
    playheadTick: 2000,
    zoom: 1,
    snapEnabled: false,
    collapsed: false,
    previewOpen: false,
    editable: true,
    selectedElementId: null as string | null,
    playbackStates: new Map<string, ElementPlaybackStatus>(),
    agentWorking: false,
    onPlayheadChange: vi.fn(),
    onSelectElement: vi.fn(),
    onActiveElementIdsChange: vi.fn(),
    onDragOverridesChange: vi.fn(),
    onCommitSpans: vi.fn(),
    onZoomChange: vi.fn(),
    ...overrides,
  };
  const utils = render(<TimelineTracks {...props} />);
  const chart = utils.container.querySelector(
    "[data-timeline-chart]",
  ) as HTMLDivElement;
  vi.spyOn(chart, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 100,
    right: 692,
    bottom: 300,
    width: 692,
    height: 200,
  } as DOMRect);
  return { ...utils, props, chart };
}

function query(root: ParentNode, selector: string) {
  return root.querySelector(selector) as HTMLElement;
}

/** Full pointer down → move → up gesture with a shared pointer id. */
function drag(
  down: Element,
  move: Element,
  pointerId: number,
  fromX: number,
  toX: number,
  extra: Record<string, unknown> = {},
) {
  fireEvent.pointerDown(down, {
    button: 0,
    pointerId,
    clientX: fromX,
    ...extra,
  });
  fireEvent.pointerMove(move, { pointerId, clientX: toX, ...extra });
  fireEvent.pointerUp(move, { button: 0, pointerId, clientX: toX, ...extra });
}

const committed = (elementId: string, start: number, duration: number) => ({
  elementId,
  span: { start_tick: start, duration_tick: duration },
});

/** Second clip overlapping edit-opening by 1s, bridged by a crossfade. */
function withBridgedSecondClip() {
  const project = structuredClone(projectDocument) as ProjectDocument;
  const timeline = project.timelines.items["timeline:main"] as TimelineDocument;
  timeline.elements_by_id["edit-second"] = {
    ...structuredClone(timeline.elements_by_id["edit-opening"]),
    element_id: "edit-second",
    label: "第二段素材",
    span: { start_tick: 7000, duration_tick: 8000 },
  };
  timeline.elements_by_id["transition-2"] = {
    ...structuredClone(timeline.elements_by_id.transition),
    element_id: "transition-2",
    label: "片段间转场",
    span: { start_tick: 7000, duration_tick: 1000 },
    creation: {
      type: "transition",
      from_element_id: "edit-opening",
      to_element_id: "edit-second",
      transition_kind: "crossfade",
      easing: "ease-in-out",
    },
  };
  return { project, timeline };
}

describe("TimelineTracks direct manipulation", () => {
  it("renders transitions as junction badges instead of track blocks", () => {
    const { container } = setup();
    expect(
      container.querySelector('[data-element-block="transition"]'),
    ).not.toBeInTheDocument();
    // Junction badge sits at the transition center: 7500/20000 = 37.5%.
    const badge = query(container, '[data-transition-junction="transition"]');
    expect(badge.style.left).toBe("37.5%");
  });

  it("trims a bridged clip past the cut by shrinking the transition (FCP style)", () => {
    const { container, props } = setup({
      ...withBridgedSecondClip(),
      selectedElementId: "edit-opening",
    });
    const handle = query(
      container,
      '[data-element-block="edit-opening"] [data-element-trim="end"]',
    );
    const block = query(container, '[data-element-block="edit-opening"]');
    // Trim the outgoing clip left by 1500 ticks (-45px): real end 8000→6500
    // would sever the transition, so the trim clamps at the minimum overlap
    // (edit-second.start + 100 = 7100) and the transition shrinks into it.
    fireEvent.pointerDown(handle, { button: 0, pointerId: 41, clientX: 300 });
    fireEvent.pointerMove(block, { pointerId: 41, clientX: 255 });
    fireEvent.pointerUp(block, { button: 0, pointerId: 41, clientX: 255 });
    expect(props.onCommitSpans).toHaveBeenCalledWith([
      committed("edit-opening", 0, 7100),
      // The cross-track fixture transition (edit-opening → r2v-window) also
      // follows back inside the shrunken overlap.
      committed("transition", 6100, 1000),
      committed("transition-2", 7000, 100),
    ]);
  });

  it("selects an element on plain click without committing spans", () => {
    const { container, props } = setup();
    const block = query(container, '[data-element-block="edit-opening"]');
    fireEvent.pointerDown(block, { button: 0, pointerId: 3, clientX: 120 });
    fireEvent.pointerUp(block, { button: 0, pointerId: 3, clientX: 121 });
    fireEvent.click(block);
    expect(props.onSelectElement).toHaveBeenCalledWith("edit-opening");
    expect(props.onCommitSpans).not.toHaveBeenCalled();
  });

  it("moves a block with a live drag tip and commits the validated span", () => {
    const { container, props } = setup();
    const block = query(container, '[data-element-block="edit-opening"]');
    // Lane width = 692 - 68 - 24 = 600px for 20000 ticks → 30px = 1000 ticks.
    fireEvent.pointerDown(block, { button: 0, pointerId: 5, clientX: 100 });
    fireEvent.pointerMove(block, { pointerId: 5, clientX: 130 });
    const overrides = vi
      .mocked(props.onDragOverridesChange)
      .mock.calls.at(-1)?.[0] as Map<string, unknown>;
    expect(overrides.get("edit-opening")).toEqual({
      start_tick: 1000,
      duration_tick: 8000,
    });
    // A time tip chip follows the gesture and disappears on release.
    expect(query(container, "[data-timeline-drag-tip]")).toHaveTextContent(
      "1s – 9s · 8s",
    );
    fireEvent.pointerUp(block, { button: 0, pointerId: 5, clientX: 130 });
    expect(props.onCommitSpans).toHaveBeenCalledWith([
      committed("edit-opening", 1000, 8000),
    ]);
    expect(container.querySelector("[data-timeline-drag-tip]")).toBeNull();
    // The click fired after a drag must not change the selection.
    fireEvent.click(block);
    expect(props.onSelectElement).not.toHaveBeenCalled();
  });

  it("clamps a move so an attached transition keeps its minimum overlap", () => {
    const { container, props } = setup();
    const block = query(container, '[data-element-block="r2v-window"]');
    // +150px = +5000 ticks would leave no overlap: the move clamps at the
    // minimum 0.1s overlap and the transition shrinks instead of vetoing.
    drag(block, block, 7, 200, 350);
    expect(props.onCommitSpans).toHaveBeenCalledWith([
      committed("r2v-window", 7900, 10000),
      committed("transition", 7900, 100),
    ]);
  });

  it("starts a range selection with shift+drag even on top of blocks", () => {
    const { container, chart, props } = setup();
    const block = query(container, '[data-element-block="edit-opening"]');
    // Shift+drag on a block selects a range: no overrides/commit, toolbar on.
    fireEvent.pointerDown(block, {
      button: 0,
      pointerId: 31,
      clientX: 110,
      shiftKey: true,
    });
    drag(chart, chart, 31, 110, 290, { shiftKey: true });
    expect(props.onDragOverridesChange).not.toHaveBeenCalled();
    expect(props.onCommitSpans).not.toHaveBeenCalled();
    expect(
      container.querySelector("[data-timeline-selection-range]"),
    ).toBeInTheDocument();
    expect(
      document.querySelector("[data-timeline-selection-toolbar]"),
    ).toBeInTheDocument();
    expect(props.onActiveElementIdsChange).toHaveBeenCalledWith([
      "edit-opening",
      "audio-bgm",
      "overlay-title",
      "r2v-window",
      "overlay-os",
    ]);
  });

  it("scrubs the playhead by press-dragging the ruler", () => {
    const { container, props } = setup();
    const ruler = query(container, "[data-timeline-scale]");
    // Lane width 600px for 20000 ticks; x includes 12px padding + 68px labels
    // → clientX 380 ≈ tick 10000, clientX 230 ≈ tick 5000.
    drag(ruler, ruler, 21, 380, 230);
    expect(
      vi.mocked(props.onPlayheadChange).mock.calls.map(([tick]) => tick),
    ).toEqual([10000, 5000]);
    // Scrubbing must not open the range-selection toolbar.
    expect(
      document.querySelector("[data-timeline-selection-toolbar]"),
    ).not.toBeInTheDocument();
  });

  it("drags the junction badge within the from/to overlap window", () => {
    const { container, props } = setup();
    const badge = query(container, '[data-transition-junction="transition"]');
    // Transition [7000,8000] inside window [5000,8000]; dragging right is
    // clamped so the span cannot leave the overlap.
    drag(badge, badge, 8, 400, 520);
    expect(props.onCommitSpans).toHaveBeenCalledWith([
      committed("transition", 7000, 1000),
    ]);
  });
});

describe("TimelineTracks subtitle lane switch", () => {
  it("carries the batch caption toggle inside the CC row header", () => {
    const onToggle = vi.fn();
    const { container, props } = setup({
      subtitleToggle: { enabled: true, count: 2, busy: false, onToggle },
    });
    const button = query(container, "[data-subtitle-toggle]");
    // The switch is the lane's own caption icon, not a separate control.
    expect(button.closest('[data-track="subtitle"]')).not.toBeNull();
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
    // Toggling captions must not also select the whole lane or seek.
    expect(props.onActiveElementIdsChange).not.toHaveBeenCalled();
    expect(props.onPlayheadChange).not.toHaveBeenCalled();
  });

  it("marks the switch off, keeps one copy per timeline", () => {
    const project = structuredClone(projectDocument) as ProjectDocument;
    const timeline = project.timelines.items[
      "timeline:main"
    ] as TimelineDocument;
    // An overlapping copy overlay forces a second CC lane; a timeline-wide
    // switch must not be duplicated on every lane header.
    timeline.elements_by_id["overlay-late"] = {
      ...structuredClone(timeline.elements_by_id["overlay-title"]),
      element_id: "overlay-late",
      span: { start_tick: 3000, duration_tick: 2000 },
    };
    const { container } = setup({
      project,
      timeline,
      subtitleToggle: {
        enabled: false,
        count: 3,
        busy: true,
        onToggle: vi.fn(),
      },
    });
    const switches = container.querySelectorAll("[data-subtitle-toggle]");
    expect(switches).toHaveLength(1);
    expect(switches[0].getAttribute("aria-pressed")).toBe("true");
    expect(switches[0]).toBeDisabled();

    const plain = setup();
    expect(
      plain.container.querySelector("[data-subtitle-toggle]"),
    ).not.toBeInTheDocument();
  });
});

describe("Timeline details", () => {
  function setup(elementId: string) {
    const project = structuredClone(projectDocument) as ProjectDocument;
    const timeline = project.timelines.items[
      "timeline:main"
    ] as TimelineDocument;
    const element = timeline.elements_by_id[
      elementId
    ] as TimelineElementDocument;
    const utils = render(
      <ElementDetail
        project={project}
        timeline={timeline}
        element={element}
        tasks={[]}
        applying={false}
        dirtyCount={0}
        conflictPaths={[]}
        onClose={vi.fn()}
        onChange={vi.fn()}
        onApply={vi.fn()}
        onDiscard={vi.fn()}
        onAcceptConflicts={vi.fn()}
        onOpenWorkbench={vi.fn()}
      />,
    );
    const advanced = utils.container.querySelector(
      "[data-element-detail-advanced]",
    ) as HTMLDetailsElement | null;
    const summary = advanced?.querySelector("summary");
    if (summary) fireEvent.click(summary);
    return { ...utils, advanced };
  }

  /**
   * CR #252 P1: the guard on the numeric start / duration / z-index block was
   * inverted — the fields the caption editor is meant to drop showed up for
   * captions instead, while video and audio elements lost them.
   */
  describe("ElementDetail advanced time/layer editors", () => {
    it("hides the numeric time/layer fields for a caption overlay", () => {
      const { advanced } = setup("overlay-title");
      expect(advanced).not.toBeNull();
      expect(advanced!.textContent).not.toContain("时间与层");
    });

    it("keeps the numeric time/layer fields for a non-caption element", () => {
      const { advanced } = setup("r2v-window");
      expect(advanced).not.toBeNull();
      expect(advanced!.textContent).toContain("时间与层");
    });
  });

  const plan: EditPlanDocument = {
    concept: "猫的越狱日记",
    dials: { energy: "high", density: "mid", decoration: "low" },
    signature_device: "爪印转场",
    pacing: "hook 1.2s，动静交替",
    design_floor: {
      opening: "1.5s 标题卡",
      transitions: "硬切 + 爪印族",
      body: "每场景一个设计节拍",
      ending: "定格硬停",
    },
    mechanical_exemption: false,
    scene_ledger: [
      {
        scene_id: "scene-1",
        label: "越狱",
        element_ids: ["el-1"],
        status: "locked",
        review_round: 1,
      },
      {
        scene_id: "scene-2",
        label: "追逐",
        element_ids: ["el-2"],
        status: "draft",
        review_round: 0,
      },
    ],
  };

  describe("EditPlanCard", () => {
    it("shows the concept and the ledger lock ratio, expanding on click", () => {
      render(<EditPlanCard editPlan={plan} />);
      expect(screen.getByText("猫的越狱日记")).toBeInTheDocument();
      expect(screen.getByText(/1\/2/)).toBeInTheDocument();
      expect(screen.queryByText(/爪印转场/)).toBeNull();
      fireEvent.click(screen.getByRole("button"));
      expect(screen.getByText(/爪印转场/)).toBeInTheDocument();
      expect(screen.getByText(/1\.5s 标题卡/)).toBeInTheDocument();
      expect(screen.getByText("越狱")).toBeInTheDocument();
      expect(screen.getByText("追逐")).toBeInTheDocument();
    });
  });
});

function fixtureTimeline(): TimelineDocument {
  return structuredClone(
    projectDocument.timelines.items["timeline:main"],
  ) as TimelineDocument;
}

function resolveDrag(
  timeline: TimelineDocument,
  elementId: string,
  mode: "move" | "trim-start" | "trim-end",
  deltaTick: number,
  snap?: { snapTicks: number[]; snapThresholdTick: number },
) {
  const element = timeline.elements_by_id[elementId];
  return resolveSpanDrag({
    timeline,
    element,
    mode,
    originSpan: element.span,
    deltaTick,
    snapEnabled: Boolean(snap),
    snapTicks: snap?.snapTicks ?? [],
    snapThresholdTick: snap?.snapThresholdTick ?? 0,
  });
}

describe("timelineEditing span drags", () => {
  it("clamps move drags at tick zero and enforces the 0.1s duration floor", () => {
    const moved = resolveDrag(
      fixtureTimeline(),
      "r2v-window",
      "move",
      -9999999,
    );
    expect(moved.span.start_tick).toBe(0);
    expect(moved.span.duration_tick).toBe(10000);

    const timeline = fixtureTimeline();
    const element = timeline.elements_by_id["overlay-title"];
    const floor = minDurationTick(timeline.ticks_per_second);
    const trimmedEnd = resolveDrag(
      timeline,
      "overlay-title",
      "trim-end",
      -999999,
    );
    expect(trimmedEnd.span.duration_tick).toBe(floor);
    const trimmedStart = resolveDrag(
      timeline,
      "overlay-title",
      "trim-start",
      999999,
    );
    expect(trimmedStart.span.duration_tick).toBe(floor);
    expect(trimmedStart.span.start_tick + trimmedStart.span.duration_tick).toBe(
      element.span.start_tick + element.span.duration_tick,
    );
  });

  it("snaps a moved block edge onto a neighbour edge", () => {
    const timeline = fixtureTimeline();
    const snapTicks = collectSnapTicks(timeline, new Set(["overlay-os"]), [0]);
    // Origin start 6000; drag towards edit-opening's end (8000).
    const result = resolveDrag(timeline, "overlay-os", "move", 1940, {
      snapTicks,
      snapThresholdTick: 120,
    });
    expect(result.span.start_tick).toBe(8000);
    expect(result.snapTick).toBe(8000);
  });

  it("keeps a transition inside its from/to overlap window", () => {
    const timeline = fixtureTimeline();
    expect(
      transitionOverlapWindow(timeline, timeline.elements_by_id.transition),
    ).toEqual({ startTick: 5000, endTick: 8000 });
    const draggedLeft = resolveDrag(timeline, "transition", "move", -99999);
    expect(draggedLeft.span.start_tick).toBe(5000);
    const draggedRight = resolveDrag(timeline, "transition", "move", 99999);
    expect(draggedRight.span.start_tick + draggedRight.span.duration_tick).toBe(
      8000,
    );
  });
});

describe("timelineEditing transition follow", () => {
  const follow = (start_tick: number) =>
    transitionFollowChanges(fixtureTimeline(), [
      { elementId: "r2v-window", span: { start_tick, duration_tick: 10000 } },
    ]);

  it("keeps, shrinks or vetoes the transition as overlap changes", () => {
    expect(follow(6000)).toEqual({ ok: true, changes: [] });

    const shrunk = follow(7500);
    expect(shrunk.ok).toBe(true);
    if (shrunk.ok) {
      expect(shrunk.changes).toEqual([
        {
          elementId: "transition",
          span: { start_tick: 7500, duration_tick: 500 },
        },
      ]);
    }

    const vetoed = follow(9000);
    expect(vetoed.ok).toBe(false);
    if (vetoed.ok === false) {
      expect(vetoed.reason).toContain("转场");
    }
  });
});

describe("timelineEditing patch building", () => {
  it("emits replace operations only for changed span fields", () => {
    const operations = buildSpanOperations(fixtureTimeline(), "timeline:main", [
      {
        elementId: "edit-opening",
        span: { start_tick: 1000, duration_tick: 8000 },
      },
    ]);
    expect(operations).toEqual([
      {
        op: "replace",
        path: "/timelines/items/timeline:main/elements_by_id/edit-opening/span/start_tick",
        before: 0,
        value: 1000,
      },
    ]);
  });
});

describe("timelineEditing transition display split", () => {
  it("lifts resolvable transitions into junctions and keeps dangling ones as orphans", () => {
    const resolved = splitTransitionsForDisplay(fixtureTimeline());
    expect(resolved.junctions).toHaveLength(1);
    expect(resolved.junctions[0].transition.element_id).toBe("transition");
    expect(resolved.junctions[0].centerTick).toBe(7500);
    expect(resolved.orphanTransitionIds.size).toBe(0);

    const timeline = fixtureTimeline();
    const transition = timeline.elements_by_id.transition;
    if (transition.creation.type === "transition") {
      transition.creation.to_element_id = "missing-element";
    }
    const dangling = splitTransitionsForDisplay(timeline);
    expect(dangling.junctions).toHaveLength(0);
    expect(dangling.orphanTransitionIds.has("transition")).toBe(true);
  });
});

/*
 * Regression: dragging a clip across a neighbour (out-of-order transition
 * data) must still resolve to a valid span with the bridging transition
 * following into the new overlap — mirrors the roof-climb restore scenario.
 */
function el(
  id: string,
  type: string,
  start: number,
  dur: number,
  extra: Record<string, unknown> = {},
) {
  return {
    element_id: id,
    enabled: true,
    span: { start_tick: start, duration_tick: dur },
    z_index: 0,
    creation: { type, ...extra },
    outputs: {},
    render_source: null,
  };
}

const xf = (id: string, at: number, from: string, to: string) =>
  el(id, "transition", at, 1000, {
    from_element_id: from,
    to_element_id: to,
    transition_kind: "crossfade",
    easing: "ease-in-out",
  });

const chain: Array<[string, number, string, string]> = [
  ["xf1", 13570, "edit-roof-climb", "edit-pond-drink"],
  ["xf2", 22000, "edit-pond-drink", "edit-cat-face"],
];

const tl = {
  timeline_id: "timeline:main",
  name: "",
  description: "",
  ticks_per_second: 1000,
  elements_by_id: {
    "edit-pond-drink": el("edit-pond-drink", "edit", 11000, 12000),
    "edit-roof-climb": el("edit-roof-climb", "edit", 13570, 12000),
    "edit-cat-face": el("edit-cat-face", "edit", 22000, 10000),
    ...Object.fromEntries(
      chain.map(([id, at, from, to]) => [id, xf(id, at, from, to)]),
    ),
  },
} as unknown as TimelineDocument;

describe("cross-neighbour move with transition follow", () => {
  it("moves roof back to 0 with xf follow", () => {
    const snapTicks = collectSnapTicks(
      tl,
      new Set(["edit-roof-climb"]),
      [0, 0, 60000],
    );
    const result = resolveDrag(tl, "edit-roof-climb", "move", -13570, {
      snapTicks,
      snapThresholdTick: 431,
    });
    const follow = transitionFollowChanges(tl, [
      { elementId: "edit-roof-climb", span: result.span },
    ]);
    expect(follow.ok).toBe(true);
  });
});
