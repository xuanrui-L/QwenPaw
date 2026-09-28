import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { message } from "antd";
import PresentationEditor from "../PresentationEditor";
import InteractionWorkbench from "../InteractionWorkbench";
import { formatControlPrompt, parseControlPrompt } from "../controlPrompt";
import { projectDocument } from "@/test/creatorFixtures";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";

const dispatch = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ dispatched: true }),
);
const getWorkGraph = vi.hoisted(() => vi.fn());
vi.mock("antd", () => ({
  message: { info: vi.fn(), success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api/creator/workGraph", () => ({
  dispatchWorkGraphNode: dispatch,
  getWorkGraph,
}));
vi.mock("@/routing/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
// The shared modal has its own UI tests. Exercise the actual persisted-prompt
// callbacks here; Chromium covers the same modal in the complete workspace.
vi.mock("@/pages/AssetsPage", () => ({
  GenerationPromptEditor: ({
    target,
    onSave,
    onRegenerate,
    regenerateLabel,
    saving,
    regenerating,
  }: any) => (
    <div data-testid={target.pointer}>
      <textarea
        aria-label={target.label}
        value={target.value}
        disabled={saving}
        onChange={(event) =>
          void onSave(target, event.target.value, []).catch(() => {})
        }
      />
      {onRegenerate && (
        <button
          disabled={saving}
          aria-busy={regenerating}
          onClick={onRegenerate}
        >
          {regenerateLabel}
        </button>
      )}
    </div>
  ),
}));
vi.mock("../PresentationPreview", () => ({
  PresentationPreview: ({ selection, onInspect }: any) => (
    <div data-testid="preview-screen">
      {selection.screen}
      <button onClick={() => onInspect({ screen: "title", action: "start" })}>
        选择实际开始按钮
      </button>
    </div>
  ),
}));
const patch = vi.fn().mockResolvedValue({});
const project = () => ({
  ...structuredClone(projectDocument),
  narrative_edges: [
    {
      edge_id: "edge:a",
      source_timeline_id: "timeline:main",
      target_timeline_id: "timeline:ep2",
      label: "继续",
      prompt: "下一步",
    },
  ],
  interactive_presentation: {
    design_prompt: "已有的 Agent 设计提示词",
    screens: {},
    motion: null,
  },
});
beforeEach(() => {
  vi.clearAllMocks();
  dispatch.mockResolvedValue({ dispatched: true });
  patch.mockResolvedValue({});
  useProjectSnapshotStore.setState({
    patch,
    pollOnce: vi.fn().mockResolvedValue(undefined),
  });
  useCreatorTaskViewStore.setState({
    refresh: vi.fn().mockResolvedValue(undefined),
  });
});

it("keeps page requirements visible before branches exist without offering an unavailable generation action", () => {
  render(
    <PresentationEditor project={{ ...project(), narrative_edges: [] }} />,
  );
  expect(screen.getByRole("tab", { name: "首页" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "播放页" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "剧情地图" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "结局页" })).toBeInTheDocument();
  expect(
    screen.getByText("分支连接尚未建立，剧情结构就绪后可生成作品页面。"),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "生成作品页面" }),
  ).not.toBeInTheDocument();
  expect(dispatch).not.toHaveBeenCalled();
});

it("saves button copy and appearance together with CAS before explicit regeneration", async () => {
  const value = project();
  const view = render(<PresentationEditor project={value} status="ready" />);
  fireEvent.click(screen.getByText("选择实际开始按钮"));
  expect(screen.queryByLabelText("按钮文案")).not.toBeInTheDocument();
  expect(screen.getAllByRole("textbox")).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("按钮生成提示词"), {
    target: {
      value: "按钮文案：沿海出发\n\n外观与动效：黑色细线，悬停时轻微上移",
    },
  });
  await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
  const operations = patch.mock.calls[0][1];
  expect(operations).toEqual([
    {
      op: "replace",
      path: "/interactive_presentation/screens",
      before: {},
      missingBefore: false,
      value: {
        title: {
          design_prompt: "",
          controls: {
            start: {
              label: "沿海出发",
              design_prompt: "黑色细线，悬停时轻微上移",
            },
          },
        },
      },
    },
  ]);
  expect(dispatch).not.toHaveBeenCalled();
  view.rerender(
    <PresentationEditor
      project={{
        ...value,
        interactive_presentation: {
          ...value.interactive_presentation,
          screens: operations[0].value,
        },
      }}
      status="ready"
    />,
  );
  expect(screen.getByLabelText("按钮生成提示词")).toHaveValue(
    "按钮文案：沿海出发\n\n外观与动效：黑色细线，悬停时轻微上移",
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "生成作品页面" })).toBeEnabled(),
  );
  const details = view.container.querySelector(
    "[data-interaction-details]",
  ) as HTMLElement;
  fireEvent.click(
    within(details).getByRole("button", { name: "生成作品页面" }),
  );
  await waitFor(() =>
    expect(dispatch).toHaveBeenCalledWith(
      value.project_id,
      "interaction:project",
      { regenerate: true },
    ),
  );
  expect(
    screen.queryByRole("button", { name: "保存设计" }),
  ).not.toBeInTheDocument();
});

it.each([
  { label: "沿海\n出发", design_prompt: "黑色细线\n悬停时轻微上移" },
  { label: "", design_prompt: "" },
])(
  "preserves multiline or empty button fields in one editor: %j",
  (control) => {
    expect(
      parseControlPrompt(
        formatControlPrompt(control.label, control.design_prompt),
      ),
    ).toEqual(control);
  },
);

