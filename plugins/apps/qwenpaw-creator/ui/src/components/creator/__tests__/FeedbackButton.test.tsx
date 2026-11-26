import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ConfigProvider } from "antd";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/creator/feedback";
import { CreatorHttpError } from "@/api/creator/client";
import { captureFeedbackScreenshot } from "@/lib/feedbackScreenshot";
import FeedbackButton from "../FeedbackButton";

vi.mock("@/lib/feedbackScreenshot", () => ({
  captureFeedbackScreenshot: vi.fn(),
}));

const context = {
  projectId: "p1",
  sessionId: "session1",
  conversationId: "chat1",
};
const button = (name: string) => screen.getByRole("button", { name });
function feedbackUi(conversationId = context.conversationId) {
  return (
    <ConfigProvider theme={{ token: { motion: false } }}>
      <FeedbackButton {...context} conversationId={conversationId} />
    </ConfigProvider>
  );
}

describe("Creator feedback", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("captures once before opening and sends only the selected images with the original context", async () => {
    const automatic = api.makeFeedbackScreenshot(
      new File(["shot"], "workspace.png", { type: "image/png" }),
    );
    let finish!: (value: api.FeedbackScreenshot) => void;
    vi.mocked(captureFeedbackScreenshot).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const send = vi
      .spyOn(api, "submitFeedback")
      .mockResolvedValueOnce(undefined);
    const { rerender } = render(feedbackUi());
    const trigger = button("意见反馈");
    fireEvent.click(trigger);
    fireEvent.click(trigger);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(captureFeedbackScreenshot).toHaveBeenCalledTimes(1);
    rerender(feedbackUi("chat-opened-after-the-click"));
    await act(async () => finish(automatic));

    const description = screen.getByRole("textbox", { name: /问题或建议/ });
    const submit = button("发送反馈");
    expect(screen.getByRole("img", { name: "截图 1" })).toBeInTheDocument();
    expect(submit).toBeDisabled();
    fireEvent.change(description, { target: { value: "预览卡住" } });
    const invalid = new File(["svg"], "drawing.svg", { type: "image/svg+xml" });
    fireEvent.change(
      screen.getByLabelText("添加或粘贴截图", { selector: "input" }),
      { target: { files: [invalid] } },
    );
    expect(screen.getByRole("alert")).toHaveTextContent("5 MB");
    const pasted = new File(["paste"], "clipboard.png", { type: "image/png" });
    fireEvent.paste(description, { clipboardData: { files: [pasted] } });
    fireEvent.click(button("移除截图 1"));
    fireEvent.change(screen.getByRole("textbox", { name: /联系邮箱/ }), {
      target: { value: "editor@example.com" },
    });
    fireEvent.click(submit);
    expect(send).toHaveBeenLastCalledWith(
      {
        ...context,
        feedback: "预览卡住",
        contactEmail: "editor@example.com",
        screenshots: [{ key: expect.any(String), file: pasted }],
      },
      expect.any(Object),
    );
    const link = await screen.findByRole("link", { name: /^这里$/ });
    expect(link.closest("p")).toHaveTextContent(/^后续可以在这里查看回复。$/);
    expect(screen.getByRole("heading", { name: "反馈已发送" })).toHaveFocus();
    expect(link).toHaveAttribute(
      "href",
      "https://platform.agentscope.io/notifications",
    );
    expect(link).toHaveAttribute("target", "_blank");
    const openExternalLink = vi.fn();
    vi.stubGlobal("QwenPaw", { host: { openExternalLink } });
    fireEvent.click(link);
    expect(openExternalLink).toHaveBeenCalledWith(link.getAttribute("href"));
    fireEvent.click(button("关闭"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("preserves text-only feedback across a failed capture and account reconnect", async () => {
    vi.mocked(captureFeedbackScreenshot).mockRejectedValueOnce(
      new Error("capture unavailable"),
    );
    const send = vi
      .spyOn(api, "submitFeedback")
      .mockRejectedValueOnce(
        new CreatorHttpError(409, { code: "PLATFORM_ACCOUNT_REQUIRED" }),
      )
      .mockResolvedValueOnce(undefined);
    render(feedbackUi());
    fireEvent.click(button("意见反馈"));
    expect(await screen.findByRole("status")).toHaveTextContent("未能截取");
    const input = screen.getByRole("textbox", { name: /问题或建议/ });
    fireEvent.change(input, { target: { value: "草稿内容" } });
    fireEvent.click(button("发送反馈"));
    const settings = await screen.findByRole("link", { name: "打开账号设置" });
    expect(settings).toHaveAttribute("href", "/settings/community");
    expect(settings).toHaveAttribute("target", "_blank");
    expect(input).toHaveValue("草稿内容");
    expect(screen.queryByRole("link", { name: /^这里$/ })).toBeNull();
    fireEvent.click(button("发送反馈"));
    expect(
      await screen.findByRole("link", { name: /^这里$/ }),
    ).toBeInTheDocument();
    expect(send).toHaveBeenLastCalledWith(
      {
        ...context,
        feedback: "草稿内容",
        contactEmail: "",
        screenshots: [],
      },
      expect.any(Object),
    );
    expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
  });
});
