import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BlueprintRoughCutStrip from "@/components/blueprint/BlueprintRoughCutStrip";
import { projectDocument } from "@/test/creatorFixtures";
import type { ProjectDocument } from "@/contracts/creator";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function renderStrip(project: ProjectDocument) {
  return render(
    <BlueprintRoughCutStrip project={project} onSelectTimeline={vi.fn()} />,
  );
}

it("previews the full planned duration with design images before any shot video exists", () => {
  const project = cloneProject();
  project.timelines.order = ["timeline:main"];
  for (const [id, slot] of Object.entries(
    project.assets.artifact_slots_by_id,
  )) {
    if (slot.kind === "final_video" || slot.owner_ref.startsWith("element:"))
      delete project.assets.artifact_slots_by_id[id];
  }
  const { container, baseElement } = renderStrip(project);
  fireEvent.click(container.querySelector("[data-roughcut-preview-all]")!);
  expect(baseElement.querySelector("[data-roughcut-live]")).toBeTruthy();
  const scrubber = baseElement.querySelector<HTMLInputElement>(
    '[data-roughcut-live] input[type="range"]',
  )!;
  expect(Number(scrubber.max)).toBe(20000);
  fireEvent.change(scrubber, { target: { value: "6000" } });
  expect(
    baseElement.querySelector('img[data-live-layer="r2v-window"]'),
  ).toBeTruthy();
  expect(
    baseElement.querySelector("[data-live-preview-incomplete]"),
  ).toBeNull();
  fireEvent.change(scrubber, { target: { value: "15000" } });
  expect(scrubber.value).toBe("15000");
});
