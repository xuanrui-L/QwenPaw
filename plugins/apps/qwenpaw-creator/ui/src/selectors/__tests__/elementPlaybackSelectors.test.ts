import { describe, expect, it } from "vitest";
import type {
  ProjectDocument,
  TaskView,
  TimelineElementDocument,
} from "@/contracts/creator";
import {
  playbackLayersAtTick,
  playbackLayersInWindow,
  resolveElementPlayback,
} from "@/selectors/elementPlaybackSelectors";
import { projectDocument } from "@/test/creatorFixtures";

function cloneProject(): ProjectDocument {
  return structuredClone(projectDocument);
}

function timelineOf(project: ProjectDocument) {
  return project.timelines.items["timeline:main"];
}

function task(overrides: Partial<TaskView>): TaskView {
  return {
    id: "task-1",
    projectId: "p1",
    transactionId: null,
    specialistRunId: null,
    kind: "r2v_generation",
    targetRef: "element:r2v-window",
    status: "RUNNING",
    progress: null,
    resultRefs: [],
    createdAt: "2026-07-20T00:00:00Z",
    ...overrides,
  };
}

describe("resolveElementPlayback", () => {
  it("resolves edit elements from their source asset render_source", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const playback = resolveElementPlayback(
      project,
      timeline,
      timeline.elements_by_id["edit-opening"],
    );
    expect(playback.status).toBe("ready");
    expect(playback.media).toMatchObject({
      url: "/api/qwenpaw-creator/media/assets/cat-video-v1",
      mediaKind: "video",
      versionId: "cat-video-v1",
      sourceInSeconds: 0,
      sourceOutSeconds: 8,
      playbackRate: 1,
    });
  });

  it("resolves r2v elements through element_output onto the selected artifact", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const playback = resolveElementPlayback(
      project,
      timeline,
      timeline.elements_by_id["r2v-window"],
    );
    expect(playback.status).toBe("ready");
    expect(playback.media).toMatchObject({
      url: "/api/qwenpaw-creator/media/artifacts/r2v-window-v1",
      mediaKind: "video",
      versionId: "r2v-window-v1",
    });
  });

  it("falls back to selected outputs when render_source is missing", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const element = timeline.elements_by_id["r2v-window"];
    element.render_source = null;
    const playback = resolveElementPlayback(project, timeline, element);
    expect(playback.status).toBe("ready");
    expect(playback.media).toMatchObject({
      versionId: "r2v-window-v1",
      sourceInSeconds: 0,
      sourceOutSeconds: null,
    });
  });

  it("maps the related Task status when no media is available", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const element = timeline.elements_by_id["r2v-window"];
    element.render_source = null;
    element.outputs = {};

    expect(
      resolveElementPlayback(project, timeline, element, [
        task({ status: "RUNNING" }),
      ]).status,
    ).toBe("generating");
    expect(
      resolveElementPlayback(project, timeline, element, [
        task({ status: "QUEUED" }),
      ]).status,
    ).toBe("queued");
    expect(
      resolveElementPlayback(project, timeline, element, [
        task({ status: "FAILED" }),
      ]).status,
    ).toBe("failed");
    expect(resolveElementPlayback(project, timeline, element).status).toBe(
      "pending",
    );
  });

  it("treats a slot without a selected version as not ready", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const element = timeline.elements_by_id["r2v-window"];
    project.assets.artifact_slots_by_id[
      "element:r2v-window:video"
    ].selected_version_id = null;
    const playback = resolveElementPlayback(project, timeline, element, [
      task({ status: "RUNNING" }),
    ]);
    expect(playback.status).toBe("generating");
    expect(playback.media).toBeNull();
  });

  it("marks copy overlays (pet_os/interview_summary) ready for deterministic rendering", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const playback = resolveElementPlayback(
      project,
      timeline,
      timeline.elements_by_id["overlay-os"],
    );
    expect(playback.status).toBe("ready");
    expect(playback.media).toBeNull();
  });

  it("keeps motion/media overlays pending until they have an artifact, matching compose", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    // overlay-title 是 motion 类 overlay，成片合成不会直接画文本。
    const playback = resolveElementPlayback(
      project,
      timeline,
      timeline.elements_by_id["overlay-title"],
    );
    expect(playback.status).toBe("pending");
    expect(playback.media).toBeNull();
  });

  it("treats transitions as ready without a media layer", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const playback = resolveElementPlayback(
      project,
      timeline,
      timeline.elements_by_id.transition,
    );
    expect(playback.status).toBe("ready");
    expect(playback.media).toBeNull();
  });
});

describe("playbackLayersAtTick", () => {
  it("returns overlapping layers sorted by z_index without transitions", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const layers = playbackLayersAtTick(project, timeline, 7000);
    expect(layers.map((layer) => layer.element.element_id)).toEqual([
      "audio-bgm",
      "edit-opening",
      "r2v-window",
      "overlay-os",
    ]);
  });

  it("skips disabled elements", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    (
      timeline.elements_by_id["edit-opening"] as TimelineElementDocument
    ).enabled = false;
    const layers = playbackLayersAtTick(project, timeline, 7000);
    expect(layers.map((layer) => layer.element.element_id)).not.toContain(
      "edit-opening",
    );
  });
});

describe("playbackLayersInWindow", () => {
  it("premounts layers around the playhead within the window", () => {
    const project = cloneProject();
    const timeline = timelineOf(project);
    const layers = playbackLayersInWindow(project, timeline, 0);
    expect(layers.map((layer) => layer.element.element_id)).toEqual([
      "audio-bgm",
      "edit-opening",
      "r2v-window",
      "overlay-title",
      "overlay-os",
    ]);
  });
});
