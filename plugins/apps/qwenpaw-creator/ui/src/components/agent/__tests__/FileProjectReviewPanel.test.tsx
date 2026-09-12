import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import FileProjectReviewPanel, {
  reviewPendingUnits,
} from "@/components/agent/FileProjectReviewPanel";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";

const navigateToLocator = vi.fn();

vi.mock("@/routing/locators", () => ({
  navigateToLocator: (...args: unknown[]) => navigateToLocator(...args),
}));

vi.mock("@/api/creator", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/creator")>();
  return {
    ...actual,
    getArtifactVersionMediaUrl: (versionId: string) =>
      `https://media.test/${versionId}`,
  };
});

const review = (operationCount = 1) =>
  makeReviewRecord({
    operations: Array.from({ length: operationCount }, (_, index) => {
      const pointer = index === 0 ? "/description" : `/story/scenes/${index}`;
      return makeReviewOperation({
        json_pointer: pointer,
        before: index === 0 ? "Old title" : { index, enabled: false },
        after: index === 0 ? "New title" : { index, enabled: true },
        operation_id: `operation-${index + 1}`,
        ui_locator: { page: "plan", mediaType: "text", field: pointer },
      });
    }),
  });

const mediaReview = () =>
  makeReviewRecord({
    operations: [
      makeReviewOperation({
        json_pointer:
          "/assets/artifact_slots_by_id/element:el-1:main/selected_version_id",
        before: "ver-old",
        after: "ver-new",
        operation_id: "operation-media",
        ui_locator: {
          page: "element",
          elementId: "el-1",
          mediaType: "video",
          artifactKind: "r2v_video",
          artifactVersionId: "ver-new",
        },
      }),
    ],
  });

/** Seeds the review store and renders the panel; returns the decide spy. */
function setup(
  value = review(),
  decide = vi.fn(async () => value),
  syncError: string | null = null,
) {
  useFileProjectReviewStore.setState({
    projectId: "p1",
    reviews: [value],
    etag: '"token-1"',
    syncStatus: "healthy",
    syncError,
    decisionInFlight: false,
    decide,
  });
  render(<FileProjectReviewPanel projectId="p1" review={value} />);
  return decide;
}

afterEach(() => {
  useFileProjectReviewStore.getState().reset();
  navigateToLocator.mockClear();
  useProjectSnapshotStore.getState().reset();
});

