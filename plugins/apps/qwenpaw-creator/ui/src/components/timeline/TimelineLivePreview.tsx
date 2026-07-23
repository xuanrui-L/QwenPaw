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
const RETIRED_MOTION_MOTIFS = new Set([
  "speed_lines",
  "side_eye",
  "sassy_cat",
  "surprised_cat",
  "happy_cat",
  "confused_cat",
]);

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

export function syncMotionAnimation(
  animation: Animation,
  localTimeMs: number,
  playing: boolean,
) {
  const endTime = animation.effect?.getComputedTiming().endTime ?? Infinity;
  const finiteEnd = Number.isFinite(endTime) ? Number(endTime) : null;
  animation.currentTime =
    finiteEnd === null ? localTimeMs : Math.min(localTimeMs, finiteEnd);

  // Calling play() on a finished finite entrance animation auto-rewinds it
  // to its transparent first frame. Keep it filled at the final keyframe;
  // only active/infinite animations should advance with the preview clock.
  if (!playing || (finiteEnd !== null && localTimeMs >= finiteEnd)) {
    animation.pause();
  } else {
    animation.play();
  }
}

export function motionExitProgress(
  exitStyle: string | undefined,
  localTimeMs: number,
  durationMs: number,
) {
  if (!exitStyle || exitStyle === "none" || durationMs <= 0) return 0;
  const progress = localTimeMs / durationMs;
  return Math.max(0, Math.min(1, (progress - 0.85) / 0.15));
}

function motionDataSetting(html: string, name: string): string | undefined {
  const match = new RegExp(`data-motion-${name}=["']([^"']+)["']`, "i").exec(
    html,
  );
  return match?.[1];
}

function motionPreviewDocument(html: string, keepInsideViewport: boolean): string {
  const safetyStyle = keepInsideViewport
    ? `<style data-qwenpaw-viewport-safety>html{padding:5%!important;box-sizing:border-box!important;overflow:hidden!important}body{width:100%!important;height:100%!important;box-sizing:border-box!important;overflow:visible!important;transform:scale(.9)!important;transform-origin:center!important}p,[class*=text],[class*=title],[class*=caption]{max-width:100%!important;font-size:min(8vh,8vw)!important;line-height:1.15!important;white-space:normal!important;overflow-wrap:anywhere!important;word-break:break-word!important;letter-spacing:.02em!important;-webkit-text-stroke:0!important;text-shadow:1px 1px 0 rgba(0,0,0,.22)!important}[data-motion-motif=caption_card] [class*=text]{font-size:min(10vh,12vw)!important;line-height:1.08!important;max-height:100%!important}${pawTrailCompatibilityCss()}</style>`
    : `<style data-qwenpaw-motion-compat>${pawTrailCompatibilityCss()}</style>`;
  return /<\/head>/i.test(html)
    ? html.replace(/<\/head>/i, `${safetyStyle}</head>`)
    : `${safetyStyle}${html}`;
}

function pawTrailCompatibilityCss(): string {
  return "[data-motion-motif=paw_trail] .p4,[data-motion-motif=paw_trail] .p5{display:none!important}[data-motion-motif=paw_trail] .toe{width:20%!important;height:20%!important}[data-motion-motif=paw_trail] .t1{left:2%!important;top:28%!important}[data-motion-motif=paw_trail] .t2{left:27%!important;top:7%!important}[data-motion-motif=paw_trail] .t3{left:auto!important;right:27%!important;top:3%!important}[data-motion-motif=paw_trail] .t4{left:auto!important;right:2%!important;top:24%!important}[data-motion-motif=paw_trail] .p1,[data-motion-motif=paw_trail] .p2,[data-motion-motif=paw_trail] .p3{opacity:0;animation:qwenpaw-paw-appear .36s cubic-bezier(.2,.85,.2,1) forwards!important}[data-motion-motif=paw_trail] .p1{animation-delay:.08s!important}[data-motion-motif=paw_trail] .p2{animation-delay:.38s!important}[data-motion-motif=paw_trail] .p3{animation-delay:.68s!important}[data-motion-motif=alert_mark] .bar{top:29%!important;height:29%!important}[data-motion-motif=alert_mark] .dot{left:45%!important;top:62%!important;width:10%!important}@keyframes qwenpaw-paw-appear{0%{opacity:0}100%{opacity:1}}";
}

function MotionOverlayLayer({
  layer,
  playheadTick,
  ticksPerSecond,
  playing,
}: {
  layer: ElementPlayback;
  playheadTick: number;
  ticksPerSecond: number;
  playing: boolean;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const { element } = layer;
  const motion =
    element.creation.type === "overlay" ? element.creation.motion : null;
  const isTextOverlay =
    element.creation.type === "overlay" &&
    ["pet_os", "interview_summary"].includes(element.creation.overlay_kind);
  const localTimeMs =
    (Math.max(0, playheadTick - element.span.start_tick) / ticksPerSecond) *
    1000;
  // While playing, CSS animations advance on their own after one alignment.
  // Paused scrubbing still needs every playhead change reflected immediately.
  const pausedSeekTimeMs = playing ? null : localTimeMs;
  const durationMs = (element.span.duration_tick / ticksPerSecond) * 1000;
  const exitStyle =
    motion?.exit ??
    (motion?.html ? motionDataSetting(motion.html, "exit") : undefined);
  const exitProgress = motionExitProgress(
    exitStyle,
    localTimeMs,
    durationMs,
  );
  const boxStyle = locationBoxStyle(element.location);
  const exitScale = exitStyle === "shrink" ? 1 - exitProgress * 0.18 : 1;
  const baseOpacity =
    typeof boxStyle.opacity === "number" ? boxStyle.opacity : 1;

  const syncAnimations = () => {
    const document = iframeRef.current?.contentDocument;
    if (!document || typeof document.getAnimations !== "function") return;
    document.getAnimations().forEach((animation) => {
      syncMotionAnimation(animation, localTimeMs, playing);
    });
  };

  useEffect(syncAnimations, [playing, pausedSeekTimeMs]);

  if (!motion?.html) return null;
  const motif = motionDataSetting(motion.html, "motif");
  if (motif && RETIRED_MOTION_MOTIFS.has(motif)) return null;
  return (
    <iframe
      ref={iframeRef}
      data-live-motion-overlay={element.element_id}
      srcDoc={motionPreviewDocument(motion.html, isTextOverlay)}
      title={element.label || "动态动效"}
      // 不开放脚本；allow-same-origin 仅用于父页面同步 CSS 动画时间轴。
      sandbox="allow-same-origin"
      onLoad={syncAnimations}
      className="pointer-events-none absolute border-0 bg-transparent"
      style={{
        ...boxStyle,
        opacity:
          exitStyle === "none" ? baseOpacity : baseOpacity * (1 - exitProgress),
        transform: `${boxStyle.transform ?? ""} scale(${exitScale})`.trim(),
      }}
    />
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
            if (
              element.creation.type === "overlay" &&
              element.creation.motion?.html
            ) {
              return (
                <MotionOverlayLayer
                  key={elementId}
                  layer={layer}
                  playheadTick={playheadTick}
                  ticksPerSecond={ticksPerSecond}
                  playing={playing}
                />
              );
            }
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
