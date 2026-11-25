import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import {
  jsonResponse as response,
  snapshotResponse as snapshot,
} from "@/test/projectSnapshotFixtures";
import { projectDocument } from "@/test/creatorFixtures";
import { useCreatorEditBufferStore } from "@/store/creatorEditBufferStore";
import type { ProjectDocument } from "@/contracts/creator";

const store = () => useProjectSnapshotStore.getState();
const poll = () => store().pollOnce("p1");
const startPolling = (overrides: Record<string, number> = {}) =>
  store().startPolling("p1", {
    activeIntervalMs: 100,
    hiddenIntervalMs: 1_000,
    retryBaseMs: 50,
    maxBackoffMs: 200,
    jitterRatio: 0,
    random: () => 0.5,
    ...overrides,
  });

/** Advance fake timers step by step, asserting the fetch call count. */
async function ticks(mock: unknown, steps: Array<[number, number]>) {
  for (const [ms, calls] of steps) {
    await vi.advanceTimersByTimeAsync(ms);
    expect(mock).toHaveBeenCalledTimes(calls);
  }
}

afterEach(() => {
  store().reset();
  vi.useRealTimers();
});

describe("Project snapshot authority store", () => {
  it("retains the last-good Project when a later file snapshot is invalid", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(snapshot(3, "Last good"))
      .mockResolvedValueOnce(
        response(409, {
          code: "PROJECT_INVALID",
          syncStatus: "invalid",
          lastGoodGeneration: 3,
          message: "project.json is invalid",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await poll();
    const lastGood = store().project;
    await poll();
    expect(store().project).toBe(lastGood);
    expect(store()).toMatchObject({
      generation: 3,
      syncStatus: "invalid",
      syncError: "project.json is invalid",
    });
    expect(
      new Headers(fetchMock.mock.calls[1][1].headers).get("If-None-Match"),
    ).toBe('"sha256:g3"');
  });

  it("deduplicates concurrent polls and rejects a later low-generation response", async () => {
    let resolveFetch!: (value: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = poll();
    const second = poll();
    expect(first).toBe(second);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveFetch(snapshot(5, "Current"));
    await Promise.all([first, second]);
    expect(store().generation).toBe(5);

    const third = poll();
    resolveFetch(snapshot(4, "Stale"));
    await third;
    expect(store().generation).toBe(5);
    expect(store().project?.name).toBe("Current");
    expect(store().appliedRequestSequence).toBe(store().issuedRequestSequence);
  });

  it("recovers from startup 404s and only evicts and stops after confirmed deletion", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(snapshot(2, "Before deletion"))
      .mockResolvedValueOnce(response(404, { detail: "Not Found" }))
      .mockResolvedValueOnce(snapshot(3, "Recovered"))
      .mockResolvedValueOnce(
        response(404, { code: "NOT_FOUND", message: "Project 不存在" }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const stop = startPolling();
    await vi.advanceTimersByTimeAsync(0);
    expect(store().project?.name).toBe("Before deletion");
    const lastGood = store().project;

    await vi.advanceTimersByTimeAsync(100);
    expect(store().project).toBe(lastGood);
    expect(store()).toMatchObject({ syncStatus: "degraded", polling: true });
    await vi.advanceTimersByTimeAsync(50);
    expect(store().project?.name).toBe("Recovered");
    expect(store().syncStatus).toBe("healthy");
    await vi.advanceTimersByTimeAsync(100);
    expect(store()).toMatchObject({
      project: null,
      generation: null,
      etag: null,
      syncStatus: "not_found",
      polling: false,
    });
    await ticks(fetchMock, [[1_000, 4]]);
    stop();
  });

  it("uses active and hidden intervals without overlapping requests", async () => {
    vi.useFakeTimers();
    const originalVisibility = Object.getOwnPropertyDescriptor(
      document,
      "visibilityState",
    );
    let visibility: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
    const fetchMock = vi.fn(async () => snapshot(1));
    vi.stubGlobal("fetch", fetchMock);

    const stop = startPolling();
    await ticks(fetchMock, [
      [0, 1],
      [99, 1],
      [1, 2],
    ]);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await ticks(fetchMock, [
      [999, 2],
      [1, 3],
    ]);

    stop();
    if (originalVisibility)
      Object.defineProperty(document, "visibilityState", originalVisibility);
  });

  it("caps exponential retry delay after poll failures", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => {
      throw new Error("offline");
    });
    vi.stubGlobal("fetch", fetchMock);

    const stop = startPolling({ retryBaseMs: 100, maxBackoffMs: 250 });
    await ticks(fetchMock, [
      [0, 1],
      [100, 2],
      [200, 3],
      [249, 3],
      [1, 4],
    ]);
    stop();
  });
});

const PROJECT_ID = "project-1";
const editBuffer = () => useCreatorEditBufferStore.getState();

function record(
  path: string,
  before: unknown,
  value: unknown,
  generation: number,
) {
  editBuffer().recordPatch({
    projectId: PROJECT_ID,
    projectBefore: structuredClone(projectDocument) as ProjectDocument,
    operations: [{ op: "replace", path, before, value }],
    generation,
  });
}

const SPAN_START =
  "/timelines/items/timeline:main/elements_by_id/edit-opening/span/start_tick";

describe("creatorEditBufferStore", () => {
  beforeEach(() => {
    editBuffer().reset();
  });

  it("records patch operations with element targets and affected ranges", () => {
    record(SPAN_START, 0, 2000, 7);
    const state = editBuffer();
    expect(state.projectId).toBe(PROJECT_ID);
    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      target: {
        kind: "element",
        id: "edit-opening",
        label: "开场 · 晨光中的小猫",
      },
      field: "span/start_tick",
      before: 0,
      after: 2000,
    });
    // Union of old span [0,8000] and new span [2000,10000].
    expect(state.affectedRangesByTimeline["timeline:main"]).toEqual([
      { startTick: 0, endTick: 10000 },
    ]);
    expect(state.lastRecordGeneration).toBe(7);
  });

  it("merges overlapping affected ranges", () => {
    const path =
      "/timelines/items/timeline:main/elements_by_id/overlay-title/span/duration_tick";
    record(path, 5000, 6000, 8);
    record(path, 5000, 9000, 8);
    expect(editBuffer().affectedRangesByTimeline["timeline:main"]).toEqual([
      { startTick: 1000, endTick: 10000 },
    ]);
  });

  it("truncates long values in entries", () => {
    record("/strategy/creative_brief", "旧的总纲", "长".repeat(500), 9);
    const entry = editBuffer().entries[0];
    expect(String(entry.after)).toContain("…(500 chars)");
    expect(entry.target.kind).toBe("strategy");
  });

  it("consume + markFlushed clears delivered entries, scoped to the project", () => {
    record("/settings/aspect_ratio", "16:9", "9:16", 10);
    expect(editBuffer().consumeContext("other-project")).toBeNull();
    const context = editBuffer().consumeContext(PROJECT_ID);
    expect(context?.count).toBe(1);
    expect(context?.edits[0].field).toBe("settings/aspect_ratio");
    editBuffer().markFlushed(PROJECT_ID, context?.lastEntryAt);
    expect(editBuffer().entries).toHaveLength(0);
    expect(editBuffer().consumeContext(PROJECT_ID)).toBeNull();
  });

  it("clears affected ranges only once the render catches up", () => {
    record(SPAN_START, 0, 500, 12);
    editBuffer().clearAffectedRanges("timeline:main", 11);
    expect(
      editBuffer().affectedRangesByTimeline["timeline:main"],
    ).toBeDefined();
    editBuffer().clearAffectedRanges("timeline:main", 12);
    expect(
      editBuffer().affectedRangesByTimeline["timeline:main"],
    ).toBeUndefined();
  });
});
