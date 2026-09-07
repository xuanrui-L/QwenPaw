import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AgentWaitHint from "../AgentWaitHint";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-07T00:00:00Z"));
  useCreatorSessionStore.getState().reset();
});
afterEach(() => vi.useRealTimers());
it("uses the server event time, counts down only the backoff and clears on recovery", async () => {
  const retry = {
    runId: "m1",
    attempt: 2,
    maxAttempts: 5,
    delaySeconds: 8,
    createdAt: "2026-09-07T00:00:00Z",
  };
  const view = render(
    <AgentWaitHint projectId="p1" active retrying retry={retry} />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("模型限流");
  expect(screen.getByRole("status")).toHaveTextContent("8 秒后重试");
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(screen.getByRole("status")).toHaveTextContent("5 秒后重试");
  await act(() => vi.advanceTimersByTimeAsync(6000));
  expect(screen.getByRole("status")).toHaveTextContent("等待模型响应");
  expect(document.body.textContent).not.toContain("100%");
  view.rerender(
    <AgentWaitHint projectId="p1" active retrying={false} retry={null} />,
  );
  expect(screen.queryByText(/模型限流/)).toBeNull();
});
it("explains input recovery without exposing internal data or claiming throttling", () => {
  render(
    <AgentWaitHint
      projectId="p1"
      active
      retrying
      retry={{
        runId: "private-context",
        attempt: 1,
        maxAttempts: 1,
        delaySeconds: 0,
        reason: "context_recovery",
      }}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    "正在整理任务信息，随后继续",
  );
  expect(document.body.textContent).not.toMatch(
    /private-context|限流|429|token|快照/,
  );
});
it("shows all affected roles without inventing missing timing or exposing identities", () => {
  render(
    <AgentWaitHint
      projectId="p1"
      active={false}
      retrying={false}
      others={[
        {
          runId: "private-a",
          label: "编剧",
          attempt: 1,
          maxAttempts: 3,
          reason: "transient",
        },
        {
          runId: "private-b",
          label: "分镜师",
          attempt: 2,
          maxAttempts: 5,
          reason: "rate_limit",
        },
      ]}
    />,
  );
  expect(screen.getAllByRole("status")).toHaveLength(2);
  expect(document.body.textContent).toContain("编剧 · 模型连接暂时中断");
  expect(document.body.textContent).toContain("分镜师 · 模型限流");
  expect(document.body.textContent).not.toMatch(/private-|秒后|%/);
});

it("changes the long wait hint from elapsed time and resets for a different project", async () => {
  const view = render(<AgentWaitHint projectId="p1" active retrying={false} />);
  const initial = view.container.textContent;
  await act(() => vi.advanceTimersByTimeAsync(29_999));
  expect(view.container.textContent).toBe(initial);
  await act(() => vi.advanceTimersByTimeAsync(1));
  expect(view.container.textContent).not.toBe(initial);
  expect(view.container.textContent).not.toMatch(/%|秒后完成|剩余/);
  view.rerender(<AgentWaitHint projectId="p2" active retrying={false} />);
  expect(view.container.textContent).toBe(initial);
  view.rerender(
    <AgentWaitHint projectId="p2" active={false} retrying={false} />,
  );
  expect(view.container).toBeEmptyDOMElement();
});
