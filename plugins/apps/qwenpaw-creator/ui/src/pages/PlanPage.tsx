import { useCallback, useEffect, useMemo, useState } from "react";
import { message } from "antd";
import { Loader2, Scissors } from "lucide-react";
import { navigate, useParams, useSearchParams } from "@/routing/navigation";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { renderTimeline } from "@/api/creator";
import {
  selectPrimaryTimeline,
  timelineEndTick,
} from "@/selectors/timelineElementSelectors";
import { resolveElementPlayback } from "@/selectors/elementPlaybackSelectors";
import { projectJsonPointer } from "@/lib/projectJsonPointer";
import { useReviewFieldFocus } from "@/routing/reviewFocus";
import TimelineCanvas from "@/components/timeline/TimelineCanvas";
import ElementList from "@/components/timeline/ElementList";
import ElementDetail from "@/components/timeline/ElementDetail";
import PageSkeleton from "@/components/PageSkeleton";
import PageLoadError from "@/components/PageLoadError";
import type { TimelineElementDocument } from "@/contracts/creator";

function sec(tick: number, ticksPerSecond: number): string {
  return (tick / ticksPerSecond).toFixed(1).replace(/\.0$/, "");
}

export default function PlanPage() {
  const { id = "" } = useParams();
  const query = useSearchParams();
  const project = useProjectSnapshotStore((state) =>
    state.projectId === id ? state.project : null,
  );
  const syncStatus = useProjectSnapshotStore((state) => state.syncStatus);
  const syncError = useProjectSnapshotStore((state) => state.syncError);
  const requestInFlight = useProjectSnapshotStore(
    (state) => state.requestInFlight,
  );
  const patching = useProjectSnapshotStore((state) => state.patching);
  const patchProject = useProjectSnapshotStore((state) => state.patch);
  const pollOnce = useProjectSnapshotStore((state) => state.pollOnce);
  const tasks = useCreatorTaskViewStore((state) => state.tasks);
  const timeline = useMemo(() => selectPrimaryTimeline(project), [project]);
  const selectedElementId = query.get("element");
  const selectedElement =
    selectedElementId && timeline
      ? timeline.elements_by_id[selectedElementId] ?? null
      : null;
  const [playheadTick, setPlayheadTick] = useState(0);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [activeElementIds, setActiveElementIds] = useState<string[]>([]);
  const durationTick = timelineEndTick(timeline);
  const displayDurationTick = timeline
    ? durationTick ||
      Math.round(
        (project?.settings.target_duration_seconds || 10) *
          timeline.ticks_per_second,
      )
    : 1;
  const reviewMode = query.get("review") === "1";
  const reviewField = query.get("field");
  const reviewPulse = query.get("reviewPulse");
  useReviewFieldFocus({
    path: `/project/${id}/plan`,
    field: reviewField,
    enabled: reviewMode,
    pulse: reviewPulse,
  });

  useEffect(() => {
    useCreatorInteractionStore
      .getState()
      .select(selectedElement ? `element:${selectedElement.element_id}` : null);
  }, [selectedElement]);

  useEffect(() => {
    if (
      !selectedElement ||
      (playheadTick >= selectedElement.span.start_tick &&
        playheadTick <
          selectedElement.span.start_tick + selectedElement.span.duration_tick)
    )
      return;
    setPlayheadTick(selectedElement.span.start_tick);
  }, [selectedElement]);

  const base = `/project/${id}/plan`;
  const selectElement = useCallback(
    (elementId: string) => {
      const element = timeline?.elements_by_id[elementId];
      if (element) setPlayheadTick(element.span.start_tick);
      navigate(
        selectedElementId === elementId
          ? base
          : `${base}?element=${encodeURIComponent(elementId)}`,
      );
    },
    [base, selectedElementId, timeline],
  );

  if (!project) {
    if (syncStatus === "invalid" || syncStatus === "not_found") {
      return (
        <PageLoadError
          message={syncError || "Project 无法读取"}
          retry={() => void pollOnce(id)}
        />
      );
    }
    return <PageSkeleton type="list" />;
  }
  if (!timeline) {
    return (
      <PageLoadError
        message="视频方案中没有可用的时间轴"
        retry={() => void pollOnce(id)}
      />
    );
  }

  const patchValue = (path: string, before: unknown, value: unknown) =>
    patchProject(id, [{ op: "replace", path, before, value }]);
  // 导出门禁：只统计参与成片合成的主轨元素（r2v/edit）；文案 overlay
  // 由合成器确定性绘制，motion/media overlay 与 audio 不参与合成。
  const composeElements = Object.values(timeline.elements_by_id).filter(
    (element) =>
      element.enabled &&
      (element.creation.type === "r2v" || element.creation.type === "edit"),
  );
  const notReadyCount = composeElements.filter(
    (element) =>
      resolveElementPlayback(project, timeline, element, tasks).status !==
      "ready",
  ).length;
  const exportDisabled =
    composeElements.length === 0 || notReadyCount > 0 || exporting;
  const exportTimeline = async () => {
    setExporting(true);
    try {
      // 导出是确定性后端合成，不经过 Agent；完成后立即拉新快照，
      // 预览会自动切换到新鲜成片。
      await renderTimeline(id, timeline.timeline_id);
      await pollOnce(id);
      message.success("成片已导出，预览已切换到成片");
    } catch (error) {
      message.error(`导出成片失败：${(error as Error).message}`);
    } finally {
      setExporting(false);
    }
  };
  const openElementWorkbench = (element: TimelineElementDocument) =>
    navigate(`${base}/element/${encodeURIComponent(element.element_id)}`);

  return (
    <div
      data-plan-page
      className={`flex h-full min-h-0 flex-col bg-[var(--color-bg-layout)] ${
        previewOpen ? "overflow-y-auto overscroll-contain" : "overflow-hidden"
      }`}
    >
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)]/60 px-5 py-3 backdrop-blur">
        <div className="min-w-0">
          {project.strategy.creative_brief ||
          project.strategy.creative_direction ? (
            <details className="max-w-3xl">
              <summary className="w-fit cursor-pointer select-none text-base font-semibold text-[var(--color-text-primary)]">
                创作总纲
              </summary>
              <div
                data-creator-field="project:strategy/creative_brief"
                data-creator-path={projectJsonPointer(
                  "strategy",
                  "creative_brief",
                )}
                data-creator-field-label="创作总纲"
                className="mt-2 max-h-[92px] overflow-y-auto whitespace-pre-wrap rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-3 text-xs leading-5 text-[var(--color-text-secondary)]"
              >
                {project.strategy.creative_brief}
                {project.strategy.creative_direction &&
                  `\n\n创作方向：${project.strategy.creative_direction}`}
              </div>
            </details>
          ) : (
            <h2 className="text-base font-semibold text-[var(--color-text-primary)]">
              创作总纲
            </h2>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="rounded-full border border-[var(--color-border)] bg-white px-2.5 py-1 text-[11px] font-semibold text-[var(--color-text-secondary)]">
            {sec(durationTick, timeline.ticks_per_second)}s
          </span>
          <span className="rounded-full border border-[var(--color-border)] bg-white px-2.5 py-1 text-[11px] font-semibold text-[var(--color-text-secondary)]">
            {project.settings.aspect_ratio}
          </span>
          <span className="rounded-full border border-[var(--color-border)] bg-white px-2.5 py-1 text-[11px] font-semibold text-[var(--color-text-secondary)]">
            {Object.keys(timeline.elements_by_id).length} 项内容
          </span>
          <button
            type="button"
            disabled={exportDisabled}
            title={
              composeElements.length === 0
                ? "时间轴还没有可合成的画面内容"
                : notReadyCount > 0
                ? `还有 ${notReadyCount} 项内容生成中，全部就绪后可导出`
                : exporting
                ? "正在合成导出成片"
                : "合成并导出最终成片文件"
            }
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:border-[var(--color-border)] disabled:hover:bg-[var(--color-bg-primary)]"
            onClick={() => void exportTimeline()}
          >
            {exporting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Scissors className="h-3.5 w-3.5" />
            )}
            {exporting ? "导出中…" : "导出成片"}
          </button>
        </div>
      </header>

      {syncStatus === "degraded" && (
        <div className="shrink-0 border-b border-[var(--color-warning)]/20 bg-[var(--color-warning-soft)] px-5 py-1.5 text-[11px] text-[var(--color-warning)]">
          当前显示最后一次可用快照；后台同步暂时异常。
          {syncError ? ` ${syncError}` : ""}
        </div>
      )}

      <TimelineCanvas
        project={project}
        timeline={timeline}
        durationTick={displayDurationTick}
        playheadTick={Math.min(playheadTick, displayDurationTick)}
        selectedElementId={selectedElementId}
        activeElementIds={activeElementIds}
        previewOpen={previewOpen}
        tasks={tasks}
        onPreviewOpenChange={setPreviewOpen}
        onPlayheadChange={(tick) =>
          setPlayheadTick(Math.max(0, Math.min(displayDurationTick, tick)))
        }
        onSelectElement={selectElement}
        onActiveElementIdsChange={setActiveElementIds}
      />

      <main
        className={`grid min-h-0 gap-4 p-4 ${
          previewOpen
            ? "h-[340px] shrink-0 grid-cols-[minmax(280px,36fr)_minmax(0,64fr)]"
            : "flex-1 grid-cols-[minmax(280px,36fr)_minmax(0,64fr)]"
        }`}
      >
        <ElementList
          timeline={timeline}
          playheadTick={playheadTick}
          activeElementIds={activeElementIds}
          selectedElementId={selectedElementId}
          tasks={tasks}
          onSelect={selectElement}
        />
        <ElementDetail
          project={project}
          timeline={timeline}
          element={selectedElement}
          tasks={tasks}
          patching={patching || requestInFlight}
          onClose={() => navigate(base)}
          onPatch={patchValue}
          onOpenWorkbench={openElementWorkbench}
        />
      </main>
    </div>
  );
}
