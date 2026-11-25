import { importTimelineScript, setSubtitleStyle } from "@/api/creator";
import { installMockFetch } from "@/test/mockFetch";
import type { ProjectDocument } from "@/contracts/creator";
import {
  createSnapshotOperations,
  deleteTimelineOperations,
  duplicateTimelineOperations,
  snapshotMatchesTimeline,
} from "@/api/creator/timelines";
import { projectDocument } from "@/test/creatorFixtures";
import { resolveElementPlayback } from "@/selectors/elementPlaybackSelectors";
import { describe, expect, it, vi } from "vitest";
import {
  getProjectSnapshot,
  decideFileProjectReview,
  getActiveFileProjectReview,
  parseFileProjectReview,
} from "@/api/creator";

function response(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 304 ? "Not Modified" : "OK",
    headers: new Headers(headers),
    json: vi.fn(async () => body),
  } as unknown as Response;
}

function project(generation: number) {
  return {
    schema_version: 1,
    project_id: "p1",
    generation,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z",
    name: "Project",
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

describe("Project snapshot API", () => {
  it("returns a validated snapshot envelope, then honours If-None-Match with a 304", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        response(
          200,
          {
            projectId: "p1",
            generation: 8,
            etag: "sha256:body",
            syncStatus: "healthy",
            project: project(8),
          },
          { ETag: '"sha256:header"' },
        ),
      )
      .mockResolvedValueOnce(
        response(304, undefined, {
          ETag: '"sha256:header"',
          "X-Project-Generation": "8",
          "X-Project-Sync-Status": "healthy",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    // The HTTP entity tag wins over the body etag.
    await expect(getProjectSnapshot("p1")).resolves.toMatchObject({
      kind: "updated",
      projectId: "p1",
      generation: 8,
      etag: '"sha256:header"',
      syncStatus: "healthy",
    });
    await expect(getProjectSnapshot("p1", '"sha256:header"')).resolves.toEqual({
      kind: "not_modified",
      etag: '"sha256:header"',
      generation: 8,
      syncStatus: "healthy",
    });
    const request = fetchMock.mock.calls[1];
    expect(request[0]).toBe("/api/qwenpaw-creator/projects/p1/project");
    expect(new Headers(request[1]?.headers).get("If-None-Match")).toBe(
      '"sha256:header"',
    );
  });

  it("exposes PROJECT_INVALID as sync state instead of raw Project data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(409, {
          code: "PROJECT_INVALID",
          syncStatus: "invalid",
          lastGoodGeneration: 8,
          message: "schema validation failed",
        }),
      ),
    );

    await expect(getProjectSnapshot("p1", '"sha256:g8"')).resolves.toEqual({
      kind: "invalid",
      code: "PROJECT_INVALID",
      syncStatus: "invalid",
      lastGoodGeneration: 8,
      message: "schema validation failed",
    });
  });
});

