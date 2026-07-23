import type {
  ArtifactVersionDocument,
  ElementRenderSourceDocument,
  ProjectDocument,
  TaskView,
  TimelineDocument,
  TimelineElementDocument,
} from "@/contracts/creator";
import {
  getArtifactVersionMediaUrl,
  getAssetVersionMediaUrl,
} from "@/api/creator";
import { elementsAtTick } from "@/selectors/timelineElementSelectors";

/** 实时拼装预览里单个 Element 的可播放性状态。 */
export type ElementPlaybackStatus =
  | "ready"
  | "generating"
  | "queued"
  | "failed"
  | "stale"
  | "pending";

export type ElementPlaybackMediaKind = "video" | "image" | "audio" | "other";

export interface ElementPlaybackMedia {
  url: string;
  mediaKind: ElementPlaybackMediaKind;
  /** artifact 或 source asset 的版本 ID，作层的稳定 key 用。 */
  versionId: string;
  sourceInSeconds: number;
  sourceOutSeconds: number | null;
  playbackRate: number;
  loop: boolean;
  durationSeconds: number | null;
  stale: boolean;
}

export interface ElementPlayback {
  element: TimelineElementDocument;
  status: ElementPlaybackStatus;
  media: ElementPlaybackMedia | null;
}

export const ELEMENT_PLAYBACK_STATUS_LABEL: Record<
  ElementPlaybackStatus,
  string
> = {
  ready: "已就绪",
  generating: "生成中",
  queued: "排队中",
  failed: "生成失败",
  stale: "需重新生成",
  pending: "待生成",
};

function mediaKindOfType(mediaType: string): ElementPlaybackMediaKind {
  if (mediaType.startsWith("video/")) return "video";
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType.startsWith("audio/")) return "audio";
  return "other";
}

function artifactMediaKind(
  project: ProjectDocument,
  version: ArtifactVersionDocument,
): ElementPlaybackMediaKind {
  const file = project.assets.files_by_id[version.file_id];
  return mediaKindOfType(file?.media_type ?? "");
}

interface ResolvedMediaRef {
  url: string;
  mediaKind: ElementPlaybackMediaKind;
  versionId: string;
  durationSeconds: number | null;
  stale: boolean;
}

function resolveArtifactVersionRef(
  project: ProjectDocument,
  versionId: string,
): ResolvedMediaRef | null {
  const version = project.assets.artifact_versions_by_id[versionId];
  if (!version) return null;
  return {
    url: getArtifactVersionMediaUrl(versionId),
    mediaKind: artifactMediaKind(project, version),
    versionId,
    durationSeconds: version.duration_seconds,
    stale: version.stale,
  };
}

function resolveSourceVersionRef(
  project: ProjectDocument,
  versionId: string,
): ResolvedMediaRef | null {
  const version = project.assets.source_versions_by_id[versionId];
  if (!version) return null;
  const kind = version.media_kind;
  return {
    url: getAssetVersionMediaUrl(versionId),
    mediaKind:
      kind === "video" || kind === "image" || kind === "audio"
        ? kind
        : "other",
    versionId,
    durationSeconds: version.duration_seconds,
    stale: false,
  };
}

/** element.outputs 兜底：优先 video 产物，其次任一已有选中版本的产物槽。 */
function resolveSelectedOutputRef(
  project: ProjectDocument,
  element: TimelineElementDocument,
  outputName?: string,
): ResolvedMediaRef | null {
  const names = outputName
    ? [outputName]
    : ["video", ...Object.keys(element.outputs)];
  for (const name of names) {
    const output = element.outputs[name];
    if (!output) continue;
    const slot = project.assets.artifact_slots_by_id[output.slot_id];
    if (!slot?.selected_version_id) continue;
    const resolved = resolveArtifactVersionRef(
      project,
      slot.selected_version_id,
    );
    if (resolved) return resolved;
  }
  return null;
}

function resolveRenderSourceRef(
  project: ProjectDocument,
  timeline: TimelineDocument,
  renderSource: ElementRenderSourceDocument,
): ResolvedMediaRef | null {
  if (renderSource.type === "artifact_version") {
    return resolveArtifactVersionRef(project, renderSource.version_id);
  }
  if (renderSource.type === "source_asset_version") {
    return resolveSourceVersionRef(project, renderSource.version_id);
  }
  const target = timeline.elements_by_id[renderSource.element_id];
  if (!target) return null;
  return resolveSelectedOutputRef(project, target, renderSource.output_name);
}

