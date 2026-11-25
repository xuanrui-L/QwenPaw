import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { afterEach, describe, expect, it, vi } from "vitest";
import FileProjectReviewPanel, {
  reviewPendingUnits,
} from "@/components/agent/FileProjectReviewPanel";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import InlineReviewDiff, { matchReviewOperations } from "../InlineReviewDiff";
import type {
  FileProjectReviewOperation,
  FileProjectReviewRecord,
} from "@/contracts/creator";
import DiffView from "@/components/agent/DiffView";

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

    // F1/F2: keeping every change is one of the four primary decision buttons.
    const keepAll = document.querySelector("[data-file-review-keep-all]");
    expect(keepAll).toHaveClass("btn-decision");
    expect(keepAll).toHaveTextContent("全部保留");

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

  it("warns instead of reporting success when the redo follow-up is deferred", async () => {
    // The decision commits durably and the endpoint returns 200, but a deferred
    // follow-up means the regeneration has not actually started. Reporting plain
    // success here is exactly what made "undo & redo" look like it did nothing.
    const value = review();
    const decide = vi.fn(async () => ({ ...value, followupDeferred: true }));
    setup(value, decide);
    const warnSpy = vi
      .spyOn(message, "warning")
      .mockImplementation(() => (() => {}) as never);
    const successSpy = vi
      .spyOn(message, "success")
      .mockImplementation(() => (() => {}) as never);

    fireEvent.click(
      screen.getByRole("button", { name: "撤销 当前项目 · 描述" }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "反馈与调整要求" }), {
      target: { value: "人物状态不对" },
    });
    fireEvent.click(screen.getByRole("button", { name: "撤销并重做" }));

    await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(warnSpy).toHaveBeenCalledTimes(1));
    expect(successSpy).not.toHaveBeenCalled();

    warnSpy.mockRestore();
    successSpy.mockRestore();
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

describe("Inline review diffs", () => {
  const operation = (overrides: Partial<FileProjectReviewOperation>) =>
    makeReviewOperation({
      json_pointer: "/strategy/creative_brief",
      before: "旧文案",
      after: "新文案",
      ui_locator: { page: "plan", mediaType: "text" },
      ...overrides,
    });

  const review = (
    operations: FileProjectReviewOperation[],
    overrides: Partial<FileProjectReviewRecord> = {},
  ) => makeReviewRecord({ operations, ...overrides });

  // One replaced Element object shared by both ancestor-slice tests.
  const elementReplaced = review([
    operation({
      json_pointer: "/timelines/items/0/elements_by_id/element-1",
      before: { title: "旧标题", description: "同样" },
      after: { title: "新标题", description: "同样" },
    }),
  ]);

  afterEach(() => {
    useFileProjectReviewStore.getState().reset();
  });

  describe("matchReviewOperations", () => {
    it("matches an exact pointer and ignores unrelated ones", () => {
      const matches = matchReviewOperations(
        [review([operation({})])],
        "/strategy/creative_brief",
      );
      expect(matches).toHaveLength(1);
      expect(matches[0].relation).toBe("exact");
      expect(matches[0].before).toBe("旧文案");
      expect(matches[0].after).toBe("新文案");
      expect(
        matchReviewOperations(
          [
            review([
              operation({ json_pointer: "/strategy/creative_direction" }),
            ]),
          ],
          "/strategy/creative_brief",
        ),
      ).toHaveLength(0);
    });

    it("extracts only the changed field slice when an ancestor object was replaced", () => {
      const matches = matchReviewOperations(
        [elementReplaced],
        "/timelines/items/0/elements_by_id/element-1/title",
      );
      expect(matches).toHaveLength(1);
      expect(matches[0].relation).toBe("ancestor");
      expect(matches[0].before).toBe("旧标题");
      expect(matches[0].after).toBe("新标题");
      // The untouched sibling field yields no match.
      expect(
        matchReviewOperations(
          [elementReplaced],
          "/timelines/items/0/elements_by_id/element-1/description",
        ),
      ).toHaveLength(0);
    });

    it("reports descendant operations with their sub path", () => {
      const matches = matchReviewOperations(
        [
          review([
            operation({
              json_pointer:
                "/timelines/items/0/elements_by_id/element-1/shots/items/shot-1/description",
            }),
          ]),
        ],
        "/timelines/items/0/elements_by_id/element-1",
      );
      expect(matches).toHaveLength(1);
      expect(matches[0].relation).toBe("descendant");
      expect(matches[0].subPath).toBe("/shots/items/shot-1/description");
    });

    it("ignores resolved reviews and decided operations", () => {
      const matches = matchReviewOperations(
        [
          review([operation({ decision: "ACCEPTED" })]),
          review([operation({})], {
            review_id: "review-2",
            status: "RESOLVED",
          }),
        ],
        "/strategy/creative_brief",
      );
      expect(matches).toHaveLength(0);
    });

    it("collects rejection intent before submitting an inline undo", async () => {
      const current = review([operation({})]);
      const decide = vi.fn(async () => current);
      useFileProjectReviewStore.setState({
        projectId: "project-1",
        reviews: [current],
        decisionInFlight: false,
        decide,
      });
      render(<InlineReviewDiff pointer="/strategy/creative_brief" />);

      fireEvent.click(screen.getByRole("button", { name: "撤销该修改" }));
      expect(decide).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "仅撤销" }));

      await waitFor(() =>
        expect(decide).toHaveBeenCalledWith(
          "project-1",
          "review-1",
          [{ operation_id: "operation-1", decision: "REJECT" }],
          { action: "UNDO_ONLY" },
        ),
      );
    });

    it("warns instead of reporting success when the follow-up is deferred", async () => {
      // The decision commits durably (HTTP 200), but a deferred follow-up means
      // the undo/redo was not actually launched. Plain success here is exactly the
      // "undo & redo did nothing" false positive this inline panel must not repeat.
      const current = review([operation({})]);
      const decide = vi.fn(async () => ({
        ...current,
        followupDeferred: true,
      }));
      useFileProjectReviewStore.setState({
        projectId: "project-1",
        reviews: [current],
        decisionInFlight: false,
        decide,
      });
      const warnSpy = vi
        .spyOn(message, "warning")
        .mockImplementation(() => (() => {}) as never);
      const successSpy = vi
        .spyOn(message, "success")
        .mockImplementation(() => (() => {}) as never);
      render(<InlineReviewDiff pointer="/strategy/creative_brief" />);

      fireEvent.click(screen.getByRole("button", { name: "撤销该修改" }));
      fireEvent.click(screen.getByRole("button", { name: "仅撤销" }));

      await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(warnSpy).toHaveBeenCalledTimes(1));
      expect(successSpy).not.toHaveBeenCalled();

      warnSpy.mockRestore();
      successSpy.mockRestore();
    });
  });

  describe("DiffView", () => {
    it("renders removed and added lines for text changes", () => {
      render(<DiffView before={"alpha\nbeta"} after={"alpha\ngamma"} />);
      expect(screen.getByText("beta")).toBeInTheDocument();
      expect(screen.getByText("gamma")).toBeInTheDocument();
      expect(document.querySelector('[data-diff-kind="removed"]')).toBeTruthy();
      expect(document.querySelector('[data-diff-kind="added"]')).toBeTruthy();
    });

    it("shows an empty-state message when both sides are empty", () => {
      render(<DiffView before={null} after={null} />);
      expect(screen.getByText("（无内容变化）")).toBeInTheDocument();
    });
  });
});
