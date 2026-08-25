import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  FolderSearch,
  LayoutList,
  Package,
  Palette,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { navigate, useParams, useSearchParams } from "@/routing/navigation";
import { getInteractiveBundleUrl } from "@/api/creator";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useWorkGraphStore } from "@/store/workGraphStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import {
  selectNarrativeShape,
} from "@/selectors/timelineElementSelectors";
import {
  selectNarrativeEdges,
  selectResearchSlots,
  selectTimelineSummaries,
} from "@/selectors/blueprintSelectors";
import BlueprintStructureArea from "@/components/blueprint/BlueprintStructureArea";
import BlueprintScriptPanel from "@/components/blueprint/BlueprintScriptPanel";
import BlueprintRoughCutStrip from "@/components/blueprint/BlueprintRoughCutStrip";
import BlueprintPrepDrawer, {
  type PrepFocus,
  type PreproductionTab,
} from "@/components/blueprint/BlueprintPrepDrawer";
import PageLoadError from "@/components/PageLoadError";
import PageSkeleton from "@/components/PageSkeleton";

/**
 * Project blueprint — the project's default landing view (plan §4.2).
 * Renders inside ProjectLayout's Outlet: TopNav / AgentDock / polling are
 * provided by the layout, and inline panels only cover the workspace column
 * so the dock stays usable (hard rule §4).
 */
