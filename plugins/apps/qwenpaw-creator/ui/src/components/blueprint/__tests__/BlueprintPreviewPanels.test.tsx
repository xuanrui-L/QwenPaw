import { cloneProject } from "@/test/projectSnapshotFixtures";
import TimelineCanvas from "@/components/timeline/TimelineCanvas";
import TimelineLivePreview from "@/components/timeline/TimelineLivePreview";
import {
  hasTimelinePreviewContent,
  roughCutFrameForElement,
} from "@/selectors/blueprintSelectors";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import BlueprintRoughCutStrip from "@/components/blueprint/BlueprintRoughCutStrip";
import { projectDocument } from "@/test/creatorFixtures";
import type { ProjectDocument } from "@/contracts/creator";
import BlueprintScriptPanel from "@/components/blueprint/BlueprintScriptPanel";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { message } from "antd";

/**
 * A whole film only exists for a single live timeline whose render slot
 * carries a fresh, user-selected video version (the selector's contract).
 * Trim the fixture to its first episode; `final-v1` is that slot's
 * currently selected, non-stale render.
 */
function withWholeFilm(project: ProjectDocument): ProjectDocument {
  project.timelines.order = ["timeline:main"];
  return project;
}

/** Two outgoing edges from the entry timeline = a branching choice point. */
function withBranching(project: ProjectDocument): ProjectDocument {
  project.narrative_edges = [
    {
      edge_id: "edge:a",
      source_timeline_id: "timeline:main",
      target_timeline_id: "timeline:ep2",
      label: "选择A · 星夜归途",
      prompt: "此刻，你决定——",
    },
    {
      edge_id: "edge:b",
      source_timeline_id: "timeline:main",
      target_timeline_id: "timeline:ep2",
      label: "选择B · 回到晨光",
      prompt: "",
    },
  ];
  return project;
}

function renderStrip(project: ProjectDocument) {
  return render(
    <BlueprintRoughCutStrip project={project} onSelectTimeline={vi.fn()} />,
  );
}

it("loads only visible video thumbnails and releases them when scrolled away", () => {
  const observers: {
    callback: IntersectionObserverCallback;
    target?: Element;
    disconnect: ReturnType<typeof vi.fn<() => void>>;
  }[] = [];
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      record: (typeof observers)[number];
      constructor(callback: IntersectionObserverCallback) {
        this.record = { callback, disconnect: vi.fn() };
        observers.push(this.record);
      }
      observe(target: Element) {
        this.record.target = target;
      }
      disconnect() {
        this.record.disconnect();
      }
    },
  );

  try {
    const { container, unmount } = renderStrip(cloneProject());
    expect(observers.length).toBeGreaterThan(1);
    expect(container.querySelectorAll("video")).toHaveLength(0);
    const first = observers[0];
    const intersect = (visible: boolean) =>
      act(() =>
        first.callback(
          [
            { isIntersecting: visible, target: first.target },
          ] as IntersectionObserverEntry[],
          {} as IntersectionObserver,
        ),
      );

    intersect(true);
    expect(container.querySelectorAll("video")).toHaveLength(1);
    const originalSrc = container.querySelector("video")!.getAttribute("src");
    expect(originalSrc).toMatch(/\/media\/(assets|artifacts)\//);

    intersect(false);
    expect(container.querySelectorAll("video")).toHaveLength(0);
    intersect(true);
    expect(container.querySelector("video")!.getAttribute("src")).toBe(
      originalSrc,
    );

    unmount();
    expect(
      observers.every(
        (observer) => observer.disconnect.mock.calls.length === 1,
      ),
    ).toBe(true);
  } finally {
    vi.unstubAllGlobals();
  }
});

describe("Interactive whole-film and branch preview", () => {
  it("branching works require generated pages and never fall back to a fixed interface", async () => {
    const project = withBranching(cloneProject());
    const { container, baseElement, findByText } = renderStrip(project);
    const chip = container.querySelector("[data-roughcut-play-film]");
    expect(chip).toBeTruthy();
    expect(chip!.textContent).toContain("播放整个互动包");
    fireEvent.click(chip!);
    expect(baseElement.querySelector("[data-authored-cinema]")).toBeTruthy();
    expect(await findByText(/作品页面尚未生成/)).toBeInTheDocument();
    expect(
      baseElement.querySelector("[data-roughcut-player] video"),
    ).toBeNull();
    expect(baseElement.querySelector("[data-edge-ref]")).toBeNull();
  });

  it("a composed film cannot substitute for missing authored interactive pages", async () => {
    const project = withBranching(withWholeFilm(cloneProject()));
    const { container, baseElement, findByText } = renderStrip(project);
    fireEvent.click(container.querySelector("[data-roughcut-play-film]")!);
    expect(await findByText(/作品页面尚未生成/)).toBeInTheDocument();
    expect(
      baseElement.querySelector("[data-roughcut-player] video"),
    ).toBeNull();
  });
});

