import { beforeEach, describe, expect, it, vi } from "vitest";
import { dispatchWorkGraphNode, getWorkGraph } from "@/api/creator/workGraph";
import type { WorkGraphView } from "@/contracts/creator/workGraph";
import { useWorkGraphStore } from "@/store/workGraphStore";

vi.mock("@/api/creator/workGraph", () => ({
  getWorkGraph: vi.fn(),
  dispatchWorkGraphNode: vi.fn(),
}));

const store = () => useWorkGraphStore.getState();
const getGraph = vi.mocked(getWorkGraph);
const dispatch = vi.mocked(dispatchWorkGraphNode);
const result = { ok: true, nodeId: "video:one", dispatched: true };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function graph(projectId: string, generation = 1): WorkGraphView {
  return {
    projectId,
    generation,
    counts: { total: 0 },
    nodes: [],
    mediaCalls: 0,
    mediaCallBudget: 200,
  };
}

describe("WorkGraph async project isolation", () => {
  beforeEach(() => {
    store().reset();
    vi.resetAllMocks();
    getGraph.mockImplementation(async (projectId) => graph(projectId));
    dispatch.mockResolvedValue(result);
  });

  it("retains the latest snapshot when an older refresh arrives last", async () => {
    const older = deferred<WorkGraphView>();
    getGraph.mockReturnValueOnce(older.promise);
    const oldRefresh = store().refresh("p1");
    getGraph.mockResolvedValueOnce(graph("p1", 2));
    await store().refresh("p1");
    older.resolve(graph("p1", 1));
    await oldRefresh;
    expect(store().graph?.generation).toBe(2);
  });

  it.each(["success", "failure"] as const)(
    "ignores a pending refresh %s after reset",
    async (outcome) => {
      const pending = deferred<WorkGraphView>();
      getGraph.mockReturnValueOnce(pending.promise);
      const refresh = store().refresh("p1");
      store().reset();
      if (outcome === "success") pending.resolve(graph("p1"));
      else pending.reject(new Error("obsolete refresh error"));
      await refresh;
      expect(store()).toMatchObject({
        projectId: null,
        graph: null,
        loading: false,
        error: null,
        dispatching: {},
      });
    },
  );

  it("does not refresh the old project or clear the new project's dispatch", async () => {
    await store().refresh("p1");
    const oldRequest = deferred<typeof result>();
    const newRequest = deferred<typeof result>();
    dispatch
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);
    const oldDispatch = store().dispatchNode("p1", result.nodeId);
    await store().refresh("p2");
    const newDispatch = store().dispatchNode("p2", result.nodeId);
    oldRequest.resolve(result);
    await oldDispatch;
    expect(getGraph.mock.calls.map(([projectId]) => projectId)).toEqual([
      "p1",
      "p2",
    ]);
    expect(store()).toMatchObject({
      projectId: "p2",
      graph: graph("p2"),
      dispatching: { [result.nodeId]: true },
    });
    newRequest.resolve(result);
    await newDispatch;
    expect(store().dispatching).toEqual({});
    expect(getGraph).toHaveBeenLastCalledWith("p2");
  });

  it("fences old dispatches when the same project is reopened", async () => {
    await store().refresh("p1");
    const oldRequest = deferred<typeof result>();
    const newRequest = deferred<typeof result>();
    dispatch
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);
    const oldDispatch = store().dispatchNode("p1", result.nodeId);
    await store().refresh("p2");
    await store().refresh("p1");
    const newDispatch = store().dispatchNode("p1", result.nodeId);
    oldRequest.resolve(result);
    await oldDispatch;
    expect(getGraph).toHaveBeenCalledTimes(3);
    expect(store().dispatching[result.nodeId]).toBe(true);
    newRequest.resolve(result);
    await newDispatch;
    expect(getGraph).toHaveBeenCalledTimes(4);
    expect(store().dispatching).toEqual({});
  });

  it("does not revive a reset store when a dispatch finishes", async () => {
    await store().refresh("p1");
    const pending = deferred<typeof result>();
    dispatch.mockReturnValueOnce(pending.promise);
    const active = store().dispatchNode("p1", result.nodeId);
    store().reset();
    pending.resolve(result);
    await active;
    expect(getGraph).toHaveBeenCalledTimes(1);
    expect(store()).toMatchObject({
      projectId: null,
      graph: null,
      dispatching: {},
    });
  });

  it("keeps dispatch ownership through an ordinary same-project refresh", async () => {
    await store().refresh("p1");
    const pending = deferred<typeof result>();
    dispatch.mockReturnValueOnce(pending.promise);
    const active = store().dispatchNode("p1", result.nodeId);
    await store().refresh("p1");
    expect(store().dispatching[result.nodeId]).toBe(true);
    pending.resolve(result);
    await active;
    expect(store().dispatching).toEqual({});
    expect(getGraph).toHaveBeenCalledTimes(3);
  });

  it("rejects a current dispatch failure but suppresses an obsolete one", async () => {
    await store().refresh("p1");
    dispatch.mockRejectedValueOnce(new Error("current failure"));
    await expect(store().dispatchNode("p1", result.nodeId)).rejects.toThrow(
      "current failure",
    );
    expect(store().dispatching).toEqual({});
    expect(getGraph).toHaveBeenCalledTimes(2);

    const pending = deferred<typeof result>();
    dispatch.mockReturnValueOnce(pending.promise);
    const obsolete = store().dispatchNode("p1", result.nodeId);
    store().reset();
    await store().refresh("p1");
    pending.reject(new Error("obsolete failure"));
    await expect(obsolete).resolves.toBeUndefined();
    expect(getGraph).toHaveBeenCalledTimes(3);
    expect(store().error).toBeNull();
  });

  it("prevents duplicate pending requests and stale-project clicks", async () => {
    await store().refresh("p1");
    const pending = deferred<typeof result>();
    dispatch.mockReturnValueOnce(pending.promise);
    const active = store().dispatchNode("p1", result.nodeId);
    await store().dispatchNode("p1", result.nodeId);
    await store().dispatchNode("p2", result.nodeId);
    expect(dispatch).toHaveBeenCalledTimes(1);
    pending.resolve(result);
    await active;
  });
});
