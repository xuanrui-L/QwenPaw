import { useRef, useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MentionInput } from "@/components/agent";
import type { MentionInputHandle } from "@/components/agent/MentionInput";
import {
  type SelectionAttachment,
  useAgentDockUiStore,
} from "@/store/agentDockUiStore";
import SelectionToolbar from "@/components/agent/SelectionToolbar";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";

const selection: SelectionAttachment = {
  text: "雪夜 SUV",
  ref: "element:opening",
  field: "element:opening/label",
  path: "/timelines/items/timeline:main/elements_by_id/opening/label",
  start: 2,
  end: 8,
  label: "Element 文本",
};

function Harness() {
  const ref = useRef<MentionInputHandle>(null);
  const [serialized, setSerialized] = useState("");
  const read = () => setSerialized(JSON.stringify(ref.current?.getContent()));
  return (
    <>
      <MentionInput ref={ref} onChange={vi.fn()} onSubmit={vi.fn()} />
      <button onClick={() => ref.current?.insertSelection(selection)}>
        插入选择
      </button>
      <button onClick={read}>读取内容</button>
      <output data-testid="serialized">{serialized}</output>
    </>
  );
}

describe("MentionInput inline Timeline/Element selection", () => {
  it("inserts and serializes an exact Project JSON Pointer selection", () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("插入选择"));
    fireEvent.click(screen.getByText("读取内容"));

    const editor = screen.getByRole("textbox");
    expect(editor.querySelector("[data-selection-ref]")).toHaveTextContent(
      "雪夜 SUV",
    );
    const serialized = screen.getByTestId("serialized");
    expect(serialized).toHaveTextContent("element:opening/label");
    expect(serialized).toHaveTextContent(
      "/timelines/items/timeline:main/elements_by_id/opening/label",
    );
  });

  it("restores the structured selection from clipboard HTML", () => {
    render(<Harness />);
    const token = encodeURIComponent(JSON.stringify(selection));
    fireEvent.paste(screen.getByRole("textbox"), {
      clipboardData: {
        getData: (type: string) =>
          type === "text/html"
            ? `<span data-selection-ref="${token}">雪夜 SUV</span>`
            : "",
      },
    });
    fireEvent.click(screen.getByText("读取内容"));
    expect(screen.getByTestId("serialized")).toHaveTextContent(
      "element:opening",
    );
  });
});

describe("SelectionToolbar project locator", () => {
  beforeEach(() => {
    useAgentDockUiStore.getState().reset();
    useCreatorInteractionStore.getState().reset();
    window.getSelection()?.removeAllRanges();
  });

  it("opens for a Creator field and keeps its semantic locator and exact project.json path", async () => {
    const field = "element:r2v-1/label";
    const path = "/timelines/items/timeline:main/elements_by_id/r2v-1/label";
    const { container } = render(
      <>
        <p
          data-creator-field={field}
          data-creator-path={path}
          data-creator-field-label="VLM 分镜 1 · 描述"
        >
          源视频关键帧
        </p>
        <SelectionToolbar />
      </>,
    );
    const target = container.querySelector("p")!;
    const range = document.createRange();
    range.selectNodeContents(target);
    // The toolbar anchors on the mouseup point; only rect edges are read.
    range.getBoundingClientRect = () =>
      ({ top: 80, right: 160, bottom: 96, left: 80 }) as DOMRect;
    window.getSelection()!.addRange(range);

    fireEvent.mouseUp(target, { clientX: 120, clientY: 80 });
    fireEvent.click(await screen.findByRole("button", { name: "添加到对话" }));

    expect(useAgentDockUiStore.getState().selection).toMatchObject({
      text: "源视频关键帧",
      ref: "element:r2v-1",
      field,
      path,
      label: "VLM 分镜 1 · 描述",
      start: 0,
      end: 6,
    });
    expect(useAgentDockUiStore.getState()).toMatchObject({
      open: true,
      tab: "conversation",
    });
  });
});
