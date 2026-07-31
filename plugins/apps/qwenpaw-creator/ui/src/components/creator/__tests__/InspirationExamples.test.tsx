import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { installMockFetch } from "@/test/mockFetch";

const push = vi.fn();
vi.mock("@/routing/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}));

import InspirationExamples from "../InspirationExamples";

const EXAMPLE = {
  id: "rainy-day-umbrella",
  title: "短剧制作",
  description: "做一个10秒左右的温馨短剧《雨天送伞》。",
  projectId: "project-af8e135859635dd9ad007dfd88434ba9",
  installed: false,
};

describe("InspirationExamples", () => {
  it("renders the bundled catalogue and opens an example into its project", async () => {
    const { calls } = installMockFetch([
      {
        match: "/examples/rainy-day-umbrella/open",
        method: "POST",
        response: {
          json: { projectId: EXAMPLE.projectId, installed: true },
        },
      },
      { match: "/examples", response: { json: { items: [EXAMPLE] } } },
    ]);
    render(<InspirationExamples />);

    const card = await screen.findByRole("button", { name: /短剧制作/ });
    expect(screen.getByText("灵感示例")).toBeInTheDocument();
    await userEvent.click(card);

    await waitFor(() =>
      expect(push).toHaveBeenCalledWith(`/project/${EXAMPLE.projectId}/plan`),
    );
    const opens = calls.filter((call) => call.method === "POST");
    expect(opens).toHaveLength(1);
    expect(opens[0].url).toContain("/examples/rainy-day-umbrella/open");
  });

  it("stays hidden when the backend has no bundled examples", async () => {
    installMockFetch([
      { match: "/examples", response: { json: { items: [] } } },
    ]);
    const { container } = render(<InspirationExamples />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
