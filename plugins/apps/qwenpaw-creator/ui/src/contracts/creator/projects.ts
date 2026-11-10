import type { CreatorSessionStatus } from "./sessions";

export type CreatorScenario = "short_drama" | "video_edit" | "general";

/**
 * User overrides for the standard subtitle bar. Every field is optional:
 * `null`/absent keeps the renderer default, so an untouched project looks
 * exactly as before. Applied by the deterministic plain-subtitle renderer
 * and its live preview; designed 花字 keeps its own per-footage styling.
 */
export interface SubtitleStyle {
  /** Solid text colour as `#rrggbb`; null = default white. */
  color?: string | null;
  /** Font-size multiplier (0.6–1.8); null = base size. */
  fontScale?: number | null;
  /** Outline-width multiplier (0–3, 0 = no outline); null = base outline. */
  strokeScale?: number | null;
}

export interface ProjectCreateRequest {
  productionStage?: "script" | "media";
  clientRequestId: string;
  name: string;
  description?: string;
  scenario: CreatorScenario;
  aspectRatio: string;
  resolution: string;
  contentType?: string | null;
  templateId?: string;
  subtitlePolicy?: "auto" | "on" | "off";
  /** Optional JianYing-like subtitle style pack id; null = smart recommend. */
  subtitleTemplate?: string | null;
  /** Optional overrides for the standard subtitle bar (colour/size/outline). */
  subtitleStyle?: SubtitleStyle | null;
  /** Optional atomic bootstrap goal persisted in the file Runtime aggregate. */
  initialGoal?: string;
}

export interface ProjectCreateResponse {
  projectId: string;
  creatorSessionId: string;
  conversationId: string;
  projectSnapshotId: string;
  header: ProjectCreateHeader;
}

export interface ProjectCreateHeader {
  id: string;
  name: string;
  description: string;
  scenario: CreatorScenario;
  aspectRatio: string;
  resolution: string;
  contentType: string | null;
  subtitleTemplate?: string | null;
  subtitleStyle?: SubtitleStyle | null;
}

export interface ProjectSummary {
  projectId: string;
  name: string;
  description: string;
  scenario: CreatorScenario;
  aspectRatio: string;
  resolution: string;
  contentType?: string | null;
  createdAt: string;
  updatedAt: string;
  /** Version id of the media the backend picked as the project preview. */
  coverVersionId?: string | null;
  /**
   * Which media route serves the cover: the version bytes themselves for
   * images, or an extracted keyframe when only a video exists.
   */
  coverVersionSource?:
    | "artifact"
    | "source"
    | "artifact_frame"
    | "source_frame"
    | null;
  /**
   * Newest rendered final cut, when the Project has one; drives the card
   * preview button on the home page.
   */
  finalVideoVersionId?: string | null;
  /** Current session status, null when no runtime session exists. */
  status?: CreatorSessionStatus | null;
}

export interface ProjectListResponse {
  items: ProjectSummary[];
  limit: number;
  offset: number;
}

/** One OSS-hosted inspiration example backed by a built-in Project. */
export interface InspirationExampleSummary {
  id: string;
  title: string;
  description: string;
  projectId: string;
  installed: boolean;
}

export interface InspirationExampleListResponse {
  items: InspirationExampleSummary[];
}

export interface InspirationExampleOpenResponse {
  projectId: string;
  installed: boolean;
}

/** Polled download progress while an example archive streams in from OSS. */
export interface InspirationExampleOpenProgress {
  state: "installed" | "downloading" | "idle";
  receivedBytes?: number;
  totalBytes?: number | null;
}
