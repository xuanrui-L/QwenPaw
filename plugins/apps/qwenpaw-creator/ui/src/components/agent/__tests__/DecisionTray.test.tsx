import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DecisionTray from "@/components/agent/DecisionTray";
import type { FileProjectReviewRecord } from "@/contracts/creator";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { installMockFetch } from "@/test/mockFetch";
import {
  makePendingAuthorization,
  makeReviewOperation,
  makeReviewRecord,
} from "@/test/agentFixtures";

vi.mock("@/routing/locators", () => ({
  navigateToLocator: vi.fn(),
}));

vi.mock("@/api/creator", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/creator")>();
  return {
    ...actual,
    getArtifactVersionMediaUrl: (versionId: string) =>
      `https://media.test/${versionId}`,
  };
});

const pendingAuthorization = makePendingAuthorization({
  scope: { operation: "image_generation", message: "生成开场分镜图" },
});

const textReview = (reviewId: string, pointer: string) =>
  makeReviewRecord({
    review_id: reviewId,
    round_id: `round-${reviewId}`,
    decision_token: `token-${reviewId}`,
    operations: [
      makeReviewOperation({
        json_pointer: pointer,
        before: "旧文案",
        after: "新文案",
        operation_id: `operation-${reviewId}`,
        ui_locator: { page: "plan", mediaType: "text", field: pointer },
      }),
    ],
  });

function seed({
  authorizations = [pendingAuthorization],
  reviews = [] as FileProjectReviewRecord[],
} = {}) {
  useExecutionAuthorizationStore.setState({
    projectId: "p1",
    items: authorizations,
    error: null,
  });
  useFileProjectReviewStore.setState({
    projectId: "p1",
    reviews,
    decisionInFlight: false,
  });
}

afterEach(() => {
  useAgentDockUiStore.getState().reset();
  useExecutionAuthorizationStore.getState().reset();
  useFileProjectReviewStore.getState().reset();
});

describe("DecisionTray", () => {
  it("focuses the blocking authorization first, then steps to the review", () => {
    seed({ reviews: [textReview("review-1", "/description")] });
    render(<DecisionTray projectId="p1" />);

    // Blocking item focused first: only the confirmation card is expanded.
    expect(screen.getByText(/生成画面/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创作修改 · 1 处" }),
    ).toHaveAttribute("title", expect.stringContaining("下一条"));
    expect(screen.queryByText(/旧文案/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "折叠决策托盘" }));
    expect(document.querySelector("[data-decision-tray]")).toHaveAttribute(
      "data-decision-tray-urgent",
      "true",
    );
    expect(screen.getByText(/生产确认 1 项阻塞执行中/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开决策托盘" }));

    fireEvent.click(screen.getByRole("button", { name: "下一条决策" }));
    expect(screen.getByText(/旧文案/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一条决策" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一条决策" })).toBeEnabled();
  });

  it.each([
    { action: "approve", label: "继续", status: "APPROVED" },
    { action: "decline", label: "取消", status: "DECLINED" },
  ])(
    "submits the exact authorization token on $action and removes the resolved tray",
    async ({ action, label, status }) => {
      const pending = makePendingAuthorization({
        targetRef: "element:r2v-window",
        model: "wan2.7-r2v",
        maxCandidates: 2,
        scope: {
          operation: "r2v_generation",
          message: "private execution details",
        },
      });
      seed({ authorizations: [pending] });
      const { calls } = installMockFetch([
        {
          match: `/projects/p1/execution-authorizations/authorization-1/${action}`,
          response: { json: { ...pending, status } },
        },
      ]);
      render(<DecisionTray projectId="p1" />);
      expect(screen.getByText("生成视频等待确认")).toBeInTheDocument();
      expect(
        screen.queryByText("private execution details"),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() =>
        expect(
          document.querySelector("[data-decision-tray]"),
        ).not.toBeInTheDocument(),
      );
      expect(calls).toHaveLength(1);
      expect(calls[0].body).toEqual(
        action === "approve"
          ? {
              authorizationToken: "token-1",
              provider: "dashscope",
              model: "wan2.7-r2v",
              maxCost: 0,
              maxCandidates: 2,
            }
          : { authorizationToken: "token-1" },
      );
    },
  );

  it("shows every pending card in list mode and filters by decision kind", () => {
    seed({
      reviews: [
        textReview("review-1", "/description"),
        textReview("review-2", "/title"),
      ],
    });
    render(<DecisionTray projectId="p1" />);

    fireEvent.click(screen.getByRole("button", { name: /列表/ }));
    expect(screen.getByText(/生成画面/)).toBeInTheDocument();
    expect(screen.getAllByText(/旧文案/)).toHaveLength(2);
    expect(screen.getByRole("button", { name: /堆叠/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^审阅 2$/ }));
    expect(screen.queryByText(/生成画面/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/旧文案/)).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: /^全部 3$/ }));
    expect(screen.getByText(/生成画面/)).toBeInTheDocument();
  });

  it("omits a legacy pending review containing only automatic version bookkeeping", () => {
    const value = makeReviewRecord({
      operations: [
        makeReviewOperation({
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
    seed({ authorizations: [], reviews: [value] });
    const decide = vi.fn();
    useFileProjectReviewStore.setState({ decide });
    const { container } = render(<DecisionTray projectId="p1" />);
    expect(container).toBeEmptyDOMElement();
    expect(decide).not.toHaveBeenCalled();
  });

  it("batch keeps only human content from mixed records and leaves system reconciliation to the backend", async () => {
    const first = textReview("review-1", "/description");
    first.operations.push(
      makeReviewOperation({
        operation_id: "history",
        kind: "reorder",
        json_pointer: "/timelines/order",
        before: ["timeline:main"],
        after: ["timeline:main", "snapshot:timeline:main:8"],
      }),
    );
    const second = textReview("review-2", "/title");
    seed({ authorizations: [], reviews: [first, second] });
    const decide = vi.fn(async () => first);
    useFileProjectReviewStore.setState({ decide });
    render(<DecisionTray projectId="p1" />);
    fireEvent.click(
      screen.getByTitle("保留全部审阅修改（生产确认需单独决定）"),
    );
    await waitFor(() => expect(decide).toHaveBeenCalledTimes(2));
    expect(decide).toHaveBeenNthCalledWith(1, "p1", "review-1", [
      { operation_id: "operation-review-1", decision: "ACCEPT" },
    ]);
    expect(decide).toHaveBeenNthCalledWith(2, "p1", "review-2", [
      { operation_id: "operation-review-2", decision: "ACCEPT" },
    ]);
  });
});
