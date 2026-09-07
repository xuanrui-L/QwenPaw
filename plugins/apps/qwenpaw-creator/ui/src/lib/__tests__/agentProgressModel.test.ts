import { describe, expect, it } from "vitest";
import { projectDocument } from "@/test/creatorFixtures";
import { projectJsonPointer } from "@/lib/projectJsonPointer";
import type {
  ProjectDocument,
  SpecialistRunView,
  TaskView,
  WorkGraphNode,
  WorkGraphView,
} from "@/contracts/creator";
import {
  buildAgentProgressModel,
  type AgentProgressInput,
} from "@/lib/agentProgressModel";

const projectId = "project-progress";
const first = "timeline:upper";
const second = "timeline:lower";
const snapshot = "snapshot:old";

it.each(["镜头与提示词待同步或待审阅确认", "镜头生成说明待更新或待审阅确认"])(
  "shows the synchronization gate %s as preparation without asking the user to act",
  (reason) => {
    const result = buildAgentProgressModel({
      projectId,
      project,
      tasks: [],
      runs: [],
      graph: graph([
        node({
          id: "storyboard:opening",
          kind: "storyboard",
          status: "gated",
          dispatchable: false,
          missing: [reason],
          locator: { page: "plan", elementId: "opening", timelineId: first },
        }),
      ]),
    });
    const item = result.groups.flatMap((group) => group.items)[0];
    expect(item.phase).toBe("preparing");
    expect(item.statusLabel).toBe("准备生成内容");
    expect(result.counts.attention).toBe(0);
  },
);
const project = {
  project_id: projectId,
  generation: 1,
  timelines: {
    order: [first, second, snapshot],
    items: {
      [first]: {
        timeline_id: first,
        title: "上篇 · 色彩启幕",
        elements_by_id: {
          opening: { element_id: "opening", label: "清晨开场" },
        },
      },
      [second]: {
        timeline_id: second,
        title: "下篇 · 光影流动",
        elements_by_id: { ending: { element_id: "ending", label: "落日收尾" } },
      },
      [snapshot]: {
        timeline_id: snapshot,
        title: "历史快照",
        elements_by_id: { frozen: { element_id: "frozen", label: "旧镜头" } },
      },
    },
  },
  visual: {
    entities: { items: { fox: { name: "小狐狸" } } },
    cast_lineups: { items: {} },
  },
  assets: { source_versions_by_id: {}, artifact_versions_by_id: {} },
  sources: {
    sources: { items: { footage: { display_name: "城市漫步.mp4" } } },
  },
} as unknown as ProjectDocument;

function node(overrides: Partial<WorkGraphNode> = {}): WorkGraphNode {
  return {
    id: "compose:timeline:upper",
    kind: "compose",
    label: "private-internal-label",
    status: "ready",
    deps: [],
    lane: "compose",
    taskId: null,
    timelineId: first,
    progress: null,
    error: null,
    missing: [],
    locator: { page: "plan" },
    dispatchable: true,
    ...overrides,
  };
}

function graph(nodes: WorkGraphNode[], id = projectId): WorkGraphView {
  return {
    projectId: id,
    generation: 2,
    counts: nodes.reduce<Record<string, number>>((result, item) => {
      result[item.status] = (result[item.status] ?? 0) + 1;
      return result;
    }, {}),
    mediaCalls: 0,
    mediaCallBudget: 5,
    nodes,
  };
}

function task(overrides: Partial<TaskView> = {}): TaskView {
  return {
    id: "task-compose",
    projectId,
    transactionId: null,
    specialistRunId: null,
    kind: "compose",
    // The timeline id itself contains a prefix. Strip the ref prefix once.
    targetRef: `timeline:${first}`,
    status: "SUCCEEDED",
    progress: null,
    resultRefs: [],
    ...overrides,
  };
}

function run(overrides: Partial<SpecialistRunView> = {}): SpecialistRunView {
  return {
    id: "run-source",
    role: "source_intelligence_agent",
    displayName: "/tmp/private/specialist.json",
    status: "SUCCEEDED",
    targetRefs: ["source:footage"],
    taskRefs: [],
    finalSummaryText: '{"private":"not a public summary"}',
    metadata: { thinking: "private thought", providerUsage: { token: 999 } },
    ...overrides,
  };
}

function model(overrides: Partial<AgentProgressInput> = {}) {
  return buildAgentProgressModel({
    projectId,
    project,
    graph: null,
    tasks: [],
    runs: [],
    ...overrides,
  });
}

function items(result: ReturnType<typeof model>) {
  return result.groups.flatMap((group) => group.items);
}

