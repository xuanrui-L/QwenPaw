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
import DesignViewport from "./DesignViewport";
import { mockVideoPoster } from "./mockVideoPoster";
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

// Polling/status updates must not reload the iframe or restart CSS animations.
export function presentationPreviewKey(
  project: ProjectDocument,
  review: boolean,
) {
  const ids = selectLiveTimelineIds(project);
  return JSON.stringify({
    id: project.project_id,
    name: project.name,
    description: project.description,
    brief: project.strategy.creative_brief,
    motion: project.interactive_presentation?.motion,
    edges: project.narrative_edges,
    nodes: ids.map((id) => {
      const node = project.timelines.items[id];
      return {
        id,
        title: node.title,
        synopsis: node.synopsis,
        ticks: node.ticks_per_second,
        points: Object.values(node.elements_by_id).filter(
          (e) => e.enabled && e.creation.type === "interaction",
        ),
      };
    }),
    assets: review ? undefined : project.assets,
  });
}

export function PresentationPreview({
  project,
  review = true,
  selection,
  onInspect,
  onControls,
  reviewPointId,
  onChoiceInspect,
}: {
  project: ProjectDocument;
  review?: boolean;
  selection?: Selection;
  onInspect?: (selection: Selection) => void;
  onControls?: (controls: PreviewControl[]) => void;
  reviewPointId?: string;
  onChoiceInspect?: (edgeRef: string) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const handle = useRef<Handle | null>(null);
  const callbacks = useRef({ onInspect, onControls, onChoiceInspect });
  callbacks.current = { onInspect, onControls, onChoiceInspect };
  const projectRef = useRef(project);
  projectRef.current = project;
  const previewKey = presentationPreviewKey(project, review);
  const [localScreen, setLocalScreen] = useState<PresentationScreenId>("title");
  const [mobile, setMobile] = useState(false);
  const selected = selection ?? { screen: localScreen };
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [error, setError] = useState("");
  useEffect(() => {
    const project = projectRef.current;
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
                element_id: e.element_id,
                source_timeline_id: id,
                base_frame_url: review
                  ? mockVideoPoster
                  : frameRef
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
            synopsis:
              project.description.trim() ||
              project.strategy.creative_brief.trim(),
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
          reviewPoster: review ? mockVideoPoster : undefined,
          reviewPoint: review
            ? interactions.find((p) => p?.element_id === reviewPointId)
            : undefined,
          onChoiceInspect(edgeRef: string) {
            if (!cancelled) callbacks.current.onChoiceInspect?.(edgeRef);
          },
          progress: review
            ? { visited: ids, endings: [], current_timeline: ids[0] }
            : undefined,
          segmentUrl(id: string) {
            if (review) return "";
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
  }, [previewKey, review, reviewPointId]);
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
          <span>实际生成界面 · 视频画面为示意 · 点击按钮查看详情</span>
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
            "，在右侧编辑提示词并生成后可查看效果。"}
        </p>
      )}
      <DesignViewport mobile={mobile} active={review}>
        <div
          ref={root}
          className={review ? "h-full w-full" : "h-[82vh] w-full"}
          data-presentation-preview
        />
      </DesignViewport>
    </div>
  );
}
