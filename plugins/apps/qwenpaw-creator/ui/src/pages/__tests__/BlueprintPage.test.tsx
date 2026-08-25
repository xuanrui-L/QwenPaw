import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import BlueprintPage from "@/pages/BlueprintPage";
import { NavigationRuntime } from "@/routing/navigation";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { projectDocument } from "@/test/creatorFixtures";
import type { ProjectDocument } from "@/contracts/creator";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function seedProject(project: ProjectDocument) {
  useProjectSnapshotStore.getState().reset("p1");
  useProjectSnapshotStore.setState({
    projectId: "p1",
    project,
    generation: project.generation,
    etag: '"sha256:g3"',
    syncStatus: "healthy",
    syncError: null,
  });
}

function singleProject(): ProjectDocument {
  const project = cloneProject();
  project.timelines.order = ["timeline:main"];
  delete project.timelines.items["timeline:ep2"];
  return project;
}

function renderPage(entry = "/project/p1") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <NavigationRuntime />
      <Routes>
        <Route path="/project/:id" element={<BlueprintPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("BlueprintPage narrative shapes", () => {
  beforeEach(() => {
    useProjectSnapshotStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useWorkGraphStore.getState().reset();
  });

  it("renders the single-node production board with real artifact cards", () => {
    seedProject(singleProject());
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="single"]'),
    ).toBeInTheDocument();
    // Shape chips are derived from data, not a scenario enum.
    expect(screen.getByText("单集生成")).toBeInTheDocument();
    // Understanding column shows the real source; script column shows the
    // legacy read-only mapping card (project predates timeline_script).
    expect(screen.getByText("橘猫原始视频")).toBeInTheDocument();
    expect(
      screen.getByText("创作总纲与镜头表（只读映射）"),
    ).toBeInTheDocument();
    // Visual design column carries the real entity.
    expect(screen.getByText("圆润大橘猫")).toBeInTheDocument();
    // Rough-cut strip: one frame per picture-carrying element (edit + r2v);
    // overlays / audio / transitions are not shots and stay out.
    expect(container.querySelectorAll("[data-roughcut-frame]")).toHaveLength(
      2,
    );

    // Clicking the script card opens the inline review panel with the legacy
    // mapping and selects the timeline as the assistant reference.
    fireEvent.click(screen.getByText("创作总纲与镜头表（只读映射）"));
    expect(
      container.querySelector("[data-blueprint-script-panel]"),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(
        "本项目创建于剧本功能之前，以下为既有信息的只读映射",
      ).length,
    ).toBeGreaterThan(0);
    expect(useCreatorInteractionStore.getState().selectedRef).toBe(
      "timeline:timeline:main",
    );
    // Approval never lives on the page — only the dock hint plus edit actions.
    expect(
      screen.getByText("审阅通过 / 驳回在右侧创作助手的待决策卡中完成"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "提出修改" }),
    ).toBeInTheDocument();
  });

  it("renders the linear episode list for multi-timeline projects without edges", () => {
    seedProject(cloneProject());
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="linear"]'),
    ).toBeInTheDocument();
    expect(screen.getByText("线性 2 节点")).toBeInTheDocument();
    expect(screen.getByText("第1集 · 晨光出发")).toBeInTheDocument();
    expect(screen.getByText("第2集 · 星夜归途")).toBeInTheDocument();

    // Selecting an episode opens the script panel and references the node.
    fireEvent.click(screen.getByText("第2集 · 星夜归途"));
    expect(
      container.querySelector("[data-blueprint-script-panel]"),
    ).toBeInTheDocument();
    expect(useCreatorInteractionStore.getState().selectedRef).toBe(
      "timeline:timeline:ep2",
    );
    // Synopsis is an editable creator field wired to the JSON pointer.
    const synopsis = container.querySelector(
      '[data-creator-field="timeline:timeline:ep2/synopsis"]',
    );
    expect(synopsis).toBeInTheDocument();
    expect(synopsis).toHaveAttribute(
      "data-creator-path",
      "/timelines/items/timeline:ep2/synopsis",
    );
  });
});
