import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TimelineCanvas from "@/components/timeline/TimelineCanvas";
import { projectDocument } from "@/test/creatorFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import type { ProjectDocument } from "@/contracts/creator";

type CanvasProps = Parameters<typeof TimelineCanvas>[0];

function setup(overrides: Partial<CanvasProps> = {}) {
  const project = overrides.project ?? structuredClone(projectDocument);
  const timeline = project.timelines.items["timeline:main"];
  const props = {
    project,
    timeline,
    durationTick: 20000,
    playheadTick: 2000,
    selectedElementId: null,
    previewOpen: true,
    tasks: [],
    onPreviewOpenChange: vi.fn(),
    onPlayheadChange: vi.fn(),
    onSelectElement: vi.fn(),
    onActiveElementIdsChange: vi.fn(),
    ...overrides,
  };
  return { ...render(<TimelineCanvas {...props} />), props };
}

function mockRect(element: Element, rect: Partial<DOMRect>) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue(rect as DOMRect);
}

const previewVideo = (container: HTMLElement) =>
  container.querySelector("[data-timeline-video-preview] video")!;

const incompleteCover = (container: HTMLElement) =>
  container.querySelector("[data-final-preview-incomplete]");

describe("TimelineCanvas preview scrubber", () => {
  it("moves the shared playhead throughout a real pointer drag", () => {
    const { props } = setup();
    const scrubber = screen.getByRole("slider", { name: "拖动预览时间轴" });
    mockRect(scrubber, {
      left: 100,
      top: 400,
      right: 500,
      bottom: 428,
      width: 400,
      height: 28,
    });

    fireEvent.pointerDown(scrubber, {
      button: 0,
      buttons: 1,
      pointerId: 7,
      clientX: 200,
    });
    fireEvent.pointerMove(scrubber, { buttons: 1, pointerId: 7, clientX: 440 });
    fireEvent.pointerUp(scrubber, { button: 0, pointerId: 7, clientX: 440 });

    expect(
      vi.mocked(props.onPlayheadChange).mock.calls.map(([tick]) => tick),
    ).toEqual([5000, 17000, 17000]);
  });

  it("covers the final render until the requested frame finishes seeking", () => {
    vi.useFakeTimers();
    try {
      const { container } = setup();
      const video = previewVideo(container);

      expect(incompleteCover(container)).toHaveTextContent("正在定位画面");

      Object.defineProperties(video, {
        duration: { configurable: true, value: 20 },
        readyState: { configurable: true, value: 2 },
      });
      fireEvent.loadedData(video);
      fireEvent.seeked(video);

      expect(incompleteCover(container)).not.toBeInTheDocument();

      // A transient seek keeps the last painted frame; the opaque cover only
      // returns when the gap outlives the debounce window.
      Object.defineProperty(video, "seeking", {
        configurable: true,
        value: true,
      });
      fireEvent.seeking(video);
      expect(incompleteCover(container)).not.toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(incompleteCover(container)).toBeInTheDocument();

      Object.defineProperty(video, "seeking", {
        configurable: true,
        value: false,
      });
      fireEvent.seeked(video);
      expect(incompleteCover(container)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses completed stale frames outside the Element changed by the latest edit", () => {
    const project = structuredClone(projectDocument);
    const finalRender = project.assets.artifact_versions_by_id["final-v1"];
    finalRender.stale = true;
    finalRender.stale_reason = "时间线内容已修改，需要重新合成";
    finalRender.metadata.pendingAffectedElementIds = ["overlay-os"];
    const { container, props, rerender } = setup({
      project,
      playheadTick: 5000,
    });
    const sourceChip = () =>
      container.querySelector("[data-preview-source-chip]");

    expect(sourceChip()).toHaveTextContent("未受影响 · 已完成画面");

    rerender(<TimelineCanvas {...props} playheadTick={6000} />);
    expect(sourceChip()).toHaveTextContent("内容已更新 · 实时预览");

    rerender(<TimelineCanvas {...props} playheadTick={10500} />);
    expect(sourceChip()).toHaveTextContent("未受影响 · 已完成画面");
  });
});

function setupHistory() {
  const project = structuredClone(projectDocument) as ProjectDocument;
  const timeline = project.timelines.items["timeline:main"];
  const patchMock = vi.fn(
    async (
      _projectId: string,
      operations: { path: string; value: unknown }[],
    ) => {
      const state = useProjectSnapshotStore.getState();
      const next = structuredClone(state.project) as ProjectDocument;
      for (const operation of operations) {
        const segments = operation.path.split("/").filter(Boolean);
        let cursor: Record<string, unknown> = next as never;
        for (const key of segments.slice(0, -1)) {
          cursor = cursor[key] as Record<string, unknown>;
        }
        cursor[segments[segments.length - 1]] = operation.value;
      }
      useProjectSnapshotStore.setState({ project: next });
      return {
        projectId: "p1",
        generation: 2,
        etag: '"sha256:x"',
        changedPointers: [],
        project: next,
        editImpact: {
          affectedElementIds: [],
          renderTimelineIds: [],
          regenerationRequired: false,
        },
      } as never;
    },
  );
  useProjectSnapshotStore.getState().reset("p1");
  useProjectSnapshotStore.setState({
    projectId: "p1",
    project,
    generation: 1,
    etag: '"sha256:g1"',
    patch: patchMock as never,
  });
  const props = {
    project,
    timeline,
    durationTick: 20000,
    playheadTick: 0,
    selectedElementId: "edit-opening",
    previewOpen: false,
    tasks: [],
    onPreviewOpenChange: vi.fn(),
    onPlayheadChange: vi.fn(),
    onSelectElement: vi.fn(),
    onActiveElementIdsChange: vi.fn(),
  };
  const utils = render(<TimelineCanvas {...props} />);
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
  const refresh = () => {
    const p = useProjectSnapshotStore.getState().project as ProjectDocument;
    const tl = p.timelines.items["timeline:main"];
    utils.rerender(<TimelineCanvas {...props} project={p} timeline={tl} />);
  };
  return { ...utils, patchMock, refresh };
}

const editOpeningSpan = () =>
  (useProjectSnapshotStore.getState().project as ProjectDocument).timelines
    .items["timeline:main"].elements_by_id["edit-opening"].span;

const span = (start_tick: number, duration_tick: number) => ({
  start_tick,
  duration_tick,
});

const undo = (shiftKey = false) =>
  fireEvent.keyDown(document.body, { key: "z", ctrlKey: true, shiftKey });

/** Start-trim drag on edit-opening: +31.2px = +1000 ticks (624px/20000). */
function trim(container: HTMLElement, pointerId: number, fromX = 100) {
  const handle = container.querySelector(
    '[data-element-block="edit-opening"] [data-element-trim="start"]',
  ) as HTMLElement;
  const block = container.querySelector(
    '[data-element-block="edit-opening"]',
  ) as HTMLElement;
  fireEvent.pointerDown(handle, { button: 0, pointerId, clientX: fromX });
  fireEvent.pointerMove(block, { pointerId, clientX: fromX + 31.2 });
  fireEvent.pointerUp(block, { button: 0, pointerId, clientX: fromX + 31.2 });
}

describe("TimelineCanvas span edit history", () => {
  it("undoes and redoes a committed trim with Ctrl+Z / Ctrl+Shift+Z", async () => {
    const { container, patchMock } = setupHistory();
    // Nothing to undo yet: Ctrl+Z must not fire a patch.
    undo();
    expect(patchMock).not.toHaveBeenCalled();

    trim(container, 5);
    await waitFor(() => expect(editOpeningSpan()).toEqual(span(1000, 7000)));

    undo();
    await waitFor(() => expect(editOpeningSpan()).toEqual(span(0, 8000)));

    undo(true);
    await waitFor(() => expect(editOpeningSpan()).toEqual(span(1000, 7000)));
    expect(patchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps the history entry when the undo patch fails", async () => {
    const { container, patchMock } = setupHistory();
    trim(container, 6);
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));

    // First undo attempt hits a transient failure (409/network/503).
    patchMock.mockImplementationOnce(async () => {
      throw new Error("瞬时失败");
    });
    undo();
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(2));
    expect(editOpeningSpan()).toEqual(span(1000, 7000));

    undo();
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(editOpeningSpan()).toEqual(span(0, 8000)));
  });

  it("undoes only the newest edit when pressed while a commit is pending", async () => {
    const { container, patchMock, refresh } = setupHistory();
    trim(container, 8);
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    refresh();

    // Second trim B -> C(2000/6000): its PATCH hangs until released.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const original = patchMock.getMockImplementation()!;
    patchMock.mockImplementationOnce(async (...args) => {
      await gate;
      return original(...(args as Parameters<typeof original>)) as never;
    });
    trim(container, 9, 200);
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(2));

    // Undo pressed while the second commit is in flight must wait for the
    // queue and then revert C -> B, never jumping back to A.
    undo();
    release();
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(editOpeningSpan()).toEqual(span(1000, 7000)));
  });

  it("refuses to undo over a change written by someone else", async () => {
    const { container, patchMock } = setupHistory();
    trim(container, 7);
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));

    // An Agent (or another page) rewrites the same span in between.
    const next = structuredClone(
      useProjectSnapshotStore.getState().project,
    ) as ProjectDocument;
    next.timelines.items["timeline:main"].elements_by_id["edit-opening"].span =
      span(2000, 6000);
    useProjectSnapshotStore.setState({ project: next });

    undo();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(editOpeningSpan()).toEqual(span(2000, 6000));
  });
});