describe("Project review requests", () => {
  function review(overrides: Record<string, unknown> = {}) {
    return {
      review_id: "review-1",
      round_id: "round-1",
      request_id: "request-1",
      request_message_seq: 4,
      interrupted_run_id: "run-1",
      baseline_generation: 2,
      baseline_etag: "base-2",
      candidate_generation: 3,
      candidate_etag: "candidate-3",
      decision_token: "token-1",
      status: "PENDING",
      operations: [
        {
          kind: "update",
          json_pointer: "/story/title",
          file_id: null,
          target_ref: null,
          before_hash: "before-hash",
          after_hash: "after-hash",
          before: "Old title",
          after: "New title",
          operation_id: "operation-1",
          ui_locator: { route: "/project/p1/plan" },
          decision: "PENDING",
        },
      ],
      created_at: "2026-07-15T00:00:00Z",
      updated_at: "2026-07-15T00:00:01Z",
      ...overrides,
    };
  }

  function response(
    status: number,
    body?: unknown,
    headers: Record<string, string> = {},
  ): Response {
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 304 ? "Not Modified" : "OK",
      headers: new Headers(headers),
      json: vi.fn(async () => body),
    } as unknown as Response;
  }

  describe("file-native Project Review API", () => {
    it("reads the strict snake_case contract and treats 204/304 as protocol results", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(response(200, [review()], { ETag: '"token-1"' }))
        .mockResolvedValueOnce(response(204))
        .mockResolvedValueOnce(response(304, undefined, { ETag: '"token-1"' }));
      vi.stubGlobal("fetch", fetchMock);

      await expect(getActiveFileProjectReview("p1")).resolves.toMatchObject({
        kind: "updated",
        etag: '"token-1"',
        reviews: [
          {
            review_id: "review-1",
            candidate_generation: 3,
            decision_token: "token-1",
          },
        ],
      });
      await expect(getActiveFileProjectReview("p1")).resolves.toEqual({
        kind: "empty",
      });
      await expect(
        getActiveFileProjectReview("p1", '"token-1"'),
      ).resolves.toEqual({
        kind: "not_modified",
        etag: '"token-1"',
      });
      expect(
        new Headers(fetchMock.mock.calls[2][1].headers).get("If-None-Match"),
      ).toBe('"token-1"');
    });

    it("posts operation decisions with camel request metadata and an Idempotency-Key", async () => {
      const resolved = review({
        decision_token: "token-2",
        status: "RESOLVED",
        operations: [
          {
            ...review().operations[0],
            decision: "ACCEPTED",
          },
        ],
      });
      const fetchMock = vi.fn(
        async (_input: RequestInfo | URL, _init: RequestInit = {}) =>
          response(200, resolved),
      );
      vi.stubGlobal("fetch", fetchMock);

      await expect(
        decideFileProjectReview(
          "p1",
          "review-1",
          {
            decisionId: "decision-1",
            decisionToken: "token-1",
            decisions: [{ operation_id: "operation-1", decision: "ACCEPT" }],
          },
          "idempotency-1",
        ),
      ).resolves.toMatchObject({ status: "RESOLVED" });

      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(
        "/api/qwenpaw-creator/projects/p1/runtime/reviews/review-1/decisions",
      );
      expect(init.method).toBe("POST");
      expect(new Headers(init.headers).get("Idempotency-Key")).toBe(
        "idempotency-1",
      );
      expect(JSON.parse(String(init.body))).toEqual({
        decisionId: "decision-1",
        decisionToken: "token-1",
        decisions: [{ operation_id: "operation-1", decision: "ACCEPT" }],
      });
    });

    it("rejects malformed operations instead of exposing an untyped payload", () => {
      const malformed = review({
        operations: [
          { ...review().operations[0], json_pointer: "story/title" },
        ],
      });
      expect(() => parseFileProjectReview(malformed)).toThrow("JSON Pointer");
    });
  });
});

const IMPORT_RESULT = {
  resultKind: "script_import",
  projectId: "p1",
  timelineId: "timeline:main",
  slotId: "script:timeline:main",
  artifactVersionId: "script-1",
  fileId: "file-script-1",
  checksum: "a".repeat(64),
  blockCount: 3,
  characterCount: 42,
  generation: 4,
  etag: "etag-4",
  replayed: false,
  reviewId: null,
  changedPointers: ["/assets/artifact_slots_by_id/script:timeline:main"],
  summary: "剧本正文（3 块 / 42 字）已直接登记为该集剧本版本。",
};

