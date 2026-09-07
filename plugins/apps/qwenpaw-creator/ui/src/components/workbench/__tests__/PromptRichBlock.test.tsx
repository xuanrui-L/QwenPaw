import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PromptRichBlock from "../PromptRichBlock";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";

vi.mock("@/components/agent/InlineReviewDiff", () => ({ default: () => null }));

describe("prompt entity names in preview and editor", () => {
  beforeEach(() => {
    useProjectSnapshotStore.getState().reset();
    const project = structuredClone(projectDocument);
    project.visual.entities.items["char:woman"] = {
      ...project.visual.entities.items.cat,
      entity_id: "char:woman",
      name: "找钥匙的女子",
    };
    useProjectSnapshotStore.setState({ projectId: "p1", project });
  });

  it("uses actual names while retaining image token identity and unchanged saved text", async () => {
    const raw = "[Image 2]角色身份（char:woman），台词为“char:woman”。";
    const onChange = vi.fn();
    const { container } = render(
      <PromptRichBlock
        label="视频提示词"
        value={raw}
        onChange={onChange}
        field="video_prompt"
        path="/creation/video_prompt"
        tokens={[
          { index: 2, name: "人物参考图", thumbUrl: null, kind: "entity" },
        ]}
      />,
    );
    expect(
      container.querySelector('[data-prompt-segment="all"]')?.textContent,
    ).toContain("角色身份（找钥匙的女子），台词为“char:woman”。");
    expect(container.querySelector('[data-prompt-token="2"]')).not.toBeNull();
    fireEvent.click(
      container.querySelector('[data-prompt-edit="video_prompt"]')!,
    );
    await screen.findByRole("dialog");
    const editor = document.querySelector("[data-prompt-token-editor]")!;
    expect(editor.textContent).toContain("角色身份（找钥匙的女子）");
    expect(editor.querySelector('[data-image-index="2"]')).not.toBeNull();
    fireEvent.click(document.querySelector("[data-prompt-editor-done]")!);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(raw));
  });

  it("cancelling the named editor does not write anything", async () => {
    const onChange = vi.fn();
    const { container } = render(
      <PromptRichBlock
        label="分镜提示词"
        value="角色：char:woman"
        onChange={onChange}
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[]}
      />,
    );
    fireEvent.click(
      container.querySelector('[data-prompt-edit="storyboard_prompt"]')!,
    );
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: /^取\s*消$/ }));
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("stage-specific inline reference tokens", () => {
  it("uses the supplied storyboard image at its actual text position and never carries the video image across a stage switch", async () => {
    const props = {
      label: "视频提示词",
      value: "先看 [Image 1] 再行动",
      field: "video_prompt",
      path: "/creation/video_prompt",
      onChange: vi.fn(),
      tokens: [
        {
          index: 1,
          referenceId: "sb-result",
          name: "已选分镜图",
          thumbUrl: "/media/sb-result.png",
          kind: "storyboard" as const,
        },
      ],
    };
    const { container, rerender } = render(<PromptRichBlock {...props} />);
    expect(
      container.querySelector('[data-prompt-token="1"] img'),
    ).toHaveAttribute("src", "/media/sb-result.png");
    rerender(
      <PromptRichBlock
        {...props}
        label="分镜提示词"
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[
          {
            index: 1,
            referenceId: "woman-reference",
            name: "女子参考图",
            thumbUrl: "/media/woman.png",
            kind: "entity",
          },
        ]}
      />,
    );
    const segment = container.querySelector('[data-prompt-segment="all"]')!;
    expect(segment.textContent).toBe("先看 IMG 1女子参考图 再行动");
    const pill = segment.querySelector<HTMLElement>('[data-prompt-token="1"]')!;
    expect(pill.querySelector("img")).toHaveAttribute(
      "src",
      "/media/woman.png",
    );
    expect(
      container.querySelector('img[src="/media/sb-result.png"]'),
    ).toBeNull();
    fireEvent.mouseEnter(pill);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.querySelector("img")).toHaveAttribute(
      "src",
      "/media/woman.png",
    );
    expect(tooltip).toHaveTextContent("女子参考图");
  });

  it("keeps missing, zero, out-of-range and duplicate indices unavailable without borrowing another image", () => {
    const { container } = render(
      <PromptRichBlock
        label="分镜提示词"
        value="[Image 0] [Image 1] [Image 2] [Image 3] [Image 9]"
        onChange={vi.fn()}
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[
          {
            index: 1,
            name: "缺失的参考图",
            referenceId: "missing",
            thumbUrl: null,
            kind: "artifact",
            missing: true,
          },
          {
            index: 2,
            name: "实际场景图",
            referenceId: "scene",
            thumbUrl: "/media/scene.png",
            kind: "artifact",
          },
          {
            index: 3,
            name: "不应猜测A",
            referenceId: "duplicate-a",
            thumbUrl: "/media/a.png",
            kind: "artifact",
          },
          {
            index: 3,
            name: "不应猜测B",
            referenceId: "duplicate-b",
            thumbUrl: "/media/b.png",
            kind: "artifact",
          },
        ]}
      />,
    );
    expect(
      container.querySelectorAll("[data-prompt-token-missing]"),
    ).toHaveLength(4);
    expect(container.querySelectorAll("[data-prompt-token]")).toHaveLength(1);
    expect(
      container.querySelector('[data-prompt-token="2"] img'),
    ).toHaveAttribute("src", "/media/scene.png");
    expect(
      container.querySelectorAll("[data-prompt-token-missing] button"),
    ).toHaveLength(0);
  });

  it("can block generation while leaving the real prompt editor available", () => {
    const { container } = render(
      <PromptRichBlock
        label="分镜提示词"
        value="正文"
        onChange={vi.fn()}
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[]}
        onRegenerate={vi.fn()}
        regenerateDisabled
        regenerateLabel="再次生成图片"
      />,
    );
    expect(container.querySelector("[data-prompt-edit]")).toBeEnabled();
    expect(container.querySelector("[data-prompt-regenerate]")).toBeDisabled();
  });
});

