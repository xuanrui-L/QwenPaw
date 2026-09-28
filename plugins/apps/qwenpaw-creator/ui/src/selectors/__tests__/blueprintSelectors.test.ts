import { describe, expect, it } from "vitest";
import type { ProjectDocument } from "@/contracts/creator";
import {
  selectFinalFilmVersionId,
  selectRoughCutFrames,
  roughCutFrameForElement,
  hasTimelinePreviewContent,
} from "@/selectors/blueprintSelectors";
import { selectLiveTimelineIds } from "@/selectors/timelineElementSelectors";
import { projectDocument } from "@/test/creatorFixtures";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

/** Trim the two-episode fixture to a single live timeline. */
function singleTimeline(project: ProjectDocument): ProjectDocument {
  project.timelines.order = ["timeline:main"];
  return project;
}

/** Append a frozen history snapshot cloned from timeline:main. */
function withSnapshot(project: ProjectDocument): ProjectDocument {
  const raw = structuredClone(project.timelines.items["timeline:main"]);
  raw.timeline_id = "snapshot:timeline:main:1";
  project.timelines.items["snapshot:timeline:main:1"] = raw;
  project.timelines.order.push("snapshot:timeline:main:1");
  return project;
}

describe("selectLiveTimelineIds", () => {
  it("excludes snapshot:* frozen history from the live order", () => {
    const project = withSnapshot(cloneProject());
    expect(selectLiveTimelineIds(project)).toEqual([
      "timeline:main",
      "timeline:ep2",
    ]);
  });
});

describe("selectFinalFilmVersionId", () => {
  it("multi-episode projects have no whole film — a single episode's final render must not masquerade as one", () => {
    // Both live timelines exist; timeline:main even has a fresh selected
    // render — still not the whole project.
    expect(selectFinalFilmVersionId(cloneProject())).toBeNull();
  });

  it("single live timeline returns its selected fresh render", () => {
    const project = singleTimeline(cloneProject());
    expect(selectFinalFilmVersionId(project)).toBe("final-v1");
  });

  it("a history snapshot does not turn a single-timeline project into a multi-episode one", () => {
    const project = withSnapshot(singleTimeline(cloneProject()));
    expect(selectFinalFilmVersionId(project)).toBe("final-v1");
  });

  it("a stale selected version is never offered as the film", () => {
    const project = singleTimeline(cloneProject());
    project.assets.artifact_versions_by_id["final-v1"].stale = true;
    expect(selectFinalFilmVersionId(project)).toBeNull();
  });

  it("a newer unselected version never shadows the user's active choice", () => {
    const project = singleTimeline(cloneProject());
    const slot =
      project.assets.artifact_slots_by_id["timeline:timeline:main:render"];
    project.assets.artifact_versions_by_id["final-v2"] = {
      ...structuredClone(project.assets.artifact_versions_by_id["final-v1"]),
      version_id: "final-v2",
      created_at: "2026-07-21T00:00:00Z",
    };
    slot.version_ids = ["final-v1", "final-v2"];
    // The user keeps final-v1 selected; the newer final-v2 must not win.
    expect(selectFinalFilmVersionId(project)).toBe("final-v1");
  });
});

describe("selectRoughCutFrames", () => {
  it("never substitutes a character reference for a missing, stale or invalid storyboard", () => {
    const project = cloneProject();
    const timeline = project.timelines.items["timeline:main"];
    const shot = timeline.elements_by_id["r2v-window"];
    timeline.elements_by_id = { "r2v-window": shot };
    delete project.assets.artifact_slots_by_id["element:r2v-window:video"];
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    expect(hasTimelinePreviewContent(project, timeline)).toBe(false);
    const slot = (project.assets.artifact_slots_by_id[
      "element:r2v-window:storyboard"
    ] = {
      slot_id: "element:r2v-window:storyboard",
      owner_ref: "element:r2v-window",
      kind: "r2v_storyboard_image",
      selected_version_id: "missing-version",
      version_ids: ["missing-version"],
      metadata: {},
    });
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    const version = (project.assets.artifact_versions_by_id["storyboard"] = {
      ...project.assets.artifact_versions_by_id["cat-anchor-v1"],
      version_id: "storyboard",
      kind: "r2v_storyboard_image",
      slot_id: slot.slot_id,
      owner_ref: slot.owner_ref,
      stale: true as boolean,
    });
    slot.selected_version_id = version.version_id;
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
    version.stale = false;
    expect(roughCutFrameForElement(project, shot)).toMatchObject({
      source: "storyboard",
      versionId: "storyboard",
    });
    expect(hasTimelinePreviewContent(project, timeline)).toBe(true);
    delete project.assets.files_by_id[version.file_id];
    expect(roughCutFrameForElement(project, shot).source).toBe("none");
  });
  it("history snapshots contribute no frames and do not inflate counts", () => {
    const baseline = selectRoughCutFrames(cloneProject());
    const frames = selectRoughCutFrames(withSnapshot(cloneProject()));
    expect(frames).toHaveLength(baseline.length);
    expect(
      frames.filter((frame) => frame.timelineId.startsWith("snapshot:")),
    ).toHaveLength(0);
  });
});