describe("importTimelineScript (剧本直登)", () => {
  it("uploads a script file as multipart against the timeline route", async () => {
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/timelines/timeline%3Amain/script-import",
        method: "POST",
        response: { ok: true, status: 201, json: IMPORT_RESULT },
      },
    ]);
    const file = new File(["## 场 1\n\n开场。"], "我的剧本.md", {
      type: "text/markdown",
    });

    const outcome = await importTimelineScript("p1", "timeline:main", { file });

    expect(outcome.artifactVersionId).toBe("script-1");
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("POST");
    // FormData wraps the uploaded File, so assert its identity by name.
    const uploaded = (calls[0].body as Record<string, File>).file;
    expect(uploaded.name).toBe("我的剧本.md");
    expect(uploaded.type).toBe("text/markdown");
  });

  it("sends pasted text as a JSON body without the script bytes in a query", async () => {
    const { calls } = installMockFetch([
      {
        match: "/script-import",
        method: "POST",
        response: {
          ok: true,
          status: 201,
          json: { ...IMPORT_RESULT, replayed: true },
        },
      },
    ]);

    const outcome = await importTimelineScript("p1", "timeline:main", {
      content: "## 场 1\n\n开场。",
      name: "粘贴剧本",
    });

    expect(outcome.replayed).toBe(true);
    expect(calls[0].body).toEqual({
      content: "## 场 1\n\n开场。",
      name: "粘贴剧本",
    });
    expect(calls[0].headers["content-type"]).toBe("application/json");
  });

  it("sends a per-request Idempotency-Key so re-picking old content is a new edit", async () => {
    const { calls } = installMockFetch([
      {
        match: "/script-import",
        method: "POST",
        response: { ok: true, status: 201, json: IMPORT_RESULT },
      },
    ]);

    // Two separate imports (the A -> B -> A flow) each carry a fresh key, so
    // the backend mints a distinct edit transaction and never collides with a
    // previously committed one for the same content.
    await importTimelineScript("p1", "timeline:main", { content: "A" });
    await importTimelineScript("p1", "timeline:main", { content: "B" });
    const [first, second] = calls.map((c) => c.headers["idempotency-key"]);
    expect(first).toMatch(/^script-import-/);
    expect(second).toMatch(/^script-import-/);
    expect(first).not.toBe(second);

    // An explicit key is passed through verbatim, so a network retry of the
    // same request reuses it and the backend dedupes the retry.
    await importTimelineScript(
      "p1",
      "timeline:main",
      { content: "A" },
      "script-import-fixed",
    );
    expect(calls[2].headers["idempotency-key"]).toBe("script-import-fixed");
  });
});

const STYLE_RESULT = {
  ok: true,
  generation: 7,
  subtitleTemplate: "variety_pop",
  subtitleStyle: null,
  styleLine: "综艺花字：特粗无衬线大字",
  syncedPrompts: 12,
  redrawnDocuments: 12,
  skippedNonTemplate: 0,
};

describe("setSubtitleStyle", () => {
  it("patches the subtitle style route and returns what the sync reached", async () => {
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/subtitles/style",
        method: "PATCH",
        response: { ok: true, status: 200, json: STYLE_RESULT },
      },
    ]);

    const outcome = await setSubtitleStyle("p1", {
      subtitleTemplate: "variety_pop",
    });

    expect(outcome.redrawnDocuments).toBe(12);
    // The refusal counter has to survive the round trip: without it the UI can
    // only ever claim a full success.
    expect(outcome.skippedNonTemplate).toBe(0);
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("PATCH");
    expect(calls[0].body).toEqual({ subtitleTemplate: "variety_pop" });
  });

  it("sends only the keys the caller set, so an untouched control cannot reset the project", async () => {
    const { calls } = installMockFetch([
      {
        match: "/subtitles/style",
        method: "PATCH",
        response: { ok: true, status: 200, json: STYLE_RESULT },
      },
    ]);

    await setSubtitleStyle("p1", {
      animation: { entrance: "slide", loop: false },
      elementIds: ["caption-1"],
    });

    expect(calls[0].body).toEqual({
      animation: { entrance: "slide", loop: false },
      elementIds: ["caption-1"],
    });
  });

  it("reports captions whose own design beat the pack", async () => {
    installMockFetch([
      {
        match: "/subtitles/style",
        method: "PATCH",
        response: {
          ok: true,
          status: 200,
          json: {
            ...STYLE_RESULT,
            syncedPrompts: 0,
            redrawnDocuments: 0,
            skippedNonTemplate: 2,
          },
        },
      },
    ]);

    const outcome = await setSubtitleStyle("p1", {
      animation: { entrance: "pop" },
      elementIds: ["cap-1", "cap-2"],
    });

    expect(outcome).toMatchObject({
      syncedPrompts: 0,
      redrawnDocuments: 0,
      skippedNonTemplate: 2,
    });
  });

  it("keeps an explicit null so the UI can go back to smart recommend", async () => {
    const { calls } = installMockFetch([
      {
        match: "/subtitles/style",
        method: "PATCH",
        response: { ok: true, status: 200, json: STYLE_RESULT },
      },
    ]);

    await setSubtitleStyle("p1", {
      subtitleTemplate: null,
      subtitleStyle: null,
    });

    expect(calls[0].body).toEqual({
      subtitleTemplate: null,
      subtitleStyle: null,
    });
  });
});

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

