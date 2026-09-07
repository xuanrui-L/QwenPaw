import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkspaceEmptyState from "@/components/WorkspaceEmptyState";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useLaunchUploadStore } from "@/store/launchUploadStore";
import type { CreatorSessionView } from "@/contracts/creator";
import { projectDocument } from "@/test/creatorFixtures";
import i18n from "@/i18n";

function session(status: CreatorSessionView["status"]): CreatorSessionView {
  return {
    id: "session-test",
    projectId: "p1",
    status,
    lastMessageSeq: 0,
    lastConsumedMessageSeq: 0,
    lastEventSeq: 0,
  };
}
function seed(status: CreatorSessionView["status"] = "RUNNING") {
  useCreatorSessionStore.setState({
    projectId: "p1",
    session: session(status),
    connectionState: "connected",
  });
}
const stateElement = () => document.querySelector("[data-workspace-empty]")!;

describe("Workspace first-output feedback", () => {
  beforeEach(() => {
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useLaunchUploadStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useProjectSnapshotStore.setState({
      projectId: "p1",
      project: structuredClone(projectDocument),
    });
  });
  afterEach(() => vi.useRealTimers());

  it("covers attachment preparation before session hydration without leaking another project's activity", () => {
    useLaunchUploadStore.getState().begin("p1", 3);
    useLaunchUploadStore.getState().fileFinished("p1", "notes.txt", true);
    const { rerender } = render(
      <WorkspaceEmptyState projectId="p1" area="blueprint" />,
    );
    expect(stateElement()).toHaveAttribute("data-state", "preparing");
    expect(screen.getByRole("status")).toHaveTextContent(
      i18n.t("launchUpload.uploading", { done: 1, total: 3 }),
    );
    act(() => useLaunchUploadStore.getState().messaging("p1"));
    expect(screen.getByRole("status")).toHaveTextContent(
      i18n.t("launchUpload.messaging"),
    );
    rerender(<WorkspaceEmptyState projectId="p2" area="blueprint" />);
    expect(stateElement()).toHaveAttribute("data-state", "idle");
    rerender(<WorkspaceEmptyState projectId="p1" area="blueprint" />);
    act(() => {
      useLaunchUploadStore.getState().finish("p1", true);
      seed();
    });
    expect(stateElement()).toHaveAttribute("data-state", "working");
  });

  it("starts feedback immediately from the pending send and clears it after a failed send", () => {
    seed("IDLE");
    render(<WorkspaceEmptyState projectId="p1" area="blueprint" />);
    expect(stateElement()).toHaveAttribute("data-state", "idle");
    act(() =>
      useCreatorSessionStore.setState({
        queuedUi: [
          {
            clientMessageId: "msg-1",
            requestSignature: "request",
            text: "写短剧",
            state: "sending",
          },
        ],
      }),
    );
    expect(stateElement()).toHaveAttribute("data-state", "working");
    expect(screen.getByRole("status")).toHaveTextContent(
      i18n.t("liveStatus.commandSent"),
    );
    expect(
      stateElement().querySelector(".workspace-empty-flow"),
    ).toBeInTheDocument();
    act(() =>
      useCreatorSessionStore.setState({
        queuedUi: [
          {
            clientMessageId: "msg-1",
            requestSignature: "request",
            text: "写短剧",
            state: "failed",
          },
        ],
      }),
    );
    expect(stateElement()).toHaveAttribute("data-state", "idle");
    expect(
      stateElement().querySelector(".workspace-empty-flow"),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["RUNNING", "working"],
    ["RESUMING", "working"],
    ["WAITING_RUNTIME", "working"],
    ["WAITING_USER_INPUT", "waiting"],
    ["WAITING_EXECUTION_AUTH", "waiting"],
    ["PENDING_REVIEW", "waiting"],
    ["INTERRUPT_REQUESTED", "stopping"],
    ["CANCELLED", "stopped"],
    ["ERROR", "error"],
    ["IDLE", "idle"],
  ] as const)(
    "reflects %s without fabricating running activity",
    (status, expected) => {
      seed(status);
      render(<WorkspaceEmptyState projectId="p1" area="blueprint" />);
      expect(stateElement()).toHaveAttribute("data-state", expected);
      expect(
        Boolean(stateElement().querySelector(".workspace-empty-flow")),
      ).toBe(expected === "working");
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    },
  );

  it("shows current retry events but never raw milestones or specialist instructions", () => {
    seed();
    useCreatorSessionStore.setState({
      agentStatusBar: {
        progress: {
          phase: "creative_strategy",
          label: "private project.json /timelines/items secret",
          sourceEventSeq: 1,
          updatedAt: "now",
        },
        badges: [],
      },
      rateLimitRetry: {
        runId: "run-1",
        attempt: 2,
        maxAttempts: 3,
        reason: "rate_limit",
      },
    });
    render(<WorkspaceEmptyState projectId="p1" area="blueprint" />);
    expect(screen.getByRole("status")).toHaveTextContent(
      i18n.t("liveStatus.rateLimitRetrying", { attempt: 2, max: 3 }),
    );
    expect(stateElement()).not.toHaveTextContent(
      /private|project\.json|secret/,
    );
    act(() =>
      useCreatorSessionStore.setState({
        rateLimitRetry: null,
        subagentActivities: {
          specialist: {
            parentActionId: "parent",
            runId: "specialist",
            role: "script_agent",
            roleDisplayName: "private worker",
            delegationText: "secret patch /timelines/items",
            targetRefs: [],
            firstEventSeq: 3,
            completed: false,
            status: "RUNNING_MODEL",
            messages: {},
            tools: {},
          },
        },
      }),
    );
    expect(stateElement()).not.toHaveTextContent(/private|secret|\/timelines/);
  });

  it("distinguishes reconnecting and historical replay from creative work", () => {
    seed();
    const { rerender } = render(
      <WorkspaceEmptyState projectId="p1" area="episodes" compact />,
    );
    act(() =>
      useCreatorSessionStore.setState({ connectionState: "reconnecting" }),
    );
    expect(stateElement()).toHaveAttribute("data-state", "reconnecting");
    expect(
      stateElement().querySelector(".workspace-empty-flow"),
    ).not.toBeInTheDocument();
    act(() =>
      useCreatorSessionStore.setState({
        connectionState: "connected",
        isReplaying: true,
      }),
    );
    expect(stateElement()).toHaveAttribute("data-state", "loading");
    rerender(<WorkspaceEmptyState projectId="p2" area="episodes" compact />);
    expect(stateElement()).toHaveAttribute("data-state", "idle");
  });

  it("keeps detached work visible after the main agent becomes idle", () => {
    seed("IDLE");
    useCreatorTaskViewStore.setState({
      projectId: "p1",
      tasks: [
        {
          id: "task",
          projectId: "p1",
          kind: "source_intelligence",
          targetRef: "timeline:timeline:main",
          status: "RUNNING",
          progress: null,
          resultRefs: [],
          transactionId: null,
          specialistRunId: null,
        },
      ],
    });
    render(<WorkspaceEmptyState projectId="p1" area="script" />);
    expect(stateElement()).toHaveAttribute("data-state", "working");
    act(() => useCreatorTaskViewStore.setState({ projectId: "p2" }));
    expect(stateElement()).toHaveAttribute("data-state", "idle");
  });

  it("adds calm long-wait guidance without a simulated percentage or completion estimate", () => {
    vi.useFakeTimers();
    seed();
    render(<WorkspaceEmptyState projectId="p1" area="assets" />);
    act(() => vi.advanceTimersByTime(30_000));
    expect(stateElement()).toHaveTextContent(i18n.t("workspaceEmpty.longWait"));
    expect(stateElement()).not.toHaveTextContent(/\d+%|预计|seconds remaining/);
    act(() =>
      useCreatorSessionStore.setState({ session: session("PENDING_REVIEW") }),
    );
    expect(stateElement()).not.toHaveTextContent(
      i18n.t("workspaceEmpty.longWait"),
    );
  });
});
