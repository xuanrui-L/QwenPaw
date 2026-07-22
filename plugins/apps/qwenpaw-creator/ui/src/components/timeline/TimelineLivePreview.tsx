import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Clock3, ImageOff, Loader2 } from "lucide-react";
import type {
  ElementLocationDocument,
  ProjectDocument,
  TaskView,
  TimelineDocument,
} from "@/contracts/creator";
import type { ElementPlayback } from "@/selectors/elementPlaybackSelectors";
import {
  ELEMENT_PLAYBACK_STATUS_LABEL,
  playbackLayersInWindow,
} from "@/selectors/elementPlaybackSelectors";
import { ELEMENT_TYPE_META } from "@/selectors/timelineElementSelectors";
import {
  InterviewSummaryBox,
  PetOsBubble,
} from "@/components/timeline/OverlayCopyLayer";

interface TimelineLivePreviewProps {
  project: ProjectDocument;
  timeline: TimelineDocument;
  durationTick: number;
  playheadTick: number;
  playing: boolean;
  muted: boolean;
  tasks: TaskView[];
  onPlayheadChange: (tick: number) => void;
  onPlayingChange: (playing: boolean) => void;
}

/** 视频层与播放头允许的最大偏差（秒），超过则回拉。 */
const DRIFT_TOLERANCE_SECONDS = 0.3;

function aspectRatioStyle(aspectRatio: string): string {
  const match = /^(\d+(?:\.\d+)?)[:x](\d+(?:\.\d+)?)$/.exec(
    aspectRatio.trim(),
  );
  if (!match) return "16 / 9";
  return `${match[1]} / ${match[2]}`;
}

/** 归一化画布坐标 → 舞台内百分比定位（与 ElementDetail 的位置框同口径）。 */
function locationBoxStyle(
  location: ElementLocationDocument | null,
): React.CSSProperties {
  if (!location) {
    return { left: 0, top: 0, width: "100%", height: "100%" };
  }
  return {
    left: `${(location.x - location.anchor_x * location.width) * 100}%`,
    top: `${(location.y - location.anchor_y * location.height) * 100}%`,
    width: `${location.width * 100}%`,
    height: `${location.height * 100}%`,
    transform: location.rotation_degrees
      ? `rotate(${location.rotation_degrees}deg)`
      : undefined,
    opacity: location.opacity,
  };
}

function isFullFrame(location: ElementLocationDocument | null): boolean {
  if (!location) return true;
  return location.width >= 0.9 && location.height >= 0.9;
}

function mediaTargetSeconds(
  layer: ElementPlayback,
  playheadTick: number,
  ticksPerSecond: number,
): number {
  const media = layer.media!;
  const localSeconds =
    Math.max(0, playheadTick - layer.element.span.start_tick) /
    ticksPerSecond;
  let offset = localSeconds * media.playbackRate;
  const windowSeconds =
    media.sourceOutSeconds != null
      ? media.sourceOutSeconds - media.sourceInSeconds
      : media.durationSeconds;
  if (media.loop && windowSeconds && windowSeconds > 0) {
    offset %= windowSeconds;
  }
  return media.sourceInSeconds + offset;
}