function beforeStoryboards(): ProjectDocument {
  const project = cloneProject();
  project.timelines.order = ["timeline:main"];
  const timeline = project.timelines.items["timeline:main"];
  // Keep only the generated shot, with character references but no shot media.
  timeline.elements_by_id = {
    "r2v-window": timeline.elements_by_id["r2v-window"],
  };
  for (const [id, slot] of Object.entries(
    project.assets.artifact_slots_by_id,
  )) {
    if (
      slot.owner_ref.startsWith("timeline:") ||
      slot.owner_ref.startsWith("element:")
    )
      delete project.assets.artifact_slots_by_id[id];
  }
  return project;
}

function addStoryboard(project: ProjectDocument) {
  project.assets.artifact_slots_by_id["element:r2v-window:storyboard"] = {
    slot_id: "element:r2v-window:storyboard",
    kind: "r2v_storyboard_image",
    owner_ref: "element:r2v-window",
    version_ids: ["storyboard-v1"],
    selected_version_id: "storyboard-v1",
    metadata: {},
  };
  project.assets.artifact_versions_by_id["storyboard-v1"] = {
    ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
    version_id: "storyboard-v1",
    slot_id: "element:r2v-window:storyboard",
    owner_ref: "element:r2v-window",
    kind: "r2v_storyboard_image",
  };
}

it("disables all playback and explains the missing storyboard when only character reference images exist", () => {
  const project = beforeStoryboards();
  withBranching(project);
  const { container, baseElement, getByText } = renderStrip(project);
  expect(getByText("分镜图尚未生成，暂时无法预览")).toBeInTheDocument();
  const buttons = container.querySelectorAll(
    "[data-roughcut-preview-all], [data-roughcut-play], [data-roughcut-play-film]",
  );
  expect(buttons.length).toBeGreaterThanOrEqual(2);
  for (const button of buttons) {
    expect(button).toBeDisabled();
    fireEvent.click(button);
  }
  expect(
    baseElement.querySelector(
      "[data-roughcut-cinema], [data-authored-cinema], video, audio, img",
    ),
  ).toBeNull();
});

it("previews generated storyboard images over the planned duration before shot video exists", () => {
  const project = beforeStoryboards();
  addStoryboard(project);
  const { container, baseElement } = renderStrip(project);
  expect(container.querySelector("[data-roughcut-preview-all]")).toBeEnabled();
  fireEvent.click(container.querySelector("[data-roughcut-preview-all]")!);
  expect(baseElement.querySelector("[data-roughcut-live]")).toBeTruthy();
  const scrubber = baseElement.querySelector<HTMLInputElement>(
    '[data-roughcut-live] input[type="range"]',
  )!;
  fireEvent.change(scrubber, { target: { value: "6000" } });
  const image = baseElement.querySelector('img[data-live-layer="r2v-window"]');
  expect(image).toBeTruthy();
  expect(image!.getAttribute("src")).toContain(
    "/media/artifacts/storyboard-v1",
  );
  expect(baseElement.querySelector('img[src*="cat-anchor-v1"]')).toBeNull();
});

it("follows a narrative edge after a storyboard preview and stops at an ungenerated node", () => {
  const project = withBranching(beforeStoryboards());
  project.timelines.order.push("timeline:ep2");
  project.timelines.items["timeline:ep2"].elements_by_id = {};
  addStoryboard(project);
  project.narrative_edges = project.narrative_edges!.slice(0, 1);
  const { container, baseElement } = renderStrip(project);
  fireEvent.click(container.querySelector("[data-roughcut-preview-all]")!);
  const scrubber = baseElement.querySelector<HTMLInputElement>(
    '[data-roughcut-live] input[type="range"]',
  )!;
  fireEvent.change(scrubber, { target: { value: scrubber.max } });
  expect(
    baseElement.querySelector("[data-roughcut-unavailable]"),
  ).toHaveTextContent("分镜图尚未生成");
  expect(
    baseElement.querySelector(
      "[data-roughcut-cinema] video, [data-roughcut-cinema] audio",
    ),
  ).toBeNull();
});