describe("Final cut progress", () => {
  function withFinalCut(stale = true) {
    const snapshot = structuredClone(project);
    const slotId = `timeline:${first}:render`;
    snapshot.assets.artifact_slots_by_id = {
      [slotId]: {
        slot_id: slotId,
        kind: "final_video",
        owner_ref: `timeline:${first}`,
        version_ids: ["final-v1"],
        selected_version_id: "final-v1",
        metadata: {},
      },
    };
    snapshot.assets.artifact_versions_by_id["final-v1"] = {
      ...projectDocument.assets.artifact_versions_by_id["final-v1"],
      slot_id: slotId,
      kind: "final_video",
      owner_ref: `timeline:${first}`,
      stale,
    };
    return snapshot;
  }

  it.each(["ready", "gated"] as const)(
    "keeps an existing outdated final cut out of not-started operations (%s)",
    (status) => {
      const graphNode = node({
        status,
        missing: status === "gated" ? ["video:opening"] : [],
      });
      const result = model({
        project: withFinalCut(),
        graph: graph([graphNode]),
      });
      expect(items(result)[0]).toMatchObject({
        phase: "attention",
        status: "stale",
        statusLabel: "成片需更新",
      });
      expect(result.counts).toMatchObject({ attention: 1, preparing: 0 });
      expect(graphNode.status).toBe(status);
    },
  );

  it("uses the current graph's frozen-input verdict without letting an older graph reject a new cut", () => {
    const snapshot = withFinalCut(false);
    const current = graph([node({ status: "gated" })]);
    expect(
      items(model({ project: snapshot, graph: current }))[0].statusLabel,
    ).toBe("成片需更新");
    snapshot.generation = current.generation + 1;
    expect(items(model({ project: snapshot, graph: current }))[0].phase).toBe(
      "preparing",
    );
  });

  it.each(["running", "done", "failed", "waiting_review"] as const)(
    "keeps a newer %s graph authoritative over an older stale project",
    (status) => {
      const result = model({
        project: withFinalCut(),
        graph: graph([node({ status })]),
      });
      expect(items(result)[0].status).toBe(status);
      expect(items(result)[0].statusLabel).not.toBe("成片需更新");
    },
  );

  it.each(["RUNNING", "QUEUED", "FAILED", "CANCELLED"] as const)(
    "shows the latest %s attempt while an old cut needs updating",
    (status) => {
      const result = model({
        project: withFinalCut(),
        graph: graph([node()]),
        tasks: [task({ status, createdAt: "2026-09-07T01:00:00Z" })],
      });
      expect(items(result)[0].status).toBe(status);
      expect(items(result)[0].statusLabel).not.toBe("成片需更新");
    },
  );

  it("does not infer an existing final cut from completed task history, unselected versions, or another episode", () => {
    const current = graph([node({ status: "gated" })]);
    expect(items(model({ graph: current, tasks: [task()] }))[0].phase).toBe(
      "preparing",
    );
    const snapshot = withFinalCut();
    snapshot.assets.artifact_slots_by_id[
      `timeline:${first}:render`
    ].selected_version_id = null;
    expect(items(model({ project: snapshot, graph: current }))[0].phase).toBe(
      "preparing",
    );
    expect(
      items(
        model({
          project: withFinalCut(),
          graph: graph([node({ timelineId: second })]),
        }),
      )[0].phase,
    ).toBe("preparing");
  });
});

