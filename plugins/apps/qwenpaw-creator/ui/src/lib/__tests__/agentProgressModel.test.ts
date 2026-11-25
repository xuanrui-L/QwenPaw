import type { CreatorMessage } from "@/contracts/creator";
import {
  publicAssistantText,
  shouldRenderConversationMessage,
  isUserAuthorityMessage,
} from "@/lib/creatorMessagePresentation";
import { humanizeCreatorRefs } from "@/lib/creatorPresentation";
import i18n from "@/i18n";
import type {
  AgentStatusBarView,
  CreatorSessionStatus,
  CreatorSessionView,
} from "@/contracts/creator";
import type { ToolCallPresentation } from "@/lib/creatorMessagePresentation";
import {
  deriveAgentLiveStatus,
  type AgentLiveStatusInput,
} from "@/lib/agentLiveStatus";
import { describe, expect, it } from "vitest";
import type {
  ProjectDocument,
  SpecialistRunView,
  TaskView,
  WorkGraphNode,
} from "@/contracts/creator";
import { buildAgentProgressModel } from "../agentProgressModel";
import { projectDocument } from "@/test/creatorFixtures";

const node = (overrides: Partial<WorkGraphNode> = {}): WorkGraphNode => ({
  id: "visual:cat:anchor",
  kind: "visual",
  label: "橘猫",
  status: "gated",
  deps: [],
  lane: "visual",
  taskId: null,
  progress: null,
  error: null,
  missing: [],
  locator: { page: "assets", assetId: "cat" },
  dispatchable: false,
  ...overrides,
});
const task = (overrides: Partial<TaskView> = {}): TaskView => ({
  id: "image-task",
  projectId: "p1",
  transactionId: null,
  specialistRunId: null,
  kind: "image_generation",
  targetRef: "asset:cat",
  status: "QUEUED",
  progress: 0,
  resultRefs: [],
  createdAt: "2026-09-14T00:00:00Z",
  ...overrides,
});
const run = (
  overrides: Partial<SpecialistRunView> = {},
): SpecialistRunView => ({
  id: "image-run",
  role: "visual_development_agent",
  displayName: "视觉设计",
  status: "QUEUED_CAPACITY",
  targetRefs: ["visual-entity:cat"],
  taskRefs: [],
  metadata: {},
  ...overrides,
});
function model(
  nodes: WorkGraphNode[],
  tasks: TaskView[] = [],
  runs: SpecialistRunView[] = [],
  project: ProjectDocument = structuredClone(projectDocument),
) {
  return buildAgentProgressModel({
    projectId: "p1",
    project,
    tasks,
    runs,
    graph: {
      projectId: "p1",
      generation: project.generation,
      nodes,
      counts: {},
      mediaCalls: 0,
    },
  });
}
const items = (result: ReturnType<typeof model>) =>
  result.groups.flatMap((group) => group.items);

