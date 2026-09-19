import { act, fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BlueprintRoughCutStrip from "@/components/blueprint/BlueprintRoughCutStrip";
import { projectDocument } from "@/test/creatorFixtures";
import type { ProjectDocument } from "@/contracts/creator";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

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

describe("BlueprintRoughCutStrip whole-film preview", () => {
  it("offers no whole-film chip before a final_video is composed", () => {
    const { container } = renderStrip(cloneProject());
    expect(container.querySelector("[data-blueprint-roughcut]")).toBeTruthy();
    expect(container.querySelector("[data-roughcut-play-film]")).toBeNull();
  });

  it("plays the entire composed film in the floating cinema overlay", () => {
    const { container, baseElement, getByText } = renderStrip(
      withWholeFilm(cloneProject()),
    );

    const chip = container.querySelector("[data-roughcut-play-film]");
    expect(chip).toBeTruthy();
    fireEvent.click(chip!);

    // The player is a near-fullscreen overlay portaled to <body>, not an
    // inline expansion inside the strip.
    expect(container.querySelector("[data-roughcut-cinema]")).toBeNull();
    const overlay = baseElement.querySelector("[data-roughcut-cinema]");
    expect(overlay).toBeTruthy();
    const video = baseElement.querySelector<HTMLVideoElement>(
      "[data-roughcut-player] video",
    );
    expect(video).toBeTruthy();
    // Streams the final film artifact, not a per-timeline draft rough cut.
    expect(video!.getAttribute("src")).toContain("/media/artifacts/final-v1");
    expect(getByText("测试项目 · 成片")).toBeInTheDocument();

    // Toggling the chip again closes the player.
    fireEvent.click(chip!);
    expect(baseElement.querySelector("[data-roughcut-cinema]")).toBeNull();
  });

  it("closes the cinema overlay on Escape and on backdrop click", () => {
    const { container, baseElement } = renderStrip(
      withWholeFilm(cloneProject()),
    );
    const chip = container.querySelector("[data-roughcut-play-film]");

    fireEvent.click(chip!);
    expect(baseElement.querySelector("[data-roughcut-cinema]")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(baseElement.querySelector("[data-roughcut-cinema]")).toBeNull();

    fireEvent.click(chip!);
    const backdrop = baseElement.querySelector("[data-roughcut-cinema]");
    expect(backdrop).toBeTruthy();
    // Clicking the video frame itself must NOT close the overlay…
    fireEvent.click(baseElement.querySelector("[data-roughcut-player]")!);
    expect(baseElement.querySelector("[data-roughcut-cinema]")).toBeTruthy();
    // …but clicking the scrim backdrop does.
    fireEvent.click(backdrop!);
    expect(baseElement.querySelector("[data-roughcut-cinema]")).toBeNull();
  });

  it("opens the same floating overlay from a per-timeline play chip", () => {
    const { container, baseElement } = renderStrip(cloneProject());
    const chip = container.querySelector(
      "[data-roughcut-play]",
    ) as HTMLElement | null;
    expect(chip).toBeTruthy();
    fireEvent.click(chip!);
    const video = baseElement.querySelector<HTMLVideoElement>(
      "[data-roughcut-cinema] video",
    );
    expect(video).toBeTruthy();
  });

  it("keeps the whole-film entry even when the shots-only filter leaves no frames", () => {
    // a290de0e regression shape: every element filtered out of the strip
    // (e.g. overlay/audio/interaction-only timelines) must not hide the
    // 成片 entry point along with the frames.
    const project = withWholeFilm(cloneProject());
    for (const timeline of Object.values(project.timelines.items)) {
      for (const element of Object.values(timeline.elements_by_id)) {
        element.enabled = false;
      }
    }
    const { container } = renderStrip(project);
    expect(container.querySelectorAll("[data-roughcut-frame]")).toHaveLength(0);
    expect(container.querySelector("[data-roughcut-play-film]")).toBeTruthy();
  });

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
