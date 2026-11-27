import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ElementDetail from "@/components/timeline/ElementDetail";
import { projectDocument } from "@/test/creatorFixtures";
import type {
  ProjectDocument,
  TimelineDocument,
  TimelineElementDocument,
} from "@/contracts/creator";

function setup(elementId: string) {
  const project = structuredClone(projectDocument) as ProjectDocument;
  const timeline = project.timelines.items["timeline:main"] as TimelineDocument;
  const element = timeline.elements_by_id[elementId] as TimelineElementDocument;
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
/**
 * Storyboard tiers (2026-11-25): an anchored simple continuation shows
 * the "no storyboard" copy plus the manual override instead of the
 * storyboard preview box.
 */
describe("ElementDetail storyboard tiers", () => {
  it("shows the none-tier note and the force-storyboard override", () => {
    const project = structuredClone(projectDocument) as ProjectDocument;
    const timeline = project.timelines.items[
      "timeline:main"
    ] as TimelineDocument;
    const first = timeline.elements_by_id[
      "r2v-window"
    ] as TimelineElementDocument;
    (first.creation as Record<string, unknown>).video_prompt = "男孩站着。";
    (first.creation as Record<string, unknown>).narrative = "男孩站着。";
    const second = structuredClone(first) as TimelineElementDocument;
    second.element_id = "r2v-next";
    second.span = {
      ...second.span,
      start_tick: first.span.start_tick + first.span.duration_tick,
      duration_tick: 5000,
    };
    (second.creation as Record<string, unknown>).video_prompt =
      "男孩抬头看手机。";
    (second.creation as Record<string, unknown>).continuity = "承接上一段";
    (second.creation as Record<string, unknown>).transition_intent = "continue";
    (second.creation as Record<string, unknown>).storyboard_intent = "none";
    (second.creation as Record<string, unknown>).storyboard_prompt = "";
    timeline.elements_by_id["r2v-next"] = second;

    const onChange = vi.fn();
    const utils = render(
      <ElementDetail
        project={project}
        timeline={timeline}
        element={second}
        tasks={[]}
        applying={false}
        dirtyCount={0}
        conflictPaths={[]}
        onClose={vi.fn()}
        onChange={onChange}
        onApply={vi.fn()}
        onDiscard={vi.fn()}
        onAcceptConflicts={vi.fn()}
        onOpenWorkbench={vi.fn()}
      />,
    );
    const box = utils.container.querySelector(
      "[data-element-overview-storyboard]",
    );
    expect(box?.textContent).toContain("无需分镜");
    const button = Array.from(box?.querySelectorAll("button") ?? []).find(
      (item) => item.textContent?.includes("手动指定分镜"),
    );
    expect(button).toBeTruthy();
    fireEvent.click(button as HTMLButtonElement);
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
