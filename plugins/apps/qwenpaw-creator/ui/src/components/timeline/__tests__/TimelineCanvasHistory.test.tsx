import { fireEvent, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TimelineCanvas from "@/components/timeline/TimelineCanvas";
import { projectDocument } from "@/test/creatorFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import type { ProjectDocument } from "@/contracts/creator";

/**
 * Undo/redo replays committed span edits through the same patch pipeline.
 * The store patch is mocked to apply operations like the backend would.
 */
function setup() {
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
    activeElementIds: [] as string[],
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
    x: 0,
    y: 100,
    left: 0,
    top: 100,
    right: 692,
    bottom: 300,
    width: 692,
    height: 200,
    toJSON: () => ({}),
  });
  return { ...utils, patchMock };
}

function editOpeningSpan() {
  const project = useProjectSnapshotStore.getState().project as ProjectDocument;
  return project.timelines.items["timeline:main"].elements_by_id["edit-opening"]
    .span;
}

describe("TimelineCanvas span edit history", () => {
  it("undoes and redoes a committed trim with Ctrl+Z / Ctrl+Shift+Z", async () => {
    const { container, patchMock } = setup();
    const handle = container.querySelector(
      '[data-element-block="edit-opening"] [data-element-trim="start"]',
    ) as HTMLElement;
    const block = container.querySelector(
      '[data-element-block="edit-opening"]',
    ) as HTMLButtonElement;
    // Inner width 692-68 = 624px over 20000 ticks: +31.2px = +1000 ticks.
    fireEvent.pointerDown(handle, { button: 0, pointerId: 5, clientX: 100 });
    fireEvent.pointerMove(block, { pointerId: 5, clientX: 131.2 });
    fireEvent.pointerUp(block, { button: 0, pointerId: 5, clientX: 131.2 });
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(editOpeningSpan()).toEqual({
        start_tick: 1000,
        duration_tick: 7000,
      }),
    );

    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(editOpeningSpan()).toEqual({
        start_tick: 0,
        duration_tick: 8000,
      }),
    );

    fireEvent.keyDown(document.body, {
      key: "z",
      ctrlKey: true,
      shiftKey: true,
    });
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(editOpeningSpan()).toEqual({
        start_tick: 1000,
        duration_tick: 7000,
      }),
    );
  });

  it("ignores Ctrl+Z when there is nothing to undo", () => {
    const { patchMock } = setup();
    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });
    expect(patchMock).not.toHaveBeenCalled();
  });
});
