import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectLayout from "@/components/layout/ProjectLayout";
import { NavigationRuntime } from "@/routing/navigation";
import type { TaskView } from "@/contracts/creator";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { projectDocument, status } from "@/test/creatorFixtures";
import { installMockFetch, type MockRoute } from "@/test/mockFetch";

// Keep this test about the project shell's real durable reads. Child panels
// have independent mount effects covered by their own integration tests.
vi.mock("@/components/layout/TopNav", () => ({ default: () => null }));
vi.mock("@/components/layout/WorkspaceSidebar", () => ({
  default: () => null,
}));
vi.mock("@/components/creator/ReturnBanner", () => ({ default: () => null }));
vi.mock("@/components/creator/LaunchUploadProgressCard", () => ({
  default: () => null,
}));
vi.mock("@/components/agent", () => ({ SelectionToolbar: () => null }));
vi.mock("@/components/onboarding", () => ({
  ProjectTour: () => null,
  AssetsTour: () => null,
}));

function task(projectId: string, taskStatus: TaskView["status"]): TaskView {
  return {
    id: `${projectId}-manual-image`,
    projectId,
    transactionId: null,
    specialistRunId: null,
    kind: "image_generation",
    targetRef: "element:shot-one",
    status: taskStatus,
    progress: taskStatus === "SUCCEEDED" ? 1 : null,
    resultRefs: [],
  };
}

function projectRoutes(projectId: string) {
  const tasks: MockRoute = { json: { items: [] } };
  const graph: MockRoute = {
    json: {
      projectId,
      generation: 0,
      counts: { gated: 1 },
      nodes: [],
      mediaCalls: 0,
      mediaCallBudget: 0,
    },
  };
  const project = {
    ...structuredClone(projectDocument),
    project_id: projectId,
  };
  return {
    tasks,
    graph,
    routes: [
      { match: `/projects/${projectId}/tasks`, response: tasks },
      { match: `/projects/${projectId}/work-graph`, response: graph },
      {
        match: `/projects/${projectId}/specialist-runs`,
        response: { json: { items: [] } },
      },
      {
        match: `/projects/${projectId}/runtime/reviews/active`,
        response: { status: 204 },
      },
      {
        match: `/projects/${projectId}/project`,
        response: {
          json: {
            projectId,
            project,
            generation: 0,
            etag: `"${projectId}:0"`,
            syncStatus: "healthy",
          },
          headers: { ETag: `"${projectId}:0"` },
        },
      },
      {
        match: `/projects/${projectId}/session`,
        response: {
          json: {
            session: {
              id: `${projectId}-session`,
              projectId,
              status: "IDLE",
              lastMessageSeq: 0,
              lastConsumedMessageSeq: 0,
              lastEventSeq: 0,
            },
            agentStatusBar: status,
          },
        },
      },
      {
        match: `/projects/${projectId}/conversations/c1/messages`,
        response: { json: { items: [] } },
      },
      {
        match: `/projects/${projectId}/conversations`,
        response: {
          json: {
            items: [
              {
                conversationId: "c1",
                title: "测试",
                isDefault: true,
                createdAt: "now",
              },
            ],
          },
        },
      },
    ],
  };
}