function scriptProject() {
  const project = structuredClone(projectDocument);
  project.timelines.items["timeline:main"].description = "较早的内联草稿";
  project.assets.artifact_slots_by_id["script:timeline:main"] = {
    slot_id: "script:timeline:main",
    kind: "timeline_script",
    owner_ref: "timeline:timeline:main",
    version_ids: ["script-v1"],
    selected_version_id: "script-v1",
    metadata: {},
  };
  project.assets.artifact_versions_by_id["script-v1"] = {
    ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
    version_id: "script-v1",
    slot_id: "script:timeline:main",
    kind: "timeline_script",
    owner_ref: "timeline:timeline:main",
    file_id: "script-file",
    name: "序章",
    stale: true,
    stale_reason: "剧本创作依据已修改，需要重新起草",
  };
  return project;
}

afterEach(() => {
  vi.unstubAllGlobals();
  useProjectSnapshotStore.getState().reset();
});

describe("script synchronization status", () => {
  it("explains automatic input changes while preserving the selected script and backend stale fact", async () => {
    const project = scriptProject();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => "# 序章正文\n已经生成的正式剧本。",
    });
    vi.stubGlobal("fetch", fetchMock);
    const patch = vi.spyOn(useProjectSnapshotStore.getState(), "patch");
    render(
      <BlueprintScriptPanel
        project={project}
        projectId="p1"
        timelineId="timeline:main"
        open
        onClose={vi.fn()}
        onOpenTimeline={vi.fn()}
        onOpenVisualEntity={vi.fn()}
      />,
    );

    expect(screen.getByText("待同步")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "更新原因：剧本创作依据已修改，需要重新起草",
    );
    expect(await screen.findByText("已经生成的正式剧本。")).toBeInTheDocument();
    expect(screen.queryByText("较早的内联草稿")).not.toBeInTheDocument();
    expect(project.assets.artifact_versions_by_id["script-v1"].stale).toBe(
      true,
    );
    expect(patch).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      expect.stringContaining("/media/artifacts/script-v1"),
    );
  });

  it("removes the notice only when an updated snapshot selects a synchronized script", async () => {
    const project = scriptProject();
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, text: async () => "当前剧本正文" });
    vi.stubGlobal("fetch", fetchMock);
    const props = {
      projectId: "p1",
      timelineId: "timeline:main",
      open: true,
      onClose: vi.fn(),
      onOpenTimeline: vi.fn(),
      onOpenVisualEntity: vi.fn(),
    };
    const { rerender } = render(
      <BlueprintScriptPanel {...props} project={project} />,
    );
    await screen.findByText("当前剧本正文");
    const updated = structuredClone(project);
    updated.assets.artifact_versions_by_id["script-v1"].stale = false;
    updated.assets.artifact_versions_by_id["script-v1"].stale_reason = null;
    rerender(<BlueprintScriptPanel {...props} project={updated} />);
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("当前剧本正文")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("stale script action routing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useAgentDockUiStore.getState().reset();
  });

  function renderPanel(project: ReturnType<typeof scriptProject>) {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => "# 序章正文\n已经生成的正式剧本。",
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <BlueprintScriptPanel
        project={project}
        projectId="p1"
        timelineId="timeline:main"
        open
        inline
        onClose={vi.fn()}
        onOpenTimeline={vi.fn()}
        onOpenVisualEntity={vi.fn()}
      />,
    );
  }

  it("routes the stale action to a redraft request while media production is approved", async () => {
    const project = scriptProject();
    project.settings.production_stage = "media";
    const info = vi.spyOn(message, "info");
    renderPanel(project);
    await screen.findByText("已经生成的正式剧本。");

    fireEvent.click(screen.getByRole("button", { name: "更新剧本" }));

    // The composer carries the redraft request instead of a dead probe.
    expect(useAgentDockUiStore.getState().draft).toContain("重新起草");
    expect(useAgentDockUiStore.getState().selection?.ref).toBe(
      "timeline:timeline:main",
    );
    expect(info).toHaveBeenCalledWith(
      expect.objectContaining({
        content: expect.stringContaining("不阻塞已就绪任务的派发"),
      }),
    );
    expect(info).not.toHaveBeenCalledWith(
      expect.objectContaining({
        content: expect.stringContaining("未找到确认按钮"),
      }),
    );
  });

  it("keeps focusing the production-stage confirm button while the script stage owns approval", async () => {
    const project = scriptProject();
    project.settings.production_stage = "script";
    const success = vi.spyOn(message, "success");
    renderPanel(project);
    await screen.findByText("已经生成的正式剧本。");
    // The blueprint header renders this button only in the script stage.
    render(<button type="button" data-production-stage-confirm="" />);

    fireEvent.click(screen.getByRole("button", { name: "重新确认剧本" }));

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith(
        expect.stringContaining("已定位到「确认剧本并继续」"),
      ),
    );
    expect(useAgentDockUiStore.getState().draft).toBe("");
  });
});