function PlaceholderLayer({ layer }: { layer: ElementPlayback }) {
  const { element, status } = layer;
  const meta = ELEMENT_TYPE_META[element.creation.type];
  const label = ELEMENT_PLAYBACK_STATUS_LABEL[status];
  const fullFrame = isFullFrame(element.location);
  const StatusIcon =
    status === "generating"
      ? Loader2
      : status === "queued"
        ? Clock3
        : ImageOff;
  if (fullFrame) {
    return (
      <div
        data-live-placeholder={element.element_id}
        data-live-placeholder-state={status}
        className="absolute flex flex-col items-center justify-center gap-2 bg-[radial-gradient(circle_at_center,#2b2521_0,#161210_62%,#0d0b0a_100%)] text-center"
        style={locationBoxStyle(element.location)}
      >
        <StatusIcon
          className={`h-7 w-7 ${
            status === "generating" ? "animate-spin" : ""
          } ${status === "failed" ? "text-[var(--color-danger)]" : "text-white/70"}`}
        />
        <span className="max-w-[70%] truncate text-sm font-semibold text-white/85">
          {element.label || meta.label}
        </span>
        <span
          className="rounded-full px-2.5 py-0.5 text-[11px] font-semibold"
          style={{ color: meta.color, background: `${meta.color}26` }}
        >
          {meta.label} · {status === "generating" ? "画面生成中…" : label}
        </span>
        {status === "generating" && (
          <div className="agent-working-shimmer mt-1 h-1 w-32 rounded-full bg-white/15" />
        )}
      </div>
    );
  }
  return (
    <div
      data-live-placeholder={element.element_id}
      data-live-placeholder-state={status}
      className="absolute flex items-center justify-center rounded-lg border border-dashed border-white/45 bg-black/30"
      style={locationBoxStyle(element.location)}
    >
      <span className="inline-flex max-w-full items-center gap-1 truncate rounded-full bg-black/55 px-2 py-0.5 text-[10px] font-semibold text-white/90">
        <StatusIcon
          className={`h-3 w-3 shrink-0 ${
            status === "generating" ? "animate-spin" : ""
          }`}
        />
        {element.label || meta.label} · {label}
      </span>
    </div>
  );
}

function TextOverlayLayer({
  layer,
  stageWidth,
}: {
  layer: ElementPlayback;
  stageWidth: number;
}) {
  const { element } = layer;
  if (element.creation.type !== "overlay") return null;
  // 与成片合成器同款的确定性文案渲染，保证预览即成片效果。
  return (
    <div
      data-live-text-overlay={element.element_id}
      className="absolute"
      style={locationBoxStyle(element.location)}
    >
      {element.creation.overlay_kind === "pet_os" ? (
        <PetOsBubble
          text={element.creation.text}
          vibe={element.creation.vibe}
          stageWidth={stageWidth}
        />
      ) : (
        <InterviewSummaryBox text={element.creation.text} />
      )}
    </div>
  );
}

/**
 * 实时拼装预览：无需等待成片合成，按 element 的 span/z_index/location
 * 把已就绪的媒体直接分层播放；未就绪的层用占位符标出生成状态。
 */
