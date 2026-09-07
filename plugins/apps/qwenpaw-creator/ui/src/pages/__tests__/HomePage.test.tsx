import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import HomePage from "@/pages/HomePage";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";

function renderProject(
  finalVideoVersionId: string | null = null,
  scenario = "short_drama",
) {
  installMockFetch([
    { match: "/models/config", response: { json: configuredModelConfig } },
    {
      match: "/projects",
      response: {
        json: {
          items: [
            {
              projectId: "p1",
              name: "雪夜短片",
              description: "一段项目说明",
              scenario,
              aspectRatio: "16:9",
              resolution: "720P",
              createdAt: "2026-07-01T00:00:00Z",
              updatedAt: "2026-07-02T00:00:00Z",
              coverVersionId: "ver-cover",
              coverVersionSource: "artifact",
              finalVideoVersionId,
            },
          ],
          limit: 100,
          offset: 0,
        },
      },
    },
  ]);
  const view = render(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("tab", { name: "我的项目" }));
  return view;
}

describe("HomePage project actions", () => {
  it("shows project metadata and returns to creation without offering a missing final cut", async () => {
    renderProject();
    expect(await screen.findByText("雪夜短片")).toBeInTheDocument();
    for (const text of ["一段项目说明", "短剧", "16:9", "720P"])
      expect(screen.getByText(text)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "删除 雪夜短片" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "预览 雪夜短片 成片" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始创作" }));
    expect(screen.getByRole("tab", { name: "开始创作" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("plays the selected final cut rather than the cover from an editing project card", async () => {
    const { baseElement } = renderProject("ver-final", "video_edit");
    fireEvent.click(
      await screen.findByRole("button", { name: "预览 雪夜短片 成片" }),
    );
    expect(screen.getByText("剪辑")).toBeInTheDocument();
    expect(baseElement.querySelector("video")).toHaveAttribute(
      "src",
      expect.stringContaining("/media/artifacts/ver-final"),
    );
  });
});
