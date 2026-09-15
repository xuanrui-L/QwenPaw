import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ExecutionAuthorizationCard from "../ExecutionAuthorizationCard";
import { navigate } from "@/routing/navigation";
import { useNavigationStore } from "@/store/navigationStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { makePendingAuthorization } from "@/test/agentFixtures";
import { projectDocument } from "@/test/creatorFixtures";

vi.mock("@/routing/navigation", () => ({ navigate: vi.fn() }));

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.clearAllMocks();
  useNavigationStore.getState().clear();
  useExecutionAuthorizationStore.getState().reset();
  useProjectSnapshotStore.setState({ projectId: null, project: null });
});

it.each([
  {
    targetRef: "project:p1",
    operation: "生成作品页面",
    field: "/interactive_presentation/design_prompt",
  },
  {
    targetRef: "element:choice:one",
    operation: "生成抉择动效",
    field:
      "/timelines/items/timeline:main/elements_by_id/choice:one/creation/design_prompt",
  },
])("opens the real $operation input from its confirmation card", (test) => {
  vi.useFakeTimers();
  const project = structuredClone(projectDocument);
  const timeline = project.timelines.items["timeline:main"];
  timeline.elements_by_id["choice:one"] = {
    ...timeline.elements_by_id["r2v-window"],
    element_id: "choice:one",
    creation: {
      type: "interaction",
      question: "向哪边走？",
      design_prompt: "两张车票作为选择按钮",
      options: [{ edge_ref: "edge:left" }, { edge_ref: "edge:right" }],
    },
  };
  useProjectSnapshotStore.setState({ projectId: "p1", project });
  useExecutionAuthorizationStore.setState({ projectId: "p1" });
  render(
    <ExecutionAuthorizationCard
      project={project}
      authorization={makePendingAuthorization({
        targetRef: test.targetRef,
        scope: { operation: "interaction_draft" },
        provider: "text",
        model: "qwen3.8-max",
      })}
    />,
  );
  expect(screen.getByText(`${test.operation}等待确认`)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看" }));
  const target = vi.mocked(navigate).mock.calls[0][0] as string;
  expect(target.split("?")[0]).toBe("/project/p1");
  expect(new URLSearchParams(target.split("?")[1]).get("field")).toBe(
    test.field,
  );
  expect(target).not.toContain("storyboard_prompt");
});
