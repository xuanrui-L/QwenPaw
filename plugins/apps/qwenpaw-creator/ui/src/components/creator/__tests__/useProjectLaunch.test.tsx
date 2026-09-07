import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NavigationRuntime } from "@/routing/navigation";
import { useLocation } from "react-router-dom";
import { useProjectLaunch } from "../useProjectLaunch";
import { useModelConfigStore } from "@/store/modelConfigStore";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";

describe("project launch landing", () => {
  it.each([false, true])(
    "opens the blueprint immediately (with attachments: %s)",
    async (withAttachments) => {
      const { calls } = installMockFetch([
        { match: "/models/config", response: { json: configuredModelConfig } },
        {
          match: "/projects",
          method: "POST",
          response: { json: { projectId: "new-story" } },
        },
      ]);
      useModelConfigStore.setState({ config: configuredModelConfig });
      const { result } = renderHook(
        () => ({
          launch: useProjectLaunch({
            initialValues: {
              name: "快乐小狗",
              description: "小狗在草地上轻快跳跃",
              scenario: "short_drama",
              contentType: null,
              resolution: "720P",
              aspectRatio: "16:9",
              sourceUrls: withAttachments
                ? ["https://example.com/puppy.png"]
                : [],
            },
          }),
          location: useLocation(),
        }),
        {
          wrapper: ({ children }) => (
            <MemoryRouter>
              <NavigationRuntime />
              {children}
            </MemoryRouter>
          ),
        },
      );
      await act(async () => {
        await result.current.launch.handleLaunch();
      });
      await waitFor(() =>
        expect(result.current.location.pathname).toBe("/project/new-story"),
      );
      const creation = calls.find(
        (call) => call.method === "POST" && call.url.endsWith("/projects"),
      );
      expect(creation?.body).toMatchObject({ name: "快乐小狗" });
      if (!withAttachments)
        expect(creation?.body).toMatchObject({
          initialGoal: "小狗在草地上轻快跳跃",
        });
    },
  );
});
