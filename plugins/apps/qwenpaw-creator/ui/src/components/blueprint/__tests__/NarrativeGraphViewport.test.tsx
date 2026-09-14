import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BlueprintStructureArea from "../BlueprintStructureArea";
import { projectDocument } from "@/test/creatorFixtures";
import { selectTimelineSummaries } from "@/selectors/blueprintSelectors";
import {
  readStoryPositions,
  storyMapStorageKey,
} from "../useNarrativeGraphViewport";

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
    expect(screen.getByText("公开真相")).toBeVisible();
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
