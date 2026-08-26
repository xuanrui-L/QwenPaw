import { useMemo } from "react";
import { PanelLeftClose } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectDocument } from "@/contracts/creator";
import {
  getArtifactVersionFrameUrl,
  getArtifactVersionMediaUrl,
  getAssetVersionFrameUrl,
  getAssetVersionMediaUrl,
} from "@/api/creator";
import {
  roughCutFrameForElement,
  selectTimelineSummaries,
  type TimelineSummary,
} from "@/selectors/blueprintSelectors";
import { orderedTimelineElements } from "@/selectors/timelineElementSelectors";
import { TONE_DOT, type BlueprintTone } from "./tones";

function summaryTone(summary: TimelineSummary): BlueprintTone {
  if (summary.renderReady) return "done";
  if (summary.videoReady > 0) return "run";
  if (summary.hasScript) return "wait";
  return "idle";
}

function firstFrameUrl(
  project: ProjectDocument,
  summary: TimelineSummary,
): string | null {
  const element = orderedTimelineElements(summary.timeline).find(
    (candidate) => candidate.enabled,
  );
  if (!element) return null;
  const frame = roughCutFrameForElement(project, element);
  if (!frame.versionId) return null;
  const fromSource = frame.versionKind === "source";
  // Video artifacts can't render inside <img>; use the poster-frame endpoint.
  if (frame.mediaKind === "video") {
    return fromSource
      ? getAssetVersionFrameUrl(frame.versionId, 0, 160)
      : getArtifactVersionFrameUrl(frame.versionId, 0, 160);
  }
  return fromSource
    ? getAssetVersionMediaUrl(frame.versionId)
    : getArtifactVersionMediaUrl(frame.versionId);
}

interface EpisodeRailProps {
  project: ProjectDocument;
  activeTimelineId: string | null;
  onCollapse: () => void;
  onSwitch: (timelineId: string) => void;
}

/**
 * Left episode rail of the timeline-edit page (plan §4.6): 200px expanded,
 * fully zero-width when collapsed (parent unmounts it); only rendered for
 * multi-timeline projects.
 */
export default function EpisodeRail({
  project,
  activeTimelineId,
  onCollapse,
  onSwitch,
}: EpisodeRailProps) {
  const { t } = useTranslation();
  const summaries = useMemo(
    () => selectTimelineSummaries(project),
    [project],
  );

  return (
    <aside
      data-episode-rail
      className="flex min-h-0 w-[200px] shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-primary)]"
    >
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-border)] px-2 py-2">
        <span className="pl-1 text-[11px] font-bold text-[var(--color-text-secondary)]">
          {t("blueprint.episodesCount", { count: summaries.length })}
        </span>
        <button
          type="button"
          onClick={onCollapse}
          className="icon-button !h-7 !w-7"
          title={t("blueprint.collapseEpisodeRail")}
        >
          <PanelLeftClose className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto py-1.5">
        {summaries.map((summary) => {
          const active = summary.timelineId === activeTimelineId;
          const tone = summaryTone(summary);
          const title =
            summary.title || t("blueprint.episodeN", { n: summary.index + 1 });
          const frameUrl = firstFrameUrl(project, summary);
          const progress =
            summary.videoTotal > 0
              ? Math.round((summary.videoReady / summary.videoTotal) * 100)
              : null;
          return (
            <button
              key={summary.timelineId}
              type="button"
              title={summary.synopsis || title}
              onClick={() => onSwitch(summary.timelineId)}
              className={`mx-1.5 mb-1 flex w-[calc(100%-12px)] items-center gap-2 rounded-lg px-1.5 py-1.5 text-left transition-colors ${
                active
                  ? "bg-[var(--color-accent-soft)] shadow-[inset_0_0_0_1px_rgba(255,127,22,.25)]"
                  : "hover:bg-[var(--color-bg-secondary)]"
              }`}
            >
              <span className="h-9 w-7 shrink-0 overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)]">
                {frameUrl && (
                  <img
                    src={frameUrl}
                    alt=""
                    loading="lazy"
                    className="h-full w-full object-cover"
                  />
                )}
              </span>
              <span className="min-w-0 flex-1 leading-tight">
                <span
                  className={`block truncate text-[11px] font-bold ${
                    active
                      ? "text-[var(--color-text-primary)]"
                      : "text-[var(--color-text-secondary)]"
                  }`}
                >
                  {title}
                </span>
                <span className="mt-0.5 flex items-center gap-1">
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[tone]}`}
                  />
                  <span className="truncate text-[10px] text-[var(--color-text-tertiary)]">
                    {t(`blueprint.episodeState.${tone}`)}
                  </span>
                </span>
                {progress !== null && progress < 100 && progress > 0 && (
                  <span className="mt-1 block h-1 overflow-hidden rounded-full bg-[var(--color-bg-secondary)]">
                    <span
                      className="block h-full rounded-full bg-[var(--color-primary,#3b82f6)]"
                      style={{ width: `${progress}%` }}
                    />
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