describe("duplicateTimelineOperations", () => {
  it("copies editable elements, references and output history while reusing media files", () => {
    const project = cloneProject();
    const source = project.timelines.items["timeline:main"];
    // Legal cross-namespace collision: an element and an artifact version
    // may use the same text ID. Each reference must follow its own namespace.
    const collisionId =
      project.assets.artifact_slots_by_id["element:r2v-window:video"]
        .selected_version_id!;
    source.elements_by_id[collisionId] = {
      ...source.elements_by_id["edit-opening"],
      element_id: collisionId,
    };
    delete source.elements_by_id["edit-opening"];
    for (const element of Object.values(source.elements_by_id)) {
      if (
        element.creation.type === "transition" &&
        element.creation.from_element_id === "edit-opening"
      )
        element.creation.from_element_id = collisionId;
    }
    source.edit_plan = {
      concept: "保留源剪辑",
      dials: { energy: "mid", density: "mid", decoration: "low" },
      signature_device: "",
      pacing: "",
      mechanical_exemption: false,
      design_floor: { opening: "", transitions: "", body: "", ending: "" },
      scene_ledger: [
        {
          scene_id: "opening",
          label: "开场",
          element_ids: Object.keys(source.elements_by_id),
          status: "locked",
          review_round: 1,
          locked_fingerprint: "old-lock",
        },
      ],
    };
    const original = structuredClone(project);
    const { timelineId, operations } = duplicateTimelineOperations(
      project,
      source.timeline_id,
      "第二版",
    );
    expect(project).toEqual(original);
    // Apply the actual UI patch, including RFC 6901 escaped slot/version IDs.
    const edited = structuredClone(project);
    for (const op of operations) {
      const tokens = op.path
        .slice(1)
        .split("/")
        .map((token) => token.replace(/~1/g, "/").replace(/~0/g, "~"));
      let parent: any = edited;
      for (const token of tokens.slice(0, -1)) parent = parent[token];
      parent[tokens.at(-1)!] = structuredClone(op.value);
    }
    const copy = edited.timelines.items[timelineId];
    const copiedElements = Object.values(copy.elements_by_id);
    const originalElements = Object.values(source.elements_by_id);
    expect(copiedElements).toHaveLength(6);
    expect(copy.name).toBe("第二版");
    expect(copy.title).toBe("第二版");
    const allIds = Object.values(edited.timelines.items).flatMap((timeline) =>
      Object.keys(timeline.elements_by_id),
    );
    expect(new Set(allIds).size).toBe(allIds.length);
    const byLabel = (label: string) =>
      copiedElements.find((element) => element.label === label)!;
    for (const element of originalElements) {
      const copied = byLabel(element.label);
      expect(copied.element_id).not.toBe(element.element_id);
      expect(copied.span).toEqual(element.span);
      const playback = resolveElementPlayback(edited, copy, copied);
      const originalPlayback = resolveElementPlayback(project, source, element);
      expect(playback.status).toBe(originalPlayback.status);
      expect(playback.media?.mediaKind).toBe(originalPlayback.media?.mediaKind);
      if (
        element.creation.type === "transition" &&
        copied.creation.type === "transition"
      ) {
        expect(copied.creation.from_element_id).toBe(
          byLabel(source.elements_by_id[element.creation.from_element_id].label)
            .element_id,
        );
        expect(copied.creation.to_element_id).toBe(
          byLabel(source.elements_by_id[element.creation.to_element_id].label)
            .element_id,
        );
      }
      for (const [name, output] of Object.entries(element.outputs)) {
        const oldSlot = project.assets.artifact_slots_by_id[output.slot_id];
        const newSlot =
          edited.assets.artifact_slots_by_id[copied.outputs[name].slot_id];
        expect(newSlot.owner_ref).toBe(`element:${copied.element_id}`);
        expect(newSlot.version_ids).toHaveLength(oldSlot.version_ids.length);
        for (const [index, oldVersionId] of oldSlot.version_ids.entries()) {
          const version =
            edited.assets.artifact_versions_by_id[newSlot.version_ids[index]];
          expect(version.version_id).not.toBe(oldVersionId);
          expect(version.file_id).toBe(
            project.assets.artifact_versions_by_id[oldVersionId].file_id,
          );
          expect(version.slot_id).toBe(newSlot.slot_id);
        }
      }
    }
    expect(copy.edit_plan?.scene_ledger[0]).toMatchObject({
      element_ids: copiedElements.map((element) => element.element_id),
      status: "draft",
      locked_fingerprint: null,
      review_round: 0,
    });
    expect(edited.assets.files_by_id).toEqual(original.assets.files_by_id);
    copiedElements[0].span.duration_tick += 500;
    const copiedVideo = copiedElements.find(
      (element) => element.creation.type === "r2v",
    )!;
    edited.assets.artifact_slots_by_id[
      copiedVideo.outputs.video.slot_id
    ].selected_version_id = null;
    expect(edited.timelines.items[source.timeline_id]).toEqual(
      original.timelines.items[source.timeline_id],
    );
    expect(
      edited.assets.artifact_slots_by_id["element:r2v-window:video"],
    ).toEqual(original.assets.artifact_slots_by_id["element:r2v-window:video"]);
  });
});