describe("Agent progress durable operation projection", () => {
  it("groups the single project graph into actual parallel timelines and retains source analysis history", () => {
    const result = model({
      graph: graph([
        node({ id: `script:${first}`, kind: "script", status: "ready" }),
        node({ status: "running", progress: 0.4 }),
        node({ id: `script:${second}`, kind: "script", timelineId: second }),
        node({
          id: `compose:${second}`,
          timelineId: second,
          status: "running",
        }),
      ]),
      runs: [run()],
    });
    expect(result.groups.map((group) => group.label)).toEqual([
      "上篇 · 色彩启幕",
      "下篇 · 光影流动",
      "项目素材",
    ]);
    expect(result.groups.map((group) => group.kind)).toEqual([
      "timeline",
      "timeline",
      "source",
    ]);
    expect(result.counts).toEqual({
      total: 5,
      preparing: 2,
      running: 2,
      attention: 0,
      completed: 1,
    });
    expect(result.groups[0].counts).toEqual({
      total: 2,
      preparing: 1,
      running: 1,
      attention: 0,
      completed: 0,
    });
    expect(result.groups[2].items[0].run?.id).toBe("run-source");
    expect(items(result).every((item) => !("graphId" in item))).toBe(true);
  });

  it("keeps ready scripts after successful pure-edit compositions without claiming all operations complete", () => {
    const result = model({
      graph: graph([
        node({ id: `script:${first}`, kind: "script" }),
        node({ status: "done" }),
        node({ id: `script:${second}`, kind: "script", timelineId: second }),
        node({ id: `compose:${second}`, timelineId: second, status: "done" }),
      ]),
    });
    expect(result.counts).toEqual({
      total: 4,
      preparing: 2,
      running: 0,
      attention: 0,
      completed: 2,
    });
    expect(
      result.groups.every(
        (group) => group.counts.completed === 1 && group.counts.preparing === 1,
      ),
    ).toBe(true);
  });

  it("orders equally active timeline groups by the live narrative order rather than response array order", () => {
    const result = model({
      graph: graph([
        node({
          id: `compose:${second}`,
          timelineId: second,
          status: "running",
        }),
        node({ status: "running" }),
      ]),
    });
    expect(result.groups.map((group) => group.locator?.timelineId)).toEqual([
      first,
      second,
    ]);
  });

  it("preserves original graph nodes and cross-timeline dependencies while adding the actual navigation timeline", () => {
    const original = node({
      deps: ["video:opening", "video:ending"],
      status: "gated",
    });
    const before = JSON.stringify(original);
    const item = items(model({ graph: graph([original]) }))[0];
    expect(item.node).toBe(original);
    expect(item.node?.deps).toEqual(["video:opening", "video:ending"]);
    expect(item.locator).toEqual({ page: "plan", timelineId: first });
    expect(JSON.stringify(original)).toBe(before);
  });

  it("uses the node's explicit timeline before an ambiguous or inconsistent element association", () => {
    const result = model({
      graph: graph([
        node({
          id: "video:ending",
          kind: "video",
          timelineId: first,
          locator: { page: "plan", elementId: "ending" },
        }),
      ]),
    });
    expect(result.groups).toHaveLength(1);
    expect(result.groups[0].locator).toEqual({
      page: "plan",
      timelineId: first,
    });
    expect(result.groups[0].kind).toBe("timeline");
  });

  it("resolves missing node timeline from a unique live element and carries it into the item locator", () => {
    const result = model({
      graph: graph([
        node({
          id: "video:ending",
          kind: "video",
          timelineId: null,
          locator: { page: "plan", elementId: "ending" },
        }),
      ]),
    });
    expect(result.groups[0].label).toBe("下篇 · 光影流动");
    expect(items(result)[0].locator).toEqual({
      page: "element",
      elementId: "ending",
      timelineId: second,
    });
  });

  it("leaves work shared by two timelines in a common group and counts the run once", () => {
    const result = model({
      runs: [
        run({
          role: "ai_editing_director",
          status: "RUNNING_MODEL",
          targetRefs: ["element:opening", "element:ending"],
        }),
      ],
    });
    expect(result.groups).toHaveLength(1);
    expect(result.groups[0]).toMatchObject({
      kind: "project",
      label: "其他创作工作",
      locator: null,
    });
    expect(result.counts.total).toBe(1);
    expect(result.counts.running).toBe(1);
  });

  it("places project visual assets in the material group instead of assigning the current episode", () => {
    const result = model({
      graph: graph([
        node({
          id: "visual:fox:main",
          kind: "visual",
          timelineId: null,
          locator: { page: "assets", assetId: "fox" },
        }),
      ]),
      runs: [run({ targetRefs: ["visual-entity:fox"] })],
    });
    expect(result.groups).toHaveLength(1);
    expect(result.groups[0]).toMatchObject({
      kind: "source",
      label: "项目素材",
    });
    expect(result.groups[0].counts.total).toBe(2);
  });

  it("excludes frozen snapshot operations without moving them into a live episode", () => {
    const result = model({
      graph: graph([
        node(),
        node({ id: `compose:${snapshot}`, timelineId: snapshot }),
      ]),
      tasks: [task({ id: "snapshot-task", targetRef: `timeline:${snapshot}` })],
      runs: [run({ id: "snapshot-run", targetRefs: [`timeline:${snapshot}`] })],
    });
    expect(items(result).map((item) => item.id)).toEqual([
      "graph:compose:timeline:upper",
    ]);
    expect(result.counts.total).toBe(1);
  });

  it("rejects graph, tasks and project labels belonging to another project", () => {
    const result = model({
      project: { ...project, project_id: "other-project" },
      graph: graph([node()], "other-project"),
      tasks: [task({ projectId: "other-project" })],
      // SpecialistRunView has no projectId: callers must scope runs before calling.
      runs: [],
    });
    expect(result).toEqual({
      groups: [],
      counts: {
        total: 0,
        preparing: 0,
        running: 0,
        attention: 0,
        completed: 0,
      },
    });
    const unlabelled = model({
      project: { ...project, project_id: "other-project" },
      graph: graph([node()]),
    });
    expect(unlabelled.groups[0].label).not.toBe("上篇 · 色彩启幕");
  });

  it("collapses graph-owned tasks even after taskId becomes null and preserves the current graph status", () => {
    const result = model({
      graph: graph([node({ status: "done", taskId: null })]),
      tasks: [
        task({
          id: "old-failure",
          status: "FAILED",
          createdAt: "2026-09-05T01:00:00Z",
        }),
        task({
          id: "new-success",
          status: "SUCCEEDED",
          createdAt: "2026-09-05T02:00:00Z",
        }),
      ],
    });
    expect(items(result).map((item) => item.source)).toEqual(["graph"]);
    expect(result.counts).toEqual({
      total: 1,
      preparing: 0,
      running: 0,
      attention: 0,
      completed: 1,
    });
    const retryable = model({
      graph: graph([node({ status: "ready" })]),
      tasks: [task({ status: "FAILED" })],
    });
    expect(retryable.counts.attention).toBe(0);
    expect(retryable.counts.preparing).toBe(1);
  });

  it("deduplicates exact element production stages while retaining unrelated operations on the same target", () => {
    const result = model({
      graph: graph([
        node({
          id: "video:opening",
          kind: "video",
          status: "done",
          locator: { page: "plan", elementId: "opening" },
        }),
      ]),
      tasks: [
        task({
          id: "video-task",
          kind: "r2v_generation",
          targetRef: "element:opening",
        }),
        task({
          id: "edit-task",
          kind: "ai_edit_execute",
          targetRef: "element:opening",
        }),
      ],
    });
    expect(items(result).map((item) => item.id)).toEqual([
      "graph:video:opening",
      "task:edit-task",
    ]);
  });

  it("preserves an uncovered task's exact element target as well as its real timeline for navigation", () => {
    const result = model({
      tasks: [
        task({
          id: "edit-ending",
          kind: "ai_edit_execute",
          targetRef: "element:ending",
          status: "RUNNING",
        }),
      ],
    });
    expect(items(result)[0].locator).toEqual({
      page: "plan",
      timelineId: second,
      elementId: "ending",
    });
    expect(result.groups[0].locator).toEqual({
      page: "plan",
      timelineId: second,
    });
  });

  it("recognizes the real backend script_draft kind even before the legacy TaskView union catches up", () => {
    const scriptTask = task({
      id: "script-task",
      kind: "script_draft" as TaskView["kind"],
      status: "RUNNING",
    });
    const uncovered = model({ tasks: [scriptTask] });
    expect(uncovered.counts.running).toBe(1);
    expect(items(uncovered)[0].label).toBe("剧本");
    const covered = model({
      graph: graph([node({ kind: "script", status: "running" })]),
      tasks: [scriptTask],
    });
    expect(covered.counts.total).toBe(1);
    expect(items(covered)[0].source).toBe("graph");
  });

  it("gives an exact node taskId priority over ambiguous semantic matches for visual variants", () => {
    const firstVariant = node({
      id: "visual:fox:a",
      kind: "visual",
      timelineId: null,
      locator: { page: "assets", assetId: "fox" },
      taskId: "image-task",
      status: "running",
    });
    const secondVariant = {
      ...firstVariant,
      id: "visual:fox:b",
      taskId: null,
      status: "ready" as const,
    };
    const imageTask = task({
      id: "image-task",
      kind: "image_generation",
      targetRef: "asset:fox",
      status: "RUNNING",
    });
    expect(
      items(
        model({
          graph: graph([firstVariant, secondVariant]),
          tasks: [imageTask],
        }),
      ),
    ).toHaveLength(2);
    // Without an exact association the helper must not guess which variant owns it.
    expect(
      items(
        model({
          graph: graph([{ ...firstVariant, taskId: null }, secondVariant]),
          tasks: [imageTask],
        }),
      ),
    ).toHaveLength(3);
  });

  it("retains succeeded source analysis and excludes supporting read/observe/review tasks from core counts", () => {
    const auxiliary = [
      "read_source_video",
      "observe_source_clip",
      "review_scene",
    ].map((kind) =>
      task({
        id: `aux-${kind}`,
        kind: kind as TaskView["kind"],
        targetRef: "source:footage",
        specialistRunId: null,
      }),
    );
    const result = model({ runs: [run()], tasks: auxiliary });
    expect(result.counts).toEqual({
      total: 1,
      preparing: 0,
      running: 0,
      attention: 0,
      completed: 1,
    });
    expect(items(result)[0].source).toBe("run");
    expect(items(model({ tasks: auxiliary }))).toEqual([]);
  });

  it("uses bare taskRefs or specialistRunId to avoid counting an operation and its supporting task twice", () => {
    for (const association of [
      { taskRefs: ["source-task"], owner: null },
      { taskRefs: [], owner: "run-source" },
    ]) {
      const result = model({
        runs: [run({ taskRefs: association.taskRefs })],
        tasks: [
          task({
            id: "source-task",
            kind: "source_intelligence",
            targetRef: "source:footage",
            specialistRunId: association.owner,
          }),
        ],
      });
      expect(result.counts.completed).toBe(1);
      expect(items(result)[0].source).toBe("run");
    }
    // Target equality alone does not establish ownership.
    const unrelated = model({
      runs: [run()],
      tasks: [
        task({ kind: "source_intelligence", targetRef: "source:footage" }),
      ],
    });
    expect(unrelated.counts.total).toBe(2);
  });

  it("folds graph-covered runtime wrappers but retains independent modelling and authorization phases", () => {
    const coreTask = task({ id: "owned", specialistRunId: "editor" });
    const editor = run({
      id: "editor",
      role: "ai_editing_director",
      targetRefs: [`timeline:${first}`],
      taskRefs: ["owned"],
    });
    const base = {
      graph: graph([node({ status: "done" })]),
      tasks: [coreTask],
    };
    expect(model({ ...base, runs: [editor] }).counts.total).toBe(1);
    expect(
      model({ ...base, runs: [{ ...editor, status: "RUNNING_MODEL" }] }).counts
        .running,
    ).toBe(1);
    const needsAuthorization = model({
      ...base,
      runs: [{ ...editor, status: "WAITING_AUTHORIZATION" }],
    });
    expect(needsAuthorization.counts.attention).toBe(1);
    expect(
      items(needsAuthorization).some(
        (item) => item.status === "WAITING_AUTHORIZATION",
      ),
    ).toBe(true);
  });

  it("treats supersedesRunId as replacement and relatedRunId only as a parent relationship", () => {
    const old = run({ id: "old", status: "FAILED" });
    const replacement = run({
      id: "new",
      supersedesRunId: "old",
      status: "SUCCEEDED",
    });
    expect(
      items(model({ runs: [old, replacement] })).map((item) => item.id),
    ).toEqual(["run:new"]);
    const child = run({
      id: "child",
      relatedRunId: "old",
      status: "SUCCEEDED",
    });
    expect(items(model({ runs: [old, child] })).map((item) => item.id)).toEqual(
      ["run:old", "run:child"],
    );
  });

  it("removes an earlier failed attempt only when the same kind and target has a provably later successful attempt", () => {
    const failed = task({
      id: "failed",
      kind: "ai_edit_execute",
      status: "FAILED",
      createdAt: "2026-09-05T01:00:00Z",
    });
    const success = task({
      id: "succeeded",
      kind: "ai_edit_execute",
      createdAt: "2026-09-05T02:00:00Z",
    });
    expect(
      items(model({ tasks: [success, failed] })).map((item) => item.id),
    ).toEqual(["task:succeeded"]);
    expect(
      items(model({ tasks: [failed, success] })).map((item) => item.id),
    ).toEqual(["task:succeeded"]);
    expect(
      model({ tasks: [failed, { ...success, kind: "compose" }] }).counts.total,
    ).toBe(2);
    expect(
      model({
        tasks: [failed, { ...success, targetRef: `timeline:${second}` }],
      }).counts.total,
    ).toBe(2);
    expect(
      model({ tasks: [failed, { ...success, createdAt: undefined }] }).counts
        .total,
    ).toBe(2);
    expect(
      model({ tasks: [failed, { ...success, createdAt: "invalid-date" }] })
        .counts.total,
    ).toBe(2);
  });

  it("does not erase completed history or unresolved failure solely because a later task was queued", () => {
    const old = task({
      id: "finished",
      kind: "asset_import",
      targetRef: "source:footage",
      createdAt: "2026-09-05T01:00:00Z",
    });
    const queued = {
      ...old,
      id: "queued",
      status: "QUEUED" as const,
      createdAt: "2026-09-05T02:00:00Z",
    };
    expect(model({ tasks: [old, queued] }).counts).toMatchObject({
      total: 2,
      preparing: 1,
      completed: 1,
    });
    expect(
      model({ tasks: [{ ...old, status: "FAILED" }, queued] }).counts.attention,
    ).toBe(1);
  });

  it("shows only finite runtime progress from an actually running item", () => {
    for (const invalid of [
      null,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      -0.1,
      1.1,
    ]) {
      expect(
        items(
          model({
            graph: graph([node({ status: "running", progress: invalid })]),
          }),
        )[0].progressPercent,
      ).toBeNull();
    }
    for (const status of [
      "done",
      "ready",
      "gated",
      "waiting_review",
      "failed",
      "stale",
    ] as const) {
      expect(
        items(model({ graph: graph([node({ status, progress: 0.9 })]) }))[0]
          .progressPercent,
      ).toBeNull();
    }
    expect(
      items(
        model({ graph: graph([node({ status: "running", progress: 0.42 })]) }),
      )[0].progressPercent,
    ).toBe(42);
    expect(
      items(model({ tasks: [task({ status: "RUNNING", progress: 0.25 })] }))[0]
        .progressPercent,
    ).toBe(25);
    expect(
      items(
        model({
          runs: [
            run({ status: "WAITING_RUNTIME", metadata: { progress: 0.5 } }),
          ],
        }),
      )[0].progressPercent,
    ).toBeNull();
    expect(
      model({
        graph: graph([
          node({ status: "done" }),
          node({ id: "other", status: "ready" }),
        ]),
      }).counts,
    ).not.toHaveProperty("progressPercent");
  });

  it("keeps cancellation, review and stale distinct while sorting running and attention before history", () => {
    const result = model({
      tasks: [
        task({ id: "done", targetRef: "source:a", status: "SUCCEEDED" }),
        task({ id: "queued", targetRef: "source:b", status: "QUEUED" }),
        task({ id: "cancelled", targetRef: "source:c", status: "CANCELLED" }),
        task({ id: "running", targetRef: "source:d", status: "RUNNING" }),
      ],
      graph: graph([
        node({ id: "review", status: "waiting_review" }),
        node({ id: "stale", status: "stale" }),
      ]),
    });
    expect(result.counts).toEqual({
      total: 6,
      preparing: 1,
      running: 1,
      attention: 3,
      completed: 1,
    });
    const sourceItems = result.groups.find(
      (group) => group.kind === "source",
    )!.items;
    expect(sourceItems.map((item) => item.status)).toEqual([
      "RUNNING",
      "CANCELLED",
      "QUEUED",
      "SUCCEEDED",
    ]);
    expect(new Set(items(result).map((item) => item.statusLabel)).size).toBe(6);
  });

  it("never projects diagnostic ids, paths, raw errors or private specialist summaries as public labels", () => {
    const result = model({
      graph: graph([
        node({ label: "/tmp/private/node.json", error: '{"error":"secret"}' }),
      ]),
      runs: [run()],
      tasks: [
        task({
          id: "task-private",
          targetRef: "unknown:internal",
          error: { path: "/tmp/private" },
        }),
      ],
    });
    const publicFields = result.groups.map((group) => ({
      label: group.label,
      items: group.items.map((item) => ({
        label: item.label,
        statusLabel: item.statusLabel,
      })),
    }));
    expect(JSON.stringify(publicFields)).not.toMatch(
      /private|internal|providerUsage|thinking|run-source|task-private|secret/u,
    );
    expect(
      items(result).find((item) => item.source === "run")?.run?.metadata
        .thinking,
    ).toBe("private thought");
  });
});