export default function TimelineLivePreview({
  project,
  timeline,
  durationTick,
  playheadTick,
  playing,
  muted,
  tasks,
  onPlayheadChange,
  onPlayingChange,
}: TimelineLivePreviewProps) {
  const ticksPerSecond = timeline.ticks_per_second || 1;
  const mediaRefs = useRef(new Map<string, HTMLVideoElement>());
  const clock = useRef<{ baseTick: number; baseTime: number } | null>(null);
  const lastEmittedTick = useRef(playheadTick);
  const stageRef = useRef<HTMLDivElement>(null);
  const [stageWidth, setStageWidth] = useState(1280);

  useLayoutEffect(() => {
    const node = stageRef.current;
    if (!node) return;
    const update = () => setStageWidth(Math.max(1, node.clientWidth));
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // 与成片合成口径一致：audio 元素不参与合成（仅保留主轨原声），
  // 因此实时预览也不渲染/不发声。
  const layers = useMemo(
    () =>
      playbackLayersInWindow(project, timeline, playheadTick, tasks).filter(
        (layer) => layer.element.creation.type !== "audio",
      ),
    [playheadTick, project, tasks, timeline],
  );
  const visibleIds = useMemo(() => {
    const ids = new Set<string>();
    layers.forEach((layer) => {
      const { span } = layer.element;
      if (
        span.start_tick <= playheadTick &&
        playheadTick < span.start_tick + span.duration_tick
      )
        ids.add(layer.element.element_id);
    });
    return ids;
  }, [layers, playheadTick]);

  // rAF 主时钟：播放中按真实时间推进播放头；video 层只做跟随与纠偏。
  useEffect(() => {
    if (!playing) {
      clock.current = null;
      return;
    }
    let frame = 0;
    const step = (now: number) => {
      if (!clock.current) {
        clock.current = { baseTick: lastEmittedTick.current, baseTime: now };
      }
      const tick = Math.round(
        clock.current.baseTick +
          ((now - clock.current.baseTime) / 1000) * ticksPerSecond,
      );
      if (tick >= durationTick) {
        lastEmittedTick.current = durationTick;
        onPlayheadChange(durationTick);
        onPlayingChange(false);
        return;
      }
      lastEmittedTick.current = tick;
      onPlayheadChange(tick);
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, durationTick, ticksPerSecond]);

  // 外部 scrub（点击时间轴/进度条）时给时钟重定基准。
  useEffect(() => {
    if (Math.abs(playheadTick - lastEmittedTick.current) <= 1) return;
    lastEmittedTick.current = playheadTick;
    clock.current = null;
  }, [playheadTick]);

  // 让每个已挂载的媒体层跟随播放头：可见即播、越界即停、漂移即纠。
  useEffect(() => {
    layers.forEach((layer) => {
      if (!layer.media || layer.media.mediaKind === "image") return;
      const media = mediaRefs.current.get(layer.element.element_id);
      if (!media) return;
      const target = mediaTargetSeconds(layer, playheadTick, ticksPerSecond);
      const visible = visibleIds.has(layer.element.element_id);
      if (media.playbackRate !== layer.media.playbackRate) {
        media.playbackRate = layer.media.playbackRate;
      }
      if (!visible || !playing) {
        if (!media.paused) media.pause();
        if (
          !playing &&
          visible &&
          Math.abs(media.currentTime - target) > DRIFT_TOLERANCE_SECONDS
        ) {
          media.currentTime = target;
        }
        return;
      }
      if (Math.abs(media.currentTime - target) > DRIFT_TOLERANCE_SECONDS) {
        media.currentTime = target;
      }
      if (media.paused) {
        media.play()?.catch(() => undefined);
      }
    });
  }, [layers, playheadTick, playing, ticksPerSecond, visibleIds]);

  // 卸载即停：组件销毁时避免残留声音。
  useEffect(() => {
    const refs = mediaRefs.current;
    return () => refs.forEach((media) => media.pause());
  }, []);

  const registerMedia = (elementId: string) => (node: HTMLVideoElement | null) => {
    if (node) mediaRefs.current.set(elementId, node);
    else mediaRefs.current.delete(elementId);
  };

  const anyVisible = visibleIds.size > 0;

  return (
    <div
      data-timeline-live-preview
      className="relative flex h-full w-full items-center justify-center"
    >
      <div
        ref={stageRef}
        data-live-stage
        className="relative max-h-full max-w-full overflow-hidden bg-black"
        style={{
          aspectRatio: aspectRatioStyle(project.settings.aspect_ratio),
          height: "100%",
        }}
      >
        {!anyVisible && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-white/55">
            该时刻还没有画面内容
          </div>
        )}
        {layers.map((layer) => {
          const { element, media, status } = layer;
          const elementId = element.element_id;
          const visible = visibleIds.has(elementId);
          if (media && media.mediaKind === "video") {
            // 声音策略与成片一致：仅主轨视频保留原声，overlay 媒体静音。
            const silent = muted || element.creation.type === "overlay";
            return (
              <video
                key={`${elementId}:${media.versionId}`}
                ref={registerMedia(elementId)}
                data-live-layer={elementId}
                data-live-layer-state={status}
                src={media.url}
                className={`absolute object-contain ${
                  visible ? "" : "invisible"
                }`}
                style={locationBoxStyle(element.location)}
                muted={silent}
                loop={media.loop}
                playsInline
                preload="auto"
              />
            );
          }
          if (!visible) return null;
          if (media && media.mediaKind === "image") {
            return (
              <img
                key={`${elementId}:${media.versionId}`}
                data-live-layer={elementId}
                data-live-layer-state={status}
                src={media.url}
                alt={element.label || elementId}
                className="absolute object-contain"
                style={locationBoxStyle(element.location)}
              />
            );
          }
          if (status === "ready") {
            return (
              <TextOverlayLayer
                key={elementId}
                layer={layer}
                stageWidth={stageWidth}
              />
            );
          }
          return <PlaceholderLayer key={elementId} layer={layer} />;
        })}
      </div>
    </div>
  );
}
