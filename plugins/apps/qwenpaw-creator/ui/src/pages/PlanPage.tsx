import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { message, Modal } from "antd";
import { Download, Loader2, RefreshCw } from "lucide-react";
import { navigate, useParams, useSearchParams } from "@/routing/navigation";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { getArtifactVersionMediaUrl, renderTimeline } from "@/api/creator";
import {
  resolveTimelineRender,
  selectPrimaryTimeline,
  timelineEndTick,
} from "@/selectors/timelineElementSelectors";
import { resolveElementPlayback } from "@/selectors/elementPlaybackSelectors";
import { projectJsonPointer } from "@/lib/projectJsonPointer";
import { useReviewFieldFocus } from "@/routing/reviewFocus";
import { useProjectDraft } from "@/lib/useProjectDraft";
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
  const elementDraft = useProjectDraft(
    selectedElement,
    `${id}:${timeline?.timeline_id ?? "missing"}:${selectedElementId ?? "none"}:detail`,
    [
      "timelines",
      "items",
      timeline?.timeline_id ?? "missing",
      "elements_by_id",
      selectedElementId ?? "none",
    ],
  );
  const [playheadTick, setPlayheadTick] = useState(0);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [composing, setComposing] = useState(false);
  const [composeFailed, setComposeFailed] = useState(false);
  const composeAttemptedGeneration = useRef<number | null>(null);
  const generation = useProjectSnapshotStore((state) => state.generation);
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
    if (!elementDraft.dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [elementDraft.dirty]);

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
  const leaveDraft = useCallback(
    (next: () => void) => {
      if (!elementDraft.dirty) {
        next();
        return;
      }
      Modal.confirm({
        title: "还有未应用的修改",
        content: "离开后这些页面草稿会被放弃。你也可以先返回并点击“应用修改”。",
        okText: "放弃并离开",
        okButtonProps: { danger: true },
        cancelText: "继续编辑",
        onOk: () => {
          elementDraft.discard();
          next();
        },
      });
    },
    [elementDraft],
  );
  const selectElement = useCallback(
    (elementId: string) => {
      leaveDraft(() => {
        const element = timeline?.elements_by_id[elementId];
        if (element) {
          setPlayheadTick((currentTick) => {
            const startTick = element.span.start_tick;
            const endTick = startTick + element.span.duration_tick;
            return currentTick >= startTick && currentTick < endTick
              ? currentTick
              : startTick;
          });
        }
        navigate(
          selectedElementId === elementId
            ? base
            : `${base}?element=${encodeURIComponent(elementId)}`,
        );
      });
    },
    [base, leaveDraft, selectedElementId, timeline],
  );

  // 就绪口径：主轨画面与依赖生成结果的 motion/media overlay 必须就绪；
  // 文案 overlay 由合成器确定性绘制，transition/audio 不需要独立生成。
  const readiness = useMemo(() => {
    if (!project || !timeline) return { total: 0, notReady: 0 };
    const items = Object.values(timeline.elements_by_id).filter(
      (element) =>
        element.enabled &&
        (element.creation.type === "r2v" ||
          element.creation.type === "edit" ||
          (element.creation.type === "overlay" &&
            ["motion", "media"].includes(element.creation.overlay_kind))),
    );
    return {
      total: items.length,
      notReady: items.filter(
        (element) =>
          resolveElementPlayback(project, timeline, element, tasks).status !==
          "ready",
      ).length,
    };
  }, [project, tasks, timeline]);
  const renderOutput = useMemo(
    () =>
      project && timeline ? resolveTimelineRender(project, timeline) : null,
    [project, timeline],
  );
  const freshRender =
    renderOutput?.selected && !renderOutput.selected.stale
      ? renderOutput.selected
      : null;
  const allReady = readiness.total > 0 && readiness.notReady === 0;

  const composeNow = useCallback(async () => {
    if (!timeline) return;
    setComposing(true);
    setComposeFailed(false);
    try {
      // 合成是确定性后端操作；完成后立即拉新快照，预览自动切成片。
      await renderTimeline(id, timeline.timeline_id);
      await pollOnce(id);
    } catch (error) {
      setComposeFailed(true);
      message.error(`成片合成失败：${(error as Error).message}`);
    } finally {
      setComposing(false);
    }
  }, [id, pollOnce, timeline]);

  // 全部主轨元素就绪且没有新鲜成片时自动合成；同一 generation 只尝试
  // 一次（失败不自动重试，留手动重试入口）；短防抖吸收连续编辑。
  useEffect(() => {
    if (!allReady || freshRender || composing) return;
    if (
      generation !== null &&
      composeAttemptedGeneration.current === generation
    )
      return;
    const timer = window.setTimeout(() => {
      composeAttemptedGeneration.current = generation;
      void composeNow();
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [allReady, freshRender, composing, generation, composeNow]);

  const downloadRender = useCallback(() => {
    if (!freshRender) return;
    const link = document.createElement("a");
    link.href = getArtifactVersionMediaUrl(freshRender.version_id);
    link.download = `${freshRender.name || project?.name || "成片"}.mp4`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }, [freshRender, project?.name]);

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

  const applyElementDraft = async () => {
    const draft = elementDraft.value;
    if (!draft || !elementDraft.operations.length) return;
    if (
      draft.creation.type === "overlay" &&
      ["pet_os", "interview_summary"].includes(draft.creation.overlay_kind) &&
      !draft.creation.text.trim()
    ) {
      message.error("文案类 Overlay 的文本不能为空");
      return;
    }
    try {
      const response = await patchProject(id, elementDraft.operations);
      elementDraft.markApplied();
      if (response.editImpact?.regenerationRequired) {
        message.success("修改已应用；相关生成结果已标记为需要重新生成");
      } else if (response.editImpact?.renderTimelineIds.length) {
        message.success("修改已应用；实时预览已更新，成片将重新合成");
      } else {
        message.success("修改已应用");
      }
    } catch (error) {
      message.error(`应用修改失败：${(error as Error).message}`);
    }
  };
  const closeElementDetail = () => leaveDraft(() => navigate(base));
  const openElementWorkbench = (element: TimelineElementDocument) =>
    leaveDraft(() =>
      navigate(`${base}/element/${encodeURIComponent(element.element_id)}`),
    );

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
          {composeFailed && !composing && (
            <button
              type="button"
              title="上次自动合成失败，点击重新合成"
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-danger)]/50 bg-[var(--color-danger-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--color-danger)] transition hover:border-[var(--color-danger)]"
              onClick={() => void composeNow()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              重试合成
            </button>
          )}
          <button
            type="button"
            data-download-render
            disabled={!freshRender}
            title={
              freshRender
                ? "下载成片视频文件"
                : composing
                  ? "正在合成成片，完成后可下载"
                  : readiness.total === 0
                    ? "时间轴还没有可合成的画面内容"
                    : readiness.notReady > 0
                      ? `还有 ${readiness.notReady} 项内容生成中，全部就绪后自动合成`
                      : "等待成片合成"
            }
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:border-[var(--color-border)] disabled:hover:bg-[var(--color-bg-primary)]"
            onClick={downloadRender}
          >
            {composing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            {composing ? "合成中…" : "下载成片"}
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
          element={elementDraft.value}
          tasks={tasks}
          applying={patching}
          dirtyCount={elementDraft.dirtyCount}
          conflictPaths={elementDraft.conflictPaths}
          onClose={closeElementDetail}
          onChange={(mutator) =>
            elementDraft.update((draft) => {
              if (draft) mutator(draft);
            })
          }
          onApply={() => void applyElementDraft()}
          onDiscard={elementDraft.discard}
          onAcceptConflicts={elementDraft.acceptConflicts}
          onOpenWorkbench={openElementWorkbench}
        />
      </main>
    </div>
  );
}