describe("Interactive storyboard eligibility", () => {
  it("never substitutes a character reference for a missing, stale or invalid storyboard", () => {
    const project = cloneProject();
    const timeline = project.timelines.items["timeline:main"];
    const shot = timeline.elements_by_id["r2v-window"];
    timeline.elements_by_id = { "r2v-window": shot };
    delete project.assets.artifact_slots_by_id["element:r2v-window:video"];
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    expect(hasTimelinePreviewContent(project, timeline)).toBe(false);
    const slot = (project.assets.artifact_slots_by_id[
      "element:r2v-window:storyboard"
    ] = {
      slot_id: "element:r2v-window:storyboard",
      owner_ref: "element:r2v-window",
      kind: "r2v_storyboard_image",
      selected_version_id: "missing-version",
      version_ids: ["missing-version"],
      metadata: {},
    });
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    const version = (project.assets.artifact_versions_by_id["storyboard"] = {
      ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
      version_id: "storyboard",
      kind: "r2v_storyboard_image",
      slot_id: slot.slot_id,
      owner_ref: slot.owner_ref,
      stale: true as boolean,
    });
    slot.selected_version_id = version.version_id;
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    version.stale = false;
    expect(roughCutFrameForElement(project, shot)).toMatchObject({
      source: "storyboard",
      versionId: "storyboard",
    });
    expect(hasTimelinePreviewContent(project, timeline)).toBe(true);
    delete project.assets.files_by_id[version.file_id];
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
  });
});

