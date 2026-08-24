import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Clapperboard } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectDocument } from "@/contracts/creator";
import { getArtifactVersionMediaUrl } from "@/api/creator";
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
  const frames = useMemo(() => selectRoughCutFrames(project), [project]);
  const readyCount = frames.filter((frame) => frame.source === "final").length;
  const multiTimeline = new Set(frames.map((frame) => frame.timelineId)).size > 1;
  if (!frames.length) return null;

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
        <button
          type="button"
          onClick={() => setCollapsed((value) => !value)}
          className="icon-button ml-auto !h-7 !w-7 shrink-0"
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
      </div>
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
