import { creatorRequest, jsonBody } from "./client";
import type {
  ProjectEntityCollection,
  ShotDocument,
} from "@/contracts/creator";

export type PromptSyncStatus =
  | "legacy"
  | "current"
  | "needs_update"
  | "needs_confirmation";
export type PromptSyncChangedSource =
  | "currentPlan"
  | "storyboardPrompt"
  | "videoPrompt";
export type PromptSyncSource = PromptSyncChangedSource | "mixed";
export type PromptSyncShots = ProjectEntityCollection<ShotDocument>;
export interface PromptSyncState {
  validationMessage?: string | null;
  status: PromptSyncStatus;
  baselineToken: string;
  shots: PromptSyncShots;
  storyboardPrompt: string;
  videoPrompt: string;
  changedSources: PromptSyncChangedSource[];
  suggestedSource: PromptSyncSource | null;
}
export interface PromptProposal {
  proposalId: string;
  baselineToken: string;
  source: PromptSyncSource;
  beforeShots: PromptSyncShots;
  shots: PromptSyncShots;
  storyboardPrompt: string;
  videoPrompt: string;
  beforeStoryboardPrompt: string;
  beforeVideoPrompt: string;
}
const CHANGED_SOURCES: PromptSyncChangedSource[] = [
  "currentPlan",
  "storyboardPrompt",
  "videoPrompt",
];
const SOURCES: PromptSyncSource[] = [...CHANGED_SOURCES, "mixed"];
function validShots(value: unknown): value is PromptSyncShots {
  if (!value || typeof value !== "object") return false;
  const collection = value as PromptSyncShots;
  return (
    Array.isArray(collection.order) &&
    Boolean(collection.items) &&
    collection.order.every(
      (id) =>
        typeof id === "string" &&
        typeof collection.items[id]?.description === "string",
    )
  );
}
export interface PromptSyncScope {
  projectId: string;
  timelineId: string;
  elementId: string;
}
function scopePath(scope: PromptSyncScope) {
  return `/projects/${encodeURIComponent(
    scope.projectId,
  )}/timelines/${encodeURIComponent(
    scope.timelineId,
  )}/elements/${encodeURIComponent(scope.elementId)}`;
}
export async function getPromptSync(
  scope: PromptSyncScope,
  signal?: AbortSignal,
): Promise<PromptSyncState> {
  const result = await creatorRequest<PromptSyncState>(
    `${scopePath(scope)}/prompt-sync`,
    { signal },
  );
  if (
    !result?.baselineToken ||
    !["legacy", "current", "needs_update", "needs_confirmation"].includes(
      result.status,
    ) ||
    !validShots(result.shots) ||
    typeof result.storyboardPrompt !== "string" ||
    typeof result.videoPrompt !== "string" ||
    !Array.isArray(result.changedSources) ||
    result.changedSources.some((source) => !CHANGED_SOURCES.includes(source)) ||
    (result.suggestedSource !== null &&
      !SOURCES.includes(result.suggestedSource)) ||
    (result.changedSources.length > 1 && result.suggestedSource !== "mixed") ||
    (result.changedSources.length === 1 &&
      result.suggestedSource !== result.changedSources[0])
  ) {
    throw new Error("The shot content and prompts could not be checked.");
  }
  return result;
}
export async function createPromptProposal(
  scope: PromptSyncScope,
  source: PromptSyncSource = "currentPlan",
): Promise<PromptProposal> {
  const result = await creatorRequest<PromptProposal>(
    `${scopePath(scope)}/prompt-proposals`,
    { method: "POST", body: jsonBody({ source }) },
  );
  if (
    !result?.proposalId ||
    !result.baselineToken ||
    !SOURCES.includes(result.source) ||
    !validShots(result.beforeShots) ||
    !validShots(result.shots) ||
    [
      result.beforeStoryboardPrompt,
      result.beforeVideoPrompt,
      result.storyboardPrompt,
      result.videoPrompt,
    ].some((text) => typeof text !== "string")
  ) {
    throw new Error("The shot content and prompt preview is incomplete.");
  }
  return result;
}
export function acceptPromptProposal(
  scope: PromptSyncScope,
  proposalId: string,
) {
  return creatorRequest(
    `${scopePath(scope)}/prompt-proposals/${encodeURIComponent(
      proposalId,
    )}/accept`,
    { method: "POST", body: jsonBody({}) },
  );
}
export function confirmPromptSync(
  scope: PromptSyncScope,
  baselineToken: string,
) {
  return creatorRequest(`${scopePath(scope)}/prompt-sync/confirm`, {
    method: "POST",
    body: jsonBody({ baselineToken }),
  });
}
