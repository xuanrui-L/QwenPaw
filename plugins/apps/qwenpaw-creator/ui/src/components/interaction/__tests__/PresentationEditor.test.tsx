import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PresentationEditor from "../PresentationEditor";
import { projectDocument } from "@/test/creatorFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";

const dispatch = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ status: "running" }),
);
vi.mock("antd", () => ({
  message: { info: vi.fn(), success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api/creator/workGraph", () => ({ dispatchWorkGraphNode: dispatch }));
vi.mock("../PresentationPreview", () => ({
  PresentationPreview: ({
    selection,
    onInspect,
  }: {
    selection: { screen: string };
    onInspect: (value: object) => void;
  }) => (
    <div data-testid="preview-screen">
      {selection.screen}
      <button onClick={() => onInspect({ screen: "title", action: "start" })}>
        选择实际开始按钮
      </button>
    </div>
  ),
}));
const patch = vi.fn().mockResolvedValue({});
function project() {
  return {
    ...structuredClone(projectDocument),
    interactive_presentation: {
      design_prompt: "原始设计",
      screens: {},
      motion: null,
    },
  };
}
beforeEach(() => {
  patch.mockClear();
  dispatch.mockClear();
  useProjectSnapshotStore.setState({ patch });
});

describe("interactive interface design", () => {
  it("locates real controls and saves semantic design without overwriting HTML", async () => {
    const value = project();
    render(<PresentationEditor project={value} status="ready" />);
    fireEvent.click(screen.getByText("选择实际开始按钮"));
    fireEvent.change(screen.getByLabelText("按钮文案"), {
      target: { value: "沿海出发" },
    });
    fireEvent.change(screen.getByLabelText("按钮外观与动效"), {
      target: { value: "手写边框" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存并生成页面" }));
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith(
        value.project_id,
        "interaction:project",
      ),
    );
    const ops = patch.mock.calls[0][1];
    expect(ops.map((op: { path: string }) => op.path)).toEqual([
      "/interactive_presentation/design_prompt",
      "/interactive_presentation/screens",
    ]);
    expect(ops[1].value.title.controls.start).toEqual({
      label: "沿海出发",
      design_prompt: "手写边框",
    });
  });

  it("retains drafts across page tabs and exposes mandatory restart/map controls", () => {
    render(<PresentationEditor project={project()} status="ready" />);
    fireEvent.change(screen.getByLabelText("首页设计要求"), {
      target: { value: "封面边缘绘制海岸" },
    });
    fireEvent.click(screen.getByRole("tab", { name: "播放页" }));
    expect(screen.getByTestId("preview-screen")).toHaveTextContent("play");
    expect(
      screen.getByRole("button", { name: "重新开始 · 必备" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "剧情地图 · 必备" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "首页" }));
    expect(screen.getByLabelText("首页设计要求")).toHaveValue(
      "封面边缘绘制海岸",
    );
  });

  it("does not silently overwrite a concurrent design edit", () => {
    const value = project();
    const view = render(<PresentationEditor project={value} status="ready" />);
    fireEvent.change(screen.getByLabelText("整体视觉与动效要求"), {
      target: { value: "未保存的用户设计" },
    });
    view.rerender(
      <PresentationEditor
        project={{
          ...value,
          interactive_presentation: {
            ...value.interactive_presentation,
            design_prompt: "新服务端设计",
          },
        }}
        status="ready"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("设计已被其他操作更新");
    expect(screen.getByLabelText("整体视觉与动效要求")).toHaveValue(
      "未保存的用户设计",
    );
    expect(
      screen.getByRole("button", { name: "保存并生成页面" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "撤销未保存修改" }));
    expect(screen.getByLabelText("整体视觉与动效要求")).toHaveValue(
      "新服务端设计",
    );
  });

  it("requires review resolution before generating another page", () => {
    render(<PresentationEditor project={project()} status="waiting_review" />);
    fireEvent.change(screen.getByLabelText("首页设计要求"), {
      target: { value: "一次修改" },
    });
    expect(screen.getByRole("button", { name: "保存设计" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "保存并生成页面" }),
    ).toBeDisabled();
  });
});