describe("unchanged editor preserves canonical prompt text", () => {
  it("does not rewrite raw references when an entity's public name updates while the editor is open", async () => {
    const project = structuredClone(projectDocument);
    project.visual.entities.items["char:woman"] = {
      ...project.visual.entities.items.cat,
      entity_id: "char:woman",
      name: "女子",
    };
    useProjectSnapshotStore.setState({ projectId: "p1", project });
    const raw = "人物（char:woman）站在门边。";
    const onChange = vi.fn();
    const { container } = render(
      <PromptRichBlock
        label="分镜提示词"
        value={raw}
        onChange={onChange}
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[]}
      />,
    );
    fireEvent.click(container.querySelector("[data-prompt-edit]")!);
    await screen.findByRole("dialog");
    const updated = structuredClone(project);
    updated.visual.entities.items["char:woman"].name = "背包女子";
    useProjectSnapshotStore.setState({ project: updated });
    await waitFor(() =>
      expect(
        container.querySelector('[data-prompt-segment="all"]'),
      ).toHaveTextContent("背包女子"),
    );
    fireEvent.click(document.querySelector("[data-prompt-editor-done]")!);
    expect(onChange).toHaveBeenCalledWith(raw);
  });
});

describe("legacy authored shot labels", () => {
  it("renders old authored labels as plain text without an obsolete link", () => {
    const { container } = render(
      <PromptRichBlock
        label="分镜提示词"
        value="【Shot 1】原始分镜描述"
        onChange={vi.fn()}
        field="storyboard_prompt"
        path="/creation/storyboard_prompt"
        tokens={[]}
      />,
    );
    expect(container.querySelector("[data-shot-link]")).toBeNull();
    expect(container).toHaveTextContent("原始分镜描述");
  });
});
