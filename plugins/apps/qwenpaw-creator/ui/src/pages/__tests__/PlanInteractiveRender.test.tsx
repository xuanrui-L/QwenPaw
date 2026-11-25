import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import { projectDocument } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";
import type { ProjectDocument, TaskView } from "@/contracts/creator";
import type { WorkGraphView } from "@/contracts/creator/workGraph";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function seedProject(project = cloneProject()) {
  useProjectSnapshotStore.getState().reset("p1");
  useProjectSnapshotStore.setState({
    projectId: "p1",
    project,
    generation: project.generation,
    etag: '"sha256:g3"',
    syncStatus: "healthy",
    syncError: null,
    lastGoodAt: "2026-07-20T00:02:00Z",
  });
}

function seedWithoutFinal(project = cloneProject()) {
  delete project.assets.artifact_slots_by_id["timeline:timeline:main:render"];
  delete project.assets.artifact_versions_by_id["final-v1"];
  seedProject(project);
  return project;
}

function composeGraph(status: "ready" | "stale"): WorkGraphView {
  return {
    projectId: "p1",
    generation: 3,
    counts: { [status]: 1 },
    mediaCalls: 0,
    nodes: [
      {
        id: "compose:timeline:main",
        kind: "compose",
        label: "最终合成",
        status,
        deps: [],
        lane: "compose",
        timelineId: "timeline:main",
        taskId: null,
        progress: null,
        error: null,
        missing: [],
        locator: { page: "plan" },
        dispatchable: true,
      },
    ],
  };
}

function pollRoutes(tasks: TaskView[] = []) {
  return [
    { match: "/specialist-runs", response: { json: { items: [] } } },
    { match: "/tasks", response: { json: { items: tasks } } },
    {
      match: "/projects/p1/project",
      response: {
        status: 304,
        headers: {
          ETag: '"sha256:g3"',
          "X-Project-Generation": "3",
          "X-Project-Sync-Status": "healthy",
        },
      },
    },
  ];
}

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

  it.each([["AUTOMATIC_COMPOSE_NOT_READY", composeGraph("stale")]] as const)(
    "refreshes the graph on %s and still permits an explicit render",
    async (code, refreshed) => {
      vi.useFakeTimers();
      try {
        seedWithoutFinal();
        useWorkGraphStore.setState({
          projectId: "p1",
          graph: composeGraph("ready"),
        });
        const { calls } = installMockFetch([
          {
            match: "/render?automatic=true",
            method: "POST",
            response: {
              ok: false,
              status: 409,
              json: {
                message: code,
                details: { code },
              },
            },
          },
          {
            match: "/work-graph",
            response: { json: refreshed },
          },
          {
            match: "/render",
            method: "POST",
            response: { json: { taskId: "manual-task" } },
          },
          ...pollRoutes(),
        ]);
        renderPage();
        await act(() => vi.advanceTimersByTimeAsync(1600));
        expect(useWorkGraphStore.getState().graph).toEqual(refreshed);
        await act(() => vi.advanceTimersByTimeAsync(3200));
        expect(
          calls.filter((call) => call.url.includes("/render")),
        ).toHaveLength(1);
        await act(async () => {
          fireEvent.click(screen.getByRole("button", { name: "合成成片" }));
        });
        expect(
          calls.filter(
            (call) => call.method === "POST" && call.url.endsWith("/render"),
          ),
        ).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    },
  );

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
