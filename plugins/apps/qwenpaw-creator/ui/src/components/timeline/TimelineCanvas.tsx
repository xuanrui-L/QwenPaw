import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  ChevronDown,
  ChevronUp,
  MessageSquarePlus,
  Pause,
  Play,
  Volume2,
  VolumeX,
} from "lucide-react";
import type {
  ProjectDocument,
  TimelineDocument,
  TimelineElementDocument,
} from "@/contracts/creator";
import { getArtifactVersionMediaUrl } from "@/api/creator";
import {
  ELEMENT_TYPE_META,
  elementsAtTick,
  elementsOverlappingRange,
  packDisplayLanes,
  resolveTimelineRender,
} from "@/selectors/timelineElementSelectors";
import { projectJsonPointer } from "@/lib/projectJsonPointer";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useAgentWorkingState } from "@/selectors/agentWorkingSelectors";

interface TimelineSelection {
  startTick: number;
  endTick: number;
  kind: "point" | "range";
}

interface TimelineCanvasProps {
  project: ProjectDocument;
  timeline: TimelineDocument;
  durationTick: number;
  playheadTick: number;
  selectedElementId: string | null;
  previewOpen: boolean;
  onPreviewOpenChange: (open: boolean) => void;
  onPlayheadChange: (tick: number) => void;
  onSelectElement: (elementId: string) => void;
}

const LABEL_WIDTH = 68;

function seconds(tick: number, ticksPerSecond: number, digits = 1): string {
  return (tick / ticksPerSecond).toFixed(digits).replace(/\.0$/, "");
}

function percent(tick: number, durationTick: number): number {
  return durationTick > 0
    ? Math.min(100, Math.max(0, (tick / durationTick) * 100))
    : 0;
}

function timelineRef(timeline: TimelineDocument): string {
  return `timeline:${timeline.timeline_id}`;
}

