import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExecutionAuthorizationCard from "@/components/agent/ExecutionAuthorizationCard";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import {
  useProjectSnapshotStore,
  type ProjectSnapshotState,
} from "@/store/projectSnapshotStore";
import { projectDocument } from "@/test/creatorFixtures";
import { makePendingAuthorization } from "@/test/agentFixtures";

vi.mock("@/routing/locators", () => ({ navigateToLocator: vi.fn() }));

type PatchFn = ProjectSnapshotState["patch"];

const STORYBOARD_POINTER =
  "/timelines/items/timeline:main/elements_by_id/r2v-window/creation/storyboard_prompt";

function seed(patch: PatchFn) {
  useProjectSnapshotStore.setState({
    projectId: "p1",
    project: projectDocument,
    generation: 3,
    etag: "etag-3",
    patching: false,
    patch,
  });
  useExecutionAuthorizationStore.setState({ projectId: "p1" });
}

afterEach(() => {
  useExecutionAuthorizationStore.getState().reset();
  useProjectSnapshotStore.getState().reset();
});

describe("ExecutionAuthorizationCard inline prompt editing", () => {
  it("edits and CAS-saves the target prompt before confirming generation", async () => {
    const patchMock = vi.fn(async () => ({
      project: projectDocument,
      generation: 4,
      etag: "etag-4",
    }));
    seed(patchMock as unknown as PatchFn);
    const authorization = makePendingAuthorization({
      targetRef: "element:r2v-window",
      scope: { operation: "image_generation" },
    });

    render(
      <ExecutionAuthorizationCard
        authorization={authorization}
        project={projectDocument}
      />,
    );

    // The editor stays collapsed until requested; the toggle is offered only
    // for production confirmations that resolve to a concrete prompt field.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑提示词" }));

    const textarea = screen.getByDisplayValue("暖色餐厅窗外的橘猫");
    // Save is inert until the draft actually diverges from the stored value.
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    fireEvent.change(textarea, { target: { value: "暖色窗边打盹的橘猫" } });

    const save = screen.getByRole("button", { name: "保存" });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    expect(patchMock).toHaveBeenCalledWith("p1", [
      expect.objectContaining({
        op: "replace",
        path: STORYBOARD_POINTER,
        before: "暖色餐厅窗外的橘猫",
        value: "暖色窗边打盹的橘猫",
      }),
    ]);
    // A successful save collapses the editor so the card returns to confirm.
    await waitFor(() =>
      expect(screen.queryByRole("textbox")).not.toBeInTheDocument(),
    );
  });

  it("discards an in-progress edit without patching", async () => {
    const patchMock = vi.fn(async () => ({
      project: projectDocument,
      generation: 4,
      etag: "etag-4",
    }));
    seed(patchMock as unknown as PatchFn);
    const authorization = makePendingAuthorization({
      targetRef: "element:r2v-window",
      scope: { operation: "image_generation" },
    });

    render(
      <ExecutionAuthorizationCard
        authorization={authorization}
        project={projectDocument}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑提示词" }));
    fireEvent.change(screen.getByDisplayValue("暖色餐厅窗外的橘猫"), {
      target: { value: "临时改动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "放弃" }));

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("does not offer prompt editing on a content checkpoint card", () => {
    seed(vi.fn() as unknown as PatchFn);
    const authorization = makePendingAuthorization({
      targetRef: "timeline:main",
      scope: { operation: "creation_checkpoint_script" },
    });

    render(
      <ExecutionAuthorizationCard
        authorization={authorization}
        project={projectDocument}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "编辑提示词" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
