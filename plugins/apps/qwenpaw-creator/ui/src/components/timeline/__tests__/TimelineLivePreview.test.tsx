import { render } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import TimelineLivePreview from "@/components/timeline/TimelineLivePreview";
import type { ProjectDocument, TaskView } from "@/contracts/creator";
import { projectDocument } from "@/test/creatorFixtures";

const originalClientWidth = Object.getOwnPropertyDescriptor(
  HTMLElement.prototype,
  "clientWidth",
);
const originalClientHeight = Object.getOwnPropertyDescriptor(
  HTMLElement.prototype,
  "clientHeight",
);

beforeAll(() => {
  // jsdom 不做布局；给盒子一个可预测尺寸，让成片同款气泡 SVG 可断言。
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() {
      return 640;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return 360;
    },
  });
});

afterAll(() => {
  // 恢复原型，避免污染其他测试文件的布局断言。
  if (originalClientWidth) {
    Object.defineProperty(
      HTMLElement.prototype,
      "clientWidth",
      originalClientWidth,
    );
  } else {
    delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
  }
  if (originalClientHeight) {
    Object.defineProperty(
      HTMLElement.prototype,
      "clientHeight",
      originalClientHeight,
    );
  } else {
    delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight;
  }
});

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function runningTask(elementId: string): TaskView {
  return {
    id: "task-1",
    projectId: "p1",
    transactionId: null,
    specialistRunId: null,
    kind: "r2v_generation",
    targetRef: `element:${elementId}`,
    status: "RUNNING",
    progress: null,
    resultRefs: [],
    createdAt: "2026-07-20T00:00:00Z",
  };
}

function renderPreview(
  project: ProjectDocument,
  playheadTick: number,
  tasks: TaskView[] = [],
) {
  const timeline = project.timelines.items["timeline:main"];
  return render(
    <TimelineLivePreview
      project={project}
      timeline={timeline}
      durationTick={20000}
      playheadTick={playheadTick}
      playing={false}
      muted={false}
      tasks={tasks}
      onPlayheadChange={vi.fn()}
      onPlayingChange={vi.fn()}
    />,
  );
}

