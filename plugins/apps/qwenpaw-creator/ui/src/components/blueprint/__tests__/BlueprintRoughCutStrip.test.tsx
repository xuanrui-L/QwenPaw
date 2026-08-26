import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BlueprintRoughCutStrip from "@/components/blueprint/BlueprintRoughCutStrip";
import { projectDocument } from "@/test/creatorFixtures";
import type { ProjectDocument } from "@/contracts/creator";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

/**
 * Promote the fixture's rendered cut to the project-level final_video —
 * the artifact kind the backend derives `finalVideoVersionId` from and
 * the strip's whole-film chip keys on.
 */
function withWholeFilm(project: ProjectDocument): ProjectDocument {
  project.assets.artifact_versions_by_id["final-v1"].kind = "final_video";
  return project;
}

function renderStrip(project: ProjectDocument) {
  return render(
    <BlueprintRoughCutStrip project={project} onSelectTimeline={vi.fn()} />,
  );
}

describe("BlueprintRoughCutStrip whole-film preview", () => {
  it("offers no whole-film chip before a final_video is composed", () => {
    const { container } = renderStrip(cloneProject());
    expect(container.querySelector("[data-blueprint-roughcut]")).toBeTruthy();
    expect(container.querySelector("[data-roughcut-play-film]")).toBeNull();
  });

  it("plays the entire composed film from the dedicated chip", () => {
    const { container, getByText } = renderStrip(withWholeFilm(cloneProject()));

    const chip = container.querySelector("[data-roughcut-play-film]");
    expect(chip).toBeTruthy();
    fireEvent.click(chip!);

    const video = container.querySelector<HTMLVideoElement>(
      "[data-roughcut-player] video",
    );
    expect(video).toBeTruthy();
    // Streams the final film artifact, not a per-timeline draft rough cut.
    expect(video!.getAttribute("src")).toContain("/media/artifacts/final-v1");
    expect(getByText("测试项目 · 成片")).toBeInTheDocument();

    // Toggling the chip again closes the player.
    fireEvent.click(chip!);
    expect(container.querySelector("[data-roughcut-player]")).toBeNull();
  });

  it("keeps the whole-film entry even when the shots-only filter leaves no frames", () => {
    // a290de0e regression shape: every element filtered out of the strip
    // (e.g. overlay/audio/interaction-only timelines) must not hide the
    // 成片 entry point along with the frames.
    const project = withWholeFilm(cloneProject());
    for (const timeline of Object.values(project.timelines.items)) {
      for (const element of Object.values(timeline.elements_by_id)) {
        element.enabled = false;
      }
    }
    const { container } = renderStrip(project);
    expect(container.querySelectorAll("[data-roughcut-frame]")).toHaveLength(0);
    expect(container.querySelector("[data-roughcut-play-film]")).toBeTruthy();
  });
});