describe("creation overview reconciles admitted work and durable task progress", () => {
  it("distinguishes visual states without exposing their generation prompts", () => {
    const project = structuredClone(projectDocument);
    const entity = project.visual.entities.items.cat;
    entity.variants.order.push("v:wounded");
    entity.variants.items["v:wounded"] = {
      ...entity.variants.items[entity.variants.order[0]],
      variant_id: "v:wounded",
      requirements: "以 v:base 为主参考生成血迹与长篇制作说明",
    };
    const result = model(
      [
        node({ id: `visual:cat:${entity.variants.order[0]}` }),
        node({ id: "visual:cat:v:wounded" }),
      ],
      [],
      [],
      project,
    );
    expect(items(result).map((item) => item.label)).toEqual([
      "圆润大橘猫 · 视觉资产 · 状态 1",
      "圆润大橘猫 · 视觉资产 · 状态 2",
    ]);
  });
  it.each(["QUEUED", "RUNNING"] as const)(
    "shows an admitted %s visual task as active, even before the graph refreshes",
    (status) => {
      const result = model(
        [node()],
        [task({ status, progress: status === "RUNNING" ? 0.4 : 0 })],
      );
      expect(result.counts).toMatchObject({
        total: 1,
        running: 1,
        preparing: 0,
      });
      expect(items(result)[0]).toMatchObject({
        status,
        phase: "running",
        progressPercent: status === "RUNNING" ? 40 : 0,
      });
      expect(items(result)[0].statusLabel).toContain(
        status === "QUEUED" ? "排队" : "生成中",
      );
    },
  );
  it("shows admitted professional work waiting for capacity as active without inventing progress", () => {
    const result = model([], [], [run()]);
    expect(items(result)[0]).toMatchObject({
      phase: "running",
      status: "QUEUED_CAPACITY",
      progressPercent: null,
    });
  });
  it("keeps ready or blocked graph work in not-started and explains missing prompts", () => {
    const result = model([
      node({ missing: ["visual_prompt 缺失"] }),
      node({ id: "lineup:cast", kind: "lineup", status: "ready" }),
    ]);
    expect(result.counts).toMatchObject({ running: 0, preparing: 2 });
    expect(items(result)[0].statusLabel).toBe("等待补全生成提示词");
  });
  it("explains an outdated script prerequisite with its public name", () => {
    const result = model([
      node({
        id: "script:timeline:main",
        kind: "script",
        status: "stale",
        timelineId: "timeline:main",
        locator: {},
      }),
      node({
        id: "storyboard:r2v-window",
        kind: "storyboard",
        timelineId: "timeline:main",
        deps: ["script:timeline:main"],
        missing: ["script:timeline:main"],
      }),
    ]);
    const storyboard = items(result).find(
      (item) => item.node?.kind === "storyboard",
    )!;
    expect(storyboard.statusLabel).toContain("剧本");
    expect(storyboard.statusLabel).toContain("更新");
    expect(storyboard.statusLabel).not.toContain("timeline:");
  });
  it("drops a stale script node from the overview once media production is approved", () => {
    // The user already confirmed the script (stage=media), so "stale" now only
    // means upstream creative inputs drifted; it does not block dispatch and the
    // blueprint panel keeps the sync chip. The overview must not alarm 需要关注.
    const project = structuredClone(projectDocument);
    project.settings.production_stage = "media";
    const result = model(
      [
        node({
          id: "script:timeline:main",
          kind: "script",
          status: "stale",
          timelineId: "timeline:main",
          locator: {},
        }),
        // A stale non-script node is a real signal and must stay in attention.
        node({
          id: "storyboard:shot-1",
          kind: "storyboard",
          status: "stale",
          timelineId: "timeline:main",
          locator: {},
        }),
      ],
      [],
      [],
      project,
    );
    expect(items(result).map((item) => item.node?.kind)).toEqual([
      "storyboard",
    ]);
    expect(items(result)[0].phase).toBe("attention");
    expect(result.counts.attention).toBe(1);
  });
  it("keeps a stale script node in attention while the script stage still owns approval", () => {
    // Before approval the confirm button lives in the script stage, so the stale
    // script is a real gate the overview must keep surfacing.
    const project = structuredClone(projectDocument);
    project.settings.production_stage = "script";
    const result = model(
      [
        node({
          id: "script:timeline:main",
          kind: "script",
          status: "stale",
          timelineId: "timeline:main",
          locator: {},
        }),
      ],
      [],
      [],
      project,
    );
    expect(items(result)).toHaveLength(1);
    expect(items(result)[0]).toMatchObject({
      phase: "attention",
      status: "stale",
    });
  });
  it("retains an active child behind an independently refreshed terminal run when its visual variant cannot be matched", () => {
    const result = model(
      [node(), node({ id: "visual:cat:wounded" })],
      [
        task({
          status: "RUNNING",
          progress: 0.5,
          specialistRunId: "image-run",
        }),
      ],
      [run({ status: "SUCCEEDED", taskRefs: ["image-task"] })],
    );
    expect(result.counts.running).toBe(1);
    const active = items(result).filter((item) => item.phase === "running");
    expect(active).toHaveLength(1);
    expect(active[0]).toMatchObject({
      status: "RUNNING",
      task: { id: "image-task" },
      progressPercent: 50,
    });
    expect(active[0].statusLabel).toContain("生成中");
  });
  it.each(["WAITING_AUTHORIZATION", "BLOCKED"] as const)(
    "preserves explicit %s gates while a task poll still reports active work",
    (status) => {
      const result = model(
        [],
        [task({ status: "RUNNING", specialistRunId: "image-run" })],
        [run({ status, taskRefs: ["image-task"] })],
      );
      expect(items(result)).toHaveLength(1);
      expect(items(result)[0]).toMatchObject({ phase: "attention", status });
    },
  );
  it("uses an exact task identity to distinguish variants and does not duplicate its run", () => {
    const result = model(
      [node(), node({ id: "visual:cat:wounded", taskId: "image-task" })],
      [task({ status: "RUNNING", specialistRunId: "image-run" })],
      [run({ status: "WAITING_RUNTIME", taskRefs: ["image-task"] })],
    );
    expect(result.counts.running).toBe(1);
    expect(
      items(result).find((item) => item.phase === "running")?.node?.id,
    ).toBe("visual:cat:wounded");
    expect(items(result).filter((item) => item.source === "run")).toHaveLength(
      0,
    );
  });
  it("stops counting an exact finished task even if the graph still says running", () => {
    const result = model(
      [node({ status: "running", taskId: "image-task" })],
      [task({ status: "SUCCEEDED", progress: 1 })],
    );
    expect(result.counts).toMatchObject({ running: 0, completed: 1 });
  });
});