/** Append a frozen snapshot cloned from timeline:main under the given id. */
function withSnapshot(project: ProjectDocument, suffix: number) {
  const sid = `snapshot:timeline:main:${suffix}`;
  const raw = structuredClone(project.timelines.items["timeline:main"]);
  raw.timeline_id = sid;
  raw.name = `快照 · 备份 · 2026-09-04 10:0${suffix}`;
  const remapped: typeof raw.elements_by_id = {};
  for (const [oldId, element] of Object.entries(raw.elements_by_id)) {
    const newId = `${sid}:${oldId}`;
    remapped[newId] = { ...element, element_id: newId };
  }
  raw.elements_by_id = remapped;
  project.timelines.items[sid] = raw;
  project.timelines.order.push(sid);
  return project;
}

describe("snapshot id allocation", () => {
  it("skips past surviving suffixes after a deletion instead of colliding", () => {
    // Only :2 survives (:1 was deleted); count+1 would mint :2 again.
    const project = withSnapshot(cloneProject(), 2);
    const [addItem] = createSnapshotOperations(
      project,
      "timeline:main",
      "手动快照 · 2026-09-04 10:10",
    );
    expect(addItem.path).toBe("/timelines/items/snapshot:timeline:main:3");
  });
});

describe("deleteTimelineOperations", () => {
  it("cascades the base timeline's snapshots and removes order indices descending", () => {
    const project = withSnapshot(withSnapshot(cloneProject(), 1), 2);
    const operations = deleteTimelineOperations(project, "timeline:main");

    const itemRemovals = operations
      .filter((op) => op.path.startsWith("/timelines/items/"))
      .map((op) => op.path.split("/timelines/items/")[1]);
    expect(itemRemovals).toEqual(
      expect.arrayContaining([
        "timeline:main",
        "snapshot:timeline:main:1",
        "snapshot:timeline:main:2",
      ]),
    );
    // Order removals must run high→low so earlier removals don't shift the
    // later indices out from under their `before` checks.
    const orderIndices = operations
      .filter((op) => op.path.startsWith("/timelines/order/"))
      .map((op) => Number(op.path.split("/timelines/order/")[1]));
    expect(orderIndices).toEqual([...orderIndices].sort((a, b) => b - a));
    expect(orderIndices).toHaveLength(3);
    // Another timeline's snapshots are untouched.
    expect(
      deleteTimelineOperations(project, "timeline:ep2").map((op) => op.path),
    ).toEqual([
      "/timelines/items/timeline:ep2",
      `/timelines/order/${project.timelines.order.indexOf("timeline:ep2")}`,
    ]);
  });
});

describe("snapshotMatchesTimeline", () => {
  it("is true only while the live content equals the snapshot", () => {
    const project = withSnapshot(cloneProject(), 1);
    const sid = "snapshot:timeline:main:1";
    expect(snapshotMatchesTimeline(project, sid)).toBe(true);

    const live = project.timelines.items["timeline:main"];
    const firstId = Object.keys(live.elements_by_id)[0];
    live.elements_by_id[firstId].label = "改动后";
    expect(snapshotMatchesTimeline(project, sid)).toBe(false);
  });
});
