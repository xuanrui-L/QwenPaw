import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import FileProjectReviewPanel, {
  reviewPendingUnits,
  reviewTrayLabel,
} from "@/components/agent/FileProjectReviewPanel";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import type { ProjectDocument } from "@/contracts/creator";

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
  it("requires no human action or pending count for automatic snapshot history only", () => {
    const value = makeReviewRecord({
      operations: [
        makeReviewOperation({
          operation_id: "snapshot-record",
          kind: "create",
          json_pointer: "/timelines/items/snapshot:timeline:main:8",
          before: null,
          after: {
            timeline_id: "snapshot:timeline:main:8",
            description: "自动快照：修改前的时间轴副本",
          },
        }),
        makeReviewOperation({
          operation_id: "history-order",
          kind: "reorder",
          json_pointer: "/timelines/order",
          before: ["timeline:main"],
          after: ["timeline:main", "snapshot:timeline:main:8"],
        }),
      ],
    });
    const decide = setup(value);
    expect(reviewPendingUnits(value)).toBe(0);
    expect(document.querySelector("[data-file-project-review]")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(decide).not.toHaveBeenCalled();
  });

  it("keeps genuine live ordering and content decisions while omitting history from Keep All", async () => {
    const content = makeReviewOperation({
      operation_id: "content",
      json_pointer: "/description",
      before: "原文",
      after: "新文",
    });
    const liveOrder = makeReviewOperation({
      operation_id: "live-order",
      kind: "reorder",
      json_pointer: "/timelines/order",
      before: ["timeline:main", "timeline:second"],
      after: ["timeline:second", "timeline:main", "snapshot:timeline:main:8"],
    });
    const value = makeReviewRecord({
      operations: [
        content,
        liveOrder,
        makeReviewOperation({
          operation_id: "snapshot-record",
          kind: "create",
          json_pointer: "/timelines/items/snapshot:timeline:main:8",
          before: null,
          after: {
            timeline_id: "snapshot:timeline:main:8",
            description: "自动快照：修改前的时间轴副本",
          },
        }),
      ],
    });
    const decide = setup(value);
    expect(reviewPendingUnits(value)).toBe(2);
    expect(reviewTrayLabel(value)).toContain("2");
    expect(
      document.querySelector('[data-file-review-operation="live-order"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-file-review-operation="content"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-file-review-operation="snapshot-record"]'),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "全部保留" }));
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith("p1", "review-1", [
        { operation_id: "content", decision: "ACCEPT" },
        { operation_id: "live-order", decision: "ACCEPT" },
      ]),
    );
  });

  it("keeps real review labels and accessible names public while retaining exact navigation and decisions", async () => {
    useProjectSnapshotStore.setState({
      project: {
        timelines: {
          items: {
            "timeline:main": {
              title: "色彩练习",
              elements_by_id: { "seg:0-3": { label: "色彩启幕" } },
            },
          },
        },
      } as unknown as ProjectDocument,
    });
    const pointer =
      "/timelines/items/timeline:main/elements_by_id/seg:0-3/label";
    const value = makeReviewRecord({
      operations: [
        makeReviewOperation({
          operation_id: "rename",
          json_pointer: pointer,
          ui_locator: { page: "plan", elementId: "seg:0-3", field: pointer },
          before: "色彩练习 · 前半段",
          after: "色彩启幕",
        }),
        makeReviewOperation({
          operation_id: "order",
          kind: "reorder",
          json_pointer: "/timelines/order",
          before: ["timeline:main"],
          after: ["timeline:main", "snapshot:timeline:main:1"],
        }),
        makeReviewOperation({
          operation_id: "snapshot",
          kind: "create",
          json_pointer: "/timelines/items/snapshot:timeline:main:1",
          before: null,
          after: {
            timeline_id: "snapshot:timeline:main:1",
            description: "自动快照：修改前的时间轴副本",
            title: "色彩练习",
            color_grade: "none",
            elements_by_id: {},
          },
        }),
      ],
    });
    const decide = setup(value);
    expect(screen.getByText("色彩启幕 · 名称")).toBeInTheDocument();
    expect(screen.queryByText("版本记录已更新")).toBeNull();
    expect(screen.queryByText("已保存修改前版本")).toBeNull();
    expect(reviewPendingUnits(value)).toBe(1);
    expect(screen.getAllByRole("button", { name: /^查看 / })).toHaveLength(1);
    const visible =
      document.body.textContent +
      Array.from(document.querySelectorAll("[title], [aria-label]"))
        .map(
          (node) =>
            `${node.getAttribute("title")} ${node.getAttribute("aria-label")}`,
        )
        .join(" ");
    expect(visible).not.toMatch(
      /\/timelines|snapshot:|color_grade|elements_by_id|seg:0-3|\blabel\b|\border\b/u,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "查看 色彩启幕 · 名称" }),
    );
    expect(navigateToLocator).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ field: pointer }),
      expect.objectContaining({ field: pointer, review: true }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "保留 色彩启幕 · 名称" }),
    );
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith("p1", "review-1", [
        { operation_id: "rename", decision: "ACCEPT" },
      ]),
    );
  });

  it("never prints a deleted object or a raw synchronization error", () => {
    const value = makeReviewRecord({
      operations: [
        makeReviewOperation({
          kind: "delete",
          json_pointer: "/timelines/items/snapshot:private:1",
          before: { title: "备选方案", elements_by_id: { hidden: {} } },
          after: null,
        }),
      ],
    });
    setup(
      value,
      vi.fn(async () => value),
      "Provider rejected /tmp/private token=hidden",
    );
    expect(document.body).toHaveTextContent("已删除版本记录");
    expect(document.querySelector("[data-review-diff]")).toBeNull();
    expect(document.body.textContent).not.toMatch(/snapshot:|elements_by_id/u);
    expect(
      screen.getByText("暂时无法更新审阅状态，请稍后重试。"),
    ).toHaveAttribute("role", "alert");
    expect(document.body.textContent).not.toMatch(
      /Provider|\/tmp|token=hidden/u,
    );
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
});
