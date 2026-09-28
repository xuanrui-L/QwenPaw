import { useId, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  Brain,
  Clapperboard,
  FileText,
  Film,
  GitBranch,
  ListVideo,
  Maximize2,
  Minus,
  Plus,
  RotateCcw,
  X,
  ArrowRight,
  GripVertical,
  Expand,
  Shrink,
  Palette,
  SquarePen,
  Search,
  ChevronDown,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  NarrativeEdgeDocument,
  ProjectDocument,
} from "@/contracts/creator";
import {
  isVideoProductionElement,
  isVoiceOnlyVisualEntity,
  roughCutFrameForElement,
  selectResearchSlots,
  selectTimelineRenderSlot,
  selectTimelineScriptSlot,
  type TimelineSummary,
} from "@/selectors/blueprintSelectors";
import { orderedTimelineElements } from "@/selectors/timelineElementSelectors";
import type { NarrativeShape } from "@/selectors/timelineElementSelectors";
import { TONE_CHIP, TONE_TEXT, type BlueprintTone } from "./tones";
import WorkspaceEmptyState from "@/components/WorkspaceEmptyState";
import {
  GRAPH_NODE_HEIGHT,
  GRAPH_NODE_WIDTH,
  layoutStoryNodes,
  routeStoryEdges,
} from "./narrativeGraphLayout";
import { useNarrativeGraphViewport } from "./useNarrativeGraphViewport";

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

