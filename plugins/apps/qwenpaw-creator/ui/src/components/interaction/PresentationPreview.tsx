import { useEffect, useRef, useState } from "react";
import type {
  ProjectDocument,
  PresentationScreenId,
  PresentationActionId,
} from "@/contracts/creator";
import {
  fetchMotionDocument,
  getArtifactVersionMediaUrl,
  getAssetVersionMediaUrl,
} from "@/api/creator";
import { selectLiveTimelineIds } from "@/selectors/timelineElementSelectors";
import { screens, type PreviewControl } from "./presentationDesign";
import "../../../../player/ivb/static/interaction-runtime.js";
import "../../../../player/ivb/static/authored-player.js";

type Handle = {
  dispose(): void;
  show(name: string): void;
  inspect(screen: string, action?: string): void;
};
type Runtime = {
  mount(container: HTMLElement, bundle: object, adapter: object): Handle;
};
type Selection = {
  screen: PresentationScreenId;
  action?: PresentationActionId;
};

export function PresentationPreview({
  project,
  review = true,
  selection,
  onInspect,
  onControls,
}: {
  project: ProjectDocument;
  review?: boolean;
  selection?: Selection;
  onInspect?: (selection: Selection) => void;
  onControls?: (controls: PreviewControl[]) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const handle = useRef<Handle | null>(null);
  const callbacks = useRef({ onInspect, onControls });
  callbacks.current = { onInspect, onControls };
  const [localScreen, setLocalScreen] = useState<PresentationScreenId>("title");
  const [mobile, setMobile] = useState(false);
  const selected = selection ?? { screen: localScreen };
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const motion = project.interactive_presentation?.motion;
      const html = motion?.html_file_id
        ? await fetchMotionDocument(motion.html_file_id)
        : motion?.html;
      if (!html) {
        setError("作品页面尚未生成");
        return;
      }
      const ids = selectLiveTimelineIds(project);
      const edges = project.narrative_edges ?? [];
      const nodes = Object.fromEntries(
        ids.map((id) => [
          id,
          {
            title: project.timelines.items[id].title,
            synopsis: project.timelines.items[id].synopsis,
            children: edges
              .filter((e) => e.source_timeline_id === id)
              .map((e) => e.target_timeline_id),
          },
        ]),
      );
      const interactions = await Promise.all(
        ids.flatMap((id) =>
          Object.values(project.timelines.items[id].elements_by_id)
            .filter((e) => e.enabled && e.creation.type === "interaction")
            .map(async (e) => {
              if (e.creation.type !== "interaction") return null;
              const frameRef = e.creation.base_frame_ref?.replace(
                /^artifact-version:/,
                "",
              );
              return {
                ...e.creation,
                source_timeline_id: id,
                base_frame_url: frameRef
                  ? (project.assets.artifact_versions_by_id[frameRef]
                      ? getArtifactVersionMediaUrl
                      : getAssetVersionMediaUrl)(frameRef)
                  : null,
                at_seconds:
                  e.span.start_tick /
                  project.timelines.items[id].ticks_per_second,
                motion_html: e.creation.motion?.html_file_id
                  ? await fetchMotionDocument(e.creation.motion.html_file_id)
                  : e.creation.motion?.html,
              };
            }),
        ),
      );
      if (cancelled || !root.current) return;
      const runtime = (
        globalThis as typeof globalThis & { IVBAuthoredPlayer: Runtime }
      ).IVBAuthoredPlayer;
      handle.current = runtime.mount(
        root.current,
        {
          authored_html: html,
          meta: {
            title: project.name,
            synopsis: project.strategy.creative_brief,
          },
          nodes,
          edges: Object.fromEntries(edges.map((e) => [e.edge_id, e])),
          interactions: interactions.filter(Boolean),
          entry_timeline_id:
            ids.find((id) => !edges.some((e) => e.target_timeline_id === id)) ??
            ids[0],
        },
        {
          review,
          progress: review
            ? { visited: ids, endings: [], current_timeline: ids[0] }
            : undefined,
          segmentUrl(id: string) {
            const selectedVersion =
              project.assets.artifact_slots_by_id[`timeline:${id}:render`]
                ?.selected_version_id;
            return selectedVersion
              ? getArtifactVersionMediaUrl(selectedVersion)
              : "";
          },
          onError(err: Error) {
            if (!cancelled) setError(err.message);
          },
          onControls(controls: PreviewControl[]) {
            if (!cancelled) callbacks.current.onControls?.(controls);
          },
          onInspect(value: Selection) {
            if (!cancelled) callbacks.current.onInspect?.(value);
          },
          onReady() {
            if (review && !cancelled) {
              handle.current?.show(selectedRef.current.screen);
              handle.current?.inspect(
                selectedRef.current.screen,
                selectedRef.current.action,
              );
            }
          },
        },
      );
    };
    setError("");
    void load().catch((err) => {
      if (!cancelled) setError(err.message);
    });
    return () => {
      cancelled = true;
      handle.current?.dispose();
      handle.current = null;
    };
  }, [project, review]);
  useEffect(() => {
    handle.current?.show(selected.screen);
    handle.current?.inspect(selected.screen, selected.action);
  }, [selected.screen, selected.action]);
  return (
    <div className="min-w-0" data-presentation-preview-area>
      {review && (
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
          {!selection && (
            <div className="flex gap-2">
              {screens.map((item) => (
                <button
                  type="button"
                  className="btn-secondary"
                  key={item.id}
                  onClick={() => setLocalScreen(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          )}
          <span>实际生成页面 · 点击按钮可定位设计 · 此处不推进剧情</span>
          <div className="flex gap-1">
            {[false, true].map((value) => (
              <button
                type="button"
                key={String(value)}
                className="btn-secondary"
                aria-pressed={mobile === value}
                onClick={() => setMobile(value)}
              >
                {value ? "手机" : "桌面"}
              </button>
            ))}
          </div>
        </div>
      )}
      {error && (
        <p role="status" className="my-4 text-sm">
          {error}
          {!project.interactive_presentation?.motion &&
            "，保存设计并生成后可在这里查看实际效果。"}
        </p>
      )}
      <div
        ref={root}
        style={
          review
            ? {
                width: mobile ? 390 : "100%",
                maxWidth: "100%",
                height: mobile ? 640 : 480,
                margin: "auto",
              }
            : undefined
        }
        className={
          review
            ? "overflow-hidden rounded border border-[var(--color-border)]"
            : "h-[82vh] w-full"
        }
        data-presentation-preview
      />
    </div>
  );
}
