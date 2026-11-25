import { fireEvent, screen } from "@testing-library/react";
import { act } from "react";
import type { TaskView } from "@/contracts/creator";
import type { WorkGraphView } from "@/contracts/creator/workGraph";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { installMockFetch } from "@/test/mockFetch";
import { expect, vi } from "vitest";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";

/** Minimal typed-enough JSON Response stub for store polling tests. */
export function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    headers: new Headers(headers),
    json: vi.fn(async () => body),
  } as unknown as Response;
}

/** Minimal file-native project.json document (project p1). */
export function projectJson(
  generation: number,
  name = `Project ${generation}`,
) {
  return {
    schema_version: 1,
    project_id: "p1",
    generation,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z",
    name,
    description: "",
    scenario: "general",
    settings: {},
    strategy: {},
    sources: {},
    visual: {},
    story: {},
    production: {},
    post_production: {},
    assets: {},
  };
}

/** A healthy snapshot response with the generation-derived ETag. */
export function snapshotResponse(generation: number, name?: string): Response {
  return jsonResponse(
    200,
    {
      projectId: "p1",
      generation,
      etag: `sha256:g${generation}`,
      syncStatus: "healthy",
      project: projectJson(generation, name),
    },
    { ETag: `"sha256:g${generation}"` },
  );
}

export function cloneProject() {
  return structuredClone(projectDocument);
}

export function seedProject(project = cloneProject(), lastGoodAt?: string) {
  useProjectSnapshotStore.getState().reset("p1");
  useProjectSnapshotStore.setState({
    projectId: "p1",
    project,
    generation: project.generation,
    etag: '"sha256:g3"',
    syncStatus: "healthy",
    syncError: null,
    ...(lastGoodAt ? { lastGoodAt } : {}),
  });
}

export function singleProject() {
  const project = cloneProject();
  project.timelines.order = ["timeline:main"];
  delete project.timelines.items["timeline:ep2"];
  return project;
}

export function seedWithoutFinal(project = cloneProject()) {
  delete project.assets.artifact_slots_by_id["timeline:timeline:main:render"];
  delete project.assets.artifact_versions_by_id["final-v1"];
  seedProject(project, "2026-07-20T00:02:00Z");
  return project;
}

export function composeGraph(status: "ready" | "stale"): WorkGraphView {
  return {
    projectId: "p1",
    generation: 3,
    counts: { [status]: 1 },
    mediaCalls: 0,
    ...{ mediaCallBudget: 20 },
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

export function pollRoutes(tasks: TaskView[] = []) {
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

export async function checkAutomaticComposeRefusal(
  renderPage: () => unknown,
  code: string,
  refreshed: WorkGraphView,
) {
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
    expect(calls.filter((call) => call.url.includes("/render"))).toHaveLength(
      1,
    );
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
}
