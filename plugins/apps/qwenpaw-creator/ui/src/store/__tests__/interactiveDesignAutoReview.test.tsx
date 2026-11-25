import * as routingLocators from "@/routing/locators";
import FileProjectReviewPanel, {
  reviewPendingUnits,
} from "@/components/agent/FileProjectReviewPanel";
import ExecutionAuthorizationCard from "@/components/agent/ExecutionAuthorizationCard";
import { navigate } from "@/routing/navigation";
import { useNavigationStore } from "@/store/navigationStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";
import { makePendingAuthorization } from "@/test/agentFixtures";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import DecisionTray from "@/components/agent/DecisionTray";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FileProjectReviewRecord } from "@/contracts/creator";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { isInteractiveDesignReview } from "@/lib/interactiveDesignReview";

function review(): FileProjectReviewRecord {
  return {
    review_id: "design-review",
    round_id: "round-1",
    request_id: "draft-page",
    request_message_seq: null,
    interrupted_run_id: null,
    baseline_generation: 2,
    baseline_etag: "base-2",
    candidate_generation: 3,
    candidate_etag: "candidate-3",
    decision_token: "token-1",
    status: "PENDING",
    created_at: "2026-09-14T00:00:00Z",
    updated_at: "2026-09-14T00:00:01Z",
    operations: [
      {
        kind: "update",
        json_pointer: "/interactive_presentation/motion",
        file_id: null,
        target_ref: null,
        before_hash: "before",
        after_hash: "after",
        before: null,
        after: {
          format: "html_css",
          html: "<main>开始</main>",
          design_notes: "input_fingerprint=1234",
        },
        operation_id: "motion",
        ui_locator: { field: "/interactive_presentation/motion" },
        decision: "PENDING",
      },
    ],
  };
}
const yolo = {
  executionAuthorization: { mode: "allow_all" },
  creationCheckpoints: { mode: "skip" },
  mediaReview: { mode: "auto_approve" },
};
const store = () => useFileProjectReviewStore.getState();
function response(status: number, body?: unknown, etag?: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "test",
    headers: new Headers(etag ? { ETag: `"${etag}"` } : {}),
    json: async () => body,
  } as Response;
}
const active = (value: FileProjectReviewRecord) =>
  response(200, [value], value.decision_token);