/** 节点卡操作胶囊 (design 84:30317): h-6 filled pill, icon + bold caption. */
function NodeActionPill({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <span
      role="button"
      data-graph-action
      tabIndex={0}
      title={label}
      className="inline-flex h-6 items-center gap-1 rounded-full bg-[var(--color-bg-secondary)] px-2.5 text-[10px] font-semibold text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-accent-soft)] hover:text-[var(--color-accent)]"
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
      {icon}
      {label}
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
                  entity.variants.items[variantId]
                    ?.selected_artifact_version_id,
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
                  className={`block truncate text-[10px] ${
                    TONE_TEXT[column.tone]
                  }`}
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
      className="flex h-full flex-col overflow-hidden"
      data-blueprint-shape="linear"
    >
      <span className="flex shrink-0 items-center gap-2 pb-2.5 text-sm font-medium text-[var(--color-text-primary)]">
        <ListVideo className="h-3.5 w-3.5 text-[var(--color-accent)]" />
        {t("blueprint.episodeList", { count: summaries.length })}
      </span>
      {/* 集卡网格 (design 84:29455): filled tiles with status tag, title,
          stats and the 查看剧本/制作台编辑 pills. */}
      <div className="grid min-h-0 flex-1 auto-rows-min grid-cols-[repeat(auto-fill,minmax(250px,1fr))] gap-3 overflow-y-auto pb-1">
        {summaries.map((summary) => {
          const selected = summary.timelineId === selectedTimelineId;
          const status = summaryStatus(summary);
          return (
            <button
              key={summary.timelineId}
              type="button"
              data-blueprint-episode={summary.timelineId}
              onClick={() => onSelectTimeline(summary.timelineId)}
              className={`flex h-fit flex-col gap-1.5 rounded-lg border bg-[var(--color-bg-secondary)] p-3 text-left transition-colors hover:border-[var(--color-accent)]/50 ${
                selected
                  ? "border-[var(--color-accent)] shadow-[0_0_0_3px_var(--color-accent-soft)]"
                  : "border-transparent"
              }`}
            >
              <span className="flex items-center justify-between gap-1.5">
                <span
                  className={`rounded px-1.5 text-[10px] font-semibold leading-[18px] ${
                    TONE_CHIP[status.tone]
                  }`}
                >
                  {t(`blueprint.episodeStatus.${status.key}`)}
                </span>
                <span className="text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                  {formatDuration(summary.durationSeconds)}
                </span>
              </span>
              <b className="block truncate text-[13px] font-semibold text-[var(--color-text-primary)]">
                {episodeTitle(summary, t)}
              </b>
              <span className="text-[11px] text-[var(--color-text-tertiary)]">
                {t("blueprint.nodeMeta", {
                  ready: summary.videoReady,
                  total: summary.videoTotal,
                })}
              </span>
              <span className="mt-0.5 flex items-center gap-3">
                <NodeActionPill
                  icon={<FileText className="h-3.5 w-3.5" />}
                  label={t("blueprint.viewScript")}
                  onClick={() => onSelectTimeline(summary.timelineId)}
                />
                <NodeActionPill
                  icon={<SquarePen className="h-3.5 w-3.5" />}
                  label={t("blueprint.editTimeline")}
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

function GraphCanvas({
  project,
  summaries,
  edges,
  selectedTimelineId,
  onSelectTimeline,
  onOpenTimeline,
}: StructureAreaProps) {
  const { t } = useTranslation();
  const markerId = useId().replace(/:/g, "");
  const structurePending = edges.length === 0;
  const topology = JSON.stringify([
    summaries.map((summary) => summary.timelineId),
    edges.map((edge) => [
      edge.edge_id,
      edge.source_timeline_id,
      edge.target_timeline_id,
    ]),
  ]);
  const automatic = useMemo(() => {
    const [ids, links] = JSON.parse(topology) as [string[], string[][]];
    return layoutStoryNodes(
      ids,
      links.map(([edge_id, source_timeline_id, target_timeline_id]) => ({
        edge_id,
        source_timeline_id,
        target_timeline_id,
      })),
    );
  }, [topology]);
  const [expanded, setExpanded] = useState(false);
  const graph = useNarrativeGraphViewport(
    project.project_id,
    automatic,
    expanded,
  );
  const routes = useMemo(
    () => routeStoryEdges(edges, graph.positions),
    [edges, graph.positions],
  );
  const [localFocus, setLocalFocus] = useState<string | null | undefined>(
    undefined,
  );
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  const focusedId = localFocus === undefined ? selectedTimelineId : localFocus;
  const focused = summaries.find((summary) => summary.timelineId === focusedId);
  const incoming = routes.filter(
    (route) => route.edge.target_timeline_id === focusedId,
  );
  const outgoing = routes.filter(
    (route) => route.edge.source_timeline_id === focusedId,
  );
  const connected = new Set([
    focusedId,
    ...incoming.map((route) => route.edge.source_timeline_id),
    ...outgoing.map((route) => route.edge.target_timeline_id),
  ]);
  const endingIds = useMemo(() => {
    const withOutgoing = new Set(edges.map((edge) => edge.source_timeline_id));
    return new Set(
      edges
        .map((edge) => edge.target_timeline_id)
        .filter((id) => !withOutgoing.has(id)),
    );
  }, [edges]);
  const nodeName = (id: string) => {
    const summary = summaries.find((item) => item.timelineId === id);
    return summary
      ? `${t("blueprint.episodeN", { n: summary.index + 1 })} · ${episodeTitle(
          summary,
          t,
        )}`
      : id;
  };
  function jump(id: string) {
    setLocalFocus(id);
    setHoveredEdge(null);
    graph.focusNode(id);
  }
  function openScript(id: string) {
    graph.rememberView();
    setExpanded(false);
    onSelectTimeline(id);
  }
  const toolbarButton =
    "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text-primary)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 disabled:opacity-35";

  const content = (
    <div
      data-blueprint-shape="branching"
      data-graph-expanded={expanded}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          graph.rememberView();
          setExpanded(false);
        }
      }}
      className={`${
        expanded ? "fixed inset-x-3 bottom-3 top-16 z-[250]" : "h-full"
      } flex min-h-0 flex-col overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] shadow-[var(--shadow-xs)]`}
    >
      <div
        data-graph-toolbar
        className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-2.5"
      >
        <div
          className="flex shrink-0 items-center gap-1.5 text-sm font-semibold text-[var(--color-text-primary)]"
          title={t("blueprint.graph.navigationHint")}
        >
          <GitBranch className="h-4 w-4" />
          <span>{t("blueprint.graph.title")}</span>
          <span className="rounded-md bg-[var(--color-bg-secondary)] px-1.5 py-0.5 text-[10px] font-normal text-[var(--color-text-tertiary)]">
            {summaries.length}
          </span>
        </div>
        {structurePending ? (
          <span className="ml-auto text-xs text-[var(--color-text-secondary)]">
            {t("blueprint.branchesPending")}
          </span>
        ) : (
          <div className="ml-auto flex min-w-0 items-center gap-1.5">
            <div className="relative flex h-8 min-w-20 max-w-44 flex-1 items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)]/40 focus-within:border-[var(--color-text-secondary)]">
              <Search className="pointer-events-none absolute left-2 h-3.5 w-3.5 text-[var(--color-text-tertiary)]" />
              <select
                aria-label={t("blueprint.graph.locateNode")}
                value={focused?.timelineId ?? ""}
                onChange={(event) =>
                  event.target.value && jump(event.target.value)
                }
                className="h-full w-full min-w-0 appearance-none truncate rounded-md bg-transparent pl-7 pr-6 text-[11px] text-[var(--color-text-secondary)] outline-none"
              >
                <option value="">{t("blueprint.graph.locateNode")}</option>
                {summaries.map((summary) => (
                  <option key={summary.timelineId} value={summary.timelineId}>
                    {nodeName(summary.timelineId)}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2 h-3 w-3 text-[var(--color-text-tertiary)]" />
            </div>
            <div className="flex shrink-0 items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)]/40">
              <button
                type="button"
                className={toolbarButton}
                aria-label={t("blueprint.graph.zoomOut")}
                title={t("blueprint.graph.zoomOut")}
                disabled={graph.scale <= 0.1}
                onClick={() => graph.zoom(graph.scale / 1.25)}
              >
                <Minus className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                className="h-8 w-11 text-[11px] tabular-nums text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                title={t("blueprint.graph.actualSize")}
                onClick={() => graph.zoom(1)}
              >
                {Math.round(graph.scale * 100)}%
              </button>
              <button
                type="button"
                className={toolbarButton}
                aria-label={t("blueprint.graph.zoomIn")}
                title={t("blueprint.graph.zoomIn")}
                disabled={graph.scale >= 1.5}
                onClick={() => graph.zoom(graph.scale * 1.25)}
              >
                <Plus className="h-3.5 w-3.5" />
              </button>
            </div>
            <button
              type="button"
              className={toolbarButton}
              aria-label={t("blueprint.graph.fit")}
              title={t("blueprint.graph.fit")}
              onClick={() => {
                setLocalFocus(null);
                graph.fit();
              }}
            >
              <Maximize2 className="h-4 w-4" />
            </button>
            <button
              type="button"
              className={toolbarButton}
              aria-label={t("blueprint.graph.arrange")}
              title={t("blueprint.graph.arrange")}
              onClick={graph.resetLayout}
            >
              <RotateCcw className="h-4 w-4" />
            </button>
            <button
              type="button"
              className={toolbarButton}
              aria-label={t(
                expanded
                  ? "blueprint.graph.collapse"
                  : "blueprint.graph.expand",
              )}
              title={t(
                expanded
                  ? "blueprint.graph.collapse"
                  : "blueprint.graph.expand",
              )}
              autoFocus={expanded}
              onClick={() => {
                graph.rememberView();
                setExpanded((v) => !v);
              }}
            >
              {expanded ? (
                <Shrink className="h-4 w-4" />
              ) : (
                <Expand className="h-4 w-4" />
              )}
            </button>
          </div>
        )}
        <p className="sr-only">
          {t(
            structurePending
              ? "blueprint.branchesPendingHint"
              : "blueprint.graph.navigationHint",
          )}
        </p>
      </div>
      <div
        ref={graph.viewportRef}
        data-graph-viewport
        tabIndex={0}
        aria-label={t("blueprint.interactiveStoryMap")}
        className={`min-h-0 flex-1 overflow-auto overscroll-contain ${
          structurePending
            ? ""
            : graph.panning
            ? "cursor-grabbing"
            : "cursor-grab"
        }`}
        style={{
          touchAction: structurePending ? "auto" : "none",
          backgroundImage:
            "radial-gradient(circle, var(--color-border) 1px, transparent 1px)",
          backgroundSize: "22px 22px",
        }}
        {...(structurePending ? {} : graph.handlers)}
        onClick={(event) => {
          if (
            !(event.target as HTMLElement).closest(
              "[data-blueprint-node], [data-graph-edge]",
            )
          )
            setLocalFocus(null);
        }}
      >
        {summaries.length === 0 ? (
          <WorkspaceEmptyState
            projectId={project.project_id}
            area="interactive"
          />
        ) : (
          <div
            className={
              structurePending
                ? "grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3 p-4"
                : "relative select-none"
            }
            style={
              structurePending
                ? undefined
                : {
                    width: graph.width * graph.scale,
                    height: graph.height * graph.scale,
                  }
            }
          >
            <div
              className={
                structurePending
                  ? "contents"
                  : "absolute left-0 top-0 origin-top-left"
              }
              data-graph-canvas
              style={
                structurePending
                  ? undefined
                  : {
                      width: graph.width,
                      height: graph.height,
                      transform: `scale(${graph.scale})`,
                    }
              }
            >
              {!structurePending && (
                <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
                  <defs>
                    <marker
                      id={`${markerId}-arrow`}
                      viewBox="0 0 10 10"
                      refX="9"
                      refY="5"
                      markerWidth="7"
                      markerHeight="7"
                      orient="auto-start-reverse"
                    >
                      <path d="M 0 1 L 9 5 L 0 9 z" fill="context-stroke" />
                    </marker>
                  </defs>
                  {[...routes]
                    .sort(
                      (a, b) =>
                        Number(b.edge.edge_id !== hoveredEdge) -
                        Number(a.edge.edge_id !== hoveredEdge),
                    )
                    .map((route) => {
                      const related =
                        route.edge.source_timeline_id === focusedId ||
                        route.edge.target_timeline_id === focusedId;
                      const emphasized = hoveredEdge
                        ? route.edge.edge_id === hoveredEdge
                        : Boolean(focused && related);
                      const dim = hoveredEdge
                        ? !emphasized
                        : focused && !related;
                      return (
                        <g
                          key={route.edge.edge_id}
                          data-graph-edge={route.edge.edge_id}
                          style={{ opacity: dim ? 0.12 : 1 }}
                        >
                          <path
                            d={route.path}
                            fill="none"
                            stroke={
                              emphasized
                                ? "var(--color-text-primary)"
                                : "var(--color-text-secondary)"
                            }
                            strokeWidth={emphasized ? 2.3 : 1.5}
                            vectorEffect="non-scaling-stroke"
                            markerEnd={`url(#${markerId}-arrow)`}
                          />
                          <path
                            d={route.path}
                            fill="none"
                            stroke="transparent"
                            strokeWidth={14}
                            className="pointer-events-auto cursor-pointer"
                            onMouseEnter={() =>
                              setHoveredEdge(route.edge.edge_id)
                            }
                            onMouseLeave={() => setHoveredEdge(null)}
                            onClick={(event) => {
                              event.stopPropagation();
                              setLocalFocus(route.edge.source_timeline_id);
                            }}
                          >
                            <title>{`${nodeName(
                              route.edge.source_timeline_id,
                            )} — ${
                              route.edge.label || t("blueprint.graph.continue")
                            } → ${nodeName(
                              route.edge.target_timeline_id,
                            )}`}</title>
                          </path>
                          <foreignObject
                            x={route.label.x - route.labelWidth / 2}
                            y={route.label.y - 12}
                            width={route.labelWidth}
                            height={24}
                            className="pointer-events-auto overflow-visible"
                          >
                            <button
                              type="button"
                              data-graph-edge-label={route.edge.edge_id}
                              title={`${
                                route.edge.label ||
                                t("blueprint.graph.continue")
                              } → ${nodeName(route.edge.target_timeline_id)}`}
                              onMouseEnter={() =>
                                setHoveredEdge(route.edge.edge_id)
                              }
                              onMouseLeave={() => setHoveredEdge(null)}
                              onClick={(event) => {
                                event.stopPropagation();
                                setLocalFocus(route.edge.source_timeline_id);
                              }}
                              className="block h-6 w-full truncate rounded-full border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-2 text-center text-[11px] font-medium text-[var(--color-text-secondary)] shadow-sm hover:border-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                            >
                              {route.edge.label ||
                                t("blueprint.graph.continue")}
                            </button>
                          </foreignObject>
                        </g>
                      );
                    })}
                </svg>
              )}
              {summaries.map((summary) => {
                const position = graph.positions.get(summary.timelineId);
                if (!position) return null;
                const selected = summary.timelineId === focusedId;
                const status = summaryStatus(summary);
                const ending = endingIds.has(summary.timelineId);
                return (
                  <button
                    key={summary.timelineId}
                    type="button"
                    data-blueprint-node={summary.timelineId}
                    aria-pressed={!structurePending && selected}
                    onClick={() =>
                      structurePending
                        ? openScript(summary.timelineId)
                        : setLocalFocus(summary.timelineId)
                    }
                    className={`group ${
                      structurePending
                        ? "relative"
                        : "absolute cursor-grab active:cursor-grabbing"
                    } flex flex-col rounded-xl border bg-[var(--color-bg-card)] p-3 text-left shadow-[var(--shadow-sm)] transition-[border-color,box-shadow,opacity] hover:border-[var(--color-text-secondary)] ${
                      selected
                        ? "border-[var(--color-text-primary)] ring-2 ring-[var(--color-text-primary)]/15"
                        : "border-[var(--color-border-strong)]"
                    }`}
                    style={{
                      ...(structurePending
                        ? {}
                        : {
                            left: position.x,
                            top: position.y,
                            width: GRAPH_NODE_WIDTH,
                          }),
                      height: GRAPH_NODE_HEIGHT,
                      opacity:
                        focused && !connected.has(summary.timelineId)
                          ? 0.45
                          : 1,
                      zIndex: graph.draggedId === summary.timelineId ? 3 : 1,
                    }}
                  >
                    <div className="mb-1.5 flex h-[18px] shrink-0 items-center justify-between gap-1.5">
                      <span className="flex items-center gap-1 text-[10px] font-semibold text-[var(--color-text-primary)]">
                        <GripVertical className="h-3 w-3 text-[var(--color-text-tertiary)]" />
                        {t("blueprint.episodeN", { n: summary.index + 1 })}
                        {ending && (
                          <span className="ml-1 rounded bg-[var(--color-bg-tertiary)] px-1.5">
                            {t("blueprint.endingNode")}
                          </span>
                        )}
                      </span>
                      <span
                        className={`rounded px-1.5 text-[9px] font-semibold leading-[16px] ${
                          TONE_CHIP[status.tone]
                        }`}
                      >
                        {t(`blueprint.episodeStatus.${status.key}`)}
                      </span>
                    </div>
                    <h4
                      title={episodeTitle(summary, t)}
                      className="mb-1 line-clamp-2 h-9 shrink-0 text-[13px] font-semibold leading-[18px] text-[var(--color-text-primary)]"
                    >
                      {episodeTitle(summary, t)}
                    </h4>
                    <p
                      title={summary.synopsis}
                      className="line-clamp-2 h-8 shrink-0 text-[11px] leading-4 text-[var(--color-text-secondary)]"
                    >
                      {summary.synopsis || t("blueprint.noSynopsis")}
                    </p>
                    <div className="mt-auto w-full border-t border-dashed border-[var(--color-border)] pt-2">
                      <div className="flex items-center justify-between text-[10px] text-[var(--color-text-tertiary)]">
                        <span>
                          {t("blueprint.nodeMeta", {
                            ready: summary.videoReady,
                            total: summary.videoTotal,
                          })}
                        </span>
                        <span className="tabular-nums">
                          {formatDuration(summary.durationSeconds)}
                        </span>
                      </div>
                      <div className="mt-2 flex items-center gap-2">
                        <NodeActionPill
                          icon={<FileText className="h-3.5 w-3.5" />}
                          label={t("blueprint.viewScript")}
                          onClick={() => openScript(summary.timelineId)}
                        />
                        <NodeActionPill
                          icon={<SquarePen className="h-3.5 w-3.5" />}
                          label={t("blueprint.editTimeline")}
                          onClick={() => onOpenTimeline(summary.timelineId)}
                        />
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
      {!structurePending && focused && (
        <section
          aria-label={t("blueprint.graph.connections")}
          className="max-h-[32%] shrink-0 overflow-auto border-t border-[var(--color-border)] bg-[var(--color-bg-primary)] px-4 py-3"
        >
          <div className="mb-2 flex items-start justify-between gap-3">
            <h4 className="text-xs font-semibold text-[var(--color-text-primary)]">
              {nodeName(focused.timelineId)}
            </h4>
            <button
              type="button"
              className="shrink-0 text-[var(--color-text-secondary)]"
              aria-label={t("blueprint.graph.clearFocus")}
              onClick={() => {
                setLocalFocus(null);
                setHoveredEdge(null);
              }}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="space-y-1">
            {outgoing.length ? (
              outgoing.map((route, index) => (
                <button
                  key={route.edge.edge_id}
                  type="button"
                  data-graph-connection={route.edge.edge_id}
                  className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs text-[var(--color-text-primary)] hover:bg-[var(--color-bg-secondary)]"
                  onMouseEnter={() => setHoveredEdge(route.edge.edge_id)}
                  onMouseLeave={() => setHoveredEdge(null)}
                  onFocus={() => setHoveredEdge(route.edge.edge_id)}
                  onBlur={() => setHoveredEdge(null)}
                  onClick={() => jump(route.edge.target_timeline_id)}
                >
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-[var(--color-border-strong)] text-[10px]">
                    {index + 1}
                  </span>
                  <span className="shrink-0 font-medium">
                    {route.edge.label || t("blueprint.graph.continue")}
                  </span>
                  <ArrowRight className="h-3.5 w-3.5 shrink-0" />
                  <span>{nodeName(route.edge.target_timeline_id)}</span>
                </button>
              ))
            ) : (
              <p className="text-xs text-[var(--color-text-secondary)]">
                {t("blueprint.graph.noOutgoing")}
              </p>
            )}
          </div>
          {incoming.length > 0 && (
            <details className="mt-2 border-t border-[var(--color-border)] pt-2 text-xs text-[var(--color-text-secondary)]">
              <summary className="cursor-pointer">
                {t("blueprint.graph.incoming", { count: incoming.length })}
              </summary>
              {incoming.map((route) => (
                <button
                  key={route.edge.edge_id}
                  type="button"
                  className="mt-1 block w-full rounded-md px-2 py-1.5 text-left hover:bg-[var(--color-bg-secondary)]"
                  onMouseEnter={() => setHoveredEdge(route.edge.edge_id)}
                  onMouseLeave={() => setHoveredEdge(null)}
                  onClick={() => jump(route.edge.source_timeline_id)}
                >
                  {nodeName(route.edge.source_timeline_id)} ·{" "}
                  {route.edge.label || t("blueprint.graph.continue")}
                </button>
              ))}
            </details>
          )}
        </section>
      )}
    </div>
  );
  // Escape the workspace animation transform so expanded mode uses the full app.
  return expanded ? createPortal(content, document.body) : content;
}

export default function BlueprintStructureArea(props: StructureAreaProps) {
  if (props.shape === "branching")
    return <GraphCanvas key={props.project.project_id} {...props} />;
  if (props.shape === "linear") return <EpisodeList {...props} />;
  return <SingleBoard {...props} />;
}