describe("orphaned admissions reaped by recovery", () => {
  it("hides a recovery-reaped orphan run instead of boarding it forever", () => {
    // The field Project carried a QUEUED Run whose process died before its
    // Task existed; recovery cancels it at boot. It never executed anything,
    // so the overview must not keep it as an operation needing a person.
    const result = model(
      [],
      [],
      [
        run({
          id: "orphan-run",
          status: "CANCELLED",
          finalSummaryText:
            "ORPHANED_QUEUED_RUN: the process that admitted this Run ended " +
            "before its Task existed; no executor can dispatch it",
          taskRefs: [],
        }),
      ],
    );

    expect(items(result)).toHaveLength(0);
    expect(result.counts.total).toBe(0);
  });

  it("keeps a person-cancelled run on the board", () => {
    const result = model(
      [],
      [],
      [
        run({
          id: "stopped-run",
          status: "CANCELLED",
          finalSummaryText: "stopped by the author",
          taskRefs: [],
        }),
      ],
    );

    expect(items(result)).toHaveLength(1);
    expect(result.counts.attention).toBe(1);
  });
});

describe("retried source understanding", () => {
  it("hides an unsuccessful attempt once a later re-run of the same target succeeded", () => {
    const old = run({
      id: "src-old",
      role: "source_intelligence_agent",
      status: "FAILED",
      targetRefs: ["asset:a1"],
      createdAt: "2026-10-12T00:00:00Z",
    });
    const fresh = run({
      id: "src-new",
      role: "source_intelligence_agent",
      status: "SUCCEEDED",
      targetRefs: ["asset:a1"],
      createdAt: "2026-11-03T00:00:00Z",
    });

    const result = model([], [], [old, fresh]);
    const ids = items(result).map((item) => item.id);

    expect(ids).not.toContain("run:src-old");
    expect(ids).toContain("run:src-new");
    expect(result.counts.attention).toBe(0);
    expect(result.counts.completed).toBe(1);
  });

  it("keeps an unsuccessful attempt while no later run succeeded", () => {
    const old = run({
      id: "src-only",
      role: "source_intelligence_agent",
      status: "BLOCKED",
      targetRefs: ["asset:a2"],
      createdAt: "2026-10-12T00:00:00Z",
    });

    const result = model([], [], [old]);

    expect(items(result).map((item) => item.id)).toContain("run:src-only");
    expect(result.counts.attention).toBe(1);
  });
});

