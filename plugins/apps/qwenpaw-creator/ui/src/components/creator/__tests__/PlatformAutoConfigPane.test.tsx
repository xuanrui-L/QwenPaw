import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PlatformAutoConfigPane from "../PlatformAutoConfigPane";
import {
  applyPlatformCredentials,
  fetchPlatformCredentials,
} from "@/api/creator/platform";

vi.mock("@/api/creator/platform", () => ({
  fetchPlatformCredentials: vi.fn(),
  applyPlatformCredentials: vi.fn(),
}));

const fetchMock = vi.mocked(fetchPlatformCredentials);
const applyMock = vi.mocked(applyPlatformCredentials);

describe("PlatformAutoConfigPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchMock.mockResolvedValue({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
      display_available_credits: 120,
    });
    applyMock.mockResolvedValue({
      ok: true,
      base_url: "https://platform-pre.agentscope.io/v1",
      sections: [
        { section: "llm", model_name: "qwen3.8-flash", ready: true },
        { section: "tts", model_name: "", ready: false },
      ],
    });
  });

  it("carries the issued key straight through to the backend", async () => {
    render(<PlatformAutoConfigPane onJumpToModel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    await waitFor(() => expect(applyMock).toHaveBeenCalledTimes(1));
    // The pane must not re-shape the credential on the way: an edited URL is
    // what turns a working key into an endpoint the proxy does not serve.
    expect(applyMock.mock.calls[0][0]).toMatchObject({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
    });
  });

  it("lists what is configured and what still needs a model", async () => {
    const onJumpToModel = vi.fn();
    render(<PlatformAutoConfigPane onJumpToModel={onJumpToModel} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    expect(await screen.findByText("qwen3.8-flash")).toBeInTheDocument();
    const needsModel = screen.getByRole("button", { name: "未选择模型" });
    fireEvent.click(needsModel);
    // A missing model is not this pane's decision to make; it sends the
    // operator to the card where the choice is validated.
    expect(onJumpToModel).toHaveBeenCalledWith("tts");
  });

  it("keeps the failure visible instead of looking like a silent success", async () => {
    fetchMock.mockRejectedValue(new Error("平台未识别本子域的登录会话"));
    render(<PlatformAutoConfigPane onJumpToModel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    expect(
      await screen.findByText("平台未识别本子域的登录会话"),
    ).toBeInTheDocument();
    expect(applyMock).not.toHaveBeenCalled();
    expect(screen.queryByText("qwen3.8-flash")).toBeNull();
  });
});