describe("Agent progress independently refreshed snapshots", () => {
  it("settles parallel executions when their exact task ids succeed before the graph poll catches up", () => {
    const originals = [
      node({ status: "running", taskId: "upper-finished", progress: 1 }),
      node({
        id: `compose:${second}`,
        timelineId: second,
        status: "running",
        taskId: "lower-finished",
        progress: 1,
      }),
    ];
    const originalJson = JSON.stringify(originals);
    const finished = [
      task({ id: "upper-finished", progress: 1 }),
      task({
        id: "lower-finished",
        targetRef: `timeline:${second}`,
        progress: 1,
      }),
    ];
    const result = model({ graph: graph(originals), tasks: finished });

    expect(result.counts).toEqual({
      total: 2,
      preparing: 0,
      running: 0,
      attention: 0,
      completed: 2,
    });
    expect(result.groups.map((group) => group.locator?.timelineId)).toEqual([
      first,
      second,
    ]);
    for (const [index, item] of items(result).entries()) {
      expect(item).toMatchObject({
        status: "SUCCEEDED",
        phase: "completed",
        progressPercent: null,
        task: finished[index],
      });
      expect(item.statusLabel).not.toMatch(/进行中/u);
      // Keep original graph authority for navigation and dispatch guards.
      expect(item.node).toBe(originals[index]);
      expect(item.node?.status).toBe("running");
      expect(item.locator?.timelineId).toBe([first, second][index]);
    }
    expect(JSON.stringify(originals)).toBe(originalJson);
  });

  it.each(["waiting_review", "failed", "ready", "done"] as const)(
    "preserves the graph's %s state even when its exact task succeeded",
    (status) => {
      const original = node({ status, taskId: "finished", progress: 1 });
      const result = model({
        graph: graph([original]),
        tasks: [task({ id: "finished", progress: 1 })],
      });
      expect(items(result)[0].status).toBe(status);
      expect(items(result)[0].node).toBe(original);
      expect(items(result)[0].progressPercent).toBeNull();
    },
  );

  it.each([null, "new-attempt"])(
    "does not settle running graph taskId %s from a different successful task on the same target",
    (taskId) => {
      const result = model({
        graph: graph([node({ status: "running", taskId, progress: 1 })]),
        tasks: [task({ id: "old-success", progress: 1 })],
      });
      expect(items(result)[0]).toMatchObject({
        status: "running",
        phase: "running",
      });
      expect(result.counts).toMatchObject({ running: 1, completed: 0 });
    },
  );

  it("does not treat a failed exact task as irrevocably finished because it may retry with the same id", () => {
    const result = model({
      graph: graph([node({ status: "running", taskId: "retrying" })]),
      tasks: [task({ id: "retrying", status: "FAILED" })],
    });
    expect(items(result)[0].status).toBe("running");
    expect(result.counts).toMatchObject({ running: 1, attention: 0 });
  });

  it.each(["QUEUED", "RUNNING"] as const)(
    "merges a newer %s attempt into an old done stage without counting it twice",
    (status) => {
      const original = node({ status: "done", taskId: null });
      const originalJson = JSON.stringify(original);
      const active = task({
        id: "new-attempt",
        status,
        progress: 0.25,
        createdAt: "2026-09-05T02:00:00Z",
      });
      const result = model({
        graph: graph([original]),
        tasks: [
          task({ id: "previous-success", createdAt: "2026-09-05T01:00:00Z" }),
          active,
        ],
      });
      expect(result.counts).toEqual({
        total: 1,
        completed: 0,
        attention: 0,
        preparing: status === "QUEUED" ? 1 : 0,
        running: status === "RUNNING" ? 1 : 0,
      });
      expect(items(result)[0]).toMatchObject({
        source: "graph",
        status,
        task: active,
      });
      expect(items(result)[0].progressPercent).toBe(
        status === "RUNNING" ? 25 : null,
      );
      // Actions continue to inspect the original DONE node, which is not dispatchable.
      expect(items(result)[0].node).toBe(original);
      expect(items(result)[0].node?.status).toBe("done");
      expect(JSON.stringify(original)).toBe(originalJson);
    },
  );

  it("does not let an older running task override a later successful attempt and current artifact", () => {
    const result = model({
      graph: graph([node({ status: "done" })]),
      tasks: [
        task({
          id: "old-active",
          status: "RUNNING",
          createdAt: "2026-09-05T01:00:00Z",
        }),
        task({ id: "current-success", createdAt: "2026-09-05T02:00:00Z" }),
      ],
    });
    expect(result.counts).toMatchObject({ total: 1, completed: 1, running: 0 });
    expect(items(result)[0].status).toBe("done");
  });

  it("keeps the graph's exact taskId authoritative over a newer merely semantic task match", () => {
    const active = task({
      id: "exact-active",
      status: "RUNNING",
      progress: 0.3,
      createdAt: "2026-09-05T01:00:00Z",
    });
    const result = model({
      graph: graph([node({ status: "running", taskId: active.id })]),
      tasks: [
        active,
        task({ id: "semantic-success", createdAt: "2026-09-05T02:00:00Z" }),
      ],
    });
    expect(items(result)[0]).toMatchObject({
      status: "RUNNING",
      task: active,
      progressPercent: 30,
    });
    expect(result.counts).toMatchObject({ total: 1, running: 1, completed: 0 });
  });

  it("keeps a newer graph's real new timeline visible until project metadata catches up", () => {
    const third = "timeline:new-private-id";
    const freshGraph = graph([
      node({ id: `compose:${third}`, timelineId: third, status: "running" }),
    ]);
    const result = model({ graph: freshGraph });
    expect(result.counts).toMatchObject({ total: 1, running: 1 });
    expect(result.groups[0]).toMatchObject({
      kind: "timeline",
      label: "视频 / 剧集 3",
      locator: { page: "plan", timelineId: third },
    });
    expect(items(result)[0].locator?.timelineId).toBe(third);
    expect(result.groups[0].label).not.toContain("private-id");
    const caughtUp = model({
      graph: freshGraph,
      project: {
        ...project,
        generation: 2,
        timelines: {
          order: [...project.timelines.order, third],
          items: {
            ...project.timelines.items,
            [third]: {
              ...project.timelines.items[first],
              timeline_id: third,
              title: "第三篇 · 雨后",
            },
          },
        },
      },
    });
    expect(caughtUp.groups[0].id).toBe(result.groups[0].id);
    expect(caughtUp.groups[0].label).toBe("第三篇 · 雨后");
    expect(
      model({ graph: { ...freshGraph, generation: 1 } }).counts.total,
    ).toBe(0);
    const frozenGraph = graph([
      node({ id: `compose:${snapshot}`, timelineId: snapshot }),
    ]);
    expect(model({ graph: frozenGraph }).counts.total).toBe(0);
  });

  it.each(["FAILED", "QUARANTINED", "CANCELLED"] as const)(
    "retains the latest dated %s attempt under a retryable READY composition",
    (status) => {
      const original = node({ status: "ready" });
      const result = model({
        graph: graph([original]),
        tasks: [
          task({
            id: "latest-unsuccessful",
            status,
            createdAt: "2026-09-05T02:00:00Z",
          }),
        ],
      });
      expect(result.counts).toMatchObject({
        total: 1,
        attention: 1,
        completed: 0,
      });
      expect(items(result)[0]).toMatchObject({ status, phase: "attention" });
      expect(items(result)[0].statusLabel).toMatch(/上次尝试/u);
      expect(items(result)[0].node).toBe(original);
      expect(items(result)[0].node?.status).toBe("ready");
      expect(items(result)[0].progressPercent).toBeNull();
    },
  );

  it.each(["done", "waiting_review", "running"] as const)(
    "does not replace a current %s graph state with a failed attempt",
    (status) => {
      const result = model({
        graph: graph([node({ status })]),
        tasks: [
          task({
            status: "FAILED",
            createdAt: "2026-09-05T02:00:00Z",
          }),
        ],
      });
      expect(items(result)[0].status).toBe(status);
      expect(items(result)[0].statusLabel).not.toMatch(/上次尝试/u);
    },
  );

  it("does not revive an earlier failed attempt after a later same-stage success", () => {
    const result = model({
      graph: graph([node()]),
      tasks: [
        task({ id: "success", createdAt: "2026-09-05T02:00:00Z" }),
        task({
          id: "failed",
          status: "FAILED",
          createdAt: "2026-09-05T01:00:00Z",
        }),
      ],
    });
    expect(result.counts).toMatchObject({
      total: 1,
      preparing: 1,
      attention: 0,
    });
    expect(items(result)[0].status).toBe("ready");
  });

  it("does not guess the latest failure when timestamps are missing or equal", () => {
    const failed = task({
      id: "failed",
      status: "FAILED",
      createdAt: "2026-09-05T01:00:00Z",
    });
    for (const createdAt of [undefined, "invalid-date", failed.createdAt]) {
      const result = model({
        graph: graph([node()]),
        tasks: [failed, task({ id: "success", createdAt })],
      });
      expect(items(result)[0].status).toBe("ready");
      expect(result.counts.attention).toBe(0);
    }
  });
});

