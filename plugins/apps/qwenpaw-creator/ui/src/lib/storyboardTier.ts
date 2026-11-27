// Client mirror of the backend's three-tier storyboard triage
// (models/video_task_plans.r2v_continuity_storyboard_need). The backend
// stays authoritative for dispatch; this only drives overview copy and
// the "仍生成分镜" affordance, so keep the regexes and thresholds in
// lockstep with the Python original (tests pin both sides).
import type { ProjectDocument } from "@/contracts/creator/projectSnapshot";

export type StoryboardTier = "plain" | "none" | "combined";

type AnyRecord = Record<string, any>;

function timelinesOf(project: ProjectDocument): AnyRecord[] {
  const timelines = (project as AnyRecord).timelines as AnyRecord;
  return Object.values(timelines.items ?? {}) as AnyRecord[];
}

function locate(
  project: ProjectDocument,
  elementId: string,
): { timeline: AnyRecord; element: AnyRecord } | null {
  for (const timeline of timelinesOf(project)) {
    const element = timeline?.elements_by_id?.[elementId];
    if (element) return { timeline, element };
  }
  return null;
}

function findPredecessor(
  timeline: AnyRecord,
  element: AnyRecord,
): AnyRecord | null {
  const targetTick = Number(element.span?.start_tick ?? 0);
  let bestKey: [number, string] | null = null;
  let best: AnyRecord | null = null;
  for (const [id, candidate] of Object.entries(
    timeline.elements_by_id ?? {},
  ) as [string, AnyRecord][]) {
    if (id === element.element_id) continue;
    if (candidate.enabled === false) continue;
    const type = candidate.creation?.type;
    if (type !== "r2v" && type !== "i2v") continue;
    const tick = Number(candidate.span?.start_tick ?? 0);
    if (tick >= targetTick) continue;
    if (tick + Number(candidate.span?.duration_tick ?? 0) > targetTick) {
      continue;
    }
    const key: [number, string] = [tick, id];
    if (
      bestKey === null ||
      key[0] > bestKey[0] ||
      (key[0] === bestKey[0] && key[1] > bestKey[1])
    ) {
      bestKey = key;
      best = candidate;
    }
  }
  return best;
}

function mayConsumePredecessor(
  timeline: AnyRecord,
  element: AnyRecord,
  prev: AnyRecord | null,
): boolean {
  if (!prev) return false;
  if (prev.creation?.scene_ref !== element.creation?.scene_ref) return false;
  const prevRefs = new Set<string>([
    ...(prev.creation?.character_refs ?? []),
    ...(prev.creation?.prop_refs ?? []),
  ]);
  const curRefs: string[] = [
    ...(element.creation?.character_refs ?? []),
    ...(element.creation?.prop_refs ?? []),
  ];
  if (curRefs.some((ref) => !prevRefs.has(ref))) return false;
  for (const item of Object.values(
    timeline.elements_by_id ?? {},
  ) as AnyRecord[]) {
    const creation = item.creation;
    if (
      creation?.type === "transition" &&
      creation.from_element_id === prev.element_id &&
      creation.to_element_id === element.element_id
    ) {
      return false;
    }
  }
  // Structured intent only; a missing field defaults conservatively
  // to "do not anchor" (plain r2v), never to prose guessing.
  const authored = element.creation?.transition_intent;
  return authored === "continue" || authored === "one_take";
}

function isComplex(timeline: AnyRecord, element: AnyRecord): boolean {
  const creation = element.creation ?? {};
  const ticksPerSecond = Math.max(Number(timeline.ticks_per_second ?? 10), 1);
  const seconds = Number(element.span?.duration_tick ?? 0) / ticksPerSecond;
  // Hard, measurable constraints only: duration and cast size.
  const ruleComplex =
    seconds > 8 || (creation.character_refs ?? []).length >= 2;
  if (ruleComplex) return true;
  // Authored need wins; missing defaults conservatively to "the
  // storyboard rides" (legacy behaviour).
  return creation.storyboard_intent !== "none";
}

/** Tier for one element; "plain" whenever the segment is not anchored. */
export function storyboardTierForElement(
  project: ProjectDocument,
  elementId: string,
): StoryboardTier {
  const found = locate(project, elementId);
  if (!found) return "plain";
  const { timeline, element } = found;
  if (element.creation?.type !== "r2v") return "plain";
  if (element.creation.force_storyboard) return "combined";
  const prev = findPredecessor(timeline, element);
  if (!mayConsumePredecessor(timeline, element, prev)) return "plain";
  return isComplex(timeline, element) ? "combined" : "none";
}
