import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import BlueprintPage from "@/pages/BlueprintPage";
import { NavigationRuntime } from "@/routing/navigation";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import EpisodeListPanel from "@/components/blueprint/EpisodeListPanel";
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

describe("Blueprint before the first agent output", () => {
  function emptyProject() {
    const project = singleProject();
    const timeline = project.timelines.items["timeline:main"];
    timeline.title = "";
    timeline.synopsis = "";
    timeline.elements_by_id = {};
    project.assets.artifact_slots_by_id = {};
    project.visual.entities = { order: [], items: {} };
    project.sources.sources = { order: [], items: {} };
    return project;
  }
  beforeEach(() => {
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useWorkGraphStore.getState().reset();
    useCreatorSessionStore.setState({
      projectId: "p1",
      connectionState: "connected",
      session: {
        id: "session-new",
        projectId: "p1",
        status: "RUNNING",
        lastMessageSeq: 1,
        lastConsumedMessageSeq: 0,
        lastEventSeq: 0,
      },
    });
  });

  it("shows activity immediately and replaces it as real blueprint content arrives", () => {
    const project = emptyProject();
    seedProject(project);
    const { container } = renderPage();
    expect(
      container.querySelector('[data-workspace-empty="blueprint"]'),
    ).toHaveAttribute("data-state", "working");
    expect(container.querySelector("textarea")).not.toBeInTheDocument();
    expect(
      container.querySelector("[data-blueprint-activity]"),
    ).toBeInTheDocument();
    expect(screen.queryByText("暂无进行中的生产任务")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("本项目创建于剧本功能之前");

    const published = structuredClone(project);
    published.timelines.items["timeline:main"].title = "雨夜来信";
    published.timelines.items["timeline:main"].synopsis =
      "快递员在雨夜收到一封来自未来的信。";
    act(() => seedProject(published));
    expect(
      container.querySelector("[data-blueprint-initial]"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByDisplayValue("快递员在雨夜收到一封来自未来的信。"),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-workspace-empty="script"]'),
    ).toHaveAttribute("data-state", "working");
    expect(container).not.toHaveTextContent("本项目创建于剧本功能之前");
  });

  it("does not mistake a history snapshot or the initial user brief for the first output", () => {
    const project = emptyProject();
    const historical = structuredClone(
      projectDocument.timelines.items["timeline:main"],
    );
    historical.timeline_id = "snapshot:old";
    project.timelines.items["snapshot:old"] = historical;
    project.timelines.order.push("snapshot:old");
    seedProject(project);
    const { container } = renderPage();
    expect(
      container.querySelector('[data-workspace-empty="blueprint"]'),
    ).toBeInTheDocument();
  });

  it("shows the committed script before elements or an artifact have been created", () => {
    const project = emptyProject();
    project.timelines.items["timeline:main"].description =
      "【起 · 0–4秒】快递员在雨夜拆开一封来信。\n【承 · 4–8秒】信里写着即将发生的事。";
    seedProject(project);
    const { container } = renderPage();
    expect(
      screen.getByText("【起 · 0–4秒】快递员在雨夜拆开一封来信。"),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-workspace-empty="script"]'),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector(
        '[data-creator-path="/timelines/items/timeline:main/description"]',
      ),
    ).toBeInTheDocument();
  });

  it("shows published visual development even before a story or shot reference exists", () => {
    const project = emptyProject();
    project.visual = structuredClone(projectDocument.visual);
    seedProject(project);
    const { container } = renderPage();
    expect(screen.getByText("圆润大橘猫")).toBeInTheDocument();
    expect(
      container.querySelector('[data-workspace-empty="script"]'),
    ).toHaveAttribute("data-state", "working");
    expect(
      container.querySelector("[data-blueprint-initial]"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("暂无成片")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /视觉开发/ }),
    ).not.toHaveTextContent("待确认");
  });

  it("gives the initially empty episode sidebar the same feedback and reveals real episodes on publication", () => {
    const project = emptyProject();
    seedProject(project);
    const { container } = render(
      <MemoryRouter initialEntries={["/project/p1"]}>
        <NavigationRuntime />
        <EpisodeListPanel />
      </MemoryRouter>,
    );
    expect(
      container.querySelector('[data-workspace-empty="episodes"]'),
    ).toHaveAttribute("data-state", "working");
    const next = structuredClone(project);
    next.timelines.items["timeline:main"].title = "雨夜来信";
    act(() => seedProject(next));
    expect(
      container.querySelector("[data-workspace-empty]"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /雨夜来信/ }),
    ).toBeInTheDocument();
  });
});

