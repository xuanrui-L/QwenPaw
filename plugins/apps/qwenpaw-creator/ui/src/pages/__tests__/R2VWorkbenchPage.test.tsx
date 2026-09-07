import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import R2VWorkbenchPage, { WorkbenchSurface } from "@/pages/R2VWorkbenchPage";
import PlanPage from "@/pages/PlanPage";
import { NavigationRuntime } from "@/routing/navigation";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { message } from "antd";
import * as promptApi from "@/api/creator/promptSync";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";
import type { ProjectDocument, TaskView } from "@/contracts/creator";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function withLooseImageReferences(storyboard: string[], video: string[]) {
  const project = cloneProject();
  for (const suffix of ["a", "b", "c"]) {
    const id = `source-image-${suffix}`;
    project.assets.files_by_id[`file:${id}`] = {
      ...project.assets.files_by_id["file:source-video"],
      file_id: `file:${id}`,
      relative_uri: `sources/${id}.png`,
      media_type: "image/png",
    };
    project.assets.source_versions_by_id[id] = {
      ...project.assets.source_versions_by_id["cat-video-v1"],
      version_id: id,
      logical_asset_id: `asset:${id}`,
      name: `参考图 ${suffix.toUpperCase()}`,
      file_id: `file:${id}`,
      media_kind: "image",
      media_type: "image/png",
      duration_seconds: null,
    };
  }
  const creation =
    project.timelines.items["timeline:main"].elements_by_id["r2v-window"]
      .creation;
  if (creation.type === "r2v") {
    creation.storyboard_reference_version_ids = storyboard;
    creation.video_reference_version_ids = video;
  }
  return project;
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
  });
}

function withSecondVideoVersion(project = cloneProject()): ProjectDocument {
  project.assets.files_by_id["file:r2v-video2"] = {
    file_id: "file:r2v-video2",
    kind: "artifact",
    relative_uri: "artifacts/window-2.mp4",
    sha256: "sha-r2v-2",
    size_bytes: 2048,
    media_type: "video/mp4",
    created_at: "2026-07-20T00:01:30Z",
  };
  project.assets.artifact_versions_by_id["r2v-window-v2"] = {
    ...project.assets.artifact_versions_by_id["r2v-window-v1"],
    version_id: "r2v-window-v2",
    name: "午饭名场面视频 v2",
    file_id: "file:r2v-video2",
    checksum: "sha-r2v-2",
    created_at: "2026-07-20T00:01:30Z",
  };
  project.assets.artifact_slots_by_id[
    "element:r2v-window:video"
  ].version_ids.push("r2v-window-v2");
  return project;
}

function modelRoutes(model: string): Parameters<typeof installMockFetch>[0] {
  return [
    {
      match: "/projects/p1/tasks",
      method: "GET",
      response: { json: { items: [] } },
    },
    {
      match: "/specialist-runs",
      method: "GET",
      response: { json: { items: [] } },
    },
    {
      match: "/work-graph",
      method: "GET",
      response: {
        json: {
          projectId: "p1",
          generation: 3,
          counts: {},
          nodes: [],
          mediaCalls: 0,
          mediaCallBudget: 20,
        },
      },
    },
    {
      match: "/projects/p1/project",
      method: "GET",
      response: {
        get json() {
          const current = useProjectSnapshotStore.getState();
          return {
            projectId: "p1",
            generation: current.generation,
            etag: current.etag,
            syncStatus: "healthy",
            project: current.project,
          };
        },
      },
    },
    {
      match: "/prompt-sync",
      method: "GET",
      response: {
        json: {
          status: "current",
          baselineToken: "verified-current",
          narrative: (
            projectDocument.timelines.items["timeline:main"].elements_by_id[
              "r2v-window"
            ].creation as { narrative: string }
          ).narrative,
          changedSources: [],
          suggestedSource: null,
          storyboardPrompt: "暖色餐厅窗外的橘猫",
          videoPrompt: "镜头缓慢推近，橘猫眨眼",
        },
      },
    },
    {
      match: "/models/resolved",
      response: { json: { video: { provider: "wan", model } } },
    },
  ];
}

/** PATCH endpoint answering with the given next-generation Project. */
function patchRoutes(updated: ProjectDocument) {
  updated.generation = 4;
  const { calls } = installMockFetch([
    ...modelRoutes("wan2.7-r2v"),
    {
      match: "/projects/p1/project",
      method: "PATCH",
      response: {
        json: {
          projectId: "p1",
          generation: 4,
          etag: '"sha256:g4"',
          changedPointers: [],
          project: updated,
        },
      },
    },
  ]);
  return calls;
}

async function expectPatch(
  calls: ReturnType<typeof installMockFetch>["calls"],
  path: string,
  value: string,
) {
  await waitFor(() =>
    expect(calls.some((call) => call.method === "PATCH")).toBe(true),
  );
  expect(calls.find((call) => call.method === "PATCH")!.body).toMatchObject({
    operations: [
      ...(path.includes("/shots/items/") && path.endsWith("/description")
        ? [
            {
              op: "add",
              path: path.replace(/description$/, "dialogue"),
              value: "",
            },
          ]
        : []),
      { op: "replace", path, value },
    ],
  });
}

function renderWorkbench(entry = "/project/p1/plan/element/r2v-window") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <NavigationRuntime />
      <Routes>
        <Route path="/project/:id/plan" element={<PlanPage />} />
        <Route
          path="/project/:id/plan/element/:elementId"
          element={<R2VWorkbenchPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function withStoryboardVersions(count: number): ProjectDocument {
  const project = cloneProject();
  const slotId = "element:r2v-window:storyboard";
  project.timelines.items["timeline:main"].elements_by_id[
    "r2v-window"
  ].outputs.storyboard = { slot_id: slotId };
  project.assets.artifact_slots_by_id[slotId] = {
    ...project.assets.artifact_slots_by_id["visual:cat:anchor"],
    slot_id: slotId,
    kind: "r2v_storyboard_image",
    owner_ref: "element:r2v-window",
    selected_version_id: "sb-v1",
    version_ids: [],
  };
  for (let index = 1; index <= count; index += 1) {
    const id = `sb-v${index}`;
    project.assets.artifact_slots_by_id[slotId].version_ids.push(id);
    project.assets.artifact_versions_by_id[id] = {
      ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
      version_id: id,
      slot_id: slotId,
      owner_ref: "element:r2v-window",
      kind: "r2v_storyboard_image",
      name: `分镜图 ${index}`,
      created_at: `2026-09-06T19:3${index}:00Z`,
    };
  }
  return project;
}

function mediaTask(
  kind: TaskView["kind"],
  status: TaskView["status"],
): TaskView {
  return {
    id: `task-${kind}`,
    projectId: "p1",
    transactionId: null,
    specialistRunId: null,
    kind,
    targetRef: "element:r2v-window",
    status,
    progress: 0.4,
    resultRefs: [],
    createdAt: "2026-09-06T19:28:01Z",
    updatedAt: "2026-09-06T19:28:03Z",
  };
}

function deferDispatch(
  fetchMock: ReturnType<typeof installMockFetch>["fetchMock"],
) {
  const original = fetchMock.getMockImplementation()!;
  let pending = false;
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  fetchMock.mockImplementation(async (input, init = {}) => {
    if (String(input).endsWith("/dispatch") && init.method === "POST") {
      pending = true;
      await delayed;
    }
    return original(input, init);
  });
  return { pending: () => pending, release };
}

async function waitForWorkbenchReady(container: HTMLElement) {
  await waitFor(() =>
    expect(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      ),
    ).toBeEnabled(),
  );
}

