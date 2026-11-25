import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { pathForLocator, navigateToLocator } from "@/routing/locators";
import { useNavigationStore } from "@/store/navigationStore";
import { navigate } from "@/routing/navigation";
import { act, render } from "@testing-library/react";
import {
  findCreatorFieldElement,
  flashCreatorReviewField,
  useReviewFieldFocus,
} from "@/routing/reviewFocus";

vi.mock("@/routing/navigation", () => ({
  navigate: vi.fn(),
}));

describe("pathForLocator", () => {
  it("maps element workbench locators to plan/element/:id", () => {
    expect(
      pathForLocator("p1", {
        page: "element",
        elementId: "el-42",
      }),
    ).toBe("/project/p1/plan/element/el-42");
  });

  it("maps asset locators to the assets page", () => {
    expect(pathForLocator("p1", { page: "assets" })).toBe("/project/p1/assets");
  });

  it("maps blueprint locators to the project root", () => {
    expect(pathForLocator("p1", { page: "blueprint" })).toBe("/project/p1");
  });

  it("carries the timelineId of a blueprint locator as a query parameter", () => {
    expect(
      pathForLocator("p1", {
        page: "blueprint",
        timelineId: "timeline:ep2",
      }),
    ).toBe("/project/p1?timeline=timeline%3Aep2");
  });

  it("defaults unknown pages to plan", () => {
    expect(pathForLocator("p1", { page: "plan" })).toBe("/project/p1/plan");
  });
});

describe("navigateToLocator", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(navigate).mockClear();
    useNavigationStore.getState().clear();
    window.location.hash = "#/project/p1/plan";
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("passes artifactVersionId as version query for media reviews", () => {
    navigateToLocator(
      "p1",
      {
        page: "element",
        elementId: "el-1",
        artifactVersionId: "ver-9",
        mediaType: "video",
      },
      { review: true, field: "/assets/artifact_versions_by_id/ver-9" },
    );
    expect(navigate).toHaveBeenCalledWith(
      expect.stringContaining("/project/p1/plan/element/el-1"),
    );
    const target = vi.mocked(navigate).mock.calls[0][0] as string;
    expect(target).toContain("version=ver-9");
    expect(target).toContain("review=1");
    expect(target).toContain("field=");
  });
});

const PATH = "/project/p1/plan";
const FIELD = "element:element-1/label";
const FIELD_B = "element:element-1/creation/video_prompt";

const q = (container: HTMLElement, selector = "[data-creator-field]") =>
  container.querySelector<HTMLElement>(selector)!;

const focusField = (field: string, reviewPulse: string) =>
  act(() => {
    useNavigationStore.getState().setReviewFocus({
      path: PATH,
      ref: "element:element-1",
      query: { review: "1", field, reviewPulse },
    });
  });

type HarnessProps = { showTarget?: boolean; pulse: string };

function Harness({ showTarget = true, pulse }: HarnessProps) {
  useReviewFieldFocus({ path: PATH, field: FIELD, enabled: true, pulse });
  return showTarget ? <div data-creator-field={FIELD}>原文</div> : null;
}

function MultiFieldHarness({ pulse }: { pulse: string }) {
  useReviewFieldFocus({ path: PATH, field: FIELD, enabled: true, pulse });
  return (
    <>
      <div data-creator-field={FIELD}>原文 A</div>
      <div data-creator-field={FIELD_B}>原文 B</div>
    </>
  );
}

function DockCollisionHarness({ pulse }: { pulse: string }) {
  useReviewFieldFocus({ path: PATH, field: FIELD, enabled: true, pulse });
  return (
    <>
      <aside data-agent-dock>
        <div data-creator-field={FIELD}>AgentDock diff</div>
      </aside>
      <main data-creator-workspace-root>
        <div data-creator-field={FIELD}>工作区正文</div>
      </main>
    </>
  );
}

describe("useReviewFieldFocus", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useNavigationStore.setState({ reviewFocus: null });
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
  });

  afterEach(() => vi.useRealTimers());

  it("URL 审阅定位会滚动并闪烁字段", () => {
    const { container } = render(<Harness pulse="pulse-url" />);
    const target = q(container);
    expect(target).toHaveClass("review-flash");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(2400));
    expect(target).not.toHaveClass("review-flash");
  });

  it("AgentDock 关闭后的同页兜底会立即重放字段闪烁", () => {
    const { container } = render(<div data-creator-field={FIELD}>原文</div>);
    const target = q(container);

    expect(flashCreatorReviewField(FIELD, container)).toBe(target);
    expect(target).toHaveClass("review-flash");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("同名字段同时存在时只定位工作区，不会闪烁 AgentDock", () => {
    const { container } = render(<DockCollisionHarness pulse="pulse-dock" />);
    const dockTarget = q(container, "[data-agent-dock] [data-creator-field]");
    const workspaceTarget = q(
      container,
      "[data-creator-workspace-root] [data-creator-field]",
    );

    expect(workspaceTarget).toHaveClass("review-flash");
    expect(dockTarget).not.toHaveClass("review-flash");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("同一字段的重复 reviewFocus 请求（新 pulse）会重放定位", () => {
    render(<Harness pulse="pulse-replay" />);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);

    focusField(FIELD, "pulse-replay-2");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(2);
  });

  it("已消费的 pulse 不会在重挂载/重渲染时重放定位动画", () => {
    const { unmount } = render(<Harness pulse="pulse-consumed" />);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
    unmount();

    render(<Harness pulse="pulse-consumed" />);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("详情节点延迟挂载时会重试，不依赖固定 360ms", () => {
    const { container, rerender } = render(
      <Harness showTarget={false} pulse="pulse-retry" />,
    );
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(400));
    rerender(<Harness showTarget pulse="pulse-retry" />);
    act(() => vi.advanceTimersByTime(50));

    expect(q(container)).toHaveClass("review-flash");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("切换查看字段会清理上一个目标的闪烁类", () => {
    const { container } = render(<MultiFieldHarness pulse="pulse-switch" />);
    const first = q(container, `[data-creator-field="${FIELD}"]`);
    const second = q(container, `[data-creator-field="${FIELD_B}"]`);
    expect(first).toHaveClass("review-flash");

    focusField(FIELD_B, "pulse-switch-2");

    expect(first).not.toHaveClass("review-flash");
    expect(second).toHaveClass("review-flash");
  });
});

describe("findCreatorFieldElement", () => {
  it("深指针没有精确节点时回退到最长 '/' 边界前缀块", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <div data-creator-path="/timelines/items/t/elements_by_id/e/creation"></div>
      <div id="shots" data-creator-path="/timelines/items/t/elements_by_id/e/creation/shots"></div>
    `;
    const target = findCreatorFieldElement(
      "/timelines/items/t/elements_by_id/e/creation/shots/3/description",
      root,
    );
    expect(target?.id).toBe("shots");
  });

  it("前缀必须落在 '/' 边界，不误配相似字段名", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <div data-creator-path="/a/shots_extra"></div>
    `;
    expect(findCreatorFieldElement("/a/shots/1", root)).toBeNull();
  });
});

describe("flashCreatorReviewField + collapsed details", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("目标位于折叠的 <details> 内时会自动展开再定位", () => {
    document.body.innerHTML = `
      <details id="advanced">
        <summary>更多设置</summary>
        <div data-creator-field="element:e/label">原文</div>
      </details>
    `;
    const target = flashCreatorReviewField("element:e/label");
    expect(target).not.toBeNull();
    expect(document.querySelector<HTMLDetailsElement>("#advanced")!.open).toBe(
      true,
    );
    expect(target).toHaveClass("review-flash");
  });
});
