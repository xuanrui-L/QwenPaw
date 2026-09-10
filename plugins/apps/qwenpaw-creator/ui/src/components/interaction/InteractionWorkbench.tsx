import { useEffect, useState } from "react";
import type { ProjectDocument } from "@/contracts/creator";
import { getWorkGraph } from "@/api/creator/workGraph";
import { selectLiveTimelineIds } from "@/selectors/timelineElementSelectors";
import { useSearchParams } from "@/routing/navigation";
import PresentationEditor from "./PresentationEditor";
import ChoiceEditor from "./ChoiceEditor";

export default function InteractionWorkbench({
  project,
}: {
  project: ProjectDocument;
}) {
  const query = useSearchParams();
  const field = query.get("field") || "";
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("pages");
  const [statuses, setStatuses] = useState<Record<string, string>>({});
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
      setOpen(true);
      setTab(point?.element.element_id ?? "pages");
    }
  }, [field]);
  useEffect(() => {
    if (!open) return;
    let live = true;
    const refresh = () =>
      getWorkGraph(project.project_id)
        .then((graph) => {
          if (live)
            setStatuses(
              Object.fromEntries(
                graph.nodes.map((node) => [node.id, node.status]),
              ),
            );
        })
        .catch(() => {});
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [project.project_id, open]);
  if (!points.length && !project.narrative_edges?.length) return null;
  return (
    <>
      <div
        className="flex shrink-0 items-center justify-between gap-3 border-t border-[var(--color-border)] px-4 py-2"
        data-interaction-workbench
      >
        <span className="text-xs">
          作品界面 · 首页 / 播放 / 剧情地图 / 结局 · {points.length} 个抉择点
        </span>
        <button
          type="button"
          className="btn-primary"
          onClick={() => setOpen(true)}
        >
          交互设计
        </button>
      </div>
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
            onClick={() => setOpen(false)}
          >
            返回蓝图
          </button>
        </header>
        <nav
          className="flex shrink-0 flex-wrap gap-2 border-b border-[var(--color-border)] px-4 py-2"
          aria-label="交互设计内容"
        >
          <button
            type="button"
            className={tab === "pages" ? "btn-primary" : "btn-secondary"}
            onClick={() => setTab("pages")}
          >
            作品页面与功能按钮
          </button>
          {points.map(({ tid, element }, i) => (
            <button
              type="button"
              key={element.element_id}
              className={
                tab === element.element_id ? "btn-primary" : "btn-secondary"
              }
              onClick={() => setTab(element.element_id)}
            >
              抉择 {i + 1} · {project.timelines.items[tid].title}
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {/* Keep drafts mounted while switching between pages and choices. */}
          <div hidden={tab !== "pages"}>
            <PresentationEditor
              key={project.project_id}
              project={project}
              status={statuses["interaction:project"]}
            />
          </div>
          {points.map(({ tid, element }) => (
            <div key={element.element_id} hidden={tab !== element.element_id}>
              <ChoiceEditor
                project={project}
                timelineId={tid}
                element={element}
                status={statuses[`interaction:${element.element_id}`] ?? ""}
              />
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