const resolved = (value: FileProjectReviewRecord) => ({
  ...value,
  status: "RESOLVED",
  operations: value.operations.map((op) => ({ ...op, decision: "ACCEPTED" })),
});
afterEach(() => {
  store().reset();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("YOLO compatibility for interactive design publications", () => {
  it("accepts a generated interface once through the normal decision endpoint, without generating or resuming", async () => {
    const draft = review();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(active(draft))
      .mockResolvedValueOnce(response(200, yolo))
      .mockResolvedValueOnce(response(200, resolved(draft)));
    vi.stubGlobal("fetch", fetchMock);
    const first = store().pollOnce("p1"),
      joined = store().pollOnce("p1");
    expect(joined).toBe(first);
    await first;
    expect(store().reviews).toEqual([]);
    expect(store().autoReviewIds).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][1].cache).toBe("no-store");
    const [url, init] = fetchMock.mock.calls[2];
    expect(url).toContain("/runtime/reviews/design-review/decisions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toMatchObject({
      decisionToken: "token-1",
      decisions: [{ operation_id: "motion", decision: "ACCEPT" }],
    });
    expect(
      fetchMock.mock.calls.filter(([, options]) => options?.method === "POST"),
    ).toHaveLength(1);
  });

  it.each([
    { ...yolo, mediaReview: { mode: "required" } },
    { ...yolo, creationCheckpoints: { mode: "required" } },
    { ...yolo, executionAuthorization: { mode: "required" } },
    {},
  ])(
    "keeps manual review when the persisted ladder is not fully YOLO (%j)",
    async (config) => {
      const draft = review();
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(active(draft))
        .mockResolvedValueOnce(response(200, config));
      vi.stubGlobal("fetch", fetchMock);
      await store().pollOnce("p1");
      expect(store().reviews[0].status).toBe("PENDING");
      expect(store().autoReviewIds).toEqual([]);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it("does not accept scripts, prompt-only edits, removals or mixed reviews", async () => {
    const draft = review();
    for (const pointer of [
      "/timelines/items/t1/description",
      "/interactive_presentation/design_prompt",
      "/execution_authorization/mode",
    ]) {
      expect(
        isInteractiveDesignReview({
          ...draft,
          operations: [
            { ...draft.operations[0], json_pointer: pointer, after: "text" },
          ],
        }),
      ).toBe(false);
    }
    expect(
      isInteractiveDesignReview({
        ...draft,
        operations: [{ ...draft.operations[0], kind: "delete", after: null }],
      }),
    ).toBe(false);
    draft.operations.push({
      ...draft.operations[0],
      operation_id: "script",
      json_pointer: "/timelines/items/t1/description",
      after: "script",
    });
    const fetchMock = vi.fn().mockResolvedValueOnce(active(draft));
    vi.stubGlobal("fetch", fetchMock);
    await store().pollOnce("p1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(store().reviews[0].status).toBe("PENDING");
  });

  it("supports regenerated HTML fields with their design prompt and metadata", () => {
    const draft = review();
    for (const prefix of [
      "/interactive_presentation",
      "/timelines/items/t~1a/elements_by_id/e1/creation",
    ]) {
      draft.operations = [
        {
          ...review().operations[0],
          json_pointer: `${prefix}/motion/html`,
          after: "<main>继续</main>",
        },
        {
          ...review().operations[0],
          operation_id: "notes",
          json_pointer: `${prefix}/motion/design_notes`,
          after: "input_fingerprint=5678",
        },
        {
          ...review().operations[0],
          operation_id: "prompt",
          json_pointer: `${prefix}/design_prompt`,
          after: "new design",
        },
      ];
      expect(isInteractiveDesignReview(draft)).toBe(true);
    }
  });

  it("waits for configuration without accepting a review from a project the user has left", async () => {
    let finishMode!: (value: Response) => void;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(active(review()))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishMode = resolve;
          }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const pending = store().pollOnce("p1");
    await vi.waitFor(() =>
      expect(store().autoReviewIds).toEqual(["design-review"]),
    );
    await vi.waitFor(() => expect(finishMode).toBeDefined());
    store().reset("p2");
    finishMode(response(200, yolo));
    await pending;
    expect(store().projectId).toBe("p2");
    expect(store().reviews).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the review reachable on configuration failure", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(active(review()))
      .mockRejectedValueOnce(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    await store().pollOnce("p1");
    expect(store().reviews[0].status).toBe("PENDING");
    expect(store().autoReviewIds).toEqual([]);
    expect(store().syncError).toContain("自动处理未完成");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not loop on a failed decision when unrelated progress rotates the review token", async () => {
    vi.useFakeTimers();
    const first = review(),
      rotated = {
        ...first,
        decision_token: "token-2",
        candidate_generation: 4,
      };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(active(first))
      .mockResolvedValueOnce(response(200, yolo))
      .mockResolvedValueOnce(
        response(503, { code: "UNAVAILABLE", message: "retry later" }),
      )
      .mockResolvedValueOnce(active(rotated));
    vi.stubGlobal("fetch", fetchMock);
    await store().pollOnce("p1");
    vi.setSystemTime(Date.now() + 20_000);
    await store().pollOnce("p1");
    expect(store().reviews[0].decision_token).toBe("token-2");
    expect(store().autoReviewIds).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("reuses the CAS guard to accept only unchanged operations when a background result rotates the token", async () => {
    const first = review(),
      fresh = { ...first, decision_token: "token-2", candidate_generation: 4 };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(active(first))
      .mockResolvedValueOnce(response(200, yolo))
      .mockResolvedValueOnce(
        response(409, { code: "CAS_CONFLICT", message: "updated" }),
      )
      .mockResolvedValueOnce(active(fresh))
      .mockResolvedValueOnce(response(200, resolved(fresh)));
    vi.stubGlobal("fetch", fetchMock);
    await store().pollOnce("p1");
    expect(store().reviews).toEqual([]);
    expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toMatchObject({
      decisionToken: "token-2",
      decisions: [{ operation_id: "motion", decision: "ACCEPT" }],
    });
  });
});

it("restores manual decisions if automatic interactive review stops", () => {
  const pointer = "/interactive_presentation/motion";
  const record = makeReviewRecord({
    review_id: "design-review",
    operations: [
      makeReviewOperation({
        json_pointer: pointer,
        before: "旧文案",
        after: "新文案",
        ui_locator: { page: "plan", mediaType: "text", field: pointer },
      }),
    ],
  });
  useExecutionAuthorizationStore.setState({
    projectId: "p1",
    items: [],
    error: null,
  });
  useFileProjectReviewStore.setState({
    projectId: "p1",
    reviews: [record],
    autoReviewIds: [record.review_id],
    decisionInFlight: false,
  });
  try {
    const { container } = render(<DecisionTray projectId="p1" />);
    expect(container).toBeEmptyDOMElement();

    act(() => useFileProjectReviewStore.setState({ autoReviewIds: [] }));
    expect(
      screen.getByRole("button", { name: "保留 作品页面 · 界面效果" }),
    ).toBeInTheDocument();
  } finally {
    useExecutionAuthorizationStore.getState().reset();
  }
});

const navigateToLocator = vi.fn();
vi.mock("@/routing/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/routing/navigation")>()),
  navigate: vi.fn(),
}));

