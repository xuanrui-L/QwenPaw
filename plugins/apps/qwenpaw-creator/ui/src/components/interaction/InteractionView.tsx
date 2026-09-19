import { useEffect, useRef, useState } from "react";
import type {
  InteractionCreationDocument,
  ProjectDocument,
} from "@/contracts/creator";
import {
  fetchMotionDocument,
  getArtifactVersionMediaUrl,
  getAssetVersionMediaUrl,
} from "@/api/creator";
import "../../../../player/ivb/static/interaction-runtime.js";

type Runtime = {
  mount: (
    container: HTMLElement,
    point: object,
    edges: object,
    onSelect: (ref: string) => void,
  ) => { dispose(): void; pause(value: boolean): void };
};

/** Same sandbox, hotspot geometry and countdown as the exported/hosted player. */
export default function InteractionView({
  creation,
  project,
  onSelect,
  paused = false,
  countdown = true,
}: {
  creation: InteractionCreationDocument;
  project: ProjectDocument;
  onSelect: (ref: string) => void;
  paused?: boolean;
  countdown?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const callback = useRef(onSelect);
  callback.current = onSelect;
  const [html, setHtml] = useState(creation.motion?.html ?? null);
  const [error, setError] = useState("");
  const handle = useRef<ReturnType<Runtime["mount"]> | null>(null);
  useEffect(() => {
    let live = true;
    setError("");
    setHtml(creation.motion?.html ?? null);
    if (creation.motion?.html_file_id)
      fetchMotionDocument(creation.motion.html_file_id).then(
        (body) => {
          if (live) setHtml(body);
        },
        () => {
          if (live) setError("动效加载失败，请刷新后重试");
        },
      );
    return () => {
      live = false;
    };
  }, [creation.motion?.html, creation.motion?.html_file_id]);
  useEffect(() => {
    if (!container.current || error) return;
    if (creation.motion?.html_file_id && !html) return;
    const runtime = (
      globalThis as typeof globalThis & { IVBInteraction: Runtime }
    ).IVBInteraction;
    handle.current = runtime.mount(
      container.current,
      {
        ...creation,
        motion_html: html,
        base_frame_url: creation.base_frame_ref
          ? (project.assets.artifact_versions_by_id[
              creation.base_frame_ref.replace(/^artifact-version:/, "")
            ]
              ? getArtifactVersionMediaUrl
              : getAssetVersionMediaUrl)(
              creation.base_frame_ref.replace(/^artifact-version:/, ""),
            )
          : null,
        review: !countdown,
      },
      Object.fromEntries(
        (project.narrative_edges ?? []).map((edge) => [edge.edge_id, edge]),
      ),
      (ref) => callback.current(ref),
    );
    handle.current.pause(paused);
    return () => {
      handle.current?.dispose();
      handle.current = null;
    };
  }, [creation, html, project.narrative_edges, error, countdown]); // pause changes preserve elapsed countdown
  useEffect(() => {
    handle.current?.pause(paused);
  }, [paused]);
  return (
    <div ref={container} className="absolute inset-0" data-interaction-preview>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
