import {
  checkAutomaticComposeRefusal,
  composeGraph,
  pollRoutes,
  cloneProject,
  seedProject as seedProjectSnapshot,
} from "@/test/projectSnapshotFixtures";
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkGraphStore } from "@/store/workGraphStore";
import PlanPage from "@/pages/PlanPage";
import { NavigationRuntime } from "@/routing/navigation";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { installMockFetch } from "@/test/mockFetch";
import type { ProjectDocument, TaskView } from "@/contracts/creator";

const seedProject = (project?: ProjectDocument) =>
  seedProjectSnapshot(project, "2026-07-20T00:02:00Z");

function renderPage(entry = "/project/p1/plan") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <NavigationRuntime />
      <Routes>
        <Route path="/project/:id/plan" element={<PlanPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("PlanPage Timeline/Element frontend", () => {
  beforeEach(() => {
    useWorkGraphStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useAgentDockUiStore.getState().reset();
    useCreatorSessionStore.getState().reset();
    seedProject();
  });

  it("refreshes the stale graph and still permits an explicit render", () =>
    checkAutomaticComposeRefusal(
      renderPage,
      "AUTOMATIC_COMPOSE_NOT_READY",
      composeGraph("stale"),
    ));

  it("keeps a superseded co-creation cut manual before and after a hold", async () => {
    vi.useFakeTimers();
    try {
      const project = cloneProject();
      project.assets.artifact_versions_by_id["final-v1"].stale = true;
      seedProject(project);
      const ready = composeGraph("ready");
      const stale = composeGraph("stale");
      const { calls } = installMockFetch([
        {
          match: "/render",
          method: "POST",
          response: { json: { taskId: "manual-task" } },
        },
        ...pollRoutes(),
      ]);
      renderPage();
      const renders = () =>
        calls.filter((call) => call.url.includes("/render"));
      for (const graph of [
        { ...ready, generation: 2 },
        { ...ready, nodes: [] },
        stale,
        { ...stale, manualHold: { revision: 1, nodeIds: [stale.nodes[0].id] } },
        { ...stale, manualHold: { revision: 2, nodeIds: [] } },
      ]) {
        act(() => useWorkGraphStore.setState({ projectId: "p1", graph }));
        await act(() => vi.advanceTimersByTimeAsync(1600));
        expect(renders()).toHaveLength(0);
      }
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "合成成片" }));
      });
      expect(renders()).toHaveLength(1);
      expect(renders()[0].url).toMatch(/\/render$/);
    } finally {
      vi.useRealTimers();
    }
  });
});
