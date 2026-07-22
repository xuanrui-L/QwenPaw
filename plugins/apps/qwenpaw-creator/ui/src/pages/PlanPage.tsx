import { useCallback, useEffect, useMemo, useState } from "react";
import { message } from "antd";
import { Plus, Scissors, Sparkles } from "lucide-react";
import { navigate, useParams, useSearchParams } from "@/routing/navigation";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import {
  selectPrimaryTimeline,
  timelineEndTick,
} from "@/selectors/timelineElementSelectors";
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

  const focusAgent = useCallback((ref: string, prompt: string) => {
    useCreatorInteractionStore.getState().select(ref);
    useAgentDockUiStore.getState().setOpen(true);
    useAgentDockUiStore.getState().setTab("conversation");
    useAgentDockUiStore.getState().setDraft(prompt);
  }, []);

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
  const removeElement = async (element: TimelineElementDocument) => {
    await patchProject(id, [
      {
        op: "remove",
        path: projectJsonPointer(
          "timelines",
          "items",
          timeline.timeline_id,
          "elements_by_id",
          element.element_id,
        ),
        before: element,
      },
    ]);
    navigate(base);
    message.success("时间线内容已删除");
  };
  const openElementAgent = (
    element: TimelineElementDocument,
    instruction?: string,
  ) => {
    focusAgent(
      `element:${element.element_id}`,
      instruction ||
        `请修改时间线内容「${
          element.label || "当前内容"
        }」，先读取现有内容并说明计划。`,
    );
  };
  const timelineTargetRef = `timeline:${timeline.timeline_id}`;
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
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)]"
            onClick={() =>
              focusAgent(
                timelineTargetRef,
                "请在当前时间轴中添加新的内容。先根据项目目标判断内容类型、时间位置和持续时长，再更新视频方案。",
              )
            }
          >
            <Plus className="h-3.5 w-3.5" />
            添加内容
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)]"
            onClick={() =>
              focusAgent(
                timelineTargetRef,
                "请检查当前时间轴中的全部内容和制作结果，满足条件后渲染最终成片。",
              )
            }
          >
            <Scissors className="h-3.5 w-3.5" />
            生成成片
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-accent)] bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-white transition hover:border-[var(--color-accent-hover)] hover:bg-[var(--color-accent-hover)]"
            onClick={() =>
              focusAgent(
                timelineTargetRef,
                "请根据当前项目目标规划并完善整条时间轴，用清晰的时间范围、画面位置、叠放关系和制作方式表达全部内容。",
              )
            }
          >
            <Sparkles className="h-3.5 w-3.5" />
            Agent 规划
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
        previewOpen={previewOpen}
        tasks={tasks}
        onPreviewOpenChange={setPreviewOpen}
        onPlayheadChange={(tick) =>
          setPlayheadTick(Math.max(0, Math.min(displayDurationTick, tick)))
        }
        onSelectElement={selectElement}
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
          onDelete={removeElement}
          onAgent={openElementAgent}
          onOpenWorkbench={openElementWorkbench}
        />
      </main>
    </div>
  );
}
