import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { pathForLocator, navigateToLocator } from "@/routing/locators";
import { useNavigationStore } from "@/store/navigationStore";

vi.mock("@/routing/navigation", () => ({
  navigate: vi.fn(),
}));

import { navigate } from "@/routing/navigation";

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

  it("routes a second-episode production group to its own timeline", () => {
    expect(
      pathForLocator("p1", { page: "plan", timelineId: "timeline:ep2" }),
    ).toBe("/project/p1/t/timeline%3Aep2/plan");
    expect(pathForLocator("p1", { timelineId: "timeline:ep2" })).toBe(
      "/project/p1/t/timeline%3Aep2/plan",
    );
  });

  it("routes a second-episode media node to the scoped element workbench", () => {
    expect(
      pathForLocator("p1", {
        page: "element",
        timelineId: "timeline:ep2",
        elementId: "seg:opening/2",
      }),
    ).toBe("/project/p1/t/timeline%3Aep2/plan/element/seg%3Aopening%2F2");
    expect(
      pathForLocator("p1", { page: "element", timelineId: "timeline:ep2" }),
    ).toBe("/project/p1/t/timeline%3Aep2/plan");
  });

  it("does not change project-wide assets or legacy unscoped element routes", () => {
    expect(
      pathForLocator("p1", { page: "assets", timelineId: "timeline:ep2" }),
    ).toBe("/project/p1/assets");
    expect(pathForLocator("p1", { page: "element" })).toBe("/project/p1/plan");
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

  it("preserves the second episode and exact selected node when opening grouped progress", () => {
    window.location.hash = "#/project/p1/t/timeline%3Aep1/plan?element=first";
    navigateToLocator("p1", {
      page: "plan",
      timelineId: "timeline:ep2",
      elementId: "seg:second",
    });
    expect(navigate).toHaveBeenCalledWith(
      "/project/p1/t/timeline%3Aep2/plan?element=seg%3Asecond",
    );
    expect(useNavigationStore.getState().expectedPath).toBe(
      "/project/p1/t/timeline%3Aep2/plan",
    );
    expect(useNavigationStore.getState().reviewFocus).toMatchObject({
      path: "/project/p1/t/timeline%3Aep2/plan",
      ref: "seg:second",
      query: { element: "seg:second" },
    });
    expect(useNavigationStore.getState().stack[0].path).toBe(
      "/project/p1/t/timeline%3Aep1/plan?element=first",
    );
  });

  it("keeps media version and original review pointer when the target is in episode two", () => {
    const field =
      "/timelines/items/timeline:ep2/elements_by_id/seg:second/creation/video_prompt";
    navigateToLocator(
      "p1",
      {
        page: "element",
        timelineId: "timeline:ep2",
        elementId: "seg:second",
        artifactVersionId: "version-2",
      },
      { review: true, field },
    );
    const target = vi.mocked(navigate).mock.calls[0][0] as string;
    const url = new URL(target, "https://creator.test");
    expect(url.pathname).toBe(
      "/project/p1/t/timeline%3Aep2/plan/element/seg%3Asecond",
    );
    expect(url.searchParams.get("version")).toBe("version-2");
    expect(url.searchParams.get("field")).toBe(field);
    expect(url.searchParams.get("element")).toBe("seg:second");
    expect(url.searchParams.get("review")).toBe("1");
    expect(useNavigationStore.getState().reviewFocus?.path).toBe(url.pathname);
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
  it("drops old field-focus replays when a newer navigation changes the target", () => {
    const field =
      "/timelines/items/timeline:ep2/elements_by_id/seg:second/creation/video_prompt";
    const target = document.createElement("div");
    target.setAttribute("data-creator-path", field);
    target.scrollIntoView = vi.fn();
    document.body.append(target);
    navigateToLocator("p1", { page: "element", field }, { focusField: true });
    navigateToLocator("p2", { page: "assets" });
    vi.advanceTimersByTime(900);
    expect(target.scrollIntoView).not.toHaveBeenCalled();
    expect(target).not.toHaveClass("review-flash");
    target.remove();
  });
});
