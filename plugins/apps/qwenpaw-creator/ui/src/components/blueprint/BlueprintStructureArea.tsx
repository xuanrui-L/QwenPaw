import { useMemo } from "react";
import {
  Brain,
  Clapperboard,
  FileText,
  Film,
  GitBranch,
  ListVideo,
  Palette,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  NarrativeEdgeDocument,
  ProjectDocument,
} from "@/contracts/creator";
import {
  isVideoProductionElement,
  isVoiceOnlyVisualEntity,
  layoutNarrativeGraph,
  roughCutFrameForElement,
  selectResearchSlots,
  selectTimelineRenderSlot,
  selectTimelineScriptSlot,
  type TimelineSummary,
} from "@/selectors/blueprintSelectors";
import { orderedTimelineElements } from "@/selectors/timelineElementSelectors";
import type { NarrativeShape } from "@/selectors/timelineElementSelectors";
import { TONE_CHIP, TONE_TEXT, type BlueprintTone } from "./tones";

export interface StructureAreaCallbacks {
  onSelectTimeline: (timelineId: string) => void;
  onOpenTimeline: (timelineId: string) => void;
  onOpenElement: (timelineId: string, elementId: string) => void;
  onOpenVisualEntity: (entityId: string) => void;
  onOpenResearch: (slotId: string) => void;
  onOpenSource: (sourceId: string) => void;
}

interface StructureAreaProps extends StructureAreaCallbacks {
  project: ProjectDocument;
  shape: NarrativeShape;
  summaries: TimelineSummary[];
  edges: NarrativeEdgeDocument[];
  selectedTimelineId: string | null;
}

function episodeTitle(
  summary: TimelineSummary,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  return summary.title || t("blueprint.episodeN", { n: summary.index + 1 });
}

function summaryStatus(summary: TimelineSummary): {
  tone: BlueprintTone;
  key: string;
} {
  if (summary.renderReady) return { tone: "done", key: "done" };
  if (summary.videoReady > 0) return { tone: "run", key: "run" };
  if (summary.hasScript)
    return summary.scriptStale
      ? { tone: "wait", key: "scriptStale" }
      : { tone: "wait", key: "wait" };
  return { tone: "idle", key: "idle" };
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes > 0
    ? `${minutes}′${String(rest).padStart(2, "0")}″`
    : `${rest}s`;
}

function TimelineQuickButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <span
      role="button"
      tabIndex={0}
      title={label}
      className="ml-2 inline-flex items-center gap-0.5 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-2 py-0.5 text-[10px] font-bold text-[var(--color-text-secondary)] opacity-0 transition-all group-hover:opacity-100 hover:border-[var(--color-accent)] hover:bg-[var(--color-accent-soft)] hover:text-[var(--color-accent)]"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.stopPropagation();
          onClick();
        }
      }}
    >
      {label} »
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Single node: production board (stage columns × real artifact cards) */
/* ------------------------------------------------------------------ */

interface BoardCard {
  key: string;
  label: string;
  sub?: string;
  tone: BlueprintTone;
  emphasized?: boolean;
  onClick?: () => void;
}

interface BoardColumn {
  key: string;
  name: string;
  sub: string;
  tone: BlueprintTone;
  icon: React.ReactNode;
  cards: BoardCard[];
}

function columnTone(cards: BoardCard[]): BlueprintTone {
  if (!cards.length) return "idle";
  if (cards.every((card) => card.tone === "done")) return "done";
  if (cards.some((card) => card.tone === "run")) return "run";
  if (cards.some((card) => card.tone === "wait")) return "wait";
  return "idle";
}