describe("TimelineLivePreview", () => {
  it("stacks ready media and compose-grade copy overlays by z_index at the playhead", () => {
    const { container } = renderPreview(cloneProject(), 7000);

    const nodes = [
      ...container.querySelectorAll(
        "[data-live-layer], [data-live-text-overlay], [data-live-placeholder]",
      ),
    ];
    expect(
      nodes.map(
        (node) =>
          node.getAttribute("data-live-layer") ??
          node.getAttribute("data-live-text-overlay") ??
          node.getAttribute("data-live-placeholder"),
      ),
    ).toEqual(["edit-opening", "r2v-window", "overlay-os"]);

    const editLayer = container.querySelector(
      '[data-live-layer="edit-opening"]',
    ) as HTMLVideoElement;
    expect(editLayer.tagName).toBe("VIDEO");
    expect(editLayer).toHaveAttribute(
      "src",
      "/api/qwenpaw-creator/media/assets/cat-video-v1",
    );
    expect(editLayer).not.toHaveClass("invisible");

    // 与成片合成口径一致：audio 元素不参与预览。
    expect(
      container.querySelector('[data-live-layer="audio-bgm"]'),
    ).not.toBeInTheDocument();

    // pet_os 气泡按成片同款规格绘制：白底黑边气泡 + 尾巴 + vibe emoji。
    const bubble = container.querySelector(
      '[data-live-text-overlay="overlay-os"] [data-overlay-copy="pet_os"]',
    ) as HTMLElement;
    expect(bubble).toBeInTheDocument();
    expect(bubble.querySelector("svg rect")).toHaveAttribute(
      "stroke",
      "#141414",
    );
    expect(bubble.querySelector("svg polygon")).toBeInTheDocument();
    expect(bubble).toHaveTextContent("午饭在哪里？");
    expect(bubble.textContent).toContain("😺");
  });

  it("premounts upcoming video layers invisibly for seamless handover", () => {
    const { container } = renderPreview(cloneProject(), 2000);
    const upcoming = container.querySelector(
      '[data-live-layer="r2v-window"]',
    ) as HTMLVideoElement;
    expect(upcoming).toHaveClass("invisible");
    expect(upcoming).toHaveAttribute(
      "src",
      "/api/qwenpaw-creator/media/artifacts/r2v-window-v1",
    );
  });

  it("renders a full-frame generating placeholder while the r2v artifact is still pending", () => {
    const project = cloneProject();
    project.assets.artifact_slots_by_id[
      "element:r2v-window:video"
    ].selected_version_id = null;
    const { container } = renderPreview(project, 9000, [
      runningTask("r2v-window"),
    ]);

    const placeholder = container.querySelector(
      '[data-live-placeholder="r2v-window"]',
    ) as HTMLElement;
    expect(placeholder).toBeInTheDocument();
    expect(placeholder).toHaveAttribute(
      "data-live-placeholder-state",
      "generating",
    );
    expect(placeholder).toHaveTextContent("画面生成中");
    expect(placeholder.style.width).toBe("100%");
    // 已就绪的同刻文字层仍然正常显示。
    expect(
      container.querySelector('[data-live-text-overlay="overlay-os"]'),
    ).toBeInTheDocument();
  });

  it("keeps unready located overlays visible as positioned dashed placeholders", () => {
    const project = cloneProject();
    const timeline = project.timelines.items["timeline:main"];
    const overlay = timeline.elements_by_id["overlay-os"];
    if (overlay.creation.type === "overlay") overlay.creation.text = "";
    const { container } = renderPreview(project, 7000);

    const placeholder = container.querySelector(
      '[data-live-placeholder="overlay-os"]',
    ) as HTMLElement;
    expect(placeholder).toBeInTheDocument();
    expect(placeholder).toHaveAttribute(
      "data-live-placeholder-state",
      "pending",
    );
    expect(placeholder.className).toContain("border-dashed");
    expect(placeholder.style.left).toBe("51%");
    expect(placeholder.style.width).toBe("42%");
    // 底下的已就绪视频层不受影响。
    expect(
      container.querySelector('[data-live-layer="edit-opening"]'),
    ).not.toHaveClass("invisible");
  });

  it("hot-swaps a placeholder into a real media layer once the artifact arrives", () => {
    const pendingProject = cloneProject();
    pendingProject.assets.artifact_slots_by_id[
      "element:r2v-window:video"
    ].selected_version_id = null;
    const timeline = pendingProject.timelines.items["timeline:main"];
    const { container, rerender } = render(
      <TimelineLivePreview
        project={pendingProject}
        timeline={timeline}
        durationTick={20000}
        playheadTick={9000}
        playing={false}
        muted={false}
        tasks={[runningTask("r2v-window")]}
        onPlayheadChange={vi.fn()}
        onPlayingChange={vi.fn()}
      />,
    );
    expect(
      container.querySelector('[data-live-placeholder="r2v-window"]'),
    ).toBeInTheDocument();

    const readyProject = cloneProject();
    rerender(
      <TimelineLivePreview
        project={readyProject}
        timeline={readyProject.timelines.items["timeline:main"]}
        durationTick={20000}
        playheadTick={9000}
        playing={false}
        muted={false}
        tasks={[]}
        onPlayheadChange={vi.fn()}
        onPlayingChange={vi.fn()}
      />,
    );
    expect(
      container.querySelector('[data-live-placeholder="r2v-window"]'),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector('[data-live-layer="r2v-window"]'),
    ).toHaveAttribute(
      "src",
      "/api/qwenpaw-creator/media/artifacts/r2v-window-v1",
    );
  });

  it("seeks visible video layers onto the paused playhead", () => {
    const { container } = renderPreview(cloneProject(), 2000);
    const editLayer = container.querySelector(
      '[data-live-layer="edit-opening"]',
    ) as HTMLVideoElement;
    expect(editLayer.currentTime).toBeCloseTo(2, 3);
  });
});
