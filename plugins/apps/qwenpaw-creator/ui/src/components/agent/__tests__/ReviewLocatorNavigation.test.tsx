import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfigProvider } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FileProjectReviewPanel from "@/components/agent/FileProjectReviewPanel";
import AssetsPage from "@/pages/AssetsPage";
import PlanPage from "@/pages/PlanPage";
import BlueprintPage from "@/pages/BlueprintPage";
import R2VWorkbenchPage from "@/pages/R2VWorkbenchPage";
import { NavigationRuntime } from "@/routing/navigation";
import { findCreatorFieldElement } from "@/routing/reviewFocus";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useNavigationStore } from "@/store/navigationStore";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import { projectDocument } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";

describe("real review View reaches the authored field", () => {
  beforeEach(() => {
    useProjectSnapshotStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    useNavigationStore.getState().clear();
    const project = structuredClone(projectDocument);
    project.timelines.items["timeline:main"].description =
      "开场，小狗跑入草地；随后轻快跳跃，最后回头望向镜头。";
    const cat = project.visual.entities.items.cat;
    const base = cat.variants.items[cat.variants.order[0]];
    cat.variants.items["variant:ungenerated"] = {
      ...base,
      variant_id: "variant:ungenerated",
      prompt: "尚未生成的背包造型",
      requirements: "背包固定在右肩",
      generated_artifact_version_ids: [],
      selected_artifact_version_id: null,
    };
    cat.variants.order.push("variant:ungenerated");
    useProjectSnapshotStore.setState({
      projectId: "p1",
      project,
      generation: project.generation,
      syncStatus: "healthy",
      etag: "test",
    });
    installMockFetch([
      {
        match: "/models/resolved",
        response: {
          json: { video: { provider: "wan", model: "wan3.0-video-prime" } },
        },
      },
    ]);
  });

  function renderReviews(
    fields: string[],
    values = { before: "修改前" as unknown, after: "修改后" as unknown },
    locators?: Record<string, string>[],
  ) {
    const review = makeReviewRecord({
      operations: fields.map((field, index) =>
        makeReviewOperation({
          operation_id: `field-${index}`,
          json_pointer: field,
          ui_locator: locators?.[index] ?? {
            page: "plan",
            field,
            mediaType: "text",
            ...(field.includes("elements_by_id")
              ? { elementId: "r2v-window" }
              : {}),
          },
          before: values.before,
          after: values.after,
        }),
      ),
    });
    useFileProjectReviewStore.setState({ projectId: "p1", reviews: [review] });
    return render(
      <ConfigProvider theme={{ token: { motion: false } }}>
        <MemoryRouter initialEntries={["/project/p1/plan"]}>
          <NavigationRuntime />
          <aside>
            <FileProjectReviewPanel projectId="p1" review={review} />
          </aside>
          <main data-creator-workspace-root>
            <Routes>
              <Route path="/project/:id" element={<BlueprintPage />} />
              <Route path="/project/:id/plan" element={<PlanPage />} />
              <Route
                path="/project/:id/t/:timelineId/plan"
                element={<PlanPage />}
              />
              <Route path="/project/:id/assets" element={<AssetsPage />} />
              <Route
                path="/project/:id/t/:timelineId/plan/element/:elementId"
                element={<R2VWorkbenchPage />}
              />
            </Routes>
          </main>
        </MemoryRouter>
      </ConfigProvider>,
    );
  }

  function viewField(container: HTMLElement, index: number) {
    fireEvent.click(
      within(
        container.querySelector(
          `[data-file-review-operation="field-${index}"]`,
        ) as HTMLElement,
      ).getByRole("button", { name: /^查看 / }),
    );
  }

  it.each(["title", "synopsis", "description"])(
    "opens the blueprint original text for %s review",
    async (name) => {
      const field = `/timelines/items/timeline:main/${name}`;
      const { container } = renderReviews([field]);
      viewField(container, 0);
      await waitFor(() =>
        expect(
          findCreatorFieldElement(field, container.querySelector("main")!),
        ).not.toBeNull(),
      );
      expect(useNavigationStore.getState().reviewFocus?.path).toBe(
        "/project/p1",
      );
      expect(useNavigationStore.getState().reviewFocus?.query.field).toBe(
        field,
      );
    },
  );

  it.each(["prompt", "requirements"])(
    "opens the exact ungenerated visual variant %s instead of the active variant or an empty page",
    async (leaf) => {
      const field = `/visual/entities/items/cat/variants/items/variant:ungenerated/${leaf}`;
      const { container } = renderReviews([field]);
      viewField(container, 0);
      await waitFor(() =>
        expect(
          findCreatorFieldElement(field, container.querySelector("main")!),
        ).not.toBeNull(),
      );
      const target = findCreatorFieldElement(
        field,
        container.querySelector("main")!,
      )!;
      expect(target.textContent).toContain(
        leaf === "prompt" ? "尚未生成的背包造型" : "背包固定在右肩",
      );
      expect(useCreatorInteractionStore.getState().selectedRef).toBe(
        "visual-variant:cat@variant:ungenerated",
      );
      expect(useNavigationStore.getState().reviewFocus?.query).toMatchObject({
        asset: "cat",
        variant: "variant:ungenerated",
        field,
      });
    },
  );

  it.each(["description", "continuity"])(
    "reveals the exact visual %s text rather than the variant card summary",
    async (leaf) => {
      const field = `/visual/entities/items/cat/${leaf}`;
      const { container } = renderReviews([field]);
      viewField(container, 0);
      await waitFor(() =>
        expect(
          findCreatorFieldElement(field, container.querySelector("main")!),
        ).not.toBeNull(),
      );
      expect(
        findCreatorFieldElement(field, container.querySelector("main")!)
          ?.textContent,
      ).toContain(
        projectDocument.visual.entities.items.cat[
          leaf as "description" | "continuity"
        ],
      );
    },
  );

  it("switches from video back to storyboard on repeated review views and preserves the exact field pointer", async () => {
    const base =
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation";
    const fields = [
      `${base}/video_prompt`,
      `${base}/storyboard_prompt`,
      `${base}/narrative`,
    ];
    const { container } = renderReviews(fields);
    for (const [index, field] of fields.entries()) {
      viewField(container, index);
      await waitFor(() =>
        expect(
          findCreatorFieldElement(field, container.querySelector("main")!),
        ).not.toBeNull(),
      );
      expect(useNavigationStore.getState().reviewFocus?.path).toBe(
        "/project/p1/t/timeline%3Amain/plan/element/r2v-window",
      );
      expect(useNavigationStore.getState().reviewFocus?.query.field).toBe(
        field,
      );
      await waitFor(() =>
        expect(
          container.querySelector(
            `[data-stage-panel="${index === 0 ? "vd" : "sb"}"]`,
          ),
        ).not.toHaveAttribute("hidden"),
      );
      if (index === 2) {
        expect(
          container.querySelector("details.r2v-narrative"),
        ).toHaveAttribute("open");
        expect(container.querySelector("[data-legacy-shot-review]")).toBeNull();
      }
    }
  });

  it("opens narrative review in the workbench and opens the selected element's real text field", async () => {
    const field =
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/narrative";
    const { container } = renderReviews([field]);
    viewField(container, 0);
    await waitFor(() =>
      expect(
        findCreatorFieldElement(field, container.querySelector("main")!),
      ).not.toBeNull(),
    );
    expect(useNavigationStore.getState().reviewFocus?.path).toBe(
      "/project/p1/t/timeline%3Amain/plan/element/r2v-window",
    );
  });

  it("opens the storyboard editor during review and preserves its local draft through snapshot refreshes", async () => {
    const field =
      "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt";
    const { container } = renderReviews([field]);
    viewField(container, 0);
    const edit = await waitFor(() => {
      const button = container.querySelector<HTMLButtonElement>(
        '[data-prompt-edit="element:r2v-window/creation/storyboard_prompt"]',
      );
      expect(button).not.toBeNull();
      expect(button).toBeEnabled();
      return button!;
    });
    fireEvent.click(edit);
    const dialog = await screen.findByRole("dialog");
    const editor = within(dialog).getByRole("textbox");
    await waitFor(() => expect(editor).toBeVisible());
    fireEvent.input(editor, {
      target: { textContent: "保持左手持钥匙，右手保持放松，不碰门把手。" },
    });

    // The backend poll publishes a new object while the review URL/pulse is
    // unchanged. It must not close/reinitialize the user's uncommitted editor.
    act(() => {
      const current = useProjectSnapshotStore.getState();
      useProjectSnapshotStore.setState({
        project: structuredClone(current.project),
        generation: (current.generation ?? 0) + 1,
      });
    });
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(editor).toHaveTextContent(
      "保持左手持钥匙，右手保持放松，不碰门把手。",
    );
    expect(
      useFileProjectReviewStore.getState().reviews[0].operations[0].decision,
    ).toBe("PENDING");
    fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("opens and scrolls the exact video review result after switching stages, including repeated View", async () => {
    const project = structuredClone(
      useProjectSnapshotStore.getState().project!,
    );
    const versionId = "awaiting-video-version";
    project.assets.artifact_versions_by_id[versionId] = {
      ...project.assets.artifact_versions_by_id["r2v-window-v1"],
      version_id: versionId,
    };
    project.assets.artifact_slots_by_id[
      "element:r2v-window:video"
    ].version_ids.push(versionId);
    useProjectSnapshotStore.setState({ project });
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    const { container } = renderReviews(
      [`/assets/artifact_versions_by_id/${versionId}`],
      { before: null, after: {} },
      [
        {
          page: "element",
          elementId: "r2v-window",
          versionId,
          mediaType: "video",
        },
      ],
    );
    const view = within(container.querySelector("aside")!).getByRole("button", {
      name: "查看生成详情",
    });
    fireEvent.click(view);
    const videoStage = await waitFor(() => {
      const panel = container.querySelector<HTMLElement>(
        '[data-stage-panel="vd"]',
      );
      expect(panel).not.toBeNull();
      expect(panel).not.toHaveAttribute("hidden");
      return panel!;
    });
    const anchor = container.querySelector<HTMLElement>(
      `main [data-review-media-anchor="${versionId}"]`,
    )!;
    expect(anchor.querySelector("video")).toHaveAttribute(
      "src",
      `/api/qwenpaw-creator/media/artifacts/${versionId}`,
    );
    await waitFor(() => expect(scroll.mock.contexts).toContain(anchor));
    expect(anchor).toHaveClass("review-flash");

    fireEvent.click(container.querySelector('[data-stage-tab="sb"]')!);
    act(() =>
      useProjectSnapshotStore.setState({ project: structuredClone(project) }),
    );
    expect(videoStage).toHaveAttribute("hidden");
    scroll.mockClear();
    fireEvent.click(view);
    await waitFor(() => expect(videoStage).not.toHaveAttribute("hidden"));
    await waitFor(() => expect(scroll.mock.contexts).toContain(anchor));
    expect(
      useFileProjectReviewStore.getState().reviews[0].operations[0].decision,
    ).toBe("PENDING");
  });

  it.each([
    ["storyboard_reference_version_ids", "sb"],
    ["video_reference_version_ids", "vd"],
  ])(
    "opens %s in its visible workbench section and compares actual media instead of arrays",
    async (leaf, stage) => {
      const field = `/timelines/items/timeline:main/elements_by_id/r2v-window/creation/${leaf}`;
      const { container } = renderReviews([field], {
        before: [],
        after: ["cat-anchor-v1", "private-unknown-reference"],
      });
      viewField(container, 0);
      await waitFor(() =>
        expect(
          container.querySelector(`[data-stage-panel="${stage}"]`),
        ).not.toHaveAttribute("hidden"),
      );
      const target = findCreatorFieldElement(
        field,
        container.querySelector("main")!,
      )!;
      expect(target).not.toBeNull();
      expect(
        target.querySelector('[data-reference-comparison="before"]'),
      ).toHaveTextContent("未指定参考图");
      expect(
        target.querySelector('[data-reference-comparison="after"] img'),
      ).toHaveAttribute(
        "src",
        "/api/qwenpaw-creator/media/artifacts/cat-anchor-v1",
      );
      expect(target).toHaveTextContent("橘猫角色锚点");
      expect(target.textContent).not.toMatch(
        /cat-anchor-v1|private-unknown-reference|reference_version_ids/,
      );
      expect(
        target.querySelector('[data-review-inline-diff="field-0"]'),
      ).not.toBeNull();
      expect(useNavigationStore.getState().reviewFocus?.path).toBe(
        "/project/p1/t/timeline%3Amain/plan/element/r2v-window",
      );
    },
  );
});