describe("FileProjectReviewPanel", () => {
  it.each(["ACCEPT", "REJECT"] as const)(
    "decides generated HTML and provenance together on %s without including other edits",
    async (decision) => {
      const pointer = "/interactive_presentation/motion";
      const value = makeReviewRecord({
        operations: [
          makeReviewOperation({
            operation_id: "html",
            json_pointer: `${pointer}/html`,
            before: "old HTML",
            after: "new HTML",
            ui_locator: { page: "blueprint", field: `${pointer}/html` },
          }),
          makeReviewOperation({
            operation_id: "provenance",
            json_pointer: `${pointer}/design_notes`,
            before: "old fingerprint",
            after: "new fingerprint",
          }),
          makeReviewOperation({
            operation_id: "description",
            json_pointer: "/description",
            before: "Old story",
            after: "New story",
          }),
        ],
      });
      const decide = setup(value);
      expect(reviewPendingUnits(value)).toBe(2);
      expect(screen.getAllByTitle("作品页面 · 界面效果")).toHaveLength(1);
      fireEvent.click(
        screen.getByRole("button", {
          name: `${
            decision === "ACCEPT" ? "保留" : "撤销"
          } 作品页面 · 界面效果`,
        }),
      );
      if (decision === "REJECT") {
        expect(screen.getByRole("dialog")).toHaveTextContent("将撤销 1 项内容");
        fireEvent.click(screen.getByRole("button", { name: "仅撤销" }));
      }
      await waitFor(() => expect(decide).toHaveBeenCalled());
      expect(decide.mock.calls[0].slice(0, 3)).toEqual([
        "p1",
        "review-1",
        [
          { operation_id: "html", decision },
          { operation_id: "provenance", decision },
        ],
      ]);
    },
  );

  it.each([
    ["/interactive_presentation/motion", "作品页面 · 界面效果"],
    [
      "/timelines/items/timeline:main/elements_by_id/el-1/creation/motion",
      "月台抉择 · 抉择动效",
    ],
  ])(
    "identifies generated interfaces without exposing HTML: %s",
    (pointer, title) => {
      const project = structuredClone(projectDocument);
      const timeline = project.timelines.items["timeline:main"];
      timeline.elements_by_id["el-1"] = {
        ...timeline.elements_by_id["r2v-window"],
        element_id: "el-1",
        label: "月台抉择",
        creation: {
          type: "interaction",
          question: "向哪边走？",
          design_prompt: "两张车票作为选择按钮",
          options: [{ edge_ref: "edge:left" }, { edge_ref: "edge:right" }],
        },
      };
      useProjectSnapshotStore.setState({ projectId: "p1", project });
      setup(
        makeReviewRecord({
          operations: [
            makeReviewOperation({
              json_pointer: pointer,
              after: {
                html: '<html><button data-action="start">Start</button></html>',
              },
              ui_locator: {
                page: "blueprint",
                field: pointer,
                mediaType: "text",
              },
            }),
          ],
        }),
      );
      expect(screen.getByTitle(title)).toBeInTheDocument();
      expect(
        screen.getByText("界面效果已更新，点击查看预览"),
      ).toBeInTheDocument();
      expect(document.body.textContent).not.toContain("data-action");
      fireEvent.click(screen.getByRole("button", { name: `查看 ${title}` }));
      expect(navigateToLocator).toHaveBeenCalledWith(
        "p1",
        expect.objectContaining({ field: pointer }),
        expect.objectContaining({ review: true, field: pointer }),
      );
    },
  );

  it("shows the authored native-audio change in readable review text", () => {
    setup(
      makeReviewRecord({
        operations: [
          makeReviewOperation({
            json_pointer:
              "/timelines/items/timeline:main/elements_by_id/shot/creation/generate_audio",
            before: true,
            after: false,
          }),
        ],
      }),
    );
    expect(screen.getByText("有声 → 无声")).toBeInTheDocument();
    expect(screen.queryByText("内容已更新")).toBeNull();
  });

  it("renders a text summary and navigates to the ui_locator on inspect", () => {
    setup();
    expect(screen.getByText("创作修改")).toBeInTheDocument();
    // Text changes render only a summary; the full diff shows via "查看".
    expect(screen.getByTitle("当前项目 · 描述")).toBeInTheDocument();
    expect(screen.queryByTitle("/description")).not.toBeInTheDocument();
    expect(document.querySelector("[data-review-diff]")).toBeNull();
    expect(screen.queryByText("New title")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "查看 当前项目 · 描述" }),
    );
    expect(navigateToLocator).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ field: "/description" }),
      expect.objectContaining({ review: true, field: "/description" }),
    );
  });

  it("submits an individual Keep decision by operation_id", async () => {
    const decide = setup();

    fireEvent.click(
      screen.getByRole("button", { name: "保留 当前项目 · 描述" }),
    );
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith("p1", "review-1", [
        { operation_id: "operation-1", decision: "ACCEPT" },
      ]),
    );
  });

  it("opens feedback before undoing all pending operations", async () => {
    const decide = setup(review(2));

    fireEvent.click(screen.getByRole("button", { name: "全部撤销" }));
    expect(decide).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toHaveTextContent("撤销 2 项内容");
    fireEvent.click(screen.getByRole("button", { name: "仅撤销" }));
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith(
        "p1",
        "review-1",
        [
          { operation_id: "operation-1", decision: "REJECT" },
          { operation_id: "operation-2", decision: "REJECT" },
        ],
        { action: "UNDO_ONLY" },
      ),
    );
  });

  it("submits structured feedback when undo and regenerate is selected", async () => {
    const decide = setup();

    fireEvent.click(
      screen.getByRole("button", { name: "撤销 当前项目 · 描述" }),
    );
    const feedback = screen.getByRole("textbox", { name: "反馈与调整要求" });
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByRole("textbox", { name: "哪里不对" })).toBeNull();
    fireEvent.change(feedback, {
      target: { value: "人物状态不对；保持身份一致，改成落魄时期" },
    });
    const regenerate = screen.getByRole("button", { name: "撤销并重做" });
    fireEvent.click(regenerate);
    fireEvent.click(regenerate);

    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith(
        "p1",
        "review-1",
        [{ operation_id: "operation-1", decision: "REJECT" }],
        {
          action: "UNDO_AND_REGENERATE",
          feedbackNote: "人物状态不对；保持身份一致，改成落魄时期",
        },
      ),
    );
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it("renders a media preview and opens the generation detail locator", () => {
    setup(mediaReview());
    expect(screen.getByText("视频审阅")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看生成详情" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看生成详情" }));
    expect(navigateToLocator).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({
        page: "element",
        elementId: "el-1",
        artifactVersionId: "ver-new",
      }),
      expect.objectContaining({ review: true }),
    );
  });

  it("keeps authoring changes visible when their old storyboard becomes stale", () => {
    const value = review();
    value.operations.push(
      makeReviewOperation({
        operation_id: "stale-storyboard",
        json_pointer: "/assets/artifact_versions_by_id/ver-old/stale",
        before: false,
        after: true,
        ui_locator: {
          page: "element",
          elementId: "el-1",
          mediaType: "image",
          artifactKind: "r2v_storyboard_image",
          artifactVersionId: "ver-old",
        },
      }),
    );
    setup(value);
    expect(reviewPendingUnits(value)).toBe(2);
    expect(screen.getByText("创作修改")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查看 当前项目 · 描述" }),
    ).toBeVisible();
    expect(screen.queryByText("分镜图审阅")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看生成详情" }),
    ).not.toBeInTheDocument();
  });
});