describe("Live thinking projection", () => {
  function session(status: CreatorSessionStatus): CreatorSessionView {
    return {
      id: "session-1",
      projectId: "p1",
      status,
      lastMessageSeq: 0,
      lastConsumedMessageSeq: 0,
      lastEventSeq: 0,
    };
  }

  function statusBar(
    overrides: Partial<AgentStatusBarView["progress"]> = {},
  ): AgentStatusBarView {
    return {
      progress: {
        phase: "visual_development",
        label: "正在制作",
        sourceEventSeq: 1,
        updatedAt: "now",
        ...overrides,
      },
      badges: [],
    };
  }

  const delegateCall: ToolCallPresentation = {
    actionId: "action-1",
    order: 1,
    status: "started",
    executing: true,
    tool: "delegate_to_agent",
    arguments: { role: "visual_development_agent" },
  };

  const live = (overrides: Partial<AgentLiveStatusInput> = {}) =>
    deriveAgentLiveStatus({
      session: session("RUNNING"),
      agentStatusBar: statusBar(),
      stopping: false,
      hasQueuedInput: false,
      isReplaying: false,
      subagentActivities: {},
      toolCalls: [],
      tasks: [],
      project: projectDocument,
      ...overrides,
    });

  it("presents thinking in the live status without overriding user decisions or execution", () => {
    expect(live({ mainThinking: true }).label).toBe("正在思考");
    expect(live({ mainThinking: true, stopping: true }).state).toBe("stopping");
    expect(live({ mainThinking: true, pendingReviewCount: 1 }).state).toBe(
      "waiting",
    );
    expect(live({ mainThinking: true, session: session("IDLE") }).state).toBe(
      "idle",
    );
    expect(
      live({
        mainThinking: true,
        toolCalls: [{ ...delegateCall, tool: "read_project" }],
      }).label,
    ).toBe("处理中");
  });
});

describe("Interactive conversation messages", () => {
  it("resolves only referenced names and updates them with a new project snapshot", async () => {
    const project = {
      timelines: {
        items: {
          main: {
            elements_by_id: {
              "scene-one": { label: "序章" },
              "scene-unnamed": { label: "scene-unnamed" },
            },
          },
        },
      },
      visual: { entities: { items: {} } },
      assets: {
        artifact_versions_by_id: new Proxy(
          {
            "video-one": { name: "献诏片段" },
            "video-unnamed": { name: "" },
          },
          {
            ownKeys: () => {
              throw new Error("Do not enumerate unrelated generated versions");
            },
          },
        ),
        source_versions_by_id: {},
      },
    } as unknown as ProjectDocument;
    const raw =
      "检查 scene-one 和 `video-one`，保留《scene-one》和 https://example.com/scene-one。";
    expect(publicAssistantText(raw, { project })).toBe(
      "检查 序章 和 献诏片段，保留《scene-one》和 https://example.com/scene-one。",
    );
    const renamed = {
      ...project,
      timelines: {
        ...project.timelines,
        items: {
          ...project.timelines.items,
          main: {
            ...project.timelines.items.main,
            elements_by_id: {
              ...project.timelines.items.main.elements_by_id,
              "scene-one": {
                ...project.timelines.items.main.elements_by_id["scene-one"],
                label: "新序章",
              },
            },
          },
        },
      },
    };
    expect(publicAssistantText("scene-one", { project: renamed })).toBe(
      "新序章",
    );
    expect(publicAssistantText("scene-one", { project })).toBe("序章");
    const originalLanguage = i18n.language;
    try {
      await i18n.changeLanguage("en");
      expect(humanizeCreatorRefs("scene-unnamed", project)).toBe(
        i18n.t("presentation.targets.timelineContent"),
      );
      expect(publicAssistantText("`video-unnamed`", { project })).toBe(
        i18n.t("presentation.targets.genResult"),
      );
    } finally {
      await i18n.changeLanguage(originalLanguage);
    }
  });

  const creatorMessage = (
    overrides: Partial<CreatorMessage>,
  ): CreatorMessage => ({
    messageId: "notice",
    messageSeq: 1,
    role: "assistant",
    content: [],
    metadata: {},
    createdAt: "now",
    ...overrides,
  });
  const text = (value: string): CreatorMessage["content"] => [
    { type: "text", text: value },
  ];

  it("shows execution pause notices without granting user authority", () => {
    const notice = creatorMessage({
      role: "assistant",
      source: "creator_execution_notice",
      content: text("【自动执行已暂停】项目仍有待处理事项，请检查后继续。"),
      metadata: { executionPause: { reason: "no_committed_progress" } },
    });
    expect(shouldRenderConversationMessage(notice)).toBe(true);
    expect(isUserAuthorityMessage(notice)).toBe(false);
    expect(shouldRenderConversationMessage({ ...notice, role: "user" })).toBe(
      false,
    );
  });
});
