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
    expect(screen.getByText("线性 2 集")).toBeInTheDocument();
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

  it("video_edit board derives step completion from clips, voice role and the final cut", () => {
    seedProject(videoEditProject());
    const { container } = renderPage();

    expect(
      container.querySelector('[data-blueprint-shape="single"]'),
    ).toBeInTheDocument();
    expect(screen.getByText("素材剪辑")).toBeInTheDocument();
    // All five stage columns read completed.
    expect(screen.getAllByText("已完成")).toHaveLength(5);
    // Shot card: done via its render_source clip (per-shot durable fact).
    expect(screen.getByText("已生成")).toBeInTheDocument();
    // Narrator: voice-only role with an enrolled voice needs no visuals.
    expect(screen.getByText("音色已建立（画外音角色）")).toBeInTheDocument();
    // Source understanding and the (never-written) script slot never ran
    // on this path; the composed final cut implies both (a stale final
    // would drop the implication).
    expect(screen.getAllByText("已随成片完成")).toHaveLength(2);
    expect(screen.getByText("成片已合成")).toBeInTheDocument();
    // Rough-cut strip: the clip counts as a final frame.
    expect(container.querySelectorAll("[data-roughcut-frame]")).toHaveLength(1);
    expect(screen.getByText("成片帧")).toBeInTheDocument();
    // No pending-visual warning chip for the voice-only narrator.
    expect(screen.queryByText("1 待确认")).not.toBeInTheDocument();
  });

  it("video_edit board keeps per-shot clip completion but no implication before the final cut", () => {
    const project = videoEditProject();
    delete project.assets.artifact_slots_by_id["timeline:timeline:main:render"];
    delete project.assets.artifact_versions_by_id["final-v1"];
    seedProject(project);
    renderPage();

    // The clip-backed shot still reads generated (durable render_source fact)…
    expect(screen.getByText("已生成")).toBeInTheDocument();
    // …but with no final cut nothing is implied: understanding stays pending.
    expect(screen.getByText("待理解")).toBeInTheDocument();
    expect(screen.queryByText("已随成片完成")).not.toBeInTheDocument();
  });
});