describe("operation navigation preserves generation semantics", () => {
  function operationModel(
    nodes: WorkGraphNode[],
    document = structuredClone(projectDocument),
    tasks: TaskView[] = [],
  ) {
    document.project_id = projectId;
    return model({ project: document, graph: graph(nodes), tasks });
  }

  it.each(["storyboard", "video"] as const)(
    "targets the exact %s prompt, escaping real ids and retaining the original node",
    (kind) => {
      const document = structuredClone(projectDocument);
      const timelineId = "timeline:episode/2~draft";
      const elementId = "shot:2/~close";
      const timeline = structuredClone(
        document.timelines.items["timeline:main"],
      );
      timeline.timeline_id = timelineId;
      timeline.elements_by_id[elementId] = {
        ...timeline.elements_by_id["r2v-window"],
        element_id: elementId,
      };
      document.timelines.items[timelineId] = timeline;
      document.timelines.order.push(timelineId);
      const original = node({
        id: `${kind}:${elementId}`,
        kind,
        timelineId,
        locator: { page: "plan", elementId },
      });
      const item = items(operationModel([original], document))[0];
      expect(item.locator).toEqual({
        page: "element",
        timelineId,
        elementId,
        field: projectJsonPointer(
          "timelines",
          "items",
          timelineId,
          "elements_by_id",
          elementId,
          "creation",
          `${kind === "storyboard" ? "storyboard" : "video"}_prompt`,
        ),
      });
      expect(item.node).toBe(original);
      expect(original.locator).toEqual({ page: "plan", elementId });
    },
  );

  it("uses an S2V script, not a nonexistent video prompt, and sends uncovered video tasks to their authored field", () => {
    const document = structuredClone(projectDocument);
    document.timelines.items["timeline:main"].elements_by_id[
      "r2v-window"
    ].creation = {
      type: "s2v",
      intent: "讲话",
      character_ref: null,
      portrait_version_id: null,
      script: "你好",
      audio_version_id: null,
      recipe: null,
    };
    const original = node({
      kind: "video",
      timelineId: "timeline:main",
      locator: { page: "plan", elementId: "r2v-window" },
    });
    expect(items(operationModel([original], document))[0].locator?.field).toBe(
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/script",
    );
    expect(
      items(
        operationModel([], document, [
          task({ kind: "r2v_generation", targetRef: "element:r2v-window" }),
        ]),
      )[0].locator,
    ).toMatchObject({
      page: "element",
      field:
        "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/script",
    });
  });

  it("opens the actual colon-containing visual variant, never guessing a default on an unmatched node", () => {
    const document = structuredClone(projectDocument);
    const entityId = "char:woman";
    const variantId = "variant:formal";
    const entity = structuredClone(document.visual.entities.items.cat);
    const variant = structuredClone(
      entity.variants.items[entity.variants.order[0]],
    );
    entity.variants.order.push(variantId);
    entity.variants.items[variantId] = {
      ...variant,
      variant_id: variantId,
      prompt: "正装设计",
    };
    document.visual.entities.items[entityId] = entity;
    const original = node({
      id: `visual:${entityId}:${variantId}`,
      kind: "visual",
      timelineId: null,
      locator: { page: "assets", assetId: entityId },
    });
    expect(items(operationModel([original], document))[0].locator).toEqual({
      page: "assets",
      assetId: entityId,
      variantId,
      field: `/visual/entities/items/${entityId}/variants/items/${variantId}/prompt`,
    });
    expect(
      items(
        operationModel([{ ...original, id: "visual:unknown" }], document),
      )[0].locator,
    ).toEqual({ page: "assets", assetId: entityId });
  });

  it("retains script, composition and actual lineup destinations", () => {
    const document = structuredClone(projectDocument);
    const lineupId = "lineup:cast:evening";
    document.visual.cast_lineups ??= { order: [], items: {} };
    document.visual.cast_lineups.items[lineupId] = {
      lineup_id: lineupId,
    } as (typeof document.visual.cast_lineups.items)[string];
    const result = items(
      operationModel(
        [
          node({
            id: "script:main",
            kind: "script",
            timelineId: "timeline:main",
            locator: { page: "blueprint" },
          }),
          node({ timelineId: "timeline:main" }),
          node({
            id: `lineup:${lineupId}`,
            kind: "lineup",
            timelineId: null,
            locator: { page: "assets" },
          }),
        ],
        document,
      ),
    );
    expect(
      result.find((item) => item.node?.kind === "script")?.locator,
    ).toEqual({ page: "blueprint", timelineId: "timeline:main" });
    expect(
      result.find((item) => item.node?.kind === "compose")?.locator,
    ).toEqual({ page: "plan", timelineId: "timeline:main" });
    expect(
      result.find((item) => item.node?.kind === "lineup")?.locator,
    ).toEqual({ page: "assets", assetId: lineupId });
  });

  it("does not invent a prompt when a newer graph precedes the project or the explicit timeline has a different element", () => {
    const original = node({
      id: "video:new",
      kind: "video",
      timelineId: "timeline:new",
      locator: { page: "plan", elementId: "new" },
    });
    expect(items(model({ graph: graph([original]) }))[0].locator).toEqual({
      page: "element",
      timelineId: "timeline:new",
      elementId: "new",
    });
  });
});

it.each([
  ["waiting", "preparing", "准备生成内容"],
  ["running", "running", "正在准备生成内容"],
  ["failed", "attention", "生成准备遇到问题"],
] as const)(
  "uses actual backend preparation state %s",
  (preparationState, phase, label) => {
    const result = buildAgentProgressModel({
      projectId,
      project,
      tasks: [],
      runs: [],
      graph: graph([
        node({
          id: "storyboard:opening",
          kind: "storyboard",
          status: "gated",
          promptSyncRequired: true,
          preparationState,
          missing: [],
          locator: { page: "plan", elementId: "opening", timelineId: first },
        }),
      ]),
    });
    const item = result.groups.flatMap((group) => group.items)[0];
    expect(item.phase).toBe(phase);
    expect(item.statusLabel).toBe(label);
    expect(result.counts.attention).toBe(preparationState === "failed" ? 1 : 0);
  },
);
