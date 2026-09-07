import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { describe, expect, it, vi } from "vitest";
import PromptEditorModal from "../PromptEditorModal";
import type { PromptRichToken } from "../PromptRichBlock";

const image = (referenceId = "image-a", index = 1): PromptRichToken => ({
  index,
  referenceId,
  name: referenceId === "image-a" ? "人物图" : "场景图",
  thumbUrl: `/media/${referenceId}.png`,
  kind: "artifact",
});
const editor = () =>
  document.querySelector<HTMLElement>("[data-prompt-token-editor]")!;
const done = () =>
  document.querySelector<HTMLButtonElement>("[data-prompt-editor-done]")!;
function change(value: string) {
  editor().textContent = value;
  fireEvent.input(editor());
}

describe("prompt editor owns its opening baseline", () => {
  it("does not overwrite a background prompt update and lets the user keep an explicit local edit", async () => {
    const onDone = vi.fn();
    const props = {
      open: true,
      label: "分镜提示词",
      initialValue: "原始内容",
      tokens: [image()],
      onDone,
      onCancel: vi.fn(),
    };
    const { rerender } = render(
      <ConfigProvider theme={{ token: { motion: false } }}>
        <PromptEditorModal {...props} />
      </ConfigProvider>,
    );
    await screen.findByRole("dialog");
    change("我的局部修改");
    rerender(
      <ConfigProvider theme={{ token: { motion: false } }}>
        <PromptEditorModal {...props} initialValue="模型更新后的内容" />
      </ConfigProvider>,
    );
    expect(editor()).toHaveTextContent("我的局部修改");
    expect(done()).toBeDisabled();
    fireEvent.click(done());
    expect(onDone).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector("[data-prompt-editor-keep]")!);
    expect(done()).toBeEnabled();
    expect(onDone).not.toHaveBeenCalled();
    fireEvent.click(done());
    expect(onDone).toHaveBeenCalledWith("我的局部修改", []);
    rerender(
      <ConfigProvider theme={{ token: { motion: false } }}>
        <PromptEditorModal {...props} initialValue="又一轮模型更新" />
      </ConfigProvider>,
    );
    expect(done()).toBeDisabled();
  });

  it("reloads the latest text rather than only changing the backing React state", async () => {
    const onDone = vi.fn();
    const props = {
      open: true,
      label: "分镜提示词",
      initialValue: "旧文本",
      tokens: [image()],
      onDone,
      onCancel: vi.fn(),
    };
    const { rerender } = render(<PromptEditorModal {...props} />);
    await screen.findByRole("dialog");
    change("本地草稿");
    rerender(
      <PromptEditorModal {...props} initialValue="最新文本 [Image 1]" />,
    );
    fireEvent.click(document.querySelector("[data-prompt-editor-reload]")!);
    await waitFor(() => expect(editor()).toHaveTextContent("最新文本"));
    expect(
      editor().querySelector('[data-image-index="1"] img'),
    ).toHaveAttribute("src", "/media/image-a.png");
    expect(done()).toBeEnabled();
    fireEvent.click(done());
    expect(onDone).toHaveBeenCalledWith("最新文本 [Image 1]", []);
  });

  it("freezes reference identity and requires reload when the same index points to a different image", async () => {
    const onDone = vi.fn();
    const props = {
      open: true,
      label: "视频提示词",
      initialValue: "参考 [Image 1]",
      tokens: [image()],
      onDone,
      onCancel: vi.fn(),
    };
    const { rerender } = render(<PromptEditorModal {...props} />);
    await screen.findByRole("dialog");
    rerender(<PromptEditorModal {...props} tokens={[image("image-b")]} />);
    expect(done()).toBeDisabled();
    expect(document.querySelector("[data-prompt-editor-keep]")).toBeDisabled();
    expect(editor().querySelector("img")).toHaveAttribute(
      "src",
      "/media/image-a.png",
    );
    fireEvent.click(document.querySelector("[data-prompt-editor-reload]")!);
    await waitFor(() =>
      expect(editor().querySelector("img")).toHaveAttribute(
        "src",
        "/media/image-b.png",
      ),
    );
    expect(done()).toBeEnabled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("detects raw source changes even if the public text is the same, but ignores a signed URL refresh for a known reference", async () => {
    const props = {
      open: true,
      label: "分镜提示词",
      initialValue: "女子",
      sourceValue: "char:old",
      tokens: [image()],
      onDone: vi.fn(),
      onCancel: vi.fn(),
    };
    const { rerender } = render(<PromptEditorModal {...props} />);
    await screen.findByRole("dialog");
    rerender(
      <PromptEditorModal
        {...props}
        tokens={[{ ...image(), thumbUrl: "/media/image-a.png?new-signature" }]}
      />,
    );
    expect(done()).toBeEnabled();
    rerender(<PromptEditorModal {...props} sourceValue="char:new" />);
    expect(done()).toBeDisabled();
  });

  it("does not save a draft into a different owning field", async () => {
    const props = {
      open: true,
      label: "提示词",
      sourceKey: "storyboard",
      initialValue: "内容",
      tokens: [],
      onDone: vi.fn(),
      onCancel: vi.fn(),
    };
    const { rerender } = render(<PromptEditorModal {...props} />);
    await screen.findByRole("dialog");
    rerender(<PromptEditorModal {...props} sourceKey="video" />);
    expect(done()).toBeDisabled();
    expect(document.querySelector("[data-prompt-editor-keep]")).toBeDisabled();
  });

  it("marks missing references honestly and preserves their exact literal if text is edited", async () => {
    const onDone = vi.fn();
    render(
      <PromptEditorModal
        open
        label="分镜提示词"
        initialValue="缺图 [Image 0] [Image 8]"
        tokens={[{ ...image("image-a", 8), missing: true }]}
        onDone={onDone}
        onCancel={vi.fn()}
      />,
    );
    await screen.findByRole("dialog");
    expect(editor().querySelectorAll("[data-image-missing]")).toHaveLength(2);
    expect(editor().querySelector("img")).toBeNull();
    editor().append(document.createTextNode("，继续说明"));
    fireEvent.input(editor());
    fireEvent.click(done());
    expect(onDone).toHaveBeenCalledWith(
      "缺图 [Image 0] [Image 8]，继续说明",
      [],
    );
  });
});

describe("unchanged local text during a real background update", () => {
  it("blocks an untouched stale editor and only writes its old text after an explicit keep choice", async () => {
    const onDone = vi.fn();
    const props = {
      open: true,
      label: "分镜提示词",
      initialValue: "原始文本",
      tokens: [],
      onDone,
      onCancel: vi.fn(),
    };
    const { rerender } = render(<PromptEditorModal {...props} />);
    await screen.findByRole("dialog");
    rerender(<PromptEditorModal {...props} initialValue="新的模型文本" />);
    expect(done()).toBeDisabled();
    fireEvent.click(done());
    expect(onDone).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector("[data-prompt-editor-keep]")!);
    fireEvent.click(done());
    expect(onDone).toHaveBeenCalledWith("原始文本", []);
  });
});
