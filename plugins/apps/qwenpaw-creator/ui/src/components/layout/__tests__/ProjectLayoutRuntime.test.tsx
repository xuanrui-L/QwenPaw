import { seedProject } from "@/test/projectSnapshotFixtures";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CREATOR_ROUTE_OBJECTS } from "@/app/router";
import type { FileProjectReviewRecord } from "@/contracts/creator";
import ProjectLayout from "@/components/layout/ProjectLayout";
import { NavigationRuntime } from "@/routing/navigation";
import { useNavigationStore } from "@/store/navigationStore";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useCreatorSessionStore } from "@/store/creatorSessionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useFileProjectReviewStore } from "@/store/fileProjectReviewStore";
import { useModelCreditsStore } from "@/store/modelCreditsStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import {
  elementReview,
  commonRoutes,
  sessionState,
} from "@/test/agentFixtures";
import { status } from "@/test/creatorFixtures";
import { installMockFetch } from "@/test/mockFetch";

/** Mounts ProjectLayout with a stub workspace for all locator destinations. */
function renderShell(testId: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/project/:id",
        element: (
          <>
            <NavigationRuntime />
            <ProjectLayout />
          </>
        ),
        children: [
          { path: "*", element: <div data-testid={testId}>route</div> },
        ],
      },
    ],
    { initialEntries: ["/project/p1/plan"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("ProjectLayout visible shell", () => {
  beforeEach(() => {
    useCreatorSessionStore.getState().reset();
    useCreatorTaskViewStore.getState().reset();
    useAgentDockUiStore.getState().reset();
    useAgentDockUiStore.getState().clearUnsentDrafts();
    useCreatorInteractionStore.getState().reset();
    useProjectSnapshotStore.getState().reset();
    useFileProjectReviewStore.getState().reset();
    useNavigationStore.getState().clear();
    useModelCreditsStore.setState({ notice: null });
    seedProject();
  });

  it.each(["resolved", "rejected"])(
    "does not pile up authorization polls while a slow request is pending (%s)",
    async (outcome) => {
      installMockFetch(commonRoutes());
      let resolveRequest!: () => void;
      let rejectRequest!: (error: Error) => void;
      const pending = new Promise<void>((resolve, reject) => {
        resolveRequest = resolve;
        rejectRequest = reject;
      });
      const load = vi
        .spyOn(useExecutionAuthorizationStore.getState(), "load")
        .mockReturnValueOnce(pending)
        .mockResolvedValue();
      try {
        vi.useFakeTimers();
        await act(async () => {
          renderShell("slow-authorization-route");
        });
        expect(
          screen.getByTestId("slow-authorization-route"),
        ).toBeInTheDocument();
        await act(() => vi.advanceTimersByTimeAsync(12_000));
        act(() => {
          document.dispatchEvent(new Event("visibilitychange"));
          document.dispatchEvent(new Event("visibilitychange"));
        });
        expect(load).toHaveBeenCalledTimes(1);

        await act(async () => {
          if (outcome === "resolved") resolveRequest();
          else rejectRequest(new Error("temporary failure"));
          await pending.catch(() => undefined);
        });
        // The cadence now follows activity (5 s while nothing is pending), so
        // this waits past the idle interval. What is under test is that a slow
        // request never piles up and polling continues once it lands.
        await act(() => vi.advanceTimersByTimeAsync(6_000));
        expect(load).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
        load.mockRestore();
      }
    },
  );

  it("keeps polling tasks and the graph while the current session request is pending", async () => {
    const { calls, fetchMock } = installMockFetch(commonRoutes());
    renderShell("independent-production-polls");
    await waitFor(() =>
      expect(useCreatorSessionStore.getState().session).not.toBeNull(),
    );
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const original = fetchMock.getMockImplementation()!;
    let sessionFetches = 0;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/session")) {
        sessionFetches++;
        await pending;
      }
      return original(input, init);
    });
    try {
      const count = (path: string) =>
        calls.filter((call) => call.url.endsWith(path)).length;
      const tasks = count("/tasks"),
        graphs = count("/work-graph");
      for (let i = 0; i < 3; i++)
        await act(async () => {
          document.dispatchEvent(new Event("visibilitychange"));
        });
      expect(count("/tasks")).toBe(tasks + 3);
      expect(count("/work-graph")).toBe(graphs + 3);
      expect(sessionFetches).toBe(1);
    } finally {
      await act(async () => finish());
    }
  });

  it("keeps the Credits refusal in the navigation bar beside every project page", async () => {
    // The provider refuses an empty balance account-wide, so the fact cannot
    // belong to the one project whose failure tray happened to record it: it
    // has to survive going back to the list and entering another project.
    installMockFetch(commonRoutes());
    useModelCreditsStore.getState().markExhausted("p-other");
    render(
      <RouterProvider
        router={createMemoryRouter(CREATOR_ROUTE_OBJECTS, {
          initialEntries: ["/project/p1/plan"],
        })}
      />,
    );

    expect(await screen.findByText("测试项目")).toBeInTheDocument();
    const notice = document.querySelector("[data-model-credits-notice]");
    expect(notice).not.toBeNull();
    // Inside the 58px header, not in the page body.
    expect(notice?.closest("header")).not.toBeNull();
    // Display only: clicking it must not throw the user into the failed
    // project, which cannot fix an empty balance from there.
    expect(notice?.querySelector("a")).toBeNull();
  });

  it("settles a stale session after manual production finishes without lifecycle SSE", async () => {
    installMockFetch(commonRoutes());
    renderShell("manual-generation-route");
    await screen.findByTestId("manual-generation-route");
    await waitFor(() =>
      expect(useCreatorSessionStore.getState().session?.status).toBe("IDLE"),
    );
    await waitFor(() =>
      expect(useCreatorTaskViewStore.getState().loading).toBe(false),
    );
    act(() => {
      useCreatorSessionStore.setState({
        session: { ...sessionState, status: "WAITING_RUNTIME" },
      });
    });
    // Returning to a visible tab performs the same production revalidation as
    // its periodic tick; the server has no running tasks and an IDLE session.
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() =>
      expect(useCreatorSessionStore.getState().session?.status).toBe("IDLE"),
    );
    expect(document.querySelector("[data-agent-wait-hint]")).toBeNull();
  });

  it.each(["accepted", "manual fallback"])(
    "defers review navigation during mode handling and respects %s",
    async (outcome) => {
      installMockFetch(commonRoutes());
      const router = renderShell("auto-review-route");
      await screen.findByTestId("auto-review-route");
      await waitFor(() =>
        expect(useFileProjectReviewStore.getState().syncStatus).toBe("healthy"),
      );
      const value = elementReview();
      const poll = vi
        .spyOn(useFileProjectReviewStore.getState(), "pollOnce")
        .mockResolvedValue();
      try {
        await act(async () => {
          useFileProjectReviewStore.setState({
            reviews: [value],
            autoReviewIds: [value.review_id],
          });
          useCreatorSessionStore.getState().ingestEvents([
            {
              eventId: "design-completed",
              seq: 1,
              type: "agent.run.completed",
              projectId: "p1",
              creatorSessionId: "s1",
              at: "now",
              data: { runId: "run-element-1", reviewIds: [value.review_id] },
            },
          ]);
        });
        await waitFor(() => expect(poll).toHaveBeenCalled());
        expect(router.state.location.search).toBe("");
        await act(async () =>
          useFileProjectReviewStore.setState({
            autoReviewIds: [],
            reviews: outcome === "accepted" ? [] : [value],
          }),
        );
        if (outcome === "accepted") {
          expect(router.state.location.search).toBe("");
        } else {
          await waitFor(() =>
            expect(router.state.location.search).toContain("review=1"),
          );
        }
      } finally {
        poll.mockRestore();
      }
    },
  );
});