it("keeps incomplete combined input from overwriting button fields", async () => {
  render(<PresentationEditor project={project()} />);
  fireEvent.click(screen.getByText("选择实际开始按钮"));
  fireEvent.change(screen.getByLabelText("按钮生成提示词"), {
    target: { value: "按钮文案：沿海出发" },
  });
  await waitFor(() => expect(message.error).toHaveBeenCalled());
  expect(patch).not.toHaveBeenCalled();
  expect(dispatch).not.toHaveBeenCalled();
});

it("shows start, map and restart as required on the homepage before any media exists", () => {
  render(<PresentationEditor project={project()} />);
  for (const action of ["开始", "剧情地图", "重新开始"])
    expect(
      screen.getByRole("button", { name: `${action} · 必备` }),
    ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "播放页" }));
  expect(screen.getByTestId("preview-screen")).toHaveTextContent("play");
  expect(
    screen.getByRole("button", { name: "重新开始 · 必备" }),
  ).toBeInTheDocument();
});

it("never dispatches on a failed prompt save", async () => {
  patch.mockRejectedValueOnce(new Error("Concurrent edit"));
  render(<PresentationEditor project={project()} />);
  fireEvent.change(screen.getByLabelText("首页生成提示词"), {
    target: { value: "用户修改" },
  });
  await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
  expect(dispatch).not.toHaveBeenCalled();
});

it.each(["waiting_review", "running"])(
  "locks edits and generation while %s",
  (status) => {
    render(<PresentationEditor project={project()} status={status} />);
    expect(screen.getByLabelText("首页生成提示词")).toBeDisabled();
    expect(
      screen.getByRole("button", {
        name: status === "running" ? "生成中…" : "生成作品页面",
      }),
    ).toBeDisabled();
  },
);

it("keeps four page tabs and opens the choice inspector inside the play page", () => {
  const value = project();
  value.timelines.items["timeline:main"].elements_by_id["choice:one"] = {
    ...value.timelines.items["timeline:main"].elements_by_id["r2v-window"],
    element_id: "choice:one",
    enabled: true,
    creation: {
      type: "interaction",
      question: "向哪边走？",
      design_prompt: "旗帜形选项",
      options: [{ edge_ref: "edge:a", design_prompt: "沿岸路线按钮" }],
    },
  };
  render(
    <PresentationEditor
      project={value}
      statuses={{ "interaction:choice:one": "failed" }}
      errors={{ "interaction:choice:one": "抉择模型返回空内容" }}
    />,
  );
  expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "首页",
    "播放页",
    "剧情地图",
    "结局页",
  ]);
  expect(screen.queryByText("作品界面")).not.toBeInTheDocument();
  expect(screen.queryByText("作品页面与功能按钮")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "播放页" }));
  fireEvent.change(screen.getByLabelText("播放页预览内容"), {
    target: { value: "choice:one" },
  });
  expect(screen.getByLabelText("抉择生成提示词")).toHaveValue("旗帜形选项");
  expect(screen.getByRole("alert")).toHaveTextContent("抉择模型返回空内容");
  expect(
    screen.getByRole("button", { name: "生成抉择动效" }),
  ).toBeInTheDocument();
  expect(screen.getByTestId("preview-screen")).toHaveTextContent("play");
});

it("shows immediate progress, retains a rejected generation and clears it on retry", async () => {
  let reject!: (error: Error) => void;
  dispatch.mockReturnValueOnce(
    new Promise((_, fail) => {
      reject = fail;
    }),
  );
  render(<PresentationEditor project={project()} status="ready" />);
  fireEvent.click(screen.getByRole("button", { name: "生成作品页面" }));
  expect(screen.getByRole("button", { name: "生成中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "生成中…" })).toHaveAttribute(
    "aria-busy",
    "true",
  );
  reject(new Error("Text model 返回空内容"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Text model 返回空内容",
  );
  fireEvent.click(screen.getByRole("tab", { name: "剧情地图" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Text model 返回空内容");
  fireEvent.click(screen.getByRole("button", { name: "生成作品页面" }));
  await waitFor(() => expect(dispatch).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
  );
});

it("does not report success when generation was not admitted", async () => {
  dispatch.mockResolvedValueOnce({ dispatched: false });
  render(<PresentationEditor project={project()} />);
  fireEvent.click(screen.getByRole("button", { name: "生成作品页面" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("生成未能启动");
  expect(message.success).not.toHaveBeenCalled();
});

it("restores durable generation failures from the work graph after reopening the workspace", async () => {
  getWorkGraph.mockResolvedValue({
    nodes: [
      {
        id: "interaction:project",
        status: "failed",
        error: "服务端保存的失败原因",
      },
    ],
  });
  const view = render(
    <InteractionWorkbench project={project()} open onOpenChange={vi.fn()} />,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "服务端保存的失败原因",
  );
  view.unmount();
  render(
    <InteractionWorkbench project={project()} open onOpenChange={vi.fn()} />,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "服务端保存的失败原因",
  );
});

it("reports failures to fetch generation status instead of silently dropping them", async () => {
  getWorkGraph.mockRejectedValue(new Error("连接中断"));
  render(
    <InteractionWorkbench project={project()} open onOpenChange={vi.fn()} />,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "无法更新生成状态：连接中断",
  );
});
