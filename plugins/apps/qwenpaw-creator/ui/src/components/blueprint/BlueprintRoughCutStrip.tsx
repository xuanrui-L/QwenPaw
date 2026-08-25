import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  Clapperboard,
  Download,
  Play,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectDocument } from "@/contracts/creator";
import {
  getArtifactVersionMediaUrl,
  getTimelineRoughCutUrl,
} from "@/api/creator";
import {
  selectRoughCutFrames,
  type RoughCutSource,
} from "@/selectors/blueprintSelectors";

const SOURCE_STYLE: Record<RoughCutSource, string> = {
  final: "bg-[var(--color-success)]/90",
  storyboard: "bg-[var(--color-primary,#3b82f6)]/90",
  design: "bg-[var(--color-warning)]/90",
  none: "bg-black/50",
};

interface BlueprintRoughCutStripProps {
  project: ProjectDocument;
  onSelectTimeline: (timelineId: string) => void;
}

/**
 * Rough-cut preview strip (plan §4.8): one frame per enabled element, sourced
 * purely from existing artifacts — selected element_video ▸ storyboard image ▸
 * referenced entity design ▸ placeholder. Clicking a frame opens the owning
 * timeline's script panel.
 */
export default function BlueprintRoughCutStrip({
  project,
  onSelectTimeline,
}: BlueprintRoughCutStripProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [playError, setPlayError] = useState(false);
  const frames = useMemo(() => selectRoughCutFrames(project), [project]);
  const readyCount = frames.filter((frame) => frame.source === "final").length;
  const timelineIds = useMemo(
    () => [...new Set(frames.map((frame) => frame.timelineId))],
    [frames],
  );
  const multiTimeline = timelineIds.length > 1;
  if (!frames.length) return null;

  const timelineLabelOf = (timelineId: string) => {
    const timeline = project.timelines.items[timelineId];
    const index = frames.find(
      (frame) => frame.timelineId === timelineId,
    )?.timelineIndex;
    return (
      timeline?.title || t("blueprint.episodeN", { n: (index ?? 0) + 1 })
    );
  };

  const finalCutUrlOf = (timelineId: string) => {
    const slot =
      project.assets.artifact_slots_by_id[`timeline:${timelineId}:render`];
    if (slot?.kind !== "final_video" || !slot.selected_version_id) return null;
    return getArtifactVersionMediaUrl(slot.selected_version_id);
  };

  const togglePlay = (timelineId: string) => {
    setPlayError(false);
    setPlayingId((current) => (current === timelineId ? null : timelineId));
    setCollapsed(false);
  };

  return (
    <section
      data-blueprint-roughcut
      className="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-bg-primary)]/70 px-5 py-2 backdrop-blur"
    >
      <div className="flex items-center gap-2.5">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-primary)]">
          <Clapperboard className="h-3.5 w-3.5 text-[var(--color-accent)]" />
          {t("blueprint.roughCut")}
        </span>
        <span className="min-w-0 truncate text-[11px] text-[var(--color-text-tertiary)]">
          {t("blueprint.roughCutHint", {
            ready: readyCount,
            total: frames.length,
          })}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {timelineIds.map((timelineId) => {
            const finalUrl = finalCutUrlOf(timelineId);
            return (
              <span key={timelineId} className="inline-flex items-center">
                <button
                  type="button"
                  data-roughcut-play={timelineId}
                  onClick={() => togglePlay(timelineId)}
                  className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold transition-colors ${
                    finalUrl ? "rounded-r-none border-r-0" : ""
                  } ${
                    playingId === timelineId
                      ? "border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-accent)]"
                      : "border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
                  }`}
                >
                  <Play className="h-3 w-3" />
                  {multiTimeline
                    ? timelineLabelOf(timelineId)
                    : t("blueprint.playRoughCut")}
                  {finalUrl && (
                    <span className="rounded bg-[var(--color-success)]/15 px-1 text-[9px] font-bold text-[var(--color-success)]">
                      {t("blueprint.finalCutBadge")}
                    </span>
                  )}
                </button>
                {finalUrl && (
                  <a
                    href={finalUrl}
                    download={`${timelineId.replace(/:/g, "_")}-final.mp4`}
                    data-roughcut-download={timelineId}
                    title={t("blueprint.downloadFinalCut")}
                    className="inline-flex items-center rounded-full rounded-l-none border border-[var(--color-border)] px-1.5 py-0.5 text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
                  >
                    <Download className="h-3 w-3" />
                  </a>
                )}
              </span>
            );
          })}
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            className="icon-button !h-7 !w-7 shrink-0"
            title={
              collapsed
                ? t("blueprint.expandRoughCut")
                : t("blueprint.collapseRoughCut")
            }
          >
            {collapsed ? (
              <ChevronUp className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </button>
        </span>
      </div>
      {!collapsed && playingId && (
        <div
          data-roughcut-player
          className="relative mt-2 overflow-hidden rounded-lg border border-[var(--color-border)] bg-black"
        >
          {playError ? (
            <div className="flex h-40 items-center justify-center px-6 text-center text-xs text-[var(--color-text-tertiary)]">
              {t("blueprint.roughCutFailed")}
            </div>
          ) : (
            <video
              key={playingId}
              src={
                finalCutUrlOf(playingId) ??
                getTimelineRoughCutUrl(project.project_id, playingId)
              }
              controls
              autoPlay
              playsInline
              onError={() => setPlayError(true)}
              className="mx-auto max-h-72 w-full bg-black object-contain"
            />
          )}
          <span className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold text-white">
            {timelineLabelOf(playingId)} ·{" "}
            {finalCutUrlOf(playingId)
              ? t("blueprint.finalCutBadge")
              : t("blueprint.roughCut")}
          </span>
          <button
            type="button"
            onClick={() => setPlayingId(null)}
            title={t("blueprint.closeRoughCutPlayer")}
            className="absolute right-2 top-2 inline-flex h-6 w-6 items-center justify-center rounded-full bg-black/60 text-white transition-colors hover:bg-black/80"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {!collapsed && (
        <div className="mt-2 flex items-stretch gap-1.5 overflow-x-auto pb-1">
          {frames.map((frame, index) => {
            const isTimelineStart =
              index === 0 ||
              frames[index - 1].timelineId !== frame.timelineId;
            const timeline = project.timelines.items[frame.timelineId];
            const timelineLabel =
              timeline?.title ||
              t("blueprint.episodeN", { n: frame.timelineIndex + 1 });
            const url = frame.versionId
              ? getArtifactVersionMediaUrl(frame.versionId)
              : null;
            return (
              <span key={frame.key} className="contents">
                {isTimelineStart && multiTimeline && (
                  <span
                    className="flex w-[18px] shrink-0 items-center justify-center self-stretch rounded-sm bg-[var(--color-bg-secondary)] text-[9px] font-bold tracking-widest text-[var(--color-text-tertiary)] [writing-mode:vertical-rl]"
                    title={timelineLabel}
                  >
                    {timelineLabel}
                  </span>
                )}
                <button
                  type="button"
                  data-roughcut-frame={frame.key}
                  title={`${frame.label} · ${t(
                    `blueprint.frameSource.${frame.source}`,
                  )}`}
                  onClick={() => onSelectTimeline(frame.timelineId)}
                  className="group relative h-[88px] w-[52px] shrink-0 overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] transition-all hover:-translate-y-0.5 hover:border-[var(--color-accent)] hover:shadow-[var(--shadow-sm)]"
                >
                  {url &&
                    (frame.mediaKind === "video" ? (
                      <video
                        src={url}
                        muted
                        preload="metadata"
                        className="h-full w-full object-cover"
                      />
                    ) : (
                      <img
                        src={url}
                        alt=""
                        loading="lazy"
                        className="h-full w-full object-cover"
                      />
                    ))}
                  <span
                    className={`absolute left-0 top-0 rounded-br px-1 py-px text-[8px] font-bold text-white ${SOURCE_STYLE[frame.source]}`}
                  >
                    {t(`blueprint.frameSource.${frame.source}`)}
                  </span>
                  <span className="absolute inset-x-0 bottom-0 truncate bg-gradient-to-t from-black/70 to-transparent px-1 pb-0.5 pt-2 text-left text-[8px] font-semibold text-white">
                    {frame.label}
                  </span>
                </button>
              </span>
            );
          })}
        </div>
      )}
    </section>
  );
}