export default function TimelineCanvas({
  project,
  timeline,
  durationTick,
  playheadTick,
  selectedElementId,
  previewOpen,
  onPreviewOpenChange,
  onPlayheadChange,
  onSelectElement,
}: TimelineCanvasProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [muted, setMuted] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [selection, setSelection] = useState<TimelineSelection | null>(null);
  const agentWorking = useAgentWorkingState();
  const [toolbarPos, setToolbarPos] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const [pointCandidates, setPointCandidates] = useState<
    TimelineElementDocument[]
  >([]);
  const drag = useRef<{
    pointerId: number;
    startX: number;
    startTick: number;
    moved: boolean;
  } | null>(null);
  const chartRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const lanes = useMemo(() => packDisplayLanes(timeline), [timeline]);
  const active = useMemo(
    () => elementsAtTick(timeline, playheadTick),
    [playheadTick, timeline],
  );
  const renderOutput = useMemo(
    () => resolveTimelineRender(project, timeline),
    [project, timeline],
  );
  const renderedVersion = renderOutput?.selected ?? null;
  const renderUrl = renderedVersion
    ? getArtifactVersionMediaUrl(renderedVersion.version_id)
    : null;
  const scrollable = lanes.length > 4;
  const timelineDuration = Math.max(1, durationTick);

  const tickAt = (clientX: number): number => {
    const rect = chartRef.current?.getBoundingClientRect();
    if (!rect) return 0;
    const inner = Math.max(1, rect.width - LABEL_WIDTH);
    const x = Math.min(inner, Math.max(0, clientX - rect.left - LABEL_WIDTH));
    return Math.round((x / inner) * timelineDuration);
  };

  const clearSelection = () => {
    setSelection(null);
    setPointCandidates([]);
  };

  const beginSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (
      (event.target as HTMLElement).closest(
        "[data-element-block], [data-timeline-selection-toolbar]",
      )
    )
      return;
    const tick = tickAt(event.clientX);
    drag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startTick: tick,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setSelection(null);
    setPointCandidates([]);
  };

  const moveSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    if (Math.abs(event.clientX - current.startX) > 5) current.moved = true;
    if (!current.moved) return;
    const tick = tickAt(event.clientX);
    setSelection({
      kind: "range",
      startTick: Math.min(current.startTick, tick),
      endTick: Math.max(current.startTick, tick),
    });
  };

  const endSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    drag.current = null;
    const tick = tickAt(event.clientX);
    if (current.moved) {
      const startTick = Math.min(current.startTick, tick);
      const endTick = Math.max(current.startTick, tick);
      setSelection(
        endTick > startTick
          ? { kind: "range", startTick, endTick }
          : { kind: "point", startTick, endTick: startTick },
      );
      setPointCandidates([]);
      return;
    }
    onPlayheadChange(tick);
    const candidates = elementsAtTick(timeline, tick);
    setSelection({ kind: "point", startTick: tick, endTick: tick });
    setPointCandidates(collapsed ? candidates : []);
  };

  const addSelectionToConversation = () => {
    if (!selection) return;
    const selectedElements =
      selection.kind === "point"
        ? elementsAtTick(timeline, selection.startTick)
        : elementsOverlappingRange(
            timeline,
            selection.startTick,
            selection.endTick,
          );
    const isPoint = selection.kind === "point";
    const startText = seconds(selection.startTick, timeline.ticks_per_second);
    const endText = seconds(selection.endTick, timeline.ticks_per_second);
    const attachment = {
      kind: isPoint ? ("timeline_point" as const) : ("timeline_range" as const),
      text: isPoint
        ? `${startText}s · ${selectedElements.length} 项同时出现的内容`
        : `${startText}s – ${endText}s · ${selectedElements.length} 项时间线内容`,
      ref: timelineRef(timeline),
      field: isPoint
        ? `${timelineRef(timeline)}@${selection.startTick}`
        : `${timelineRef(timeline)}@[${selection.startTick},${
            selection.endTick
          })`,
      path: projectJsonPointer("timelines", "items", timeline.timeline_id),
      start: selection.startTick,
      end: selection.endTick,
      label: isPoint ? "时间点" : "时间区间",
      timelineId: timeline.timeline_id,
      startTick: selection.startTick,
      endTick: selection.endTick,
      elementIds: selectedElements.map((element) => element.element_id),
    };
    useAgentDockUiStore.getState().setSelection(attachment);
    useCreatorInteractionStore.getState().setSelection(attachment);
    clearSelection();
  };

  useEffect(() => {
    const close = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (
        chartRef.current?.contains(target) ||
        target.closest("[data-timeline-point-candidates]")
      )
        return;
      clearSelection();
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  // 让成片预览始终对齐播放头；预览刚打开或元数据尚未加载时也要补齐一次定位
  const seekPreviewToPlayhead = (video: HTMLVideoElement) => {
    if (!Number.isFinite(video.duration)) return;
    const target = playheadTick / timeline.ticks_per_second;
    if (Math.abs(video.currentTime - target) > 0.35) {
      video.currentTime = Math.min(video.duration || target, target);
    }
  };

  useEffect(() => {
    const video = videoRef.current;
    if (!video || document.activeElement === video) return;
    seekPreviewToPlayhead(video);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playheadTick, previewOpen, renderUrl, timeline.ticks_per_second]);

  useEffect(() => {
    if (!previewOpen || !renderUrl) setPlaying(false);
  }, [previewOpen, renderUrl]);

  useLayoutEffect(() => {
    if (!selection) {
      setToolbarPos(null);
      return;
    }
    const update = () => {
      const rect = chartRef.current?.getBoundingClientRect();
      const bar = toolbarRef.current;
      if (!rect || !bar) return;
      const width = bar.offsetWidth || 116;
      const height = bar.offsetHeight || 32;
      const centerTick =
        selection.kind === "point"
          ? selection.startTick
          : (selection.startTick + selection.endTick) / 2;
      const inner = Math.max(1, rect.width - LABEL_WIDTH - 24);
      const x =
        rect.left +
        LABEL_WIDTH +
        (inner * percent(centerTick, timelineDuration)) / 100;
      const left = Math.min(
        Math.max(x - width / 2, 8),
        Math.max(8, window.innerWidth - width - 8),
      );
      // 悬浮在时间轴图表上方，不遮挡刻度与轨道；上方放不下时退回刻度下方
      let top = rect.top - height - 6;
      if (top < 8) top = rect.top + 30;
      setToolbarPos({ left, top });
    };
    update();
    window.addEventListener("resize", update);
    document.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      document.removeEventListener("scroll", update, true);
    };
  }, [selection, timelineDuration]);

  const rulerTicks = Array.from({ length: 7 }, (_, index) =>
    Math.round((timelineDuration * index) / 6),
  );

  return (
    <section
      data-timeline-panel
      className={`mx-4 mt-3 shrink-0 overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] shadow-sm ${
        previewOpen ? "flex max-h-[66vh] flex-col" : ""
      }`}
    >
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-2 border-b border-[var(--color-border)] px-3 py-1.5 text-[11px] text-[var(--color-text-tertiary)]">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <b className="text-[var(--color-text-primary)]">时间轴</b>
          <span className="rounded-full bg-[var(--color-accent-soft)] px-2 py-0.5 font-semibold text-[var(--color-accent)]">
            {seconds(playheadTick, timeline.ticks_per_second)}s · 该时刻有
            {active.length}项内容
          </span>
          <span
            className={`rounded-full bg-[var(--color-bg-secondary)] px-2 py-0.5 ${
              scrollable ? "ring-1 ring-[var(--color-border)]" : ""
            }`}
          >
            {lanes.length} 层{scrollable ? " · 可上下滚动" : ""}
          </span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <div className="hidden flex-wrap items-center gap-2 xl:flex">
            {Object.entries(ELEMENT_TYPE_META).map(
              ([type, meta]) =>
                Object.values(timeline.elements_by_id).some(
                  (element) => element.creation.type === type,
                ) && (
                  <span key={type} className="whitespace-nowrap">
                    <i
                      className="mr-1 inline-block h-2 w-2 rounded-sm"
                      style={{ background: meta.color }}
                    />
                    {meta.label}
                  </span>
                ),
            )}
          </div>
          <button
            type="button"
            onClick={() => onPreviewOpenChange(!previewOpen)}
            className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 font-semibold ${
              previewOpen
                ? "border-[var(--color-accent)]/40 bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                : "border-[var(--color-border)] text-[var(--color-text-secondary)]"
            }`}
          >
            {previewOpen ? (
              <ChevronUp className="h-3 w-3" />
            ) : (
              <ChevronDown className="h-3 w-3" />
            )}
            {previewOpen ? "收起预览" : "视频预览"}
          </button>
          <button
            type="button"
            onClick={() => {
              setCollapsed((value) => !value);
              clearSelection();
            }}
            className="rounded-md border border-[var(--color-border)] px-2 py-1 font-semibold text-[var(--color-text-secondary)]"
          >
            {collapsed ? "展开时间轴" : "收起时间轴"}
          </button>
        </div>
      </div>

      {previewOpen && (
        <div
          data-timeline-video-preview
          className="relative flex h-[clamp(220px,40vh,400px)] min-h-0 w-full shrink items-center justify-center overflow-hidden bg-[#12100f]"
        >
          {renderUrl ? (
            <video
              ref={videoRef}
              src={renderUrl}
              className="h-full w-full bg-black object-contain"
              muted={muted}
              playsInline
              preload="metadata"
              onLoadedMetadata={(event) =>
                seekPreviewToPlayhead(event.currentTarget)
              }
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onTimeUpdate={(event) =>
                onPlayheadChange(
                  Math.round(
                    event.currentTarget.currentTime * timeline.ticks_per_second,
                  ),
                )
              }
              onEnded={(event) => {
                setPlaying(false);
                event.currentTarget.pause();
              }}
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-[radial-gradient(circle_at_center,#28221e_0,#151210_64%,#0d0b0a_100%)] text-center text-sm text-white/58">
              <span>暂无成片预览</span>
            </div>
          )}
          <div className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-gradient-to-b from-transparent to-black/80 px-4 pb-3 pt-12">
            <button
              type="button"
              disabled={!videoRef.current && !renderUrl}
              onClick={() => {
                const video = videoRef.current;
                if (!video) return;
                if (video.paused) void video.play();
                else video.pause();
              }}
              className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--color-accent)] text-white disabled:opacity-40"
              aria-label="播放或暂停成片"
            >
              {playing ? (
                <Pause className="h-3.5 w-3.5" />
              ) : (
                <Play className="h-3.5 w-3.5 fill-current" />
              )}
            </button>
            <button
              type="button"
              onClick={() => setMuted((value) => !value)}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-white/25 bg-black/30 text-white"
              aria-label={muted ? "取消静音" : "静音"}
            >
              {muted ? (
                <VolumeX className="h-3.5 w-3.5" />
              ) : (
                <Volume2 className="h-3.5 w-3.5" />
              )}
            </button>
            <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/30">
              <i
                className="block h-full bg-[var(--color-accent)]"
                style={{ width: `${percent(playheadTick, timelineDuration)}%` }}
              />
            </div>
            <span className="font-mono text-[11px] text-white">
              {seconds(playheadTick, timeline.ticks_per_second)} /{" "}
              {seconds(timelineDuration, timeline.ticks_per_second)}s
            </span>
          </div>
          {renderedVersion?.stale && (
            <span className="absolute right-3 top-3 rounded-full bg-[var(--color-warning)] px-2 py-1 text-[10px] font-semibold text-white">
              上次成片 · 已过期
            </span>
          )}
        </div>
      )}

      <div
        ref={chartRef}
        data-timeline-chart
        className={`relative shrink-0 cursor-crosshair select-none px-3 pb-2 ${
          previewOpen ? "max-h-[190px] overflow-hidden" : ""
        }`}
        onPointerDown={beginSelection}
        onPointerMove={moveSelection}
        onPointerUp={endSelection}
        onPointerCancel={() => {
          drag.current = null;
        }}
      >
        <div className="relative ml-[68px] h-6 border-b border-[var(--color-border)]">
          {rulerTicks.map((tick) => (
            <span
              key={tick}
              className="absolute top-1 -translate-x-1/2 text-[10px] text-[var(--color-text-tertiary)]"
              style={{ left: `${percent(tick, timelineDuration)}%` }}
            >
              {seconds(tick, timeline.ticks_per_second, 0)}s
            </span>
          ))}
        </div>
        {collapsed ? (
          <div className="relative flex h-8 border-b border-[var(--color-border)]/65">
            <div className="flex w-[68px] shrink-0 items-center justify-center text-[10px] font-semibold text-[var(--color-text-tertiary)]">
              概览
            </div>
            <div
              className="relative min-w-0 flex-1"
              aria-label="紧凑时间轴概览；点击任意时刻查看同时出现的内容"
            >
              {Object.values(timeline.elements_by_id).map((element) => {
                const meta = ELEMENT_TYPE_META[element.creation.type];
                const left = percent(element.span.start_tick, timelineDuration);
                const width = Math.max(
                  0.7,
                  (element.span.duration_tick / timelineDuration) * 100,
                );
                return (
                  <i
                    key={element.element_id}
                    aria-hidden
                    className={`pointer-events-none absolute inset-y-2 rounded-sm ${
                      element.enabled ? "opacity-55" : "opacity-20"
                    }`}
                    style={{
                      left: `${left}%`,
                      width: `${Math.min(100 - left, width)}%`,
                      background: meta.color,
                    }}
                  />
                );
              })}
            </div>
          </div>
        ) : (
          <div
            className={
              scrollable
                ? `${
                    previewOpen ? "max-h-[84px]" : "max-h-[210px]"
                  } overflow-y-auto overscroll-contain [scrollbar-gutter:stable]`
                : ""
            }
          >
            {lanes.length === 0 ? (
              agentWorking.working ? (
                <div
                  data-timeline-working
                  className="flex h-14 flex-col items-center justify-center gap-2 text-xs text-[var(--color-text-secondary)]"
                >
                  <span className="flex items-center gap-1.5">
                    <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-[var(--color-warning)]" />
                    Agent 正在编排时间轴，内容生成后会自动出现
                  </span>
                  <div className="agent-working-shimmer h-1.5 w-3/5 rounded-full bg-[var(--color-bg-secondary)]" />
                </div>
              ) : (
                <div className="flex h-14 items-center justify-center text-xs text-[var(--color-text-tertiary)]">
                  时间轴中还没有内容
                </div>
              )
            ) : (
              lanes.map((lane) => (
                <div
                  key={lane.id}
                  className="relative flex h-[42px] border-b border-[var(--color-border)]/65 last:border-b-0"
                >
                  <div
                    title="选取整行"
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() =>
                      useCreatorInteractionStore
                        .getState()
                        .setActiveLaneElementIds(
                          lane.elements.map((element) => element.element_id),
                        )
                    }
                    className="flex w-[68px] shrink-0 items-center justify-center text-[10px] font-semibold text-[var(--color-text-tertiary)] hover:text-[12px] hover:font-bold"
                  >
                    {lane.id}
                  </div>
                  <div className="relative min-w-0 flex-1">
                    {lane.elements.map((element) => {
                      const meta = ELEMENT_TYPE_META[element.creation.type];
                      const left = percent(
                        element.span.start_tick,
                        timelineDuration,
                      );
                      const width = Math.max(
                        0.7,
                        (element.span.duration_tick / timelineDuration) * 100,
                      );
                      const selected = element.element_id === selectedElementId;
                      return (
                        <button
                          key={element.element_id}
                          type="button"
                          data-element-block={element.element_id}
                          title={`${element.label || "时间线内容"} · ${seconds(
                            element.span.start_tick,
                            timeline.ticks_per_second,
                          )}s – ${seconds(
                            element.span.start_tick +
                              element.span.duration_tick,
                            timeline.ticks_per_second,
                          )}s`}
                          onPointerDown={(event) => event.stopPropagation()}
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelectElement(element.element_id);
                          }}
                          className={`absolute top-1.5 flex h-[30px] min-w-3 flex-col justify-center overflow-hidden rounded-[7px] border px-2 text-left text-[10px] font-semibold shadow-sm transition ${
                            selected
                              ? "z-20 border-[var(--color-accent)] ring-2 ring-[var(--color-accent)]/20"
                              : "z-10"
                          } ${element.enabled ? "" : "opacity-45"}`}
                          style={{
                            left: `${left}%`,
                            width: `${Math.min(100 - left, width)}%`,
                            color: meta.color,
                            borderColor: selected
                              ? undefined
                              : `${meta.color}80`,
                            background: meta.soft,
                          }}
                        >
                          <span className="min-w-0 truncate">
                            {element.label || "时间线内容"}
                          </span>
                          <span className="truncate whitespace-nowrap text-[9px] font-medium opacity-75">
                            {seconds(
                              element.span.start_tick,
                              timeline.ticks_per_second,
                            )}
                            s –{" "}
                            {seconds(
                              element.span.start_tick +
                                element.span.duration_tick,
                              timeline.ticks_per_second,
                            )}
                            s
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        <div
          aria-hidden
          className="pointer-events-none absolute bottom-2 top-6 z-[22] w-px bg-[var(--color-accent)]"
          style={{
            left: `calc(${LABEL_WIDTH}px + (100% - ${LABEL_WIDTH + 24}px) * ${
              percent(playheadTick, timelineDuration) / 100
            })`,
          }}
        />
        {selection && (
          <>
            <div
              aria-hidden
              className={`pointer-events-none absolute bottom-2 top-6 z-[21] border border-[var(--color-accent)] ${
                selection.kind === "point"
                  ? "w-1 bg-[var(--color-accent)]/30"
                  : "bg-[var(--color-accent)]/15"
              }`}
              style={{
                left: `calc(${LABEL_WIDTH}px + (100% - ${
                  LABEL_WIDTH + 24
                }px) * ${
                  percent(selection.startTick, timelineDuration) / 100
                })`,
                width:
                  selection.kind === "range"
                    ? `calc((100% - ${LABEL_WIDTH + 24}px) * ${
                        (selection.endTick - selection.startTick) /
                        timelineDuration
                      })`
                    : undefined,
              }}
            />
            <div
              ref={toolbarRef}
              data-timeline-selection-toolbar
              className="rounded-lg border border-[var(--color-border)] bg-white p-0.5 shadow-lg"
              style={{
                position: "fixed",
                top: toolbarPos?.top ?? -9999,
                left: toolbarPos?.left ?? -9999,
                visibility: toolbarPos ? "visible" : "hidden",
                zIndex: 50,
              }}
            >
              <button
                type="button"
                onPointerDown={(event) => event.stopPropagation()}
                onClick={(event) => {
                  event.stopPropagation();
                  addSelectionToConversation();
                }}
                className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium hover:bg-[var(--color-accent-soft)] hover:text-[var(--color-accent)]"
              >
                <MessageSquarePlus className="h-3.5 w-3.5" />
                添加到对话
              </button>
            </div>
          </>
        )}
      </div>

      {collapsed && pointCandidates.length > 0 && (
        <div
          data-timeline-point-candidates
          className="flex flex-nowrap items-center gap-1.5 overflow-x-auto border-t border-[var(--color-border)] bg-[var(--color-bg-secondary)]/70 px-3 py-2 text-[11px]"
        >
          <span className="mr-1 shrink-0 text-[var(--color-text-tertiary)]">
            该时刻有 {pointCandidates.length} 项内容：
          </span>
          {pointCandidates.map((element) => {
            const meta = ELEMENT_TYPE_META[element.creation.type];
            return (
              <button
                key={element.element_id}
                type="button"
                onClick={() => onSelectElement(element.element_id)}
                className={`max-w-48 shrink-0 truncate rounded-full border px-2 py-0.5 font-medium ${
                  selectedElementId === element.element_id
                    ? "border-[var(--color-accent)] text-[var(--color-accent)]"
                    : "border-[var(--color-border)] text-[var(--color-text-secondary)]"
                }`}
                style={{ background: meta.soft }}
              >
                {element.label || element.element_id}
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