describe("Interactive timeline previews", () => {
  const previewProps = (project: ProjectDocument, playheadTick: number) => ({
    project,
    timeline: project.timelines.items["timeline:main"],
    durationTick: 20000,
    playheadTick,
    playing: false,
    muted: false,
    tasks: [],
    onPlayheadChange: vi.fn(),
    onPlayingChange: vi.fn(),
  });

  it("scopes the playhead fraction variable to each canvas in a compare view", () => {
    // Review #243 r4548690168: PlanPage compare mounts two canvases side
    // by side with different durationTick. Both rAF clocks and scrub
    // effects wrote ``--creator-playhead-frac`` on
    // ``document.documentElement``, so the second canvas overwrote the
    // first and at least one playhead was misplaced. The variable must
    // be scoped to each canvas's own DOM subtree, and documentElement
    // must not carry a shared value that siblings still inherit.
    const project = structuredClone(projectDocument);
    const timeline = project.timelines.items["timeline:main"];
    const common = {
      project,
      timeline,
      playheadTick: 2000,
      selectedElementId: null,
      previewOpen: true,
      tasks: [],
      onPreviewOpenChange: vi.fn(),
      onPlayheadChange: vi.fn(),
      onSelectElement: vi.fn(),
      onActiveElementIdsChange: vi.fn(),
    };
    const view = render(
      <div>
        <div data-testid="canvas-a">
          <TimelineCanvas {...common} durationTick={20000} />
        </div>
        <div data-testid="canvas-b">
          <TimelineCanvas {...common} durationTick={40000} />
        </div>
      </div>,
    );

    const sections = view.container.querySelectorAll("[data-timeline-panel]");
    expect(sections).toHaveLength(2);
    const [sectionA, sectionB] = Array.from(sections) as HTMLElement[];

    // Each canvas's own fraction, isolated to its subtree.
    expect(sectionA.style.getPropertyValue("--creator-playhead-frac")).toBe(
      "0.100000",
    );
    expect(sectionB.style.getPropertyValue("--creator-playhead-frac")).toBe(
      "0.050000",
    );
    // The document-wide variable is no longer the shared channel that
    // made the two heads stomp each other.
    expect(
      document.documentElement.style.getPropertyValue(
        "--creator-playhead-frac",
      ),
    ).toBe("");
  });

  it("commits the playhead when a short overlay's span starts, not just on the throttle", () => {
    // Review #243 P2: with PLAYHEAD_COMMIT_MS at 200ms and ticks_per_second
    // at 1000, a 100-tick overlay [50, 150] could fall entirely between two
    // React commits, so visibleLayers never sampled it and the overlay was
    // invisible. The rAF clock now force-commits whenever the tick crosses
    // any span boundary, so the layer mounts on time while the throttled
    // track-scrubber cadence stays intact.
    const project = cloneProject();
    const timeline = project.timelines.items["timeline:main"];
    const overlay = timeline.elements_by_id["overlay-os"];
    overlay.span = { start_tick: 50, duration_tick: 100 };
    overlay.enabled = true;

    const rafQueue: Array<(t: number) => void> = [];
    const realRaf = globalThis.requestAnimationFrame;
    const realCancel = globalThis.cancelAnimationFrame;
    globalThis.requestAnimationFrame = (cb) => {
      rafQueue.push(cb);
      return rafQueue.length;
    };
    globalThis.cancelAnimationFrame = () => undefined;
    try {
      const onPlayheadChange = vi.fn();
      render(
        <TimelineLivePreview
          {...previewProps(project, 0)}
          playing
          onPlayheadChange={onPlayheadChange}
        />,
      );
      const fire = (now: number) => {
        const batch = rafQueue.splice(0, rafQueue.length);
        batch.forEach((cb) => cb(now));
      };
      // First frame only seeds the clock; no boundary crossed at tick 0.
      fire(0);
      expect(onPlayheadChange).not.toHaveBeenCalled();
      // tick=50 crosses the overlay's start_tick, well before the
      // 200ms throttle expires -- boundary commit fires here.
      fire(50);
      expect(onPlayheadChange).toHaveBeenCalledWith(50);
      // tick=150 crosses the overlay's end_tick, still under 200ms.
      fire(150);
      expect(onPlayheadChange).toHaveBeenCalledWith(150);
    } finally {
      globalThis.requestAnimationFrame = realRaf;
      globalThis.cancelAnimationFrame = realCancel;
    }
  });

  it("samples a one-second fade often enough that its middle frames move", () => {
    // Review #243 P2-7 remaining half: at PLAYHEAD_COMMIT_MS=200 a
    // one-second fade received five React samples total, so an
    // opacity check halfway through the transition saw the same value
    // as the boundary and the fade stepped. The cadence is now 60 ms
    // (~16 samples/second), which is enough to render a smooth fade
    // while still leaving the per-frame setState cost behind.
    const rafQueue: Array<(t: number) => void> = [];
    const realRaf = globalThis.requestAnimationFrame;
    const realCancel = globalThis.cancelAnimationFrame;
    globalThis.requestAnimationFrame = (cb) => {
      rafQueue.push(cb);
      return rafQueue.length;
    };
    globalThis.cancelAnimationFrame = () => undefined;
    try {
      const project = cloneProject();
      const onPlayheadChange = vi.fn();
      render(
        <TimelineLivePreview
          {...previewProps(project, 0)}
          playing
          onPlayheadChange={onPlayheadChange}
        />,
      );
      const fire = (now: number) => {
        const batch = rafQueue.splice(0, rafQueue.length);
        batch.forEach((cb) => cb(now));
      };
      // Simulate a one-second fade window at 1000 ticks/s: seed the
      // clock at t=0, then step through in 16 ms frames (~60 fps).
      fire(0);
      for (let t = 16; t <= 1000; t += 16) fire(t);
      // A 200 ms cadence would yield ~5 samples inside 1 s; 60 ms
      // should give at least 12 distinct React commits, enough that a
      // transition's midpoint ticks each land on their own sample.
      const distinctTicks = new Set(
        onPlayheadChange.mock.calls.map(([tick]) => tick),
      );
      expect(distinctTicks.size).toBeGreaterThanOrEqual(12);
    } finally {
      globalThis.requestAnimationFrame = realRaf;
      globalThis.cancelAnimationFrame = realCancel;
    }
  });
});