export default function BlueprintPage() {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const query = useSearchParams();
  const project = useProjectSnapshotStore((state) =>
    state.projectId === id ? state.project : null,
  );
  const syncStatus = useProjectSnapshotStore((state) => state.syncStatus);
  const syncError = useProjectSnapshotStore((state) => state.syncError);
  const pollOnce = useProjectSnapshotStore((state) => state.pollOnce);
  const workGraph = useWorkGraphStore((state) =>
    state.projectId === id ? state.graph : null,
  );

  const shape = useMemo(() => selectNarrativeShape(project), [project]);
  const summaries = useMemo(
    () => (project ? selectTimelineSummaries(project) : []),
    [project],
  );
  const edges = useMemo(() => selectNarrativeEdges(project), [project]);
  const researchSlots = useMemo(
    () => (project ? selectResearchSlots(project) : []),
    [project],
  );

  const [selectedTimelineId, setSelectedTimelineId] = useState<string | null>(
    null,
  );
  const [scriptOpen, setScriptOpen] = useState(false);
  const [prepOpen, setPrepOpen] = useState(false);
  const [prepTab, setPrepTab] = useState<PreproductionTab>("visual");
  const [prepFocus, setPrepFocus] = useState<PrepFocus | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [bundleError, setBundleError] = useState(false);

  const exportBundle = async () => {
    setBundleBusy(true);
    setBundleError(false);
    try {
      const response = await fetch(getInteractiveBundleUrl(id));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${id}-interactive.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      setBundleError(true);
    } finally {
      setBundleBusy(false);
    }
  };

  // Deep link from a Review locator (?timeline=…): select + open the node.
  const timelineFromQuery = query.get("timeline");
  useEffect(() => {
    if (!project || !timelineFromQuery) return;
    if (!project.timelines.items[timelineFromQuery]) return;
    setSelectedTimelineId(timelineFromQuery);
    setScriptOpen(true);
    useCreatorInteractionStore
      .getState()
      .select(`timeline:${timelineFromQuery}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timelineFromQuery, project ? 1 : 0]);

  const openScript = (timelineId: string) => {
    setSelectedTimelineId(timelineId);
    setScriptOpen(true);
    // 选中即引用：the node becomes the assistant's context reference.
    useCreatorInteractionStore.getState().select(`timeline:${timelineId}`);
  };
  const openTimeline = (timelineId: string) => {
    navigate(`/project/${id}/t/${encodeURIComponent(timelineId)}/plan`);
  };
  const openElement = (timelineId: string, elementId: string) => {
    navigate(
      `/project/${id}/t/${encodeURIComponent(
        timelineId,
      )}/plan?element=${encodeURIComponent(elementId)}`,
    );
  };
  const openVisualEntity = (entityId: string) => {
    setPrepFocus({ type: "visual", entityId });
    setPrepTab("visual");
    setPrepOpen(true);
    useCreatorInteractionStore.getState().select(`visual-entity:${entityId}`);
  };
  const openResearch = (slotId: string) => {
    setPrepFocus({ type: "research", slotId });
    setPrepTab("research");
    setPrepOpen(true);
    useCreatorInteractionStore.getState().select(`research:${slotId}`);
  };
  const openSource = (sourceId: string) => {
    setPrepFocus({ type: "source", sourceId });
    setPrepTab("research");
    setPrepOpen(true);
  };

  if (!project) {
    if (syncStatus === "invalid" || syncStatus === "not_found") {
      return (
        <PageLoadError
          message={syncError || t("assets.projectReadError")}
          retry={() => void pollOnce(id)}
        />
      );
    }
    return <PageSkeleton type="editor" />;
  }

  const primary = summaries[0] ?? null;
  const entityCount = project.visual.entities.order.length;
  const sourceCount = project.sources.sources.order.length;
  const pendingVisual = project.visual.entities.order.filter((entityId) => {
    const entity = project.visual.entities.items[entityId];
    if (!entity) return false;
    return !(
      entity.selected_artifact_version_id ||
      entity.variants.order.some(
        (variantId) =>
          entity.variants.items[variantId]?.selected_artifact_version_id,
      )
    );
  }).length;
  const runningNodes = (workGraph?.nodes ?? []).filter(
    (node) => node.status === "running",
  );
  const endingCount = (() => {
    if (shape !== "branching") return 0;
    const withOutgoing = new Set(edges.map((edge) => edge.source_timeline_id));
    return summaries.filter(
      (summary) => !withOutgoing.has(summary.timelineId),
    ).length;
  })();

  const chips: Array<{ text: string; warn?: boolean }> =
    shape === "branching"
      ? [
          { text: t("blueprint.chips.branching", { count: summaries.length }) },
          { text: t("blueprint.chips.endings", { count: endingCount }) },
        ]
      : shape === "linear"
      ? [{ text: t("blueprint.chips.linear", { count: summaries.length }) }]
      : [
          {
            text:
              project.scenario === "video_edit"
                ? t("blueprint.chips.videoEdit")
                : t("blueprint.chips.single"),
          },
          ...(project.settings.target_duration_seconds
            ? [
                {
                  text: t("blueprint.chips.duration", {
                    seconds: project.settings.target_duration_seconds,
                  }),
                },
              ]
            : []),
          { text: project.settings.aspect_ratio },
        ];

  return (
    <div
      data-blueprint-page
      className="relative flex h-full min-h-0 flex-col overflow-hidden bg-[var(--color-bg-layout)]"
    >
      {/* Header: shape chips; the single shape keeps the primary timeline CTA. */}
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)]/60 px-5 py-3 backdrop-blur">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold text-[var(--color-text-primary)]">
            {t("nav.blueprint")}
          </h2>
          {chips.map((chip) => (
            <span
              key={chip.text}
              className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold ${
                chip.warn
                  ? "border-[rgba(247,144,9,.5)] bg-[var(--color-warning-soft)] text-[var(--color-warning)]"
                  : "border-[var(--color-border)] bg-[var(--color-bg-primary)] text-[var(--color-text-secondary)]"
              }`}
            >
              {chip.text}
            </span>
          ))}
        </div>
        {shape === "branching" && (
          <span className="flex items-center gap-2">
            {bundleError && (
              <span className="max-w-72 truncate text-[11px] font-medium text-[var(--color-warning)]">
                {t("blueprint.exportBundleFailed")}
              </span>
            )}
            <button
              type="button"
              data-blueprint-export-bundle
              onClick={() => void exportBundle()}
              disabled={bundleBusy}
              className="inline-flex items-center gap-2 rounded-[10px] bg-[var(--color-accent)] px-4 py-2 text-sm font-bold leading-none text-white shadow-[0_2px_8px_rgba(255,127,22,.35)] transition-all hover:-translate-y-px hover:bg-[var(--color-accent-hover)] disabled:cursor-wait disabled:opacity-60"
            >
              <Package className="h-4 w-4" />
              {bundleBusy
                ? t("blueprint.exporting")
                : t("blueprint.exportBundle")}
            </button>
          </span>
        )}
        {shape === "single" && primary && (
          <button
            type="button"
            onClick={() => openTimeline(primary.timelineId)}
            className="inline-flex items-center gap-2 rounded-[10px] bg-[var(--color-accent)] px-5 py-2.5 text-sm font-bold leading-none text-white shadow-[0_2px_8px_rgba(255,127,22,.35)] transition-all hover:-translate-y-px hover:bg-[var(--color-accent-hover)] hover:shadow-[0_4px_14px_rgba(255,127,22,.4)]"
          >
            <LayoutList className="h-4 w-4" />
            {t("blueprint.enterTimeline")}
          </button>
        )}
      </header>

      {/* First screen = the narrative structure itself. */}
      <div className="min-h-0 flex-1 p-4">
        <BlueprintStructureArea
          project={project}
          shape={shape}
          summaries={summaries}
          edges={edges}
          selectedTimelineId={scriptOpen ? selectedTimelineId : null}
          onSelectTimeline={openScript}
          onOpenTimeline={openTimeline}
          onOpenElement={openElement}
          onOpenVisualEntity={openVisualEntity}
          onOpenResearch={openResearch}
          onOpenSource={openSource}
        />
      </div>

      <BlueprintRoughCutStrip project={project} onSelectTimeline={openScript} />

      {/* Bottom: running activity + pre-production entries. */}
      <footer className="flex shrink-0 flex-wrap items-center gap-2.5 border-t border-[var(--color-border)] bg-[var(--color-bg-primary)]/70 px-5 py-2.5 backdrop-blur">
        <span className="flex min-w-0 items-center gap-2.5">
          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-[var(--color-text-secondary)]">
            <Activity className="h-3.5 w-3.5 text-[var(--color-primary,#3b82f6)]" />
            {t("blueprint.running")}
          </span>
          {runningNodes.length > 0 ? (
            runningNodes.slice(0, 3).map((node) => (
              <span
                key={node.id}
                className="inline-flex items-center gap-2 rounded-full border border-[rgba(59,130,246,.3)] bg-[rgba(59,130,246,.06)] px-3 py-1 text-[11px] font-medium text-[var(--color-text-secondary)]"
              >
                {node.label}
                {node.progress != null && (
                  <>
                    <span className="h-1 w-16 overflow-hidden rounded-full bg-[var(--color-bg-secondary)]">
                      <span
                        className="block h-full rounded-full bg-[var(--color-primary,#3b82f6)] transition-[width] duration-500"
                        style={{
                          width: `${Math.round(node.progress * 100)}%`,
                        }}
                      />
                    </span>
                    <span className="tabular-nums text-[var(--color-primary,#3b82f6)]">
                      {Math.round(node.progress * 100)}%
                    </span>
                  </>
                )}
              </span>
            ))
          ) : (
            <span className="text-[11px] text-[var(--color-text-tertiary)]">
              {t("blueprint.runningEmpty")}
            </span>
          )}
        </span>
        <span className="mx-1 hidden h-5 w-px bg-[var(--color-border)] sm:block" />
        {entityCount > 0 && (
          <button
            type="button"
            onClick={() => {
              setPrepFocus(null);
              setPrepTab("visual");
              setPrepOpen(true);
            }}
            className="group inline-flex items-center gap-2 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3.5 py-1.5 text-xs font-semibold text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
          >
            <Palette className="h-3.5 w-3.5" />
            {t("blueprint.visualEntry", { count: entityCount })}
            {pendingVisual > 0 && (
              <span className="rounded-full bg-[var(--color-warning-soft)] px-2 py-0.5 text-[10px] font-bold text-[var(--color-warning)]">
                {t("blueprint.pendingCount", { count: pendingVisual })}
              </span>
            )}
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            setPrepFocus(null);
            setPrepTab("research");
            setPrepOpen(true);
          }}
          className="group inline-flex items-center gap-2 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3.5 py-1.5 text-xs font-semibold text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
        >
          <FolderSearch className="h-3.5 w-3.5" />
          {t("blueprint.researchEntry", {
            count: researchSlots.length + sourceCount,
          })}
        </button>
        <span className="ml-auto text-[11px] text-[var(--color-text-tertiary)]">
          {t("blueprint.footerHint")}
        </span>
      </footer>

      {/* Inline panels: only the workspace column, AgentDock stays visible. */}
      <BlueprintScriptPanel
        project={project}
        projectId={id}
        timelineId={selectedTimelineId}
        open={scriptOpen}
        onClose={() => setScriptOpen(false)}
        onOpenTimeline={openTimeline}
        onOpenVisualEntity={openVisualEntity}
      />
      <BlueprintPrepDrawer
        project={project}
        projectId={id}
        open={prepOpen}
        tab={prepTab}
        focus={prepFocus}
        onClose={() => {
          setPrepOpen(false);
          setPrepFocus(null);
        }}
        onTabChange={setPrepTab}
      />
    </div>
  );
}