describe("R2V Workbench page", () => {
  beforeEach(() => {
    useProjectSnapshotStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useWorkGraphStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useAgentDockUiStore.getState().reset();
    seedProject();
    // Default resolved-models mock so rendering never issues a real call.
    installMockFetch(modelRoutes("wan2.7-r2v"));
  });

  it("renders the origin/main workbench surfaces for an R2V Element", () => {
    const { container } = renderWorkbench();

    expect(
      screen.getByText(/视频方案 \/ 午饭名场面 \/ 制作工作台/),
    ).toBeInTheDocument();
    // No generation_mode in the legacy fixture → historical r2v default.
    expect(
      container.querySelector('[data-generation-mode="r2v"]'),
    ).toHaveTextContent("参考生视频");
    expect(screen.queryByDisplayValue("橘猫隔窗看向午饭")).toBeNull();
    expect(
      container.querySelector('[data-artifact-version="r2v-window-v1"]'),
    ).toBeInTheDocument();
    expect(container.querySelector("video")).toHaveAttribute(
      "src",
      "/api/qwenpaw-creator/media/artifacts/r2v-window-v1",
    );
    // 设计 84:38986 右栏只保留生成结果与元信息：引用素材/资产绑定不再渲染。
    expect(screen.queryByText(/引用素材/)).toBeNull();
    expect(screen.queryByText("资产绑定")).toBeNull();
    expect(useCreatorInteractionStore.getState().selectedRef).toBe(
      "element:r2v-window",
    );
  });

  it("round-trips between the Plan detail CTA and the workbench", async () => {
    renderWorkbench("/project/p1/plan?element=r2v-window");

    fireEvent.click(screen.getByRole("button", { name: /去制作台编辑/ }));
    // 制作台以工作区整页视图打开（片段编辑层设计，不再跳转独立路由）。
    await waitFor(() =>
      expect(
        screen.getByText(/视频方案 \/ 午饭名场面 \/ 制作工作台/),
      ).toBeInTheDocument(),
    );
    expect(
      document.querySelector("[data-workbench-modal='r2v-window']"),
    ).toBeInTheDocument();

    // 整页制作台通过页头「返回视频方案」箭头关闭。
    fireEvent.click(screen.getByRole("button", { name: "返回视频方案" }));
    await waitFor(() =>
      expect(
        document.querySelector("[data-workbench-modal='r2v-window']"),
      ).not.toBeInTheDocument(),
    );
    // The creative brief moved to the blueprint page; episode switching
    // lives in the workspace sidebar, so the plan page greets with the
    // timeline-title heading.
    expect(
      screen.getByRole("heading", { name: "第1集 · 晨光出发" }),
    ).toBeInTheDocument();
    // Prompt 编辑已迁往制作台；详情回落为关键信息总览。
    expect(screen.getByText("创作意图")).toBeInTheDocument();
  });

  it("keeps non-R2V Elements out of the workbench with a way back", () => {
    renderWorkbench("/project/p1/plan/element/edit-opening");

    expect(
      screen.getByText("该时间线内容不是 AI 生成画面，没有独立工作台"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "返回方案" }),
    ).toBeInTheDocument();
  });

  it.each<[string, string, string, string]>([
    [
      "prompt",
      "镜头缓慢推近，橘猫眨眼",
      "镜头快速拉远",
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/video_prompt",
    ],
    [
      "storyboard prompt",
      "暖色餐厅窗外的橘猫",
      "橘猫扒着窗台",
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt",
    ],
  ])(
    "auto-saves %s edits through the Project CAS Patch endpoint on blur",
    async (_field, current, next, path) => {
      const calls = patchRoutes(cloneProject());
      renderWorkbench();

      const input = screen.getByDisplayValue(current);
      fireEvent.change(input, { target: { value: next } });
      // Typing alone never commits; leaving the field is the save boundary.
      expect(calls.some((call) => call.method === "PATCH")).toBe(false);
      fireEvent.blur(input);
      await expectPatch(calls, path, next);
    },
  );

  it("dispatches the video node from the prompt-card regenerate button", async () => {
    const { calls } = installMockFetch([
      ...modelRoutes("wan2.7-r2v"),
      { match: "/specialist-runs", response: { json: { items: [] } } },
      { match: "/projects/p1/tasks", response: { json: { items: [] } } },
      {
        match: "/work-graph/nodes/video%3Ar2v-window/dispatch",
        method: "POST",
        response: {
          json: { ok: true, nodeId: "video:r2v-window", dispatched: true },
        },
      },
    ]);
    const { container } = renderWorkbench();

    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    fireEvent.click(
      container.querySelector(
        '[data-prompt-regenerate="element:r2v-window/creation/video_prompt"]',
      )!,
    );
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "POST" &&
            call.url.includes("/work-graph/nodes/video%3Ar2v-window/dispatch"),
        ),
      ).toBe(true),
    );
    // Clean draft: regenerate must not fire a project PATCH.
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });

  it("applies a dirty prompt draft before dispatching regeneration", async () => {
    const updated = cloneProject();
    updated.generation = 4;
    const { calls } = installMockFetch([
      ...modelRoutes("wan2.7-r2v"),
      { match: "/specialist-runs", response: { json: { items: [] } } },
      { match: "/projects/p1/tasks", response: { json: { items: [] } } },
      {
        match: "/projects/p1/project",
        method: "PATCH",
        response: {
          json: {
            projectId: "p1",
            generation: 4,
            etag: '"sha256:g4"',
            changedPointers: [],
            project: updated,
          },
        },
      },
      {
        match: "/work-graph/nodes/storyboard%3Ar2v-window/dispatch",
        method: "POST",
        response: {
          json: { ok: true, nodeId: "storyboard:r2v-window", dispatched: true },
        },
      },
    ]);
    const { container } = renderWorkbench();

    const sbPanel = container.querySelector('[data-stage-panel="sb"]')!;
    fireEvent.change(sbPanel.querySelector("textarea")!, {
      target: { value: "新的分镜 Prompt" },
    });
    fireEvent.click(
      container.querySelector(
        '[data-prompt-regenerate="element:r2v-window/creation/storyboard_prompt"]',
      )!,
    );
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "POST" &&
            call.url.includes(
              "/work-graph/nodes/storyboard%3Ar2v-window/dispatch",
            ),
        ),
      ).toBe(true),
    );
    // The new prompt must be persisted before the node is dispatched.
    const patchIndex = calls.findIndex((call) => call.method === "PATCH");
    const dispatchIndex = calls.findIndex(
      (call) => call.method === "POST" && call.url.includes("/dispatch"),
    );
    expect(patchIndex).toBeGreaterThanOrEqual(0);
    expect(patchIndex).toBeLessThan(dispatchIndex);
    expect(calls[patchIndex].body).toMatchObject({
      operations: [
        {
          op: "replace",
          path: "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt",
          value: "新的分镜 Prompt",
        },
      ],
    });
  });

  it("switches the current video version through a slot selection patch", async () => {
    seedProject(withSecondVideoVersion());
    const updated = withSecondVideoVersion();
    updated.assets.artifact_slots_by_id[
      "element:r2v-window:video"
    ].selected_version_id = "r2v-window-v2";
    const calls = patchRoutes(updated);
    const { container } = renderWorkbench();

    fireEvent.click(
      container.querySelector('[data-artifact-version="r2v-window-v2"]')!,
    );
    expect(container.querySelector("video")).toHaveAttribute(
      "src",
      "/api/qwenpaw-creator/media/artifacts/r2v-window-v2",
    );
    fireEvent.click(screen.getByRole("button", { name: "设为当前" }));
    await expectPatch(
      calls,
      "/assets/artifact_slots_by_id/element:r2v-window:video/selected_version_id",
      "r2v-window-v2",
    );
  });

  it("keeps the right rail to result and meta per the segment-editor design", async () => {
    installMockFetch([
      {
        match: "/models/resolved",
        response: {
          json: { video: { provider: "wan", model: "wan2.7-r2v" } },
        },
      },
      {
        match: "/r2v-references",
        response: {
          json: {
            elementId: "r2v-window",
            storyboardSelected: true,
            references: [
              {
                index: 1,
                versionId: "sb-window-v1",
                kind: "storyboard",
                name: "分镜图",
              },
              {
                index: 2,
                versionId: "cat-video-v1",
                kind: "source",
                name: "橘猫原始视频",
              },
            ],
          },
        },
      },
    ]);
    // 绑定一个还没生成设计图的道具：卡片必须以虚线占位形态出现。
    const project = cloneProject();
    project.visual.entities.order.push("lantern");
    project.visual.entities.items["lantern"] = {
      entity_id: "lantern",
      kind: "prop",
      name: "旧灯笼",
      description: "",
      continuity: "",
      required_variant_ids: [],
      variants: { order: [], items: {} },
      selected_artifact_version_id: null,
    };
    const r2vDraft =
      project.timelines.items["timeline:main"].elements_by_id["r2v-window"];
    if (r2vDraft.creation.type === "r2v")
      r2vDraft.creation.prop_refs = ["lantern"];
    seedProject(project);
    const { container } = renderWorkbench();

    // 右栏 = 生成结果 + 相关资产分组；阶段状态不再展示，旧的
    // 引用素材列表/资产绑定下拉也不回归（权威 [Image N] 只服务 prompt 胶囊）。
    expect(await screen.findByText("相关资产")).toBeInTheDocument();
    expect(screen.getByText("视频生成结果")).toBeInTheDocument();
    expect(screen.getByText("分镜图生成结果")).toBeInTheDocument();
    expect(screen.queryByText("阶段状态")).toBeNull();
    expect(screen.queryByText(/引用素材/)).toBeNull();
    expect(screen.queryByText("资产绑定")).toBeNull();
    // 添加入口只有标题行一个 +；空分类（场景/素材）不渲染分组。
    expect(container.querySelectorAll("[data-add-asset]")).toHaveLength(1);
    expect(container.querySelectorAll("[data-add-entity]")).toHaveLength(0);
    const rail = container.querySelector("[data-r2v-workbench] aside")!;
    expect(rail.textContent).not.toContain("场景");
    expect(rail.textContent).not.toContain("素材");
    // 已绑定的橘猫与灯笼卡可移除；未生成的灯笼是虚线占位卡（渲染「未生成」），
    // 已生成的卡不再标注「设计已完成」。
    expect(screen.getByText("圆润大橘猫")).toBeInTheDocument();
    expect(screen.getAllByLabelText("移除引用")).toHaveLength(2);
    expect(screen.getByText("旧灯笼")).toBeInTheDocument();
    expect(screen.getByText("未生成")).toBeInTheDocument();
    expect(screen.queryByText("设计已完成")).toBeNull();
    // 提示词卡固定引用预览（无原文切换），编辑胶囊在重新生成左侧。
    expect(screen.queryByText("编辑原文")).toBeNull();
    expect(screen.queryByText("引用预览")).toBeNull();
    const editPill = container.querySelector(
      '[data-prompt-edit="element:r2v-window/creation/storyboard_prompt"]',
    )!;
    expect(editPill.nextElementSibling).toHaveAttribute(
      "data-prompt-regenerate",
      "element:r2v-window/creation/storyboard_prompt",
    );
    expect(
      container.querySelectorAll(
        "[data-r2v-workbench] aside [role='combobox']",
      ),
    ).toHaveLength(0);
  });

  it("adds assets through the thumbnail asset picker", async () => {
    const calls = patchRoutes(cloneProject());
    const { container } = renderWorkbench();

    // 单一 + 打开缩略版资产库：分类筛选 + 已绑定项预选中。
    fireEvent.click(container.querySelector("[data-add-asset]")!);
    expect(await screen.findByText("添加相关资产")).toBeInTheDocument();
    expect(
      document.querySelector('[data-picker-asset="cat"]'),
    ).toBeInTheDocument();
    // 未做任何更改的确认必须是零改动：不产生 PATCH（顺序也不得被重排）。
    fireEvent.click(document.querySelector("[data-picker-confirm]")!);
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);

    // 点选素材候选（橘猫原始视频）并确认 → 一次性静默落盘。
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    fireEvent.click(container.querySelector("[data-add-asset]")!);
    fireEvent.click(
      document.querySelector('[data-picker-asset="cat-video-v1"]')!,
    );
    fireEvent.click(document.querySelector("[data-picker-confirm]")!);
    await waitFor(() =>
      expect(calls.some((call) => call.method === "PATCH")).toBe(true),
    );
    const operations = (
      calls.find((call) => call.method === "PATCH")!.body as {
        operations: Array<{ path: string; value?: unknown }>;
      }
    ).operations;
    expect(
      operations.some(
        (op) =>
          op.path ===
            "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/video_reference_version_ids" &&
          Array.isArray(op.value) &&
          (op.value as string[]).includes("cat-video-v1"),
      ),
    ).toBe(true);
  });

  it("removes a bound character from the rail and persists via CAS patch", async () => {
    const updated = cloneProject();
    const r2vElement =
      updated.timelines.items["timeline:main"].elements_by_id["r2v-window"];
    if (r2vElement.creation.type === "r2v") {
      r2vElement.creation.character_refs = [];
      r2vElement.creation.visual_variant_refs = {};
    }
    const calls = patchRoutes(updated);
    renderWorkbench();

    // 移除引用是离散动作 = 语义边界：点击后草稿直接静默落盘。
    fireEvent.click(screen.getByLabelText("移除引用"));
    await waitFor(() =>
      expect(calls.some((call) => call.method === "PATCH")).toBe(true),
    );
    const operations = (
      calls.find((call) => call.method === "PATCH")!.body as {
        operations: Array<{ path: string; value?: unknown }>;
      }
    ).operations;
    expect(
      operations.some(
        (op) =>
          op.path ===
            "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/character_refs" &&
          Array.isArray(op.value) &&
          op.value.length === 0,
      ),
    ).toBe(true);
  });

  it("can add a storyboard-only material to the video stage without rewriting storyboard order", async () => {
    const storyboard = ["source-image-c", "source-image-a"];
    const project = withLooseImageReferences(storyboard, ["source-image-b"]);
    seedProject(project);
    const updated = structuredClone(project);
    const creation =
      updated.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type === "r2v")
      creation.video_reference_version_ids.push("source-image-a");
    const calls = patchRoutes(updated);
    const { container } = renderWorkbench();
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    fireEvent.click(container.querySelector("[data-add-asset]")!);
    fireEvent.click(
      document.querySelector('[data-picker-asset="source-image-a"]')!,
    );
    fireEvent.click(document.querySelector("[data-picker-confirm]")!);
    await waitFor(() =>
      expect(calls.some((call) => call.method === "PATCH")).toBe(true),
    );
    const operations = (
      calls.find((call) => call.method === "PATCH")!.body as {
        operations: Array<{ path: string; value?: unknown }>;
      }
    ).operations;
    expect(operations).toHaveLength(1);
    expect(operations[0]).toMatchObject({
      path: "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/video_reference_version_ids",
      value: ["source-image-b", "source-image-a"],
    });
    const saved =
      useProjectSnapshotStore.getState().project!.timelines.items[
        "timeline:main"
      ].elements_by_id["r2v-window"].creation;
    expect(
      saved.type === "r2v" && saved.storyboard_reference_version_ids,
    ).toEqual(storyboard);
  });

  it("removing a shared material from video preserves the complete storyboard reference array", async () => {
    const storyboard = ["source-image-c", "source-image-a", "source-image-b"];
    const project = withLooseImageReferences(storyboard, [
      "source-image-a",
      "source-image-b",
    ]);
    seedProject(project);
    const updated = structuredClone(project);
    const creation =
      updated.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type === "r2v")
      creation.video_reference_version_ids = ["source-image-b"];
    const calls = patchRoutes(updated);
    const { container } = renderWorkbench();
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    const material = screen.getByText("参考图 A").parentElement!;
    fireEvent.click(within(material).getByRole("button", { name: "移除引用" }));
    await waitFor(() =>
      expect(calls.some((call) => call.method === "PATCH")).toBe(true),
    );
    const operations = (
      calls.find((call) => call.method === "PATCH")!.body as {
        operations: Array<{ path: string; value?: unknown }>;
      }
    ).operations;
    expect(operations).toHaveLength(1);
    expect(operations[0]).toMatchObject({
      path: "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/video_reference_version_ids",
      value: ["source-image-b"],
    });
    fireEvent.click(container.querySelector('[data-stage-tab="sb"]')!);
    expect(screen.getByText("参考图 A")).toBeInTheDocument();
    const saved =
      useProjectSnapshotStore.getState().project!.timelines.items[
        "timeline:main"
      ].elements_by_id["r2v-window"].creation;
    expect(
      saved.type === "r2v" &&
        JSON.stringify(saved.storyboard_reference_version_ids),
    ).toBe(JSON.stringify(storyboard));
  });

  it.each(["empty", "overlong"])(
    "does not let a hidden %s Shot plan block prompt editing",
    async (plan) => {
      const project = cloneProject();
      const creation =
        project.timelines.items["timeline:main"].elements_by_id["r2v-window"]
          .creation;
      if (creation.type !== "r2v") throw new Error("fixture");
      if (plan === "empty") creation.shots = { order: [], items: {} };
      else
        creation.shots = {
          order: ["old"],
          items: { old: { duration_seconds: 300 } },
        };
      seedProject(project);
      const updated = structuredClone(project);
      if (
        updated.timelines.items["timeline:main"].elements_by_id["r2v-window"]
          .creation.type === "r2v"
      )
        updated.timelines.items["timeline:main"].elements_by_id[
          "r2v-window"
        ].creation.storyboard_prompt = "橘猫转身离开窗台";
      const calls = patchRoutes(updated);
      const { container } = renderWorkbench();
      await waitForWorkbenchReady(container);
      expect(
        container.querySelector('[data-workbench-pane="shots"]'),
      ).toBeNull();
      const input = screen.getByDisplayValue("暖色餐厅窗外的橘猫");
      fireEvent.change(input, { target: { value: "橘猫转身离开窗台" } });
      fireEvent.blur(input);
      await expectPatch(
        calls,
        "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt",
        "橘猫转身离开窗台",
      );
      await waitForWorkbenchReady(container);
      expect(calls.some((call) => call.method === "POST")).toBe(false);
    },
  );

  it("does not dispatch when a prompt is edited without blur while the generation sync GET is pending", async () => {
    const project = cloneProject();
    const creation =
      project.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type === "r2v")
      creation.shots = { items: { old: { dialogue: "旧台词" } } };
    seedProject(project);
    const { calls, fetchMock } = installMockFetch(modelRoutes("wan2.7-r2v"));
    const original = fetchMock.getMockImplementation()!;
    let deferNextSync = false;
    let syncPending = false;
    let release!: () => void;
    const delayed = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (input, init = {}) => {
      if (String(input).includes("/prompt-sync") && deferNextSync) {
        deferNextSync = false;
        syncPending = true;
        await delayed;
      }
      return original(input, init);
    });
    const { container } = renderWorkbench();
    await waitForWorkbenchReady(container);
    deferNextSync = true;
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(syncPending).toBe(true));
    fireEvent.change(screen.getByDisplayValue("暖色餐厅窗外的橘猫"), {
      target: { value: "用户仍在编辑的新动作" },
    });
    await act(async () => release());
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);
    expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(0);
    expect(
      screen.getByDisplayValue("用户仍在编辑的新动作"),
    ).toBeInTheDocument();
    // Return this unsaved test draft to its baseline so the workbench's real
    // unmount-save boundary does not start a PATCH during the following test.
    fireEvent.change(screen.getByDisplayValue("用户仍在编辑的新动作"), {
      target: { value: "暖色餐厅窗外的橘猫" },
    });
  });

  it("does not dispatch an old generation check after A to B to A navigation", async () => {
    const project = cloneProject();
    const timeline = project.timelines.items["timeline:main"];
    timeline.elements_by_id["r2v-other"] = {
      ...structuredClone(timeline.elements_by_id["r2v-window"]),
      element_id: "r2v-other",
      label: "另一镜头",
    };
    seedProject(project);
    const { calls, fetchMock } = installMockFetch(modelRoutes("wan2.7-r2v"));
    const original = fetchMock.getMockImplementation()!;
    let deferNextSync = false;
    let syncPending = false;
    let release!: () => void;
    const delayed = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (input, init = {}) => {
      if (String(input).includes("/prompt-sync") && deferNextSync) {
        deferNextSync = false;
        syncPending = true;
        await delayed;
      }
      return original(input, init);
    });
    const surface = (elementId: string) => (
      <MemoryRouter>
        <WorkbenchSurface
          projectId="p1"
          elementId={elementId}
          onBack={() => {}}
        />
      </MemoryRouter>
    );
    const { container, rerender } = render(surface("r2v-window"));
    await waitForWorkbenchReady(container);
    deferNextSync = true;
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(syncPending).toBe(true));
    rerender(surface("r2v-other"));
    rerender(surface("r2v-window"));
    await act(async () => release());
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);
    expect(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      ),
    ).toBeEnabled();
  });

  it("does not refresh the old project when an already-issued dispatch finishes after leaving", async () => {
    const poll = vi
      .spyOn(useProjectSnapshotStore.getState(), "pollOnce")
      .mockResolvedValue(undefined);
    const { fetchMock } = installMockFetch([
      ...modelRoutes("wan2.7-r2v"),
      {
        match: "/dispatch",
        method: "POST",
        response: { json: { ok: true, dispatched: true } },
      },
    ]);
    const original = fetchMock.getMockImplementation()!;
    let dispatchPending = false;
    let release!: () => void;
    const delayed = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (input, init = {}) => {
      if (String(input).endsWith("/dispatch") && init.method === "POST") {
        dispatchPending = true;
        await delayed;
      }
      return original(input, init);
    });
    const { container, unmount } = renderWorkbench();
    await waitForWorkbenchReady(container);
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(dispatchPending).toBe(true));
    unmount();
    // The immediate read is intentional; only late completion must stay fenced.
    poll.mockClear();
    useProjectSnapshotStore.getState().reset("p2");
    await act(async () => release());
    expect(poll).not.toHaveBeenCalled();
    expect(useProjectSnapshotStore.getState().projectId).toBe("p2");
    poll.mockRestore();
  });

  it("saves prompt edits while keeping plain regeneration buttons and no separate sync action", async () => {
    const updated = cloneProject();
    const creation =
      updated.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type !== "r2v") throw new Error("fixture");
    creation.storyboard_prompt = "橘猫转身离开窗台";
    const calls = patchRoutes(updated);
    const { container } = renderWorkbench();
    const input = screen.getByDisplayValue("暖色餐厅窗外的橘猫");
    fireEvent.change(input, { target: { value: "橘猫转身离开窗台" } });
    fireEvent.blur(input);
    await expectPatch(
      calls,
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt",
      "橘猫转身离开窗台",
    );
    await waitForWorkbenchReady(container);
    expect(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      ),
    ).toHaveTextContent("重新生成图片");
    expect(
      container.querySelector(
        '[data-stage-panel="vd"] [data-prompt-regenerate]',
      ),
    ).toHaveTextContent("重新生成视频");
    expect(container.querySelector("[data-prompt-sync-status]")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /核对|同步并重新生成/ }),
    ).toBeNull();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    const patch = calls.find((call) => call.method === "PATCH")!;
    expect(JSON.stringify(patch.body)).not.toContain("shots");
    expect(JSON.stringify(patch.body)).not.toContain("video_prompt");
  });

  it("shows actual image activity before a synchronous dispatch POST finishes, then converges the new candidate", async () => {
    const project = withStoryboardVersions(1);
    seedProject(project);
    let serverProject = project;
    const task = mediaTask("image_generation", "RUNNING");
    const graph = {
      projectId: "p1",
      generation: 3,
      counts: { running: 1 },
      nodes: [
        {
          id: "storyboard:r2v-window",
          kind: "storyboard",
          label: "",
          status: "running",
          taskId: task.id,
          timelineId: "timeline:main",
          progress: 0.4,
          deps: [],
          lane: "",
          error: null,
          missing: [],
          locator: { page: "plan", elementId: "r2v-window" },
          dispatchable: false,
        },
      ],
      mediaCalls: 1,
      mediaCallBudget: 20,
    };
    const { calls, fetchMock } = installMockFetch([
      {
        match: "/projects/p1/tasks",
        response: {
          get json() {
            return { items: [{ ...task }] };
          },
        },
      },
      {
        match: "/work-graph",
        method: "GET",
        response: {
          get json() {
            return structuredClone(graph);
          },
        },
      },
      {
        match: "/projects/p1/project",
        method: "GET",
        response: {
          get json() {
            return {
              projectId: "p1",
              generation: serverProject.generation,
              etag: `"g${serverProject.generation}"`,
              syncStatus: "healthy",
              project: serverProject,
            };
          },
        },
      },
      {
        match: "/dispatch",
        method: "POST",
        response: {
          json: { ok: true, dispatched: true, status: "dispatched" },
        },
      },
      ...modelRoutes("wan2.7-r2v"),
    ]);
    const deferred = deferDispatch(fetchMock);
    const { container } = renderWorkbench(
      "/project/p1/plan/element/r2v-window?version=sb-v1&review=1",
    );
    await waitForWorkbenchReady(container);
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(deferred.pending()).toBe(true));
    await waitFor(() =>
      expect(
        container.querySelector('[data-stage-task="storyboard"]'),
      ).toHaveTextContent("正在生成分镜图 · 40%"),
    );
    expect(container.querySelector('[data-stage-task="video"]')).toBeNull();
    expect(useWorkGraphStore.getState().graph?.nodes[0].taskId).toBe(task.id);
    expect(useCreatorTaskViewStore.getState().tasks[0].id).toBe(task.id);
    expect(
      container.querySelector('[data-result-stage="storyboard"] img'),
    ).toHaveAttribute("src", "/api/qwenpaw-creator/media/artifacts/sb-v1");
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
    serverProject = withStoryboardVersions(2);
    serverProject.generation = 4;
    task.status = "SUCCEEDED";
    task.progress = 1;
    graph.counts = { running: 0 };
    graph.nodes[0].status = "done";
    const success = vi.spyOn(message, "success");
    await act(async () => deferred.release());
    await waitFor(() =>
      expect(
        container.querySelector('[data-result-stage="storyboard"] img'),
      ).toHaveAttribute("src", "/api/qwenpaw-creator/media/artifacts/sb-v2"),
    );
    expect(
      container.querySelector('[data-stage-task="storyboard"]'),
    ).toBeNull();
    expect(
      useProjectSnapshotStore.getState().project?.assets.artifact_slots_by_id[
        "element:r2v-window:storyboard"
      ].selected_version_id,
    ).toBe("sb-v1");
    expect(
      calls.filter(
        (call) => call.url.endsWith("/work-graph") && call.method === "GET",
      ),
    ).toHaveLength(2);
    expect(success).toHaveBeenCalledWith(
      "生成进展已更新，请查看结果或待确认事项",
    );
    success.mockRestore();
  });

  it("preserves a user's later old-version selection when a regeneration candidate arrives", async () => {
    seedProject(withStoryboardVersions(1));
    const { fetchMock } = installMockFetch([
      {
        match: "/dispatch",
        method: "POST",
        response: { json: { ok: true, dispatched: true } },
      },
      ...modelRoutes("wan2.7-r2v"),
    ]);
    const deferred = deferDispatch(fetchMock);
    const { container } = renderWorkbench();
    await waitForWorkbenchReady(container);
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(deferred.pending()).toBe(true));
    fireEvent.click(
      container.querySelector(
        '[data-result-stage="storyboard"] [data-artifact-version="sb-v1"]',
      )!,
    );
    await act(async () => {
      const project = withStoryboardVersions(2);
      project.generation = 4;
      useProjectSnapshotStore.setState({
        project,
        generation: 4,
        etag: '"g4"',
      });
      deferred.release();
    });
    await waitFor(() =>
      expect(
        container.querySelector(
          '[data-stage-panel="sb"] [data-prompt-regenerate]',
        ),
      ).toBeEnabled(),
    );
    expect(
      container.querySelector('[data-result-stage="storyboard"] img'),
    ).toHaveAttribute("src", "/api/qwenpaw-creator/media/artifacts/sb-v1");
    expect(
      container.querySelector(
        '[data-result-stage="storyboard"] [data-artifact-version="sb-v2"]',
      ),
    ).toBeInTheDocument();
  });

  it("keeps storyboard and video task status with their own results and never displays raw task errors", () => {
    seedProject(withStoryboardVersions(1));
    const image = mediaTask("image_generation", "FAILED");
    image.error = {
      message: "private provider body prompt_sync secret",
      detail: "internal failure",
    };
    useCreatorTaskViewStore.setState({
      projectId: "p1",
      tasks: [mediaTask("r2v_generation", "RUNNING"), image],
    });
    const { container } = renderWorkbench();
    expect(
      container.querySelector(
        '[data-result-stage="storyboard"] [data-task-status="FAILED"]',
      ),
    ).toHaveTextContent("分镜图");
    expect(
      container.querySelector(
        '[data-result-stage="video"] [data-task-status="RUNNING"]',
      ),
    ).toHaveTextContent("正在生成视频 · 40%");
    expect(container.textContent).not.toContain("private provider");
    expect(
      container.querySelector('[data-result-stage="storyboard"] img'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-result-stage="video"] video'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-result-stage="storyboard"]'),
    ).toHaveStyle({ order: "0" });
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    expect(container.querySelector('[data-result-stage="video"]')).toHaveStyle({
      order: "0",
    });
    expect(
      container.querySelector('[data-result-stage="storyboard"]'),
    ).toHaveStyle({ order: "1" });
  });

  it.each([
    ["running", "该任务正在生成中，可在创作总览查看进展"],
    ["done", "当前结果已是最新；修改 提示词 后可重新生成"],
  ])(
    "reports an existing %s operation without inventing a task",
    async (status, text) => {
      const info = vi.spyOn(message, "info");
      installMockFetch([
        {
          match: "/dispatch",
          method: "POST",
          response: { json: { ok: true, dispatched: false, status } },
        },
        ...modelRoutes("wan2.7-r2v"),
      ]);
      const { container } = renderWorkbench();
      await waitForWorkbenchReady(container);
      fireEvent.click(
        container.querySelector(
          '[data-stage-panel="sb"] [data-prompt-regenerate]',
        )!,
      );
      await waitFor(() => expect(info).toHaveBeenCalledWith(text));
      expect(useCreatorTaskViewStore.getState().tasks).toEqual([]);
      expect(container.querySelector("[data-stage-task]")).toBeNull();
      info.mockRestore();
    },
  );

  it("does not overlap discovery reads or continue its timer after leaving a pending request", async () => {
    // Drive only the discovery tick. Faking every interval also stalls
    // browser request/React scheduling unrelated to this lifecycle check.
    const originalInterval = window.setInterval.bind(window);
    let tick: (() => void) | undefined;
    const interval = vi
      .spyOn(window, "setInterval")
      .mockImplementation((handler, timeout, ...args) => {
        if (timeout === 3_000 && typeof handler === "function")
          tick = () => handler(...args);
        return originalInterval(handler, timeout, ...args);
      });
    try {
      const { fetchMock } = installMockFetch([
        {
          match: "/dispatch",
          method: "POST",
          response: { json: { ok: true, dispatched: true } },
        },
        ...modelRoutes("wan2.7-r2v"),
      ]);
      const deferred = deferDispatch(fetchMock);
      const base = fetchMock.getMockImplementation()!;
      let taskReads = 0;
      let releaseTasks!: () => void;
      const tasksPending = new Promise<void>((resolve) => {
        releaseTasks = resolve;
      });
      fetchMock.mockImplementation(async (input, init = {}) => {
        if (String(input).endsWith("/tasks")) {
          taskReads += 1;
          await tasksPending;
        }
        return base(input, init);
      });
      const { container, unmount } = renderWorkbench();
      await waitForWorkbenchReady(container);
      fireEvent.click(
        container.querySelector(
          '[data-stage-panel="sb"] [data-prompt-regenerate]',
        )!,
      );
      await waitFor(() => expect(deferred.pending()).toBe(true));
      await waitFor(() => expect(taskReads).toBe(1));
      expect(tick).toBeDefined();
      await act(async () => {
        tick!();
        tick!();
        tick!();
      });
      expect(taskReads).toBe(1);
      unmount();
      await act(async () => {
        releaseTasks();
        deferred.release();
        tick!();
        tick!();
        tick!();
      });
      expect(taskReads).toBe(1);
    } finally {
      interval.mockRestore();
    }
  });

  it("preserves a manual version choice made during save/sync preflight before dispatch starts", async () => {
    seedProject(withStoryboardVersions(1));
    const { fetchMock } = installMockFetch([
      {
        match: "/dispatch",
        method: "POST",
        response: { json: { ok: true, dispatched: true } },
      },
      ...modelRoutes("wan2.7-r2v"),
    ]);
    const deferred = deferDispatch(fetchMock);
    const original = fetchMock.getMockImplementation()!;
    let deferSync = false;
    let syncPending = false;
    let releaseSync!: () => void;
    const delayedSync = new Promise<void>((resolve) => {
      releaseSync = resolve;
    });
    fetchMock.mockImplementation(async (input, init = {}) => {
      if (String(input).includes("/prompt-sync") && deferSync) {
        deferSync = false;
        syncPending = true;
        await delayedSync;
      }
      return original(input, init);
    });
    const { container } = renderWorkbench();
    await waitForWorkbenchReady(container);
    deferSync = true;
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(syncPending).toBe(true));
    expect(deferred.pending()).toBe(false);
    fireEvent.click(
      container.querySelector(
        '[data-result-stage="storyboard"] [data-artifact-version="sb-v1"]',
      )!,
    );
    await act(async () => releaseSync());
    await waitFor(() => expect(deferred.pending()).toBe(true));
    await act(async () => {
      const project = withStoryboardVersions(2);
      project.generation = 4;
      useProjectSnapshotStore.setState({
        project,
        generation: 4,
        etag: '"g4"',
      });
      deferred.release();
    });
    await waitFor(() =>
      expect(
        container.querySelector(
          '[data-stage-panel="sb"] [data-prompt-regenerate]',
        ),
      ).toBeEnabled(),
    );
    expect(
      container.querySelector('[data-result-stage="storyboard"] img'),
    ).toHaveAttribute("src", "/api/qwenpaw-creator/media/artifacts/sb-v1");
    expect(
      container.querySelector(
        '[data-result-stage="storyboard"] [data-artifact-version="sb-v2"]',
      ),
    ).toBeInTheDocument();
  });

  it("omits every own-storyboard version from video materials while retaining another shot's explicit reference", async () => {
    const project = withStoryboardVersions(2);
    const ownSlot =
      project.assets.artifact_slots_by_id["element:r2v-window:storyboard"];
    ownSlot.selected_version_id = "sb-v2";
    project.assets.artifact_versions_by_id["other-sb"] = {
      ...project.assets.artifact_versions_by_id["sb-v1"],
      version_id: "other-sb",
      owner_ref: "element:other",
      slot_id: "element:other:storyboard",
      name: "另一镜头参考图",
    };
    const creation =
      project.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type === "r2v") {
      creation.video_reference_version_ids = [
        "sb-v1",
        "other-sb",
        "sb-v2",
        "cat-anchor-v1",
      ];
    }
    const before = JSON.stringify(creation);
    seedProject(project);
    const { calls } = installMockFetch(modelRoutes("wan2.7-r2v"));
    const { container } = renderWorkbench();
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    expect(
      container.querySelector('[data-material-version="sb-v1"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-material-version="sb-v2"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-material-version="other-sb"]'),
    ).toHaveTextContent("另一镜头参考图");
    fireEvent.click(container.querySelector("[data-add-asset]")!);
    await screen.findByRole("dialog");
    expect(screen.queryByText("分镜图 1")).toBeNull();
    expect(screen.queryByText("分镜图 2")).toBeNull();
    expect(
      JSON.stringify(
        useProjectSnapshotStore.getState().project?.timelines.items[
          "timeline:main"
        ].elements_by_id["r2v-window"].creation,
      ),
    ).toBe(before);
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });
  function syncScenario() {
    const current = useProjectSnapshotStore.getState().project!;
    const creation =
      current.timelines.items["timeline:main"].elements_by_id["r2v-window"]
        .creation;
    if (creation.type !== "r2v") throw new Error("fixture");
    const before: promptApi.PromptSyncState = {
      status: "needs_confirmation",
      baselineToken: "before",
      narrative: creation.narrative,
      storyboardPrompt: creation.storyboard_prompt,
      videoPrompt: creation.video_prompt,
      changedSources: ["storyboardPrompt"],
      suggestedSource: "storyboardPrompt",
    };
    const narrative = creation.narrative + "，尾巴缓缓放下";
    const proposal: promptApi.PromptProposal = {
      proposalId: "sync-1",
      baselineToken: "before",
      source: "storyboardPrompt",
      beforeNarrative: creation.narrative,
      narrative,
      beforeStoryboardPrompt: creation.storyboard_prompt,
      storyboardPrompt: creation.storyboard_prompt,
      beforeVideoPrompt: creation.video_prompt,
      videoPrompt: creation.video_prompt + "，尾巴缓缓放下",
    };
    let accepted = false;
    const { calls } = installMockFetch([
      {
        match: "/dispatch",
        method: "POST",
        response: { json: { dispatched: true } },
      },
      ...modelRoutes("wan2.7-r2v"),
    ]);
    vi.spyOn(promptApi, "getPromptSync").mockImplementation(async () =>
      accepted
        ? {
            ...before,
            ...proposal,
            status: "current",
            changedSources: [],
            suggestedSource: null,
          }
        : before,
    );
    const propose = vi
      .spyOn(promptApi, "createPromptProposal")
      .mockResolvedValue(proposal);
    const accept = vi
      .spyOn(promptApi, "acceptPromptProposal")
      .mockImplementation(async () => {
        accepted = true;
        const project = structuredClone(
          useProjectSnapshotStore.getState().project!,
        );
        Object.assign(
          project.timelines.items["timeline:main"].elements_by_id["r2v-window"]
            .creation,
          {
            narrative,
            storyboard_prompt: proposal.storyboardPrompt,
            video_prompt: proposal.videoPrompt,
          },
        );
        useProjectSnapshotStore.setState({ project, generation: 4 });
        return { ok: true, generation: 4 };
      });
    return { calls, propose, accept, proposal };
  }

  it("synchronizes the edited source and dispatches only the requested image in one click", async () => {
    const scenario = syncScenario();
    const { container } = renderWorkbench();
    await waitForWorkbenchReady(container);
    const button = container.querySelector(
      '[data-stage-panel="sb"] [data-prompt-regenerate]',
    )!;
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    await waitFor(() =>
      expect(
        scenario.calls.some((call) => call.url.includes("/dispatch")),
      ).toBe(true),
    );
    expect(scenario.propose).toHaveBeenCalledWith(
      { projectId: "p1", timelineId: "timeline:main", elementId: "r2v-window" },
      "storyboardPrompt",
    );
    expect(scenario.accept).toHaveBeenCalledTimes(1);
    const dispatch = scenario.calls.filter((call) =>
      call.url.includes("/dispatch"),
    );
    expect(dispatch).toHaveLength(1);
    expect(dispatch[0].url).toContain("storyboard%3Ar2v-window");
    const saved =
      useProjectSnapshotStore.getState().project!.timelines.items[
        "timeline:main"
      ].elements_by_id["r2v-window"].creation;
    expect(saved.type === "r2v" && saved.narrative).toEqual(
      scenario.proposal.narrative,
    );
    expect(saved.type === "r2v" && saved.video_prompt).toEqual(
      scenario.proposal.videoPrompt,
    );
    expect(container.querySelector("[data-prompt-sync-status]")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps generation available and shows an actionable error when synchronization fails", async () => {
    const scenario = syncScenario();
    scenario.propose.mockRejectedValue(new Error("请统一分镜格数后重新生成"));
    const { container } = renderWorkbench();
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() =>
      expect(container.querySelector(".ant-alert")).toHaveTextContent(
        "请统一分镜格数后重新生成",
      ),
    );
    expect(scenario.accept).not.toHaveBeenCalled();
    expect(scenario.calls.some((call) => call.url.includes("/dispatch"))).toBe(
      false,
    );
    expect(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      ),
    ).not.toBeDisabled();
  });

  it("does not accept or dispatch a proposal after another local prompt edit", async () => {
    const scenario = syncScenario();
    let finish!: (value: promptApi.PromptProposal) => void;
    scenario.propose.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { container } = renderWorkbench();
    fireEvent.click(
      container.querySelector(
        '[data-stage-panel="sb"] [data-prompt-regenerate]',
      )!,
    );
    await waitFor(() => expect(scenario.propose).toHaveBeenCalled());
    expect(screen.getByRole("status")).toHaveTextContent("正在准备生成");
    fireEvent.change(screen.getByDisplayValue("暖色餐厅窗外的橘猫"), {
      target: { value: "改为从右侧离开" },
    });
    await act(async () => finish(scenario.proposal));
    expect(scenario.accept).not.toHaveBeenCalled();
    expect(scenario.calls.some((call) => call.url.includes("/dispatch"))).toBe(
      false,
    );
    expect(screen.getByDisplayValue("改为从右侧离开")).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue("改为从右侧离开"), {
      target: { value: "暖色餐厅窗外的橘猫" },
    });
  });

  it("hides internal shots without removing prompt anchors or leaving a layout spacer", () => {
    const { container } = renderWorkbench();
    const prompt = container.querySelector('[data-workbench-pane="prompt"]');
    const storyboard = screen.getByDisplayValue("暖色餐厅窗外的橘猫");
    const video = screen.getByDisplayValue("镜头缓慢推近，橘猫眨眼");
    expect(container.querySelector('[data-workbench-pane="shots"]')).toBeNull();
    expect(
      container.querySelector(".r2v-plan-toggle, .r2v-plan-resize"),
    ).toBeNull();
    expect(screen.queryByDisplayValue("橘猫隔窗看向午饭")).toBeNull();
    expect(
      screen.queryByRole("separator", { name: "调整镜头栏宽度" }),
    ).toBeNull();
    expect(container.querySelector("[data-prompt-sync-status]")).toBeNull();
    fireEvent.click(container.querySelector('[data-stage-tab="vd"]')!);
    expect(container.querySelector('[data-workbench-pane="prompt"]')).toBe(
      prompt,
    );
    expect(screen.getByDisplayValue("暖色餐厅窗外的橘猫")).toBe(storyboard);
    expect(screen.getByDisplayValue("镜头缓慢推近，橘猫眨眼")).toBe(video);
  });
});
