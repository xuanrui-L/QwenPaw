import { useEffect, useState } from "react";
import type { ProjectDocument } from "@/contracts/creator";
import { getWorkGraph } from "@/api/creator/workGraph";
import {
  selectLiveTimelineIds,
  selectNarrativeShape,
} from "@/selectors/timelineElementSelectors";
import { useSearchParams } from "@/routing/navigation";
import PresentationEditor from "./PresentationEditor";
import "./interactionDesign.css";

export function hasInteractionDesign(project: ProjectDocument) {
  return selectNarrativeShape(project) === "branching";
}

export default function InteractionWorkbench({
  project,
  open,
  onOpenChange,
}: {
  project: ProjectDocument;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const query = useSearchParams();
  const field = query.get("field") || "";
  const [statuses, setStatuses] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pollError, setPollError] = useState("");
  const points = selectLiveTimelineIds(project).flatMap((tid) =>
    Object.values(project.timelines.items[tid].elements_by_id)
      .filter(
        (element) => element.enabled && element.creation.type === "interaction",
      )
      .map((element) => ({ tid, element })),
  );
  useEffect(() => {
    const point = points.find(({ element }) =>
      field.includes(`/elements_by_id/${element.element_id}/creation`),
    );
    if (field.includes("/interactive_presentation") || point) {
      onOpenChange(true);
    }
  }, [field, onOpenChange]);
  useEffect(() => {
    setStatuses({});
    setErrors({});
    setPollError("");
  }, [project.project_id]);
  useEffect(() => {
    if (!open) return;
    let live = true;
    const refresh = () =>
      getWorkGraph(project.project_id)
        .then((graph) => {
          if (live) {
            setStatuses(
              Object.fromEntries(
                graph.nodes.map((node) => [node.id, node.status]),
              ),
            );
            setErrors(
              Object.fromEntries(
                graph.nodes.map((node) => [node.id, node.error ?? ""]),
              ),
            );
            setPollError("");
          }
        })
        .catch((error) => {
          if (live)
            setPollError(
              error instanceof Error ? error.message : String(error),
            );
        });
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [project.project_id, open]);
  if (!hasInteractionDesign(project)) return null;
  return (
    <>
      <section
        style={{ display: open ? undefined : "none" }}
        className="absolute inset-0 z-30 flex min-h-0 flex-col bg-[var(--color-bg-layout)]"
        aria-label="交互设计工作台"
        data-interaction-design-panel
      >
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--color-border)] px-4 py-3">
          <div>
            <strong>交互设计</strong>
            <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
              页面与抉择均由助手生成。先查看效果，再修改设计、重新生成并审阅。
            </p>
          </div>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => onOpenChange(false)}
          >
            返回蓝图
          </button>
        </header>
        <div className="interaction-design-scroll min-h-0 flex-1 overflow-y-auto p-4">
          {pollError && (
            <p role="alert" className="mb-3 text-sm text-red-500">
              无法更新生成状态：{pollError}。正在重试，当前状态可能已过期。
            </p>
          )}
          <PresentationEditor
            key={project.project_id}
            project={project}
            status={statuses["interaction:project"]}
            statuses={statuses}
            errors={errors}
            focusField={field}
          />
        </div>
      </section>
    </>
  );
}