describe("BlueprintPage narrative shapes", () => {
  beforeEach(() => {
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useWorkGraphStore.getState().reset();
  });

  it("renders single projects as the inline script view (no structure board)", () => {
    seedProject(singleProject());
    const { container } = renderPage();

    // 单集设计 (84:37778): the script document IS the blueprint main area.
    expect(
      container.querySelector("[data-blueprint-script-panel]"),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-blueprint-shape="single"]'),
    ).not.toBeInTheDocument();
    // Legacy read-only mapping (project predates timeline_script).
    expect(
      screen.getAllByText("本项目创建于剧本功能之前，以下为既有信息的只读映射")
        .length,
    ).toBeGreaterThan(0);
    // Overview rail keeps the referenced visual entity reachable.
    expect(screen.getByText("圆润大橘猫")).toBeInTheDocument();
    // Rough-cut strip: one frame per picture-carrying element (edit + r2v);
    // overlays / audio / transitions are not shots and stay out.
    expect(container.querySelectorAll("[data-roughcut-frame]")).toHaveLength(2);
    // Approval never lives on the page — only edit actions.
    expect(
      screen.getByRole("button", { name: "提出修改" }),
    ).toBeInTheDocument();
    // Header chrome: page title left (no 返回 on the blueprint itself),
    // prep entries and the download / export dropdown right; the footer is
    // the production task status bar.
    expect(
      screen.queryByRole("button", { name: "返回" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("剧集蓝图")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /调研与素材/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /视觉开发/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载 / 导出" }),
    ).toBeInTheDocument();
    expect(screen.getByText("暂无进行中的生产任务")).toBeInTheDocument();
  });

  it("renders the linear episode card grid for multi-timeline projects without edges", () => {
    seedProject(cloneProject());
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="linear"]'),
    ).toBeInTheDocument();
    // Titles appear on the episode cards (and again on rough-cut groups).
    expect(screen.getAllByText("第1集 · 晨光出发").length).toBeGreaterThan(0);
    expect(screen.getAllByText("第2集 · 星夜归途").length).toBeGreaterThan(0);
    // 集卡 pills (design 84:29455): 查看剧本 / 制作台编辑 on every card.
    expect(screen.getAllByTitle("查看剧本").length).toBeGreaterThan(1);
    expect(screen.getAllByTitle("时间线编辑").length).toBeGreaterThan(1);

    // Selecting an episode opens the script panel and references the node.
    fireEvent.click(
      container.querySelector(
        '[data-blueprint-episode="timeline:ep2"]',
      ) as HTMLElement,
    );
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

  /**
   * The video_edit pipeline stamps none of the generation artifacts the
   * board checks: shots are real clips referenced via render_source
   * (never element_video slots), the narrator is a voice-only role, no
   * timeline_script slot or source intelligence version is ever written.
   * Completion must derive from those durable facts plus the composed
   * final cut instead of reading steps 1-4 as incomplete.
   */
  function videoEditProject(): ProjectDocument {
    const project = singleProject();
    project.scenario = "video_edit";
    const timeline = project.timelines.items["timeline:main"];
    delete timeline.elements_by_id["r2v-window"];
    delete timeline.elements_by_id["transition"];
    delete project.assets.artifact_slots_by_id["element:r2v-window:video"];
    delete project.assets.artifact_slots_by_id["visual:cat:anchor"];
    delete project.assets.artifact_versions_by_id["r2v-window-v1"];
    delete project.assets.artifact_versions_by_id["cat-anchor-v1"];
    project.visual.entities = {
      order: ["char:narrator"],
      items: {
        "char:narrator": {
          entity_id: "char:narrator",
          kind: "character",
          name: "旁白（画外音）",
          description: "仅画外音、无视觉形象",
          continuity: "全片同一音色",
          required_variant_ids: [],
          variants: { order: [], items: {} },
          selected_artifact_version_id: null,
          voice: {
            voice_id: "voice-1",
            target_model: "tts-flash",
            preferred_name: "教学旁白",
            sample_source_version_id: null,
            enrollment_key: "key-1",
            created_at: "2026-07-20T00:00:00Z",
          },
        },
      },
    };
    return project;
  }

  it("video_edit single projects render the script view; final-cut facts survive a missing render", () => {
    seedProject(videoEditProject());
    const first = renderPage();

    // 单集设计 (84:37778): no structure board, the script document renders.
    expect(
      first.container.querySelector("[data-blueprint-script-panel]"),
    ).toBeInTheDocument();
    expect(
      first.container.querySelector('[data-blueprint-shape="single"]'),
    ).not.toBeInTheDocument();
    // Legacy read-only mapping (no timeline_script slot on this path).
    expect(
      screen.getAllByText("本项目创建于剧本功能之前，以下为既有信息的只读映射")
        .length,
    ).toBeGreaterThan(0);
    // Rough-cut strip: the render_source clip counts as a final frame.
    expect(
      first.container.querySelectorAll("[data-roughcut-frame]"),
    ).toHaveLength(1);
    expect(screen.getByText("成片帧")).toBeInTheDocument();
    // No pending-visual warning chip for the voice-only narrator.
    expect(screen.queryByText("1 待确认")).not.toBeInTheDocument();
    first.unmount();

    // The clip-backed shot still reads as a final frame after the composed
    // final cut disappears — render_source is a durable per-shot fact.
    const project = videoEditProject();
    delete project.assets.artifact_slots_by_id["timeline:timeline:main:render"];
    delete project.assets.artifact_versions_by_id["final-v1"];
    seedProject(project);
    const second = renderPage();
    expect(
      second.container.querySelector("[data-blueprint-script-panel]"),
    ).toBeInTheDocument();
    expect(
      second.container.querySelectorAll("[data-roughcut-frame]"),
    ).toHaveLength(1);
    expect(screen.getByText("成片帧")).toBeInTheDocument();
  });
});