async function mountShell() {
  const router = createMemoryRouter(
    [
      {
        path: "/project/:id",
        element: (
          <>
            <NavigationRuntime />
            <ProjectLayout />
          </>
        ),
        children: [{ path: "plan", element: <div>plan</div> }],
      },
    ],
    { initialEntries: ["/project/p1/plan"] },
  );
  let view: ReturnType<typeof render>;
  await act(async () => {
    view = render(<RouterProvider router={router} />);
  });
  return { router, view: view! };
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function deferred() {
  let resolve!: () => void;
  return {
    promise: new Promise<void>((done) => {
      resolve = done;
    }),
    resolve: () => resolve(),
  };
}

describe("ProjectLayout durable production polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useWorkGraphStore.getState().reset();
    useExecutionAuthorizationStore.getState().reset();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("discovers a manual task while IDLE and graph gated, then follows active and terminal cadence", async () => {
    const server = projectRoutes("p1");
    const { calls } = installMockFetch(server.routes);
    await mountShell();
    const count = (path: string) =>
      calls.filter((call) => call.url.endsWith(path)).length;
    expect(count("p1/tasks")).toBe(1);
    expect(count("p1/work-graph")).toBe(1);
    expect(useCreatorSessionStore.getState().session?.status).toBe("IDLE");
    server.tasks.json = { items: [task("p1", "QUEUED")] };
    await advance(9_999);
    expect(count("p1/tasks")).toBe(1);
    await advance(1);
    expect(count("p1/tasks")).toBe(2);
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe("QUEUED");
    server.tasks.json = { items: [task("p1", "RUNNING")] };
    await advance(3_000);
    expect(count("p1/tasks")).toBe(3);
    expect(count("p1/work-graph")).toBe(3);
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe("RUNNING");
    server.tasks.json = { items: [task("p1", "SUCCEEDED")] };
    await advance(3_000);
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe(
      "SUCCEEDED",
    );
    expect(count("p1/tasks")).toBe(4);
    await advance(9_999);
    expect(count("p1/tasks")).toBe(4);
    await advance(1);
    expect(count("p1/tasks")).toBe(5);
  });

  it("pauses hidden tabs and immediately discovers changes when visible again", async () => {
    const server = projectRoutes("p1");
    const { calls } = installMockFetch(server.routes);
    await mountShell();
    const count = () =>
      calls.filter((call) => call.url.endsWith("p1/tasks")).length;
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    server.tasks.json = { items: [task("p1", "RUNNING")] };
    await advance(30_000);
    expect(count()).toBe(1);
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(count()).toBe(2);
    expect(useCreatorTaskViewStore.getState().tasks[0].status).toBe("RUNNING");
    await advance(3_000);
    expect(count()).toBe(3);
  });

  it("uses one task and graph loop when both the session and production are active", async () => {
    const server = projectRoutes("p1");
    const { calls } = installMockFetch(server.routes);
    await mountShell();
    server.tasks.json = { items: [task("p1", "RUNNING")] };
    server.graph.json = {
      projectId: "p1",
      generation: 0,
      counts: { running: 1 },
      nodes: [],
      mediaCalls: 1,
      mediaCallBudget: 0,
    };
    await act(async () => {
      useCreatorSessionStore.setState((current) => ({
        session: { ...current.session!, status: "RUNNING" },
      }));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const count = (path: string) =>
      calls.filter((call) => call.url.endsWith(path)).length;
    const tasksBefore = count("p1/tasks"),
      graphBefore = count("p1/work-graph");
    await advance(6_000);
    expect(count("p1/tasks") - tasksBefore).toBe(2);
    expect(count("p1/work-graph") - graphBefore).toBe(2);
  });

  it("coalesces slow reads across intervals, visibility and task events", async () => {
    const server = projectRoutes("p1");
    const { fetchMock } = installMockFetch(server.routes);
    const respond = fetchMock.getMockImplementation()!;
    const held = deferred();
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("p1/tasks")) await held.promise;
      return respond(input, init);
    });
    await mountShell();
    const count = (path: string) =>
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith(path))
        .length;
    await advance(20_000);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      useCreatorSessionStore.getState().ingestEvents([
        {
          eventId: "manual-start",
          seq: 1,
          type: "task.started",
          projectId: "p1",
          creatorSessionId: "p1-session",
          at: "now",
          data: { task: task("p1", "RUNNING") },
        },
      ]);
    });
    await advance(6_000);
    expect(count("p1/tasks")).toBe(1);
    expect(count("p1/work-graph")).toBe(1);
    server.tasks.json = { items: [task("p1", "RUNNING")] };
    await act(async () => {
      held.resolve();
    });
    await advance(3_000);
    expect(count("p1/tasks")).toBe(2);
    expect(count("p1/work-graph")).toBe(2);
  });

  it("switches projects without adopting late old reads or retaining their polling", async () => {
    const first = projectRoutes("p1"),
      second = projectRoutes("p2");
    first.tasks.json = { items: [task("p1", "RUNNING")] };
    second.tasks.json = { items: [task("p2", "SUCCEEDED")] };
    const { fetchMock } = installMockFetch([...first.routes, ...second.routes]);
    const respond = fetchMock.getMockImplementation()!;
    const held = deferred();
    fetchMock.mockImplementation(async (input, init) => {
      if (/p1\/(tasks|work-graph)$/.test(String(input))) await held.promise;
      return respond(input, init);
    });
    const { router, view } = await mountShell();
    await act(async () => {
      await router.navigate("/project/p2/plan");
    });
    expect(useCreatorTaskViewStore.getState().projectId).toBe("p2");
    expect(useCreatorTaskViewStore.getState().tasks).toEqual([
      task("p2", "SUCCEEDED"),
    ]);
    await act(async () => {
      held.resolve();
    });
    expect(useCreatorTaskViewStore.getState().tasks).toEqual([
      task("p2", "SUCCEEDED"),
    ]);
    expect(useWorkGraphStore.getState().graph?.projectId).toBe("p2");
    await advance(10_000);
    const count = (path: string) =>
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith(path))
        .length;
    expect(count("p1/tasks")).toBe(1);
    expect(count("p2/tasks")).toBe(2);
    view.unmount();
    await advance(30_000);
    expect(count("p2/tasks")).toBe(2);
  });
});
