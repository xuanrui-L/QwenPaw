import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { message } from "antd";
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  Magnet,
  Pause,
  Play,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type {
  ProjectDocument,
  TaskView,
  TimelineDocument,
  TimelineSpanDocument,
} from "@/contracts/creator";
import { getArtifactVersionMediaUrl } from "@/api/creator";
import {
  elementsAtTick,
  groupDisplayTracks,
  resolveTimelineRender,
} from "@/selectors/timelineElementSelectors";
import type { ElementPlaybackStatus } from "@/selectors/elementPlaybackSelectors";
import { resolveElementPlayback } from "@/selectors/elementPlaybackSelectors";
import TimelineLivePreview from "@/components/timeline/TimelineLivePreview";
import TimelineTracks from "@/components/timeline/TimelineTracks";
import { buildSpanOperations, type SpanChange } from "@/lib/timelineEditing";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorEditBufferStore } from "@/store/creatorEditBufferStore";
import { useAgentWorkingState } from "@/selectors/agentWorkingSelectors";

interface TimelineCanvasProps {
  project: ProjectDocument;
  timeline: TimelineDocument;
  durationTick: number;
  playheadTick: number;
  selectedElementId: string | null;
  activeElementIds: string[];
  previewOpen: boolean;
  tasks: TaskView[];
  onPreviewOpenChange: (open: boolean) => void;
  onPlayheadChange: (tick: number) => void;
  onSelectElement: (elementId: string) => void;
  onActiveElementIdsChange: (ids: string[]) => void;
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_STEP = 0.5;

function seconds(tick: number, ticksPerSecond: number, digits = 1): string {
  return (tick / ticksPerSecond).toFixed(digits).replace(/\.0$/, "");
}

/** NLE-style timecode `MM:SS.t`, matching the reference transport display. */
function timecode(tick: number, ticksPerSecond: number): string {
  const totalSeconds = Math.max(0, tick / Math.max(1, ticksPerSecond));
  const minutes = Math.floor(totalSeconds / 60);
  const rest = totalSeconds - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${
    rest < 10 ? "0" : ""
  }${rest.toFixed(1)}`;
}

function percent(tick: number, durationTick: number): number {
  return durationTick > 0
    ? Math.min(100, Math.max(0, (tick / durationTick) * 100))
    : 0;
}

export default function TimelineCanvas({
  project,
  timeline,
  durationTick,
  playheadTick,
  selectedElementId,
  activeElementIds,
  previewOpen,
  tasks,
  onPreviewOpenChange,
  onPlayheadChange,
  onSelectElement,
  onActiveElementIdsChange,
}: TimelineCanvasProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [muted, setMuted] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [finalFrameReady, setFinalFrameReady] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [dragOverrides, setDragOverrides] = useState<Map<
    string,
    TimelineSpanDocument
  > | null>(null);

  const agentWorking = useAgentWorkingState();
  const patch = useProjectSnapshotStore((state) => state.patch);
  const pollOnce = useProjectSnapshotStore((state) => state.pollOnce);
  const commitChain = useRef<Promise<void>>(Promise.resolve());
  const videoRef = useRef<HTMLVideoElement>(null);
  const previewScrubberRef = useRef<HTMLDivElement>(null);
  const previewScrub = useRef<{
    pointerId: number;
    resumeAfterScrub: boolean;
  } | null>(null);

  // Effective timeline: authoritative document plus in-flight drag overrides,
  // so both the track blocks and the live preview follow the pointer without
  // waiting for the patch round-trip.
  const effectiveTimeline = useMemo(() => {
    if (!dragOverrides?.size) return timeline;
    const elements = { ...timeline.elements_by_id };
    dragOverrides.forEach((span, elementId) => {
      const element = elements[elementId];
      if (element) elements[elementId] = { ...element, span };
    });
    return { ...timeline, elements_by_id: elements };
  }, [timeline, dragOverrides]);

  const { tracks } = useMemo(
    () => groupDisplayTracks(effectiveTimeline),
    [effectiveTimeline],
  );
  const totalLanes = tracks.reduce((sum, track) => sum + track.lanes.length, 0);
  const scrollable = totalLanes > 4;
  const active = useMemo(
    () => elementsAtTick(effectiveTimeline, playheadTick),
    [playheadTick, effectiveTimeline],
  );
  const renderOutput = useMemo(
    () => resolveTimelineRender(project, timeline),
    [project, timeline],
  );
  const renderedVersion = renderOutput?.selected ?? null;
  const renderUrl = renderedVersion
    ? getArtifactVersionMediaUrl(renderedVersion.version_id)
    : null;
  const pendingAffectedElementIds = useMemo(() => {
    const raw = renderedVersion?.metadata.pendingAffectedElementIds;
    return new Set(
      Array.isArray(raw)
        ? raw.filter(
            (value): value is string =>
              typeof value === "string" && value.length > 0,
          )
        : [],
    );
  }, [renderedVersion]);
  // Locally recorded edit ranges (union of pre/post spans of edited elements):
  // old final-render frames inside them are wrong even when the element has
  // since moved away from the playhead's current tick.
  const localAffectedRanges = useCreatorEditBufferStore((state) =>
    state.projectId === project.project_id
      ? state.affectedRangesByTimeline[timeline.timeline_id]
      : undefined,
  );
  const clearAffectedRanges = useCreatorEditBufferStore(
    (state) => state.clearAffectedRanges,
  );
  const playheadLocallyAffected = (localAffectedRanges ?? []).some(
    (range) => playheadTick >= range.startTick && playheadTick < range.endTick,
  );
  useEffect(() => {
    if (renderedVersion && !renderedVersion.stale) {
      clearAffectedRanges(
        timeline.timeline_id,
        renderedVersion.based_on_generation,
      );
    }
  }, [clearAffectedRanges, renderedVersion, timeline.timeline_id]);
  const staleFrameIsUnaffected =
    Boolean(renderedVersion?.stale) &&
    pendingAffectedElementIds.size > 0 &&
    !playheadLocallyAffected &&
    active.every(
      (element) => !pendingAffectedElementIds.has(element.element_id),
    );
  // A fresh final render always wins. When the final render is only stale
  // because of a local Element edit, old final-render frames outside that
  // Element's time range are still complete and correct; the affected range
  // switches to live assembly instead — never pass off pre-edit footage as the
  // new result.
  const previewMode =
    renderUrl && (!renderedVersion?.stale || staleFrameIsUnaffected)
      ? "final"
      : "live";
  // Readiness states for the live-assembly preview; also drive the
  // generating/failed styling of track blocks.
  const playbackStates = useMemo(() => {
    const states = new Map<string, ElementPlaybackStatus>();
    Object.values(effectiveTimeline.elements_by_id).forEach((element) => {
      states.set(
        element.element_id,
        resolveElementPlayback(project, effectiveTimeline, element, tasks)
          .status,
      );
    });
    return states;
  }, [project, tasks, effectiveTimeline]);
  const timelineDuration = Math.max(1, durationTick);
  const finalVideo = videoRef.current;
  const finalPreviewReady =
    finalFrameReady &&
    Boolean(
      finalVideo &&
        !finalVideo.error &&
        finalVideo.readyState >= 2 &&
        !finalVideo.seeking &&
        (playing ||
          Math.abs(
            finalVideo.currentTime - playheadTick / timeline.ticks_per_second,
          ) <= 0.35),
    );

  // ------------------------------------------------------------------
  // Direct write-back: span changes coming from the track surface commit to
  // project.json immediately (default-apply), serialized so consecutive drags
  // never race each other's base generation.
  // ------------------------------------------------------------------
  const commitSpans = (changes: SpanChange[]) => {
    commitChain.current = commitChain.current.then(async () => {
      const snapshot = useProjectSnapshotStore.getState();
      const latestTimeline =
        snapshot.projectId === project.project_id
          ? snapshot.project?.timelines.items[timeline.timeline_id]
          : null;
      const clearCommitted = () =>
        setDragOverrides((previous) => {
          if (!previous) return null;
          const next = new Map(previous);
          changes.forEach((change) => next.delete(change.elementId));
          return next.size ? next : null;
        });
      if (!latestTimeline) {
        clearCommitted();
        return;
      }
      const operations = buildSpanOperations(
        latestTimeline,
        timeline.timeline_id,
        changes,
      );
      if (!operations.length) {
        clearCommitted();
        return;
      }
      try {
        const response = await patch(project.project_id, operations);
        clearCommitted();
        if (response.editImpact?.regenerationRequired) {
          message.success("时间调整已应用；相关生成结果已标记为需要重新生成");
        } else if (response.editImpact?.renderTimelineIds.length) {
          message.success("时间调整已应用；实时预览已更新，成片将重新合成");
        } else {
          message.success("时间调整已应用");
        }
      } catch (error) {
        clearCommitted();
        message.error(`应用时间调整失败：${(error as Error).message}`);
        void pollOnce(project.project_id);
      }
    });
  };

  const seekPreviewToPlayhead = (video: HTMLVideoElement) => {
    if (!Number.isFinite(video.duration)) return;
    const target = playheadTick / timeline.ticks_per_second;
    if (Math.abs(video.currentTime - target) > 0.35) {
      setFinalFrameReady(false);
      video.currentTime = Math.min(video.duration || target, target);
    } else if (video.readyState >= 2 && !video.seeking) {
      setFinalFrameReady(true);
    }
  };

  useEffect(() => setFinalFrameReady(false), [renderUrl]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    seekPreviewToPlayhead(video);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playheadTick, previewOpen, renderUrl, timeline.ticks_per_second]);

  useEffect(() => {
    if (!previewOpen || previewMode !== "final" || !renderUrl) return;
    const synchronizeReadyState = () => {
      const video = videoRef.current;
      const ready = Boolean(
        video &&
          !video.error &&
          video.readyState >= 2 &&
          !video.seeking &&
          (playing ||
            Math.abs(
              video.currentTime - playheadTick / timeline.ticks_per_second,
            ) <= 0.35),
      );
      setFinalFrameReady((current) => (current === ready ? current : ready));
    };
    // Some browsers recover from waiting/stalled with readyState=4 without
    // emitting another canplay event.  Reconcile against the media element so
    // a complete frame cannot remain hidden behind a stale loading cover.
    synchronizeReadyState();
    const timer = window.setInterval(synchronizeReadyState, 250);
    return () => window.clearInterval(timer);
  }, [
    playheadTick,
    playing,
    previewMode,
    previewOpen,
    renderUrl,
    timeline.ticks_per_second,
  ]);

  useEffect(() => {
    if (!previewOpen) setPlaying(false);
    else if (previewMode === "final" && !renderUrl) setPlaying(false);
  }, [previewOpen, previewMode, renderUrl]);

  const previewTickAt = (clientX: number): number => {
    const rect = previewScrubberRef.current?.getBoundingClientRect();
    if (!rect) return playheadTick;
    const fraction = Math.min(
      1,
      Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)),
    );
    return Math.round(fraction * timelineDuration);
  };

  const pausePreviewForScrub = () => {
    if (!playing) return;
    if (previewMode === "live") setPlaying(false);
    else videoRef.current?.pause();
  };

  const resumePreviewAfterScrub = () => {
    if (previewMode === "live") {
      setPlaying(true);
      return;
    }
    void videoRef.current?.play();
  };

  const beginPreviewScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    previewScrub.current = {
      pointerId: event.pointerId,
      resumeAfterScrub: playing,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    pausePreviewForScrub();
    onPlayheadChange(previewTickAt(event.clientX));
  };

  const movePreviewScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (previewScrub.current?.pointerId !== event.pointerId) return;
    event.preventDefault();
    onPlayheadChange(previewTickAt(event.clientX));
  };

  const finishPreviewScrub = (
    event: ReactPointerEvent<HTMLDivElement>,
    commitPointerPosition: boolean,
  ) => {
    const current = previewScrub.current;
    if (!current || current.pointerId !== event.pointerId) return;
    event.preventDefault();
    if (commitPointerPosition) {
      onPlayheadChange(previewTickAt(event.clientX));
    }
    previewScrub.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (current.resumeAfterScrub) resumePreviewAfterScrub();
  };

  const movePreviewScrubberByKeyboard = (
    event: React.KeyboardEvent<HTMLDivElement>,
  ) => {
    const oneSecond = Math.max(1, timeline.ticks_per_second);
    const fiveSeconds = oneSecond * 5;
    let nextTick: number | null = null;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowDown":
        nextTick = playheadTick - oneSecond;
        break;
      case "ArrowRight":
      case "ArrowUp":
        nextTick = playheadTick + oneSecond;
        break;
      case "PageDown":
        nextTick = playheadTick - fiveSeconds;
        break;
      case "PageUp":
        nextTick = playheadTick + fiveSeconds;
        break;
      case "Home":
        nextTick = 0;
        break;
      case "End":
        nextTick = timelineDuration;
        break;
      default:
        return;
    }
    event.preventDefault();
    onPlayheadChange(Math.min(timelineDuration, Math.max(0, nextTick)));
  };

  const adjustZoom = (delta: number) => {
    setZoom((value) =>
      Math.min(
        ZOOM_MAX,
        Math.max(ZOOM_MIN, Number((value + delta).toFixed(2))),
      ),
    );
  };
  const changeZoom = (next: number) => {
    setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next)));
  };

  const togglePlayback = () => {
    if (!previewOpen) {
      onPreviewOpenChange(true);
      if (previewMode === "live") setPlaying(true);
      return;
    }
    if (previewMode === "live") {
      if (!playing && playheadTick >= timelineDuration) onPlayheadChange(0);
      setPlaying((value) => !value);
      return;
    }
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) void video.play();
    else video.pause();
  };
  const togglePlaybackRef = useRef(togglePlayback);
  togglePlaybackRef.current = togglePlayback;
  const seekByRef = useRef((deltaTick: number) => {
    onPlayheadChange(
      Math.min(timelineDuration, Math.max(0, playheadTick + deltaTick)),
    );
  });
  seekByRef.current = (deltaTick: number) => {
    onPlayheadChange(
      Math.min(timelineDuration, Math.max(0, playheadTick + deltaTick)),
    );
  };

  // NLE keyboard shortcuts: Space play/pause, ←/→ ±1s (Shift ±0.1s),
  // Home/End. Ignored while typing or when another control owns the key.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target?.closest(
          "input, textarea, select, [contenteditable='true'], [role='slider']",
        )
      )
        return;
      const tps = Math.max(1, timeline.ticks_per_second);
      if (event.key === " " || event.code === "Space") {
        if (target?.closest("button, a")) return;
        event.preventDefault();
        togglePlaybackRef.current();
        return;
      }
      const stepTick = Math.max(1, Math.round(event.shiftKey ? tps / 10 : tps));
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        seekByRef.current(-stepTick);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        seekByRef.current(stepTick);
      } else if (event.key === "Home") {
        event.preventDefault();
        seekByRef.current(Number.NEGATIVE_INFINITY);
      } else if (event.key === "End") {
        event.preventDefault();
        seekByRef.current(Number.POSITIVE_INFINITY);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [timeline.ticks_per_second]);

  return (
    <section
      data-timeline-panel
      className={`relative mx-4 mt-3 shrink-0 overflow-visible rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] shadow-sm ${
        previewOpen ? "flex max-h-[66vh] flex-col" : ""
      }`}
    >
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-2 border-b border-[var(--color-border)] px-3 py-1.5 text-[11px] text-[var(--color-text-tertiary)]">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <b className="text-[var(--color-text-primary)]">时间轴</b>
          <span className="rounded-full bg-[var(--color-accent-soft)] px-2 py-0.5 font-semibold text-[var(--color-accent)]">
            {seconds(playheadTick, timeline.ticks_per_second)}s · 该时刻有
            {activeElementIds.length}项内容
          </span>
          <span
            className={`rounded-full bg-[var(--color-bg-secondary)] px-2 py-0.5 ${
              scrollable ? "ring-1 ring-[var(--color-border)]" : ""
            }`}
          >
            {tracks.length} 轨{scrollable ? " · 可上下滚动" : ""}
          </span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 pr-3">
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
            onClick={() => setCollapsed((value) => !value)}
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
          {previewMode === "live" ? (
            <TimelineLivePreview
              project={project}
              timeline={effectiveTimeline}
              durationTick={timelineDuration}
              playheadTick={playheadTick}
              playing={playing}
              muted={muted}
              tasks={tasks}
              onPlayheadChange={onPlayheadChange}
              onPlayingChange={setPlaying}
            />
          ) : renderUrl ? (
            <video
              ref={videoRef}
              src={renderUrl}
              className="h-full w-full bg-black object-contain"
              muted={muted}
              playsInline
              preload="auto"
              onLoadedMetadata={(event) =>
                seekPreviewToPlayhead(event.currentTarget)
              }
              onLoadedData={(event) =>
                seekPreviewToPlayhead(event.currentTarget)
              }
              onCanPlay={(event) => {
                if (!event.currentTarget.seeking) setFinalFrameReady(true);
              }}
              onSeeking={() => setFinalFrameReady(false)}
              onSeeked={() => setFinalFrameReady(true)}
              onWaiting={() => setFinalFrameReady(false)}
              onStalled={() => setFinalFrameReady(false)}
              onError={() => setFinalFrameReady(false)}
              onPlay={() => {
                setFinalFrameReady(true);
                setPlaying(true);
              }}
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
          {previewMode === "final" && renderUrl && !finalPreviewReady && (
            <div
              data-final-preview-incomplete
              role="status"
              aria-live="polite"
              className="absolute inset-0 z-[5] flex flex-col items-center justify-center gap-2 bg-[radial-gradient(circle_at_center,#2b2521_0,#161210_62%,#0d0b0a_100%)] px-6 text-center"
            >
              <Loader2 className="h-7 w-7 animate-spin text-white/75" />
              <span className="text-sm font-semibold text-white/90">
                正在定位画面
              </span>
              <span className="text-xs leading-5 text-white/60">
                成片已渲染完成，正在加载该时间点的画面，无需重新渲染。
              </span>
            </div>
          )}
          <div
            data-preview-source-chip
            className="absolute left-3 top-3 z-10 flex items-center gap-1.5 rounded-full border border-white/20 bg-black/45 px-2.5 py-1 text-[11px] font-semibold text-white/85 backdrop-blur"
            title={
              previewMode === "final"
                ? renderedVersion?.stale
                  ? "当前时间点未受本次修改影响，显示上一版已完成成片"
                  : "正在播放已合成的成片"
                : "实时拼装与成片同口径渲染；成片合成后自动切换"
            }
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                previewMode === "final"
                  ? "bg-[var(--color-success)]"
                  : "animate-pulse bg-[var(--color-accent)]"
              }`}
            />
            {previewMode === "final"
              ? renderedVersion?.stale
                ? "未受影响 · 已完成画面"
                : "成片"
              : renderedVersion?.stale
              ? "内容已更新 · 实时预览"
              : "实时预览"}
          </div>
        </div>
      )}

      {/* Transport bar mirrors the reference design: playback controls and
          the shared progress in the center, snapping/zoom on the right. */}
      <div
        data-timeline-transport
        className="flex min-h-[38px] flex-wrap items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-secondary)]/45 px-2.5 py-1 text-[11px]"
      >
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <button
            type="button"
            onClick={() => {
              onPlayheadChange(
                Math.max(0, playheadTick - timeline.ticks_per_second),
              );
            }}
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]"
            aria-label="后退 1 秒"
          >
            <SkipBack className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={togglePlayback}
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-[var(--color-accent)] text-white"
            aria-label="播放或暂停预览"
          >
            {playing ? (
              <Pause className="h-3.5 w-3.5" />
            ) : (
              <Play className="h-3.5 w-3.5 fill-current" />
            )}
          </button>
          <button
            type="button"
            onClick={() => {
              onPlayheadChange(
                Math.min(
                  timelineDuration,
                  playheadTick + timeline.ticks_per_second,
                ),
              );
            }}
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]"
            aria-label="前进 1 秒"
          >
            <SkipForward className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setMuted((value) => !value)}
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]"
            aria-label={muted ? "取消静音" : "静音"}
          >
            {muted ? (
              <VolumeX className="h-3.5 w-3.5" />
            ) : (
              <Volume2 className="h-3.5 w-3.5" />
            )}
          </button>
          <div
            ref={previewScrubberRef}
            data-preview-scrubber
            role="slider"
            tabIndex={0}
            aria-label="拖动预览时间轴"
            aria-valuemin={0}
            aria-valuemax={timelineDuration}
            aria-valuenow={Math.min(playheadTick, timelineDuration)}
            aria-valuetext={`${seconds(
              playheadTick,
              timeline.ticks_per_second,
            )} 秒`}
            onPointerDown={beginPreviewScrub}
            onPointerMove={movePreviewScrub}
            onPointerUp={(event) => finishPreviewScrub(event, true)}
            onPointerCancel={(event) => finishPreviewScrub(event, false)}
            onKeyDown={movePreviewScrubberByKeyboard}
            className="relative h-6 min-w-[80px] flex-1 cursor-pointer touch-none rounded-full focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
          >
            <span className="pointer-events-none absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 overflow-hidden rounded-full bg-[var(--color-border)]">
              <i
                className="block h-full rounded-full bg-[var(--color-accent)]"
                style={{
                  width: `${percent(playheadTick, timelineDuration)}%`,
                }}
              />
            </span>
            <i
              aria-hidden
              className="pointer-events-none absolute top-1/2 block h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-[var(--color-accent)] shadow-sm"
              style={{
                left: `${percent(playheadTick, timelineDuration)}%`,
              }}
            />
          </div>
          <span
            data-timeline-timecode
            className="shrink-0 font-mono text-[11px] tabular-nums text-[var(--color-text-secondary)]"
          >
            {timecode(playheadTick, timeline.ticks_per_second)} /{" "}
            {timecode(timelineDuration, timeline.ticks_per_second)}
          </span>
        </div>
        <label
          className="flex cursor-pointer items-center gap-1.5 text-[var(--color-text-secondary)]"
          title="拖动内容时自动吸附到相邻内容边缘与播放头"
        >
          <button
            type="button"
            data-timeline-snap-toggle
            aria-pressed={snapEnabled}
            onClick={() => setSnapEnabled((value) => !value)}
            className={`inline-flex h-7 items-center gap-1 rounded-[7px] px-2 font-semibold ${
              snapEnabled
                ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                : "text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]"
            }`}
          >
            <Magnet className="h-3.5 w-3.5" />
            吸附
          </button>
        </label>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            data-timeline-zoom-out
            disabled={zoom <= ZOOM_MIN}
            onClick={() => adjustZoom(-ZOOM_STEP)}
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-30"
            aria-label="缩小时间轴"
          >
            <ZoomOut className="h-3.5 w-3.5" />
          </button>
          <span
            data-timeline-zoom-value
            className="min-w-[34px] text-center text-[10px] font-semibold text-[var(--color-text-secondary)]"
          >
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            data-timeline-zoom-in
            disabled={zoom >= ZOOM_MAX}
            onClick={() => adjustZoom(ZOOM_STEP)}
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-30"
            aria-label="放大时间轴"
          >
            <ZoomIn className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <TimelineTracks
        project={project}
        timeline={effectiveTimeline}
        authorityTimeline={timeline}
        durationTick={timelineDuration}
        playheadTick={playheadTick}
        zoom={zoom}
        snapEnabled={snapEnabled}
        collapsed={collapsed}
        previewOpen={previewOpen}
        editable
        selectedElementId={selectedElementId}
        playbackStates={playbackStates}
        agentWorking={agentWorking.working}
        onPlayheadChange={onPlayheadChange}
        onSelectElement={onSelectElement}
        onActiveElementIdsChange={onActiveElementIdsChange}
        onDragOverridesChange={setDragOverrides}
        onCommitSpans={commitSpans}
        onZoomChange={changeZoom}
      />
    </section>
  );
}