function renderReviewPanel(value: FileProjectReviewRecord) {
  const decide = vi.fn(async () => value);
  useFileProjectReviewStore.setState({
    projectId: "p1",
    reviews: [value],
    etag: '"token-1"',
    syncStatus: "healthy",
    syncError: null,
    decisionInFlight: false,
    decide,
  });
  render(<FileProjectReviewPanel projectId="p1" review={value} />);
  return decide;
}

describe("Interactive design approval and review", () => {
  beforeEach(() => {
    vi.spyOn(routingLocators, "navigateToLocator").mockImplementation(
      navigateToLocator,
    );
  });
  afterEach(() => {
    useProjectSnapshotStore.getState().reset();
    useNavigationStore.getState().clear();
    useExecutionAuthorizationStore.getState().reset();
    vi.mocked(routingLocators.navigateToLocator).mockRestore();
    navigateToLocator.mockClear();
    vi.mocked(navigate).mockClear();
  });

  it("shows mode processing instead of manual review buttons during automatic handling", () => {
    const value = review();
    useFileProjectReviewStore.setState({ autoReviewIds: [value.review_id] });
    const decide = renderReviewPanel(value);
    expect(screen.getByRole("status")).toHaveTextContent(
      "正在按已保存的执行模式处理交互设计",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(decide).not.toHaveBeenCalled();
  });

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
      const decide = renderReviewPanel(value);
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
      renderReviewPanel(
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
});

describe("Interactive authorization locators", () => {
  afterEach(() => {
    useProjectSnapshotStore.getState().reset();
    useNavigationStore.getState().clear();
    useExecutionAuthorizationStore.getState().reset();
    vi.mocked(navigate).mockClear();
  });

  it.each([
    {
      targetRef: "project:p1",
      operation: "生成作品页面",
      field: "/interactive_presentation/design_prompt",
    },
    {
      targetRef: "element:choice:one",
      operation: "生成抉择动效",
      field:
        "/timelines/items/timeline:main/elements_by_id/choice:one/creation/design_prompt",
    },
  ])("opens the real $operation input from its confirmation card", (test) => {
    vi.useFakeTimers();
    const project = structuredClone(projectDocument);
    const timeline = project.timelines.items["timeline:main"];
    timeline.elements_by_id["choice:one"] = {
      ...timeline.elements_by_id["r2v-window"],
      element_id: "choice:one",
      creation: {
        type: "interaction",
        question: "向哪边走？",
        design_prompt: "两张车票作为选择按钮",
        options: [{ edge_ref: "edge:left" }, { edge_ref: "edge:right" }],
      },
    };
    useProjectSnapshotStore.setState({ projectId: "p1", project });
    useExecutionAuthorizationStore.setState({ projectId: "p1" });
    render(
      <ExecutionAuthorizationCard
        project={project}
        authorization={makePendingAuthorization({
          targetRef: test.targetRef,
          scope: { operation: "interaction_draft" },
          provider: "text",
          model: "qwen3.8-max",
        })}
      />,
    );
    expect(screen.getByText(`${test.operation}等待确认`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    const target = vi.mocked(navigate).mock.calls[0][0] as string;
    expect(target.split("?")[0]).toBe("/project/p1");
    expect(new URLSearchParams(target.split("?")[1]).get("field")).toBe(
      test.field,
    );
    expect(target).not.toContain("storyboard_prompt");
  });
});
