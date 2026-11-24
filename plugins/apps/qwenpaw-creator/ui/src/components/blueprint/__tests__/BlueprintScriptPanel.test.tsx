import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { message } from "antd";
import BlueprintScriptPanel from "@/components/blueprint/BlueprintScriptPanel";
import { projectDocument } from "@/test/creatorFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";

vi.mock("@/api/creator", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/creator")>()),
  getArtifactVersionMediaUrl: (id: string) => `https://media.test/${id}`,
}));

function scriptProject() {
  const project = structuredClone(projectDocument);
  project.timelines.items["timeline:main"].description = "较早的内联草稿";
  project.assets.artifact_slots_by_id["script:timeline:main"] = {
    slot_id: "script:timeline:main",
    kind: "timeline_script",
    owner_ref: "timeline:timeline:main",
    version_ids: ["script-v1"],
    selected_version_id: "script-v1",
    metadata: {},
  };
  project.assets.artifact_versions_by_id["script-v1"] = {
    ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
    version_id: "script-v1",
    slot_id: "script:timeline:main",
    kind: "timeline_script",
    owner_ref: "timeline:timeline:main",
    file_id: "script-file",
    name: "序章",
    stale: true,
    stale_reason: "剧本创作依据已修改，需要重新起草",
  };
  return project;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useProjectSnapshotStore.getState().reset();
  useAgentDockUiStore.getState().reset();
});

describe("stale script action routing", () => {
  function renderPanel(project: ReturnType<typeof scriptProject>) {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => "# 序章正文\n已经生成的正式剧本。",
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <BlueprintScriptPanel
        project={project}
        projectId="p1"
        timelineId="timeline:main"
        open
        inline
        onClose={vi.fn()}
        onOpenTimeline={vi.fn()}
        onOpenVisualEntity={vi.fn()}
      />,
    );
  }

  it("routes the stale action to a redraft request while media production is approved", async () => {
    const project = scriptProject();
    project.settings.production_stage = "media";
    const info = vi.spyOn(message, "info");
    renderPanel(project);
    await screen.findByText("已经生成的正式剧本。");

    fireEvent.click(screen.getByRole("button", { name: "更新剧本" }));

    // The composer carries the redraft request instead of a dead probe.
    expect(useAgentDockUiStore.getState().draft).toContain("重新起草");
    expect(useAgentDockUiStore.getState().selection?.ref).toBe(
      "timeline:timeline:main",
    );
    expect(info).toHaveBeenCalledWith(
      expect.objectContaining({
        content: expect.stringContaining("不阻塞已就绪任务的派发"),
      }),
    );
    expect(info).not.toHaveBeenCalledWith(
      expect.objectContaining({
        content: expect.stringContaining("未找到确认按钮"),
      }),
    );
  });

  it("keeps focusing the production-stage confirm button while the script stage owns approval", async () => {
    const project = scriptProject();
    project.settings.production_stage = "script";
    const success = vi.spyOn(message, "success");
    renderPanel(project);
    await screen.findByText("已经生成的正式剧本。");
    // The blueprint header renders this button only in the script stage.
    render(<button type="button" data-production-stage-confirm="" />);

    fireEvent.click(screen.getByRole("button", { name: "重新确认剧本" }));

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith(
        expect.stringContaining("已定位到「确认剧本并继续」"),
      ),
    );
    expect(useAgentDockUiStore.getState().draft).toBe("");
  });
});
