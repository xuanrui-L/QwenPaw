import type {
  ArtifactSlotDocument,
  ArtifactVersionDocument,
  NarrativeEdgeDocument,
  ProjectDocument,
  TimelineDocument,
  TimelineElementDocument,
  VisualEntityDocument,
} from "@/contracts/creator";
import {
  orderedTimelineElements,
  timelineEndTick,
} from "./timelineElementSelectors";

/** Slot + its currently selected version, resolved from the asset index. */
export interface ResolvedSlot {
  slot: ArtifactSlotDocument;
  selected: ArtifactVersionDocument | null;
}

function resolveSlot(
  project: ProjectDocument,
  slot: ArtifactSlotDocument | undefined | null,
): ResolvedSlot | null {
  if (!slot) return null;
  return {
    slot,
    selected: slot.selected_version_id
      ? project.assets.artifact_versions_by_id[slot.selected_version_id] ??
        null
      : null,
  };
}

/**
 * The timeline_script slot of one narrative node. Convention slot id is
 * `script:<timelineId>`; owner_ref scan tolerates other id schemes.
 */
export function selectTimelineScriptSlot(
  project: ProjectDocument | null | undefined,
  timelineId: string | null | undefined,
): ResolvedSlot | null {
  if (!project || !timelineId) return null;
  const direct = project.assets.artifact_slots_by_id[`script:${timelineId}`];
  if (direct?.kind === "timeline_script") return resolveSlot(project, direct);
  const scanned = Object.values(project.assets.artifact_slots_by_id).find(
    (slot) =>
      slot.kind === "timeline_script" &&
      slot.owner_ref === `timeline:${timelineId}`,
  );
  return resolveSlot(project, scanned);
}

/** All research_report slots of the project (blueprint research tab). */
export function selectResearchSlots(
  project: ProjectDocument | null | undefined,
): ResolvedSlot[] {
  if (!project) return [];
  return Object.values(project.assets.artifact_slots_by_id)
    .filter((slot) => slot.kind === "research_report")
    .map((slot) => resolveSlot(project, slot)!)
    .sort((left, right) => left.slot.slot_id.localeCompare(right.slot.slot_id));
}

/** Final-cut slot of a timeline: timeline_render / final_video kinds. */
export function selectTimelineRenderSlot(
  project: ProjectDocument | null | undefined,
  timelineId: string | null | undefined,
): ResolvedSlot | null {
  if (!project || !timelineId) return null;
  const direct =
    project.assets.artifact_slots_by_id[`timeline:${timelineId}:render`];
  if (direct) return resolveSlot(project, direct);
  const scanned = Object.values(project.assets.artifact_slots_by_id).find(
    (slot) =>
      (slot.kind === "timeline_render" || slot.kind === "final_video") &&
      slot.owner_ref === `timeline:${timelineId}`,
  );
  return resolveSlot(project, scanned);
}

const VIDEO_CREATION_TYPES = new Set([
  "r2v",
  "t2v",
  "i2v",
  "s2v",
  "edit",
  "motion_clip",
]);

export function isVideoProductionElement(
  element: TimelineElementDocument,
): boolean {
  return VIDEO_CREATION_TYPES.has(element.creation.type);
}

function selectedEntityVersionId(
  entity: VisualEntityDocument | undefined,
): string | null {
  if (!entity) return null;
  if (entity.selected_artifact_version_id)
    return entity.selected_artifact_version_id;
  for (const variantId of entity.variants.order) {
    const versionId =
      entity.variants.items[variantId]?.selected_artifact_version_id;
    if (versionId) return versionId;
  }
  return null;
}

export type RoughCutSource = "final" | "storyboard" | "design" | "none";

export interface RoughCutFrame {
  key: string;
  timelineId: string;
  timelineIndex: number;
  elementId: string;
  label: string;
  /** Artifact version rendering this frame, if any. */
  versionId: string | null;
  mediaKind: "image" | "video" | null;
  source: RoughCutSource;
}

/**
 * Rough-cut frame of one element, derived purely from existing artifacts
 * (plan §4.8): selected element_video ▸ r2v_storyboard_image ▸ the
 * referenced entity's visual design image ▸ empty placeholder.
 */
