import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PromptSyncPanel from "../PromptSyncPanel";
import * as api from "@/api/creator/promptSync";

vi.mock("@/api/creator/promptSync", () => ({ getPromptSync: vi.fn() }));
const scope = {
  projectId: "p1",
  timelineId: "timeline:main",
  elementId: "shot-one",
};
const state = (
  status: api.PromptSyncStatus = "needs_confirmation",
): api.PromptSyncState => ({
  status,
  baselineToken: "private-baseline",
  changedSources: ["storyboardPrompt"],
  suggestedSource: "storyboardPrompt",
  narrative: "private-narrative",
  storyboardPrompt: "private-prompt",
  videoPrompt: "private-video",
});
const props = () => ({
  ...scope,
  generation: 1,
  dirty: false,
  onStatus: vi.fn(),
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getPromptSync).mockResolvedValue(state());
});
afterEach(() => vi.useRealTimers());

describe("automatic synchronization context", () => {
  it("shows saved edits without extra review buttons or internal content", async () => {
    render(<PromptSyncPanel {...props()} />);
    await screen.findByText("修改已保存，重新生成时会同步关联内容");
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.body.textContent).not.toContain("private-");
  });
  it("ignores an old route response even after returning to the same shot", async () => {
    let resolve!: (value: api.PromptSyncState) => void;
    vi.mocked(api.getPromptSync).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const p = props();
    const view = render(<PromptSyncPanel {...p} />);
    const signal = vi.mocked(api.getPromptSync).mock.calls[0][1]!;
    view.rerender(<PromptSyncPanel {...p} elementId="shot-two" />);
    await waitFor(() => expect(p.onStatus).toHaveBeenCalledWith(state()));
    view.rerender(<PromptSyncPanel {...p} generation={2} />);
    await waitFor(() => expect(api.getPromptSync).toHaveBeenCalledTimes(3));
    await act(async () => resolve(state("current")));
    expect(signal.aborted).toBe(true);
    expect(
      screen.getByText("修改已保存，重新生成时会同步关联内容"),
    ).toBeInTheDocument();
    expect(p.onStatus).not.toHaveBeenCalledWith(state("current"));
  });
  it("reports actual waiting time and keeps request failure actionable at generation", async () => {
    vi.mocked(api.getPromptSync).mockRejectedValue(
      new Error("private diagnostic"),
    );
    const p = props();
    const view = render(<PromptSyncPanel {...p} />);
    await screen.findByText("生成时会重新检查片段内容与提示词");
    vi.useFakeTimers();
    view.rerender(<PromptSyncPanel {...p} working />);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByRole("status")).toHaveTextContent("已等待 5 秒");
    expect(document.body.textContent).not.toContain("private diagnostic");
  });
});
