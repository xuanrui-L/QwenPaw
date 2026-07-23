import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TimelineCanvas from "@/components/timeline/TimelineCanvas";
import { projectDocument } from "@/test/creatorFixtures";

describe("TimelineCanvas preview scrubber", () => {
  it("moves the playhead while dragging the preview progress bar", () => {
    const project = structuredClone(projectDocument);
    const timeline = project.timelines.items["timeline:main"];
    const onPlayheadChange = vi.fn();

    render(
      <TimelineCanvas
        project={project}
        timeline={timeline}
        durationTick={20000}
        playheadTick={2000}
        selectedElementId={null}
        previewOpen
        tasks={[]}
        onPreviewOpenChange={vi.fn()}
        onPlayheadChange={onPlayheadChange}
        onSelectElement={vi.fn()}
      />,
    );

    const scrubber = screen.getByRole("slider", {
      name: "拖动预览时间轴",
    });
    fireEvent.change(scrubber, { target: { value: "8500" } });

    expect(onPlayheadChange).toHaveBeenLastCalledWith(8500);
  });
});