export function roughCutFrameForElement(
  project: ProjectDocument,
  element: TimelineElementDocument,
): { versionId: string | null; mediaKind: "image" | "video" | null; source: RoughCutSource } {
  // 1. Selected element_video output.
  for (const output of Object.values(element.outputs)) {
    const slot = project.assets.artifact_slots_by_id[output.slot_id];
    if (!slot || slot.kind !== "element_video" || !slot.selected_version_id)
      continue;
    return {
      versionId: slot.selected_version_id,
      mediaKind: "video",
      source: "final",
    };
  }
  // 2. Storyboard image (the mandatory intermediate of generated elements).
  const storyboard = Object.values(project.assets.artifact_slots_by_id).find(
    (slot) =>
      slot.owner_ref === `element:${element.element_id}` &&
      (slot.kind === "r2v_storyboard_image" ||
        slot.slot_id.endsWith(":storyboard")) &&
      slot.selected_version_id,
  );
  if (storyboard?.selected_version_id) {
    return {
      versionId: storyboard.selected_version_id,
      mediaKind: "image",
      source: "storyboard",
    };
  }
  // 3. Referenced visual entity design image.
  const creation = element.creation;
  const entityRefs: string[] = [];
  if (creation.type === "r2v") {
    entityRefs.push(...creation.character_refs);
    if (creation.scene_ref) entityRefs.push(creation.scene_ref);
  } else if (creation.type === "s2v" && creation.character_ref) {
    entityRefs.push(creation.character_ref);
  }
  for (const ref of entityRefs) {
    const entity =
      project.visual.entities.items[ref.replace(/^visual-entity:/, "")];
    const versionId = selectedEntityVersionId(entity);
    if (versionId)
      return { versionId, mediaKind: "image", source: "design" };
  }
  return { versionId: null, mediaKind: null, source: "none" };
}

/** All frames of the project: every enabled element, timeline order first. */
export function selectRoughCutFrames(
  project: ProjectDocument | null | undefined,
): RoughCutFrame[] {
  if (!project) return [];
  return project.timelines.order.flatMap((timelineId, timelineIndex) => {
    const timeline = project.timelines.items[timelineId];
    if (!timeline) return [];
    return orderedTimelineElements(timeline)
      .filter((element) => element.enabled)
      .map((element) => ({
        key: `${timelineId}:${element.element_id}`,
        timelineId,
        timelineIndex,
        elementId: element.element_id,
        label: element.label || element.element_id,
        ...roughCutFrameForElement(project, element),
      }));
  });
}

export interface TimelineSummary {
  timeline: TimelineDocument;
  timelineId: string;
  index: number;
  /** timeline.title fallback handled by callers (i18n 第N集). */
  title: string;
  synopsis: string;
  elementCount: number;
  videoTotal: number;
  videoReady: number;
  hasScript: boolean;
  scriptStale: boolean;
  renderReady: boolean;
  durationSeconds: number;
}

export function summarizeTimeline(
  project: ProjectDocument,
  timelineId: string,
  index: number,
): TimelineSummary | null {
  const timeline = project.timelines.items[timelineId];
  if (!timeline) return null;
  const elements = orderedTimelineElements(timeline).filter(
    (element) => element.enabled,
  );
  const videoElements = elements.filter(isVideoProductionElement);
  const videoReady = videoElements.filter(
    (element) =>
      roughCutFrameForElement(project, element).source === "final",
  ).length;
  const script = selectTimelineScriptSlot(project, timelineId);
  const render = selectTimelineRenderSlot(project, timelineId);
  const duration =
    timeline.planned_duration_seconds ??
    (timeline.ticks_per_second
      ? timelineEndTick(timeline) / timeline.ticks_per_second
      : 0);
  return {
    timeline,
    timelineId,
    index,
    title: timeline.title ?? "",
    synopsis: timeline.synopsis ?? "",
    elementCount: elements.length,
    videoTotal: videoElements.length,
    videoReady,
    hasScript: Boolean(script?.selected),
    scriptStale: Boolean(script?.selected?.stale),
    renderReady: Boolean(render?.selected && !render.selected.stale),
    durationSeconds: duration,
  };
}

export function selectTimelineSummaries(
  project: ProjectDocument | null | undefined,
): TimelineSummary[] {
  if (!project) return [];
  return project.timelines.order
    .map((timelineId, index) => summarizeTimeline(project, timelineId, index))
    .filter((summary): summary is TimelineSummary => Boolean(summary));
}

export function selectNarrativeEdges(
  project: ProjectDocument | null | undefined,
): NarrativeEdgeDocument[] {
  return project?.narrative_edges ?? [];
}

/**
 * Layered auto-layout for the branching canvas: longest-path layering from
 * the roots, one column per layer, row = index inside the layer.
 */
export function layoutNarrativeGraph(
  summaries: TimelineSummary[],
  edges: NarrativeEdgeDocument[],
): Map<string, { layer: number; row: number }> {
  const ids = summaries.map((summary) => summary.timelineId);
  const layerById = new Map<string, number>(ids.map((id) => [id, 0]));
  // Relax edges |V| times (graphs are tiny; cycles just stop raising).
  for (let pass = 0; pass < ids.length; pass += 1) {
    let changed = false;
    for (const edge of edges) {
      const source = layerById.get(edge.source_timeline_id);
      const target = layerById.get(edge.target_timeline_id);
      if (source === undefined || target === undefined) continue;
      if (target < source + 1) {
        layerById.set(edge.target_timeline_id, source + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const rows = new Map<number, number>();
  const positions = new Map<string, { layer: number; row: number }>();
  for (const id of ids) {
    const layer = layerById.get(id) ?? 0;
    const row = rows.get(layer) ?? 0;
    rows.set(layer, row + 1);
    positions.set(id, { layer, row });
  }
  return positions;
}