function SingleBoard({
  project,
  summaries,
  onSelectTimeline,
  onOpenElement,
  onOpenVisualEntity,
  onOpenResearch,
  onOpenSource,
}: StructureAreaProps) {
  const { t } = useTranslation();
  const summary = summaries[0];
  const columns = useMemo<BoardColumn[]>(() => {
    if (!summary) return [];
    const timelineId = summary.timelineId;
    // 1. Input understanding: sources + intelligence versions.
    const sourceCards: BoardCard[] = project.sources.sources.order
      .map((sourceId) => project.sources.sources.items[sourceId])
      .filter(Boolean)
      .map((source) => ({
        key: `source:${source.source_id}`,
        label: source.display_name || source.source_id,
        sub: source.current_intelligence_version_id
          ? t("blueprint.board.sourceUnderstood")
          : t("blueprint.board.sourcePending"),
        tone: source.current_intelligence_version_id
          ? ("done" as const)
          : ("wait" as const),
        onClick: () => onOpenSource(source.source_id),
      }));
    const researchCards: BoardCard[] = selectResearchSlots(project).map(
      (entry) => ({
        key: `research:${entry.slot.slot_id}`,
        label:
          entry.selected?.name ||
          String(entry.slot.metadata.topic || t("blueprint.researchReport")),
        sub: entry.selected
          ? t("blueprint.board.researchReady")
          : t("blueprint.board.researchRunning"),
        tone: entry.selected ? ("done" as const) : ("run" as const),
        onClick: () => onOpenResearch(entry.slot.slot_id),
      }),
    );
    // 2. Script: timeline_script slot; legacy read-only mapping otherwise.
    const script = selectTimelineScriptSlot(project, timelineId);
    const scriptCards: BoardCard[] = [
      script?.selected
        ? {
            key: "script",
            label: script.selected.name || t("blueprint.scriptTitle"),
            sub: script.selected.stale
              ? t("blueprint.board.scriptStale")
              : t("blueprint.board.scriptReady", {
                  count: script.slot.version_ids.length,
                }),
            tone: script.selected.stale ? "wait" : "done",
            emphasized: !script.selected.stale,
            onClick: () => onSelectTimeline(timelineId),
          }
        : {
            key: "script",
            label: t("blueprint.legacyScriptCard"),
            sub: t("blueprint.legacyScriptHint"),
            tone: "idle",
            onClick: () => onSelectTimeline(timelineId),
          },
    ];
    // 3. Visual design: real entity states; enrolled voice-only roles
    //    (e.g. the video_edit narrator) have no portrait to design.
    const visualCards: BoardCard[] = project.visual.entities.order
      .map((entityId) => project.visual.entities.items[entityId])
      .filter(Boolean)
      .map((entity) => {
        const voiceOnly = isVoiceOnlyVisualEntity(entity);
        const done =
          voiceOnly ||
          Boolean(
            entity.selected_artifact_version_id ||
              entity.variants.order.some(
                (variantId) =>
                  entity.variants.items[variantId]?.selected_artifact_version_id,
              ),
          );
        return {
          key: `visual:${entity.entity_id}`,
          label: entity.name,
          sub: voiceOnly
            ? t("blueprint.board.voiceReady")
            : done
            ? t("blueprint.board.visualReady")
            : t("blueprint.board.visualPending"),
          tone: done ? ("done" as const) : ("wait" as const),
          onClick: () => onOpenVisualEntity(entity.entity_id),
        };
      });
    // 4. Video generation: element_video slot state per element.
    const elementCards: BoardCard[] = orderedTimelineElements(summary.timeline)
      .filter((element) => element.enabled && isVideoProductionElement(element))
      .map((element) => {
        const frame = roughCutFrameForElement(project, element);
        return {
          key: `element:${element.element_id}`,
          label: element.label || element.element_id,
          sub:
            frame.source === "final"
              ? t("blueprint.board.elementReady")
              : frame.source === "storyboard"
              ? t("blueprint.board.elementStoryboard")
              : t("blueprint.board.elementPending"),
          tone:
            frame.source === "final"
              ? ("done" as const)
              : frame.source === "storyboard"
              ? ("run" as const)
              : ("wait" as const),
          onClick: () => onOpenElement(timelineId, element.element_id),
        };
      });
    // 5. Final cut.
    const render = selectTimelineRenderSlot(project, timelineId);
    const finalReady = Boolean(render?.selected && !render.selected.stale);
    const renderCards: BoardCard[] = [
      {
        key: "render",
        label: render?.selected?.name || t("blueprint.finalCut"),
        sub: finalReady
          ? t("blueprint.board.renderReady")
          : t("blueprint.board.renderPending"),
        tone: finalReady ? "done" : "idle",
      },
    ];
    // A selected, fresh final cut is durable proof the upstream pipeline
    // ran to completion (any upstream edit would have marked it stale via
    // staleness propagation). Steps whose scenario path never produces the
    // artifact checked above — video_edit stamps no source intelligence,
    // timeline_script or element_video slots — must not read as incomplete
    // under a composed final video.
    const impliedDone = (cards: BoardCard[]): BoardCard[] =>
      finalReady
        ? cards.map((card) =>
            card.tone === "wait" || card.tone === "idle"
              ? {
                  ...card,
                  tone: "done" as const,
                  emphasized: false,
                  sub: t("blueprint.board.impliedByFinal"),
                }
              : card,
          )
        : cards;
    const make = (
      key: string,
      name: string,
      icon: React.ReactNode,
      cards: BoardCard[],
    ): BoardColumn => {
      const tone = columnTone(cards);
      return {
        key,
        name,
        icon,
        cards,
        tone,
        sub: t(`blueprint.columnState.${tone}`),
      };
    };
    return [
      make(
        "understanding",
        t("blueprint.columns.understanding"),
        <Brain className="h-3.5 w-3.5" />,
        impliedDone([...sourceCards, ...researchCards]),
      ),
      make(
        "script",
        t("blueprint.columns.script"),
        <FileText className="h-3.5 w-3.5" />,
        impliedDone(scriptCards),
      ),
      make(
        "visual",
        t("blueprint.columns.visual"),
        <Palette className="h-3.5 w-3.5" />,
        impliedDone(visualCards),
      ),
      make(
        "video",
        t("blueprint.columns.video"),
        <Clapperboard className="h-3.5 w-3.5" />,
        impliedDone(elementCards),
      ),
      make(
        "final",
        t("blueprint.columns.final"),
        <Film className="h-3.5 w-3.5" />,
        renderCards,
      ),
    ];
  }, [
    onOpenElement,
    onOpenResearch,
    onOpenSource,
    onOpenVisualEntity,
    onSelectTimeline,
    project,
    summary,
    t,
  ]);
  if (!summary) return null;

  return (
    <div className="flex h-full flex-col" data-blueprint-shape="single">
      <div className="grid min-h-0 flex-1 grid-cols-5 gap-3">
        {columns.map((column, index) => (
          <div
            key={column.key}
            className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)]/50"
          >
            <div className="flex items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-2.5">
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-[1.5px] ${
                  column.tone === "done"
                    ? "border-[var(--color-success)] bg-[var(--color-success-soft)] text-[var(--color-success)]"
                    : column.tone === "wait"
                    ? "border-[var(--color-warning)] bg-[var(--color-warning-soft)] text-[var(--color-warning)] shadow-[0_0_0_4px_rgba(247,144,9,.08)]"
                    : column.tone === "run"
                    ? "border-[var(--color-primary,#3b82f6)] bg-[rgba(59,130,246,.08)] text-[var(--color-primary,#3b82f6)]"
                    : "border-[var(--color-border)] bg-[var(--color-bg-primary)] text-[var(--color-text-tertiary)]"
                }`}
              >
                {column.icon}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-xs font-semibold text-[var(--color-text-primary)]">
                  {index + 1}. {column.name}
                </span>
                <span
                  className={`block truncate text-[10px] ${TONE_TEXT[column.tone]}`}
                >
                  {column.sub}
                </span>
              </span>
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2">
              {column.cards.length ? (
                column.cards.map((card) => (
                  <button
                    key={card.key}
                    type="button"
                    disabled={!card.onClick}
                    onClick={card.onClick}
                    className={`w-full rounded-xl border px-2.5 py-2 text-left transition-all duration-200 ${
                      card.emphasized
                        ? "border-[var(--color-warning)] bg-[var(--color-warning-soft)] shadow-[0_0_0_3px_rgba(247,144,9,.08)] hover:-translate-y-px hover:shadow-[var(--shadow-sm)]"
                        : card.onClick
                        ? "border-[var(--color-border)] bg-[var(--color-bg-card)] hover:-translate-y-px hover:border-[var(--color-accent)] hover:shadow-[var(--shadow-md)]"
                        : "cursor-default border-dashed border-[var(--color-border)] bg-[var(--color-bg-primary)]/60"
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                          card.tone === "done"
                            ? "bg-[var(--color-success)]"
                            : card.tone === "run"
                            ? "bg-[var(--color-primary,#3b82f6)]"
                            : card.tone === "wait"
                            ? "animate-pulse bg-[var(--color-warning)]"
                            : "bg-[var(--color-border-strong)]"
                        }`}
                      />
                      <b className="min-w-0 flex-1 truncate text-[11px] font-semibold text-[var(--color-text-primary)]">
                        {card.label}
                      </b>
                    </span>
                    {card.sub && (
                      <span className="mt-0.5 block pl-3 text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
                        {card.sub}
                      </span>
                    )}
                  </button>
                ))
              ) : (
                <span className="px-1 pt-1 text-[10px] text-[var(--color-text-tertiary)]">
                  {t("blueprint.columnEmpty")}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      <p className="pt-2.5 text-center text-[11px] text-[var(--color-text-tertiary)]">
        {t("blueprint.singleHint")}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Linear: episode list                                                */
/* ------------------------------------------------------------------ */

function EpisodeList({
  summaries,
  selectedTimelineId,
  onSelectTimeline,
  onOpenTimeline,
}: StructureAreaProps) {
  const { t } = useTranslation();
  return (
    <div
      className="surface flex h-full flex-col overflow-hidden"
      data-blueprint-shape="linear"
    >
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2.5">
        <span className="flex items-center gap-2 text-xs font-semibold text-[var(--color-text-primary)]">
          <ListVideo className="h-3.5 w-3.5 text-[var(--color-accent)]" />
          {t("blueprint.episodeList", { count: summaries.length })}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {summaries.map((summary) => {
          const selected = summary.timelineId === selectedTimelineId;
          const status = summaryStatus(summary);
          return (
            <button
              key={summary.timelineId}
              type="button"
              data-blueprint-episode={summary.timelineId}
              onClick={() => onSelectTimeline(summary.timelineId)}
              className={`group flex w-full items-center gap-3.5 border-b border-[var(--color-border)] px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-[var(--color-bg-secondary)] ${
                selected
                  ? "bg-[var(--color-accent-soft)] shadow-[inset_3px_0_0_var(--color-accent)]"
                  : ""
              }`}
            >
              <span className="flex h-10 w-10 shrink-0 flex-col items-center justify-center rounded-[10px] bg-[var(--color-bg-secondary)] text-[15px] font-bold leading-none text-[var(--color-text-primary)]">
                <small className="mb-0.5 text-[9px] font-semibold text-[var(--color-text-tertiary)]">
                  EP
                </small>
                {summary.index + 1}
              </span>
              <span className="min-w-0 flex-1">
                <b className="block text-[13px] font-semibold text-[var(--color-text-primary)]">
                  {episodeTitle(summary, t)}
                </b>
                <p className="mt-0.5 truncate text-[11px] text-[var(--color-text-secondary)]">
                  {summary.synopsis || t("blueprint.noSynopsis")}
                </p>
              </span>
              <span className="flex shrink-0 items-center gap-3.5">
                <span
                  className={`rounded px-1.5 text-[10px] font-semibold leading-[18px] ${TONE_CHIP[status.tone]}`}
                >
                  {t(`blueprint.episodeStatus.${status.key}`)}
                </span>
                <span className="w-[52px] text-right text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
                  {formatDuration(summary.durationSeconds)}
                </span>
                <TimelineQuickButton
                  label={t("blueprint.timelineQuick")}
                  onClick={() => onOpenTimeline(summary.timelineId)}
                />
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Branching: layered graph canvas                                     */
/* ------------------------------------------------------------------ */

const NODE_WIDTH = 218;
const NODE_HEIGHT = 132;
const LAYER_GAP = 120;
const ROW_GAP = 42;
const CANVAS_PADDING = 28;

function GraphCanvas({
  summaries,
  edges,
  selectedTimelineId,
  onSelectTimeline,
  onOpenTimeline,
}: StructureAreaProps) {
  const { t } = useTranslation();
  const layout = useMemo(
    () => layoutNarrativeGraph(summaries, edges),
    [edges, summaries],
  );
  const positions = useMemo(() => {
    const map = new Map<string, { x: number; y: number }>();
    for (const summary of summaries) {
      const cell = layout.get(summary.timelineId);
      if (!cell) continue;
      map.set(summary.timelineId, {
        x: CANVAS_PADDING + cell.layer * (NODE_WIDTH + LAYER_GAP),
        y: CANVAS_PADDING + cell.row * (NODE_HEIGHT + ROW_GAP),
      });
    }
    return map;
  }, [layout, summaries]);
  const width =
    CANVAS_PADDING * 2 +
    (Math.max(0, ...[...layout.values()].map((cell) => cell.layer)) + 1) *
      (NODE_WIDTH + LAYER_GAP) -
    LAYER_GAP;
  const height =
    CANVAS_PADDING * 2 +
    (Math.max(0, ...[...layout.values()].map((cell) => cell.row)) + 1) *
      (NODE_HEIGHT + ROW_GAP) -
    ROW_GAP;
  const endingIds = useMemo(() => {
    const withOutgoing = new Set(edges.map((edge) => edge.source_timeline_id));
    return new Set(
      summaries
        .map((summary) => summary.timelineId)
        .filter((id) => !withOutgoing.has(id)),
    );
  }, [edges, summaries]);

  return (
    <div
      data-blueprint-shape="branching"
      className="h-full overflow-auto rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] shadow-[var(--shadow-xs)]"
      style={{
        backgroundImage:
          "radial-gradient(circle, var(--color-border) 1px, transparent 1px)",
        backgroundSize: "22px 22px",
      }}
    >
      <div className="relative" style={{ width, height }}>
        <svg className="pointer-events-none absolute inset-0 h-full w-full">
          {edges.map((edge) => {
            const source = positions.get(edge.source_timeline_id);
            const target = positions.get(edge.target_timeline_id);
            if (!source || !target) return null;
            const x1 = source.x + NODE_WIDTH;
            const y1 = source.y + NODE_HEIGHT / 2;
            const x2 = target.x;
            const y2 = target.y + NODE_HEIGHT / 2;
            const dx = Math.max(40, (x2 - x1) / 2);
            return (
              <path
                key={edge.edge_id}
                d={`M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke="var(--color-border-strong)"
                strokeWidth={1.5}
              />
            );
          })}
        </svg>
        {edges.map((edge) => {
          const source = positions.get(edge.source_timeline_id);
          const target = positions.get(edge.target_timeline_id);
          if (!source || !target || !edge.label) return null;
          const x = (source.x + NODE_WIDTH + target.x) / 2;
          const y = (source.y + target.y + NODE_HEIGHT) / 2;
          return (
            <span
              key={`label:${edge.edge_id}`}
              className="absolute -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border border-[var(--color-border-strong)] bg-[var(--color-bg-primary)]/80 px-2.5 py-1 text-[10px] font-semibold text-[var(--color-text-secondary)] shadow-[var(--shadow-sm)] backdrop-blur-md"
              style={{ left: x, top: y }}
              title={edge.prompt}
            >
              {edge.label}
            </span>
          );
        })}
        {summaries.map((summary) => {
          const position = positions.get(summary.timelineId);
          if (!position) return null;
          const selected = summary.timelineId === selectedTimelineId;
          const status = summaryStatus(summary);
          const ending = endingIds.has(summary.timelineId);
          return (
            <button
              key={summary.timelineId}
              type="button"
              data-blueprint-node={summary.timelineId}
              onClick={() => onSelectTimeline(summary.timelineId)}
              className={`group absolute rounded-2xl border bg-[var(--color-bg-card)]/85 p-3 text-left shadow-[var(--shadow-sm)] backdrop-blur-md transition-all duration-200 hover:-translate-y-1 hover:border-[var(--color-accent)] hover:shadow-[var(--shadow-lg)] ${
                selected
                  ? "border-[var(--color-accent)] shadow-[0_0_0_3px_var(--color-accent-soft)]"
                  : "border-[var(--color-border)]"
              }`}
              style={{
                left: position.x,
                top: position.y,
                width: NODE_WIDTH,
                minHeight: NODE_HEIGHT,
              }}
            >
              <div className="mb-1.5 flex items-center justify-between gap-1.5">
                <span
                  className={`badge font-bold ${
                    ending
                      ? "bg-[rgba(139,92,246,.12)] text-[#8b5cf6]"
                      : "bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                  }`}
                >
                  {ending
                    ? t("blueprint.endingNode")
                    : t("blueprint.episodeN", { n: summary.index + 1 })}
                </span>
                <span
                  className={`rounded px-1.5 text-[9px] font-semibold leading-[16px] ${TONE_CHIP[status.tone]}`}
                >
                  {t(`blueprint.episodeStatus.${status.key}`)}
                </span>
              </div>
              <h4 className="mb-1 text-[13px] font-semibold text-[var(--color-text-primary)]">
                {episodeTitle(summary, t)}
              </h4>
              <p className="mb-2 line-clamp-2 text-[11px] leading-normal text-[var(--color-text-secondary)]">
                {summary.synopsis || t("blueprint.noSynopsis")}
              </p>
              <div className="flex items-center justify-between border-t border-dashed border-[var(--color-border)] pt-2">
                <span className="text-[10px] text-[var(--color-text-tertiary)]">
                  {t("blueprint.nodeMeta", {
                    ready: summary.videoReady,
                    total: summary.videoTotal,
                  })}
                </span>
                <span className="flex shrink-0 items-center text-[10px] text-[var(--color-text-tertiary)]">
                  {formatDuration(summary.durationSeconds)}
                  <TimelineQuickButton
                    label={t("blueprint.timelineQuick")}
                    onClick={() => onOpenTimeline(summary.timelineId)}
                  />
                </span>
              </div>
            </button>
          );
        })}
        {edges.some((edge) => edge.prompt) && (
          <span className="absolute bottom-2 left-1/2 flex -translate-x-1/2 items-center gap-1 text-[10px] text-[var(--color-text-tertiary)]">
            <GitBranch className="h-3 w-3" />
            {t("blueprint.branchHint")}
          </span>
        )}
      </div>
    </div>
  );
}

export default function BlueprintStructureArea(props: StructureAreaProps) {
  if (props.shape === "branching") return <GraphCanvas {...props} />;
  if (props.shape === "linear") return <EpisodeList {...props} />;
  return <SingleBoard {...props} />;
}
