import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { message } from "antd";
import WorkspaceSidebar from "@/components/layout/WorkspaceSidebar";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorEditBufferStore } from "@/store/creatorEditBufferStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import {
  makeReviewOperation,
  makeReviewRecord,
  msg,
  seedCreatorSession,
} from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";

const selectedRef = "timeline:selected-timeline";
const manualRef = {
  ref: selectedRef,
  name: "待修改剧集",
  type: "timeline" as const,
  uiLocator: {},
};
const accepted = {
  messageSeq: 1,
  eventSeq: 1,
  classification: "mutation_instruction",
  appendState: "queued_until_message_boundary",
  creatorSessionId: "session-1",
  conversationId: "conversation-1",
};

function renderSidebar() {
  const router = createMemoryRouter(
    [{ path: "/project/:id/plan", element: <WorkspaceSidebar /> }],
    { initialEntries: ["/project/p1/plan"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const composer = () =>
  screen.getByRole("textbox", { name: "输入修改意图，@ 可引用对象…" });
function typeMessage(text: string) {
  const input = composer();
  input.textContent = text;
  fireEvent.input(input);
  return input;
}

function addResponse(messageSeq: number) {
  act(() =>
    useCreatorSessionStore.setState((state) => ({
      messages: [
        ...state.messages,
        msg({
          messageId: `response-${messageSeq}`,
          messageSeq,
          role: "assistant",
          text: "已更新作品。",
        }),
      ],
    })),
  );
}

/** Explicitly model scroll metrics; jsdom does not lay out or clamp scrolling. */
function observeScroll(element: HTMLElement) {
  let height = 1000;
  let top = 0;
  const writes: number[] = [];
  const hidden = () =>
    element.parentElement?.classList.contains("hidden") ?? false;
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, get: () => (hidden() ? 0 : height) },
    clientHeight: { configurable: true, get: () => (hidden() ? 0 : 400) },
    scrollTop: {
      configurable: true,
      get: () => (hidden() ? 0 : top),
      set: (value: number) => {
        writes.push(value);
        top = hidden() ? 0 : Math.max(0, Math.min(value, height - 400));
      },
    },
  });
  return {
    writes,
    grow: () => {
      height = 1400;
    },
  };
}

describe("AgentDock references, async sends and parallel sidebar tabs", () => {
  beforeEach(() => {
    seedCreatorSession();
    useAgentDockUiStore.getState().setSidebarTab("assistant");
    useProjectSnapshotStore.getState().reset();
    useCreatorEditBufferStore.getState().reset();
    useWorkGraphStore.getState().reset();
    installMockFetch([]);
  });

  it("updates an idle footer from actual media review arrival and resolution", () => {
    renderSidebar();
    const footer = document.querySelector<HTMLElement>(
      "[data-agent-live-status]",
    )!;
    expect(footer).toHaveAttribute("data-state", "idle");
    const review = makeReviewRecord({
      operations: [
        makeReviewOperation({
          ui_locator: {
            page: "element",
            elementId: "shot2_discovery",
            mediaType: "image",
            versionId: "generated-storyboard",
          },
        }),
      ],
    });
    act(() =>
      useFileProjectReviewStore.setState({
        projectId: "p1",
        reviews: [review],
      }),
    );
    expect(footer).toHaveAttribute("data-state", "waiting");
    expect(footer).toHaveTextContent("等待您审阅");
    act(() =>
      useFileProjectReviewStore.setState({
        reviews: [
          {
            ...review,
            status: "RESOLVED",
            operations: review.operations.map((operation) => ({
              ...operation,
              decision: "ACCEPTED",
            })),
          },
        ],
      }),
    );
    expect(footer).toHaveAttribute("data-state", "idle");
    expect(footer).toHaveTextContent("随时可以继续创作");
  });

  it("ignores another project's media review and already decided review records", () => {
    renderSidebar();
    const footer = document.querySelector<HTMLElement>(
      "[data-agent-live-status]",
    )!;
    act(() =>
      useFileProjectReviewStore.setState({
        projectId: "p2",
        reviews: [makeReviewRecord()],
      }),
    );
    expect(footer).toHaveAttribute("data-state", "idle");
    act(() =>
      useFileProjectReviewStore.setState({
        projectId: "p1",
        reviews: [makeReviewRecord({ status: "RESOLVED" })],
      }),
    );
    expect(footer).toHaveAttribute("data-state", "idle");
  });

  it.each([
    ["automatic", "remove"],
    ["manual and selected", "remove"],
    ["automatic", "clear"],
    ["manual and selected", "clear"],
  ] as const)("omits %s selected context after %s", async (source, action) => {
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/messages",
        method: "POST",
        response: { json: accepted },
      },
    ]);
    useCreatorInteractionStore.getState().select(selectedRef);
    if (source === "manual and selected")
      useCreatorInteractionStore.getState().setExtraRefs([manualRef]);
    renderSidebar();
    const chips = document.querySelector<HTMLElement>(
      "[data-agent-reference-chips]",
    )!;
    fireEvent.click(
      action === "remove"
        ? within(chips).getByRole("button", { name: /^移除 / })
        : within(chips).getByTitle("清空全部引用、划选文本与输入框内的 @ 引用"),
    );
    expect(document.querySelector("[data-agent-reference-chips]")).toBeNull();
    fireEvent.keyDown(typeMessage("请调整节奏"), { key: "Enter" });
    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.method === "POST" && call.url.includes("/messages"),
        ),
      ).toBe(true),
    );
    const body = calls.find(
      (call) => call.method === "POST" && call.url.includes("/messages"),
    )!.body as { context: Record<string, unknown> };
    expect(body.context).not.toHaveProperty("selected");
    expect(body.context.extraRefs).toEqual([]);
  });

  it("preserves the exact selected reference while its chip remains", async () => {
    const { calls } = installMockFetch([
      {
        match: "/projects/p1/messages",
        method: "POST",
        response: { json: accepted },
      },
    ]);
    useCreatorInteractionStore.getState().select(selectedRef);
    renderSidebar();
    fireEvent.keyDown(typeMessage("仅修改这个剧集"), { key: "Enter" });
    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST")).toBe(true),
    );
    expect(calls.find((call) => call.method === "POST")!.body).toMatchObject({
      context: { selected: { ref: selectedRef }, extraRefs: [selectedRef] },
    });
  });

  it.each([false, true])(
    "does not restore an old failed send after navigation (reopen=%s)",
    async (reopen) => {
      let rejectRequest!: (error: Error) => void;
      const pending = new Promise<Response>((_resolve, reject) => {
        rejectRequest = reject;
      });
      const fetchMock = vi.fn(() => pending);
      vi.stubGlobal("fetch", fetchMock);
      useCreatorInteractionStore.getState().setExtraRefs([manualRef]);
      const router = renderSidebar();
      const input = typeMessage("旧项目的修改指令");
      fireEvent.keyDown(input, { key: "Enter" });
      expect(input).toHaveTextContent("");
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await act(async () => {
        useCreatorInteractionStore.getState().reset();
        useCreatorSessionStore.setState((state) => ({
          projectId: "p2",
          session: { ...state.session!, projectId: "p2", id: "session-2" },
          activeConversationId: "conversation-2",
          messages: [],
          queuedUi: [],
        }));
        await router.navigate("/project/p2/plan");
      });
      if (reopen)
        await act(async () => {
          useCreatorSessionStore.setState((state) => ({
            projectId: "p1",
            session: { ...state.session!, projectId: "p1", id: "session-1" },
            activeConversationId: "conversation-1",
          }));
          await router.navigate("/project/p1/plan");
        });
      expect(composer()).toBe(input);
      await act(async () => {
        rejectRequest(new Error("PRIVATE_OLD_PROJECT_ERROR"));
        await pending.catch(() => undefined);
      });
      expect(input).toHaveTextContent("");
      expect(useAgentDockUiStore.getState().draft).toBe("");
      expect(useCreatorInteractionStore.getState().extraRefs).toEqual([]);
      expect(document.body).not.toHaveTextContent("PRIVATE_OLD_PROJECT_ERROR");
      expect(document.body).not.toHaveTextContent("旧项目的修改指令");
    },
  );

  it.each(["bottom", "history"] as const)(
    "preserves %s scroll intent across the episode feedSlot",
    (position) => {
      renderSidebar();
      const input = typeMessage("尚未发送的草稿");
      const feed = document.querySelector<HTMLElement>(
        ".agent-conversation-feed",
      )!;
      const metrics = observeScroll(feed);
      feed.scrollTop = position === "bottom" ? 600 : 200;
      fireEvent.scroll(feed);
      const priorWrites = metrics.writes.length;
      fireEvent.click(screen.getByRole("button", { name: "剧集列表" }));
      expect(document.querySelector("[data-agent-feed-slot]")).not.toBeNull();
      expect(feed.parentElement).toHaveClass("hidden");
      metrics.grow();
      addResponse(2);
      expect(metrics.writes).toHaveLength(priorWrites);
      expect(composer()).toBe(input);
      expect(input).toHaveTextContent("尚未发送的草稿");
      fireEvent.click(screen.getByRole("button", { name: "创作助手" }));
      expect(feed.parentElement).not.toHaveClass("hidden");
      expect(feed.scrollTop).toBe(position === "bottom" ? 1000 : 200);
      expect(composer()).toBe(input);
      expect(input).toHaveTextContent("尚未发送的草稿");
      if (position === "history") {
        expect(metrics.writes).toHaveLength(priorWrites);
        addResponse(3);
        expect(feed.scrollTop).toBe(200);
      }
    },
  );

  it.each(["success", "failure"] as const)(
    "ignores an old resume %s after A → B → A while a new resume is pending",
    async (outcome) => {
      function pendingResponse() {
        let resolve!: (response: Response) => void;
        let reject!: (error: Error) => void;
        const promise = new Promise<Response>((done, fail) => {
          resolve = done;
          reject = fail;
        });
        return { promise, resolve, reject };
      }
      const oldRequest = pendingResponse();
      const newRequest = pendingResponse();
      const fetchMock = vi
        .fn()
        .mockReturnValueOnce(oldRequest.promise)
        .mockReturnValueOnce(newRequest.promise);
      vi.stubGlobal("fetch", fetchMock);
      const errorToast = vi.spyOn(message, "error");
      function bindThrottledProject(projectId: string) {
        useCreatorSessionStore.setState((state) => ({
          projectId,
          session: {
            ...state.session!,
            projectId,
            id: projectId === "p1" ? "session-1" : "session-2",
            status: "ERROR",
            error: {
              code: "MODEL_RATE_LIMITED",
              message: "PRIVATE_PROVIDER_ERROR",
              retryable: true,
              details: { retryCount: 5 },
            },
          },
          activeConversationId:
            projectId === "p1" ? "conversation-1" : "conversation-2",
          messages: [],
          queuedUi: [],
        }));
      }
      const response = (): Response =>
        ({ ok: true, status: 202, json: async () => accepted }) as Response;
      bindThrottledProject("p1");
      const router = renderSidebar();
      fireEvent.click(screen.getByRole("button", { name: /继\s*续/ }));
      expect(screen.getByRole("button", { name: /继\s*续/ })).toHaveClass(
        "ant-btn-loading",
      );
      await act(async () => {
        bindThrottledProject("p2");
        await router.navigate("/project/p2/plan");
      });
      await act(async () => {
        bindThrottledProject("p1");
        await router.navigate("/project/p1/plan");
      });
      const newResume = screen.getByRole("button", { name: /继\s*续/ });
      expect(newResume).not.toHaveClass("ant-btn-loading");
      fireEvent.click(newResume);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(newResume).toHaveClass("ant-btn-loading");
      await act(async () => {
        if (outcome === "success") oldRequest.resolve(response());
        else oldRequest.reject(new Error("PRIVATE_OLD_RESUME_ERROR"));
        await oldRequest.promise.catch(() => undefined);
      });
      expect(newResume).toHaveClass("ant-btn-loading");
      expect(errorToast).not.toHaveBeenCalled();
      expect(document.body).not.toHaveTextContent("PRIVATE_OLD_RESUME_ERROR");
      await act(async () => {
        newRequest.resolve(response());
        await newRequest.promise;
      });
      expect(newResume).not.toHaveClass("ant-btn-loading");
      expect(errorToast).not.toHaveBeenCalled();
    },
  );

  it.each(["success", "failure"] as const)(
    "does not show an old stop %s toast after switching projects",
    async (outcome) => {
      let resolveRequest!: (response: Response) => void;
      let rejectRequest!: (error: Error) => void;
      const pending = new Promise<Response>((resolve, reject) => {
        resolveRequest = resolve;
        rejectRequest = reject;
      });
      const fetchMock = vi.fn(() => pending);
      vi.stubGlobal("fetch", fetchMock);
      const successToast = vi.spyOn(message, "success");
      const errorToast = vi.spyOn(message, "error");
      useCreatorSessionStore.setState((state) => ({
        session: { ...state.session!, status: "RUNNING" },
      }));
      const router = renderSidebar();
      fireEvent.click(screen.getByRole("button", { name: "停止所有 Agent" }));
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(useCreatorSessionStore.getState().stopping).toBe(true);
      await act(async () => {
        useCreatorSessionStore.getState().reset();
        useCreatorSessionStore.setState({
          projectId: "p2",
          session: {
            id: "session-2",
            projectId: "p2",
            status: "IDLE",
            lastMessageSeq: 0,
            lastConsumedMessageSeq: 0,
            lastEventSeq: 0,
          },
          activeConversationId: "conversation-2",
        });
        await router.navigate("/project/p2/plan");
      });
      await act(async () => {
        if (outcome === "success")
          resolveRequest({
            ok: true,
            status: 200,
            json: async () => ({ ok: true }),
          } as Response);
        else rejectRequest(new Error("PRIVATE_OLD_STOP_ERROR"));
        await pending.catch(() => undefined);
      });
      expect(successToast).not.toHaveBeenCalled();
      expect(errorToast).not.toHaveBeenCalled();
      expect(document.body).not.toHaveTextContent("PRIVATE_OLD_STOP_ERROR");
      expect(useCreatorSessionStore.getState()).toMatchObject({
        projectId: "p2",
        stopping: false,
        session: { status: "IDLE" },
      });
    },
  );
});