function elementTaskStatus(
  element: TimelineElementDocument,
  tasks: TaskView[],
): ElementPlaybackStatus | null {
  const task = tasks.find(
    (item) => item.targetRef === `element:${element.element_id}`,
  );
  if (task?.status === "RUNNING") return "generating";
  if (task?.status === "QUEUED") return "queued";
  if (task?.status === "FAILED" || task?.status === "QUARANTINED")
    return "failed";
  return null;
}

/**
 * 解析 Element 在实时拼装预览中的播放信息。与后端本地合成一致，
 * 优先按 render_source 解析媒体，找不到时回退到 outputs 的选中产物。
 * 无媒体时按关联 Task 状态给出 生成中/排队中/失败，兜底为 待生成。
 */
export function resolveElementPlayback(
  project: ProjectDocument,
  timeline: TimelineDocument,
  element: TimelineElementDocument,
  tasks: TaskView[] = [],
): ElementPlayback {
  // 转场在实时拼装中做硬切处理，不作为独立媒体层，视为已就绪。
  if (element.creation.type === "transition") {
    return { element, status: "ready", media: null };
  }
  const ticksPerSecond = timeline.ticks_per_second || 1;
  const renderSource = element.render_source;
  const fromRender = renderSource
    ? resolveRenderSourceRef(project, timeline, renderSource)
    : null;
  const resolved = fromRender ?? resolveSelectedOutputRef(project, element);
  if (resolved) {
    // render_source 解析成功时沿用其入出点/速率；outputs 兜底则从头整段播放。
    const timing = fromRender && renderSource ? renderSource : null;
    return {
      element,
      status:
        elementTaskStatus(element, tasks) ??
        (resolved.stale ? "stale" : "ready"),
      media: {
        ...resolved,
        sourceInSeconds: timing ? timing.source_in_tick / ticksPerSecond : 0,
        sourceOutSeconds:
          timing && timing.source_out_tick != null
            ? timing.source_out_tick / ticksPerSecond
            : null,
        playbackRate: timing ? timing.playback_rate : 1,
        loop: timing ? timing.loop : false,
      },
    };
  }
  // 动态 overlay 的 HTML/CSS 文档本身就是可预览内容，不需要等待独立媒体产物。
  // 成片阶段仍由后端逐帧渲染并合成；这里只负责浏览器内的实时预览。
  if (
    element.creation.type === "overlay" &&
    element.creation.motion?.html
  ) {
    return { element, status: "ready", media: null };
  }
  // 文案类 overlay（pet_os/interview_summary）没有独立产物，成片在合成时
  // 用确定性渲染器画气泡；实时预览用同款规格直接绘制，视为已就绪。
  if (
    element.creation.type === "overlay" &&
    (element.creation.overlay_kind === "pet_os" ||
      element.creation.overlay_kind === "interview_summary") &&
    element.creation.text
  ) {
    return { element, status: "ready", media: null };
  }
  return {
    element,
    status: elementTaskStatus(element, tasks) ?? "pending",
    media: null,
  };
}

/**
 * 某时刻参与实时拼装的全部层，按 z_index 升序（低层在前）排列。
 * 转场元素不产生媒体层，直接剔除。
 */
export function playbackLayersAtTick(
  project: ProjectDocument,
  timeline: TimelineDocument,
  tick: number,
  tasks: TaskView[] = [],
): ElementPlayback[] {
  return elementsAtTick(timeline, tick)
    .filter((element) => element.creation.type !== "transition")
    .sort(
      (left, right) =>
        left.z_index - right.z_index ||
        left.span.start_tick - right.span.start_tick ||
        left.element_id.localeCompare(right.element_id),
    )
    .map((element) => resolveElementPlayback(project, timeline, element, tasks));
}

/**
 * 挂载窗口内的层（当前时刻前 behindSeconds、后 aheadSeconds），用于
 * 预挂载 media 元素减少切换黑帧；同样剔除转场。
 */
export function playbackLayersInWindow(
  project: ProjectDocument,
  timeline: TimelineDocument,
  tick: number,
  tasks: TaskView[] = [],
  behindSeconds = 2,
  aheadSeconds = 8,
): ElementPlayback[] {
  const ticksPerSecond = timeline.ticks_per_second || 1;
  const windowStart = tick - behindSeconds * ticksPerSecond;
  const windowEnd = tick + aheadSeconds * ticksPerSecond;
  return Object.values(timeline.elements_by_id)
    .filter(
      (element) =>
        element.enabled &&
        element.creation.type !== "transition" &&
        element.span.start_tick < windowEnd &&
        windowStart < element.span.start_tick + element.span.duration_tick,
    )
    .sort(
      (left, right) =>
        left.z_index - right.z_index ||
        left.span.start_tick - right.span.start_tick ||
        left.element_id.localeCompare(right.element_id),
    )
    .map((element) => resolveElementPlayback(project, timeline, element, tasks));
}
