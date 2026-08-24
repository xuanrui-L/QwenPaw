import { useEffect, useMemo, useState } from "react";
import { Tabs, message } from "antd";
import { ArrowLeft, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  ProjectDocument,
  VisualEntityDocument,
} from "@/contracts/creator";
import { getArtifactVersionMediaUrl } from "@/api/creator";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import {
  selectResearchSlots,
  type ResolvedSlot,
} from "@/selectors/blueprintSelectors";
import { TONE_CHIP, TONE_TEXT } from "./tones";

export type PreproductionTab = "visual" | "research";

export type PrepFocus =
  | { type: "visual"; entityId: string }
  | { type: "research"; slotId: string }
  | { type: "source"; sourceId: string };

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="mb-1.5 block text-[10px] font-semibold uppercase tracking-wide text-[var(--color-text-tertiary)]">
      {children}
    </span>
  );
}

function KvLines({ kv }: { kv: Array<[string, string]> }) {
  return (
    <div>
      {kv.map(([key, value]) => (
        <div
          key={key}
          className="flex justify-between gap-2.5 border-b border-dashed border-[var(--color-border)] py-1.5 text-xs last:border-b-0"
        >
          <span className="shrink-0 text-[var(--color-text-tertiary)]">
            {key}
          </span>
          <span className="break-all text-right text-[var(--color-text-primary)]">
            {value}
          </span>
        </div>
      ))}
    </div>
  );
}

function entitySelectedVersionId(entity: VisualEntityDocument): string | null {
  if (entity.selected_artifact_version_id)
    return entity.selected_artifact_version_id;
  for (const variantId of entity.variants.order) {
    const versionId =
      entity.variants.items[variantId]?.selected_artifact_version_id;
    if (versionId) return versionId;
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Visual entity detail                                                 */
/* ------------------------------------------------------------------ */

function VisualDetail({
  project,
  projectId,
  entity,
  onBack,
}: {
  project: ProjectDocument;
  projectId: string;
  entity: VisualEntityDocument;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const patchProject = useProjectSnapshotStore((state) => state.patch);
  const patching = useProjectSnapshotStore((state) => state.patching);
  const primaryVariantId = entity.variants.order[0] ?? null;
  const primaryVariant = primaryVariantId
    ? entity.variants.items[primaryVariantId]
    : null;
  const selectedVersionId = entitySelectedVersionId(entity);
  const imageUrl = selectedVersionId
    ? getArtifactVersionMediaUrl(selectedVersionId)
    : null;
  const versionIds = primaryVariant?.generated_artifact_version_ids ?? [];
  const [promptDraft, setPromptDraft] = useState(primaryVariant?.prompt ?? "");
  useEffect(() => {
    setPromptDraft(primaryVariant?.prompt ?? "");
  }, [primaryVariant?.prompt, entity.entity_id]);
  const promptDirty = promptDraft !== (primaryVariant?.prompt ?? "");

  const savePrompt = async () => {
    if (!primaryVariant || !primaryVariantId) return;
    try {
      await patchProject(projectId, [
        {
          op: "replace",
          path: `/visual/entities/items/${entity.entity_id}/variants/items/${primaryVariantId}/prompt`,
          before: primaryVariant.prompt,
          value: promptDraft,
        },
      ]);
      message.success(t("blueprint.promptSaved"));
    } catch (error) {
      message.error(
        t("blueprint.promptSaveFailed", { detail: (error as Error).message }),
      );
    }
  };

  return (
    <div className="panel-enter flex h-full min-h-0 flex-col">
      <button
        type="button"
        onClick={onBack}
        className="mb-3 inline-flex w-fit items-center gap-1.5 rounded-md px-1.5 py-1 text-xs font-semibold text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text-primary)]"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        {t("blueprint.backToList")}
      </button>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-2">
        <div className="flex min-h-[220px] items-end overflow-hidden rounded-[10px] border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-0">
          {imageUrl ? (
            <img
              src={imageUrl}
              alt={entity.name}
              className="h-full w-full object-cover"
            />
          ) : (
            <span className="p-2.5 text-xs text-[var(--color-text-tertiary)]">
              {t("blueprint.noDesignYet")}
            </span>
          )}
        </div>
        {versionIds.length > 0 && (
          <div>
            <FieldLabel>{t("blueprint.versions")}</FieldLabel>
            <div className="flex flex-wrap gap-1.5">
              {versionIds.map((versionId, index) => (
                <span
                  key={versionId}
                  className={`inline-flex h-[26px] items-center rounded-full border px-3 text-[11px] font-semibold ${
                    versionId === selectedVersionId
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                      : "border-[var(--color-border)] bg-[var(--color-bg-primary)] text-[var(--color-text-secondary)]"
                  }`}
                >
                  v{index + 1}
                </span>
              ))}
            </div>
          </div>
        )}
        <div>
          <FieldLabel>{t("blueprint.infoLabel")}</FieldLabel>
          <KvLines
            kv={[
              [
                t("blueprint.entityKind"),
                t(`blueprint.entityKinds.${entity.kind}`),
              ],
              [t("blueprint.continuity"), entity.continuity || "—"],
              [
                t("blueprint.variantCount"),
                String(entity.variants.order.length),
              ],
            ]}
          />
        </div>
        {primaryVariant && (
          <div>
            <FieldLabel>{t("blueprint.designPrompt")}</FieldLabel>
            <textarea
              data-creator-field={`visual-entity:${entity.entity_id}/prompt`}
              data-creator-field-label={`${entity.name} · Prompt`}
              data-creator-path={`/visual/entities/items/${entity.entity_id}/variants/items/${primaryVariantId}/prompt`}
              value={promptDraft}
              onChange={(event) => setPromptDraft(event.target.value)}
              className="min-h-[96px] w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-2 text-xs leading-relaxed text-[var(--color-text-primary)] outline-none transition-colors focus:border-[var(--color-accent)] focus:shadow-[0_0_0_2px_rgba(255,127,22,.1)]"
            />
          </div>
        )}
        <div className="mt-auto flex items-center gap-2 pt-1">
          {promptDirty && (
            <button
              type="button"
              className="btn-secondary shrink-0"
              disabled={patching}
              onClick={() => void savePrompt()}
            >
              {t("blueprint.savePrompt")}
            </button>
          )}
          <span className="text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
            {t("blueprint.visualApproveHint")}
          </span>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Research report detail                                               */
/* ------------------------------------------------------------------ */

function ResearchDetail({
  entry,
  onBack,
}: {
  entry: ResolvedSlot;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const versionId = entry.selected?.version_id ?? null;
  useEffect(() => {
    if (!versionId) return;
    let cancelled = false;
    fetch(getArtifactVersionMediaUrl(versionId))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((body) => {
        if (!cancelled) setText(body);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [versionId]);

  return (
    <div className="panel-enter flex h-full min-h-0 flex-col">
      <button
        type="button"
        onClick={onBack}
        className="mb-3 inline-flex w-fit items-center gap-1.5 rounded-md px-1.5 py-1 text-xs font-semibold text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text-primary)]"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        {t("blueprint.backToList")}
      </button>
      <div
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pb-2"
        data-creator-field={`research:${entry.slot.slot_id}/conclusion`}
        data-creator-field-label={
          entry.selected?.name || t("blueprint.researchReport")
        }
        data-creator-path={
          entry.selected
            ? `artifact:${entry.slot.slot_id}@${entry.selected.version_id}`
            : undefined
        }
      >
        <FieldLabel>{t("blueprint.researchConclusion")}</FieldLabel>
        {text !== null ? (
          <div className="whitespace-pre-wrap text-xs leading-relaxed text-[var(--color-text-primary)]">
            {text}
          </div>
        ) : failed ? (
          <p className="text-xs text-[var(--color-text-tertiary)]">
            {t("blueprint.researchLoadFailed")}
          </p>
        ) : versionId ? (
          <p className="text-xs text-[var(--color-text-tertiary)]">
            {t("blueprint.scriptLoading")}
          </p>
        ) : (
          <p className="text-xs text-[var(--color-text-tertiary)]">
            {t("blueprint.researchRunning")}
          </p>
        )}
        <p className="mt-auto pt-1 text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
          {t("blueprint.researchApproveHint")}
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Source understanding detail                                          */
/* ------------------------------------------------------------------ */

function SourceDetail({
  project,
  sourceId,
  onBack,
}: {
  project: ProjectDocument;
  sourceId: string;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const source = project.sources.sources.items[sourceId];
  const intelligence = source?.current_intelligence_version_id
    ? project.assets.intelligence_versions_by_id[
        source.current_intelligence_version_id
      ]
    : null;
  const version = source
    ? project.assets.source_versions_by_id[source.selected_asset_version_id]
    : null;
  if (!source) return null;
  return (
    <div className="panel-enter flex h-full min-h-0 flex-col">
      <button
        type="button"
        onClick={onBack}
        className="mb-3 inline-flex w-fit items-center gap-1.5 rounded-md px-1.5 py-1 text-xs font-semibold text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text-primary)]"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        {t("blueprint.backToList")}
      </button>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-2">
        <div>
          <FieldLabel>{t("blueprint.sourceSummary")}</FieldLabel>
          <KvLines
            kv={[
              [t("common.name"), source.display_name || source.source_id],
              [
                t("blueprint.mediaKind"),
                version?.media_kind ?? "—",
              ],
              [
                t("common.duration"),
                version?.duration_seconds != null
                  ? `${version.duration_seconds}s`
                  : "—",
              ],
              [
                t("blueprint.understandingState"),
                intelligence
                  ? t("blueprint.board.sourceUnderstood")
                  : t("blueprint.board.sourcePending"),
              ],
            ]}
          />
        </div>
        {intelligence && Object.keys(intelligence.coverage).length > 0 && (
          <div>
            <FieldLabel>{t("blueprint.coverage")}</FieldLabel>
            <KvLines
              kv={Object.entries(intelligence.coverage).map(
                ([key, value]) => [key, String(value)] as [string, string],
              )}
            />
          </div>
        )}
        {source.user_notes && (
          <div className="rounded-r-lg border-l-[3px] border-[var(--color-accent)] bg-[var(--color-accent-soft)] px-3 py-2 text-xs text-[var(--color-text-secondary)]">
            {source.user_notes}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Drawer                                                               */
/* ------------------------------------------------------------------ */

interface BlueprintPrepDrawerProps {
  project: ProjectDocument;
  projectId: string;
  open: boolean;
  tab: PreproductionTab;
  focus: PrepFocus | null;
  onClose: () => void;
  onTabChange: (tab: PreproductionTab) => void;
}

/**
 * Pre-production drawer (visual development / research & sources). An
 * in-workspace overlay: the AgentDock column stays visible and usable.
 */
export default function BlueprintPrepDrawer({
  project,
  projectId,
  open,
  tab,
  focus,
  onClose,
  onTabChange,
}: BlueprintPrepDrawerProps) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<PrepFocus | null>(null);
  useEffect(() => {
    setDetail(focus);
  }, [focus, open]);

  const entities = useMemo(
    () =>
      project.visual.entities.order
        .map((entityId) => project.visual.entities.items[entityId])
        .filter(Boolean),
    [project],
  );
  const research = useMemo(() => selectResearchSlots(project), [project]);
  const sources = useMemo(
    () =>
      project.sources.sources.order
        .map((sourceId) => project.sources.sources.items[sourceId])
        .filter(Boolean),
    [project],
  );

  if (!open) return null;

  const openDetail = (next: PrepFocus, ref: string) => {
    useCreatorInteractionStore.getState().select(ref);
    setDetail(next);
  };

  const detailNode = (() => {
    if (!detail) return null;
    if (detail.type === "visual") {
      const entity = project.visual.entities.items[detail.entityId];
      return entity ? (
        <VisualDetail
          project={project}
          projectId={projectId}
          entity={entity}
          onBack={() => setDetail(null)}
        />
      ) : null;
    }
    if (detail.type === "research") {
      const entry = research.find(
        (candidate) => candidate.slot.slot_id === detail.slotId,
      );
      return entry ? (
        <ResearchDetail entry={entry} onBack={() => setDetail(null)} />
      ) : null;
    }
    return (
      <SourceDetail
        project={project}
        sourceId={detail.sourceId}
        onBack={() => setDetail(null)}
      />
    );
  })();

  return (
    <div
      data-blueprint-prep-drawer
      className="absolute inset-0 z-30 flex justify-end bg-[rgba(20,16,12,.18)]"
      onClick={onClose}
    >
      <div
        className="panel-enter flex h-full w-[min(560px,92%)] flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-card)] shadow-[-8px_0_28px_rgba(0,0,0,.08)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <span className="text-sm font-semibold text-[var(--color-text-primary)]">
            {t("blueprint.prepTitle")}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="icon-button !h-7 !w-7"
            title={t("blueprint.closeKeepRef")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-5 pb-5 pt-2">
          {detailNode ?? (
            <Tabs
              activeKey={tab}
              onChange={(key) => onTabChange(key as PreproductionTab)}
              items={[
                ...(entities.length
                  ? [
                      {
                        key: "visual",
                        label: t("blueprint.visualTab", {
                          count: entities.length,
                        }),
                        children: (
                          <div className="grid grid-cols-2 gap-2.5 pt-2">
                            {entities.map((entity) => {
                              const versionId =
                                entitySelectedVersionId(entity);
                              const pending = !versionId;
                              return (
                                <button
                                  key={entity.entity_id}
                                  type="button"
                                  onClick={() =>
                                    openDetail(
                                      {
                                        type: "visual",
                                        entityId: entity.entity_id,
                                      },
                                      `visual-entity:${entity.entity_id}`,
                                    )
                                  }
                                  className={`overflow-hidden rounded-[10px] border bg-[var(--color-bg-card)] text-left transition-all hover:border-[var(--color-accent)] hover:shadow-[var(--shadow-sm)] ${
                                    pending
                                      ? "border-[rgba(247,144,9,.55)]"
                                      : "border-[var(--color-border)]"
                                  }`}
                                >
                                  <div className="relative flex h-[108px] items-end overflow-hidden bg-[var(--color-bg-secondary)]">
                                    {versionId && (
                                      <img
                                        src={getArtifactVersionMediaUrl(
                                          versionId,
                                        )}
                                        alt={entity.name}
                                        loading="lazy"
                                        className="h-full w-full object-cover"
                                      />
                                    )}
                                    <span className="absolute right-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[9px] font-bold text-white">
                                      {t(
                                        `blueprint.entityKinds.${entity.kind}`,
                                      )}
                                    </span>
                                  </div>
                                  <div className="px-2.5 py-2">
                                    <b className="block truncate text-[11px] font-semibold text-[var(--color-text-primary)]">
                                      {entity.name}
                                    </b>
                                    <span
                                      className={`text-[10px] ${
                                        pending
                                          ? TONE_TEXT.wait
                                          : TONE_TEXT.done
                                      }`}
                                    >
                                      {pending
                                        ? t("blueprint.board.visualPending")
                                        : t("blueprint.board.visualReady")}
                                    </span>
                                  </div>
                                </button>
                              );
                            })}
                          </div>
                        ),
                      },
                    ]
                  : []),
                {
                  key: "research",
                  label: t("blueprint.researchTab", {
                    count: research.length + sources.length,
                  }),
                  children: (
                    <div className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-card)]">
                      {research.map((entry) => (
                        <button
                          key={entry.slot.slot_id}
                          type="button"
                          onClick={() =>
                            openDetail(
                              {
                                type: "research",
                                slotId: entry.slot.slot_id,
                              },
                              `research:${entry.slot.slot_id}`,
                            )
                          }
                          className="flex w-full items-start gap-2.5 border-b border-[var(--color-border)] px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-[var(--color-bg-secondary)]"
                        >
                          <span className="min-w-0 flex-1">
                            <b className="block truncate text-xs font-semibold text-[var(--color-text-primary)]">
                              {entry.selected?.name ||
                                String(
                                  entry.slot.metadata.topic ||
                                    t("blueprint.researchReport"),
                                )}
                            </b>
                            <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-[var(--color-text-secondary)]">
                              {t("blueprint.researchSummary")}
                            </p>
                          </span>
                          <span
                            className={`shrink-0 rounded px-1.5 text-[10px] font-semibold leading-[18px] ${
                              entry.selected ? TONE_CHIP.done : TONE_CHIP.run
                            }`}
                          >
                            {entry.selected
                              ? t("blueprint.board.researchReady")
                              : t("blueprint.board.researchRunning")}
                          </span>
                        </button>
                      ))}
                      {sources.map((source) => (
                        <button
                          key={source.source_id}
                          type="button"
                          onClick={() =>
                            openDetail(
                              {
                                type: "source",
                                sourceId: source.source_id,
                              },
                              `asset-version:${source.selected_asset_version_id}`,
                            )
                          }
                          className="flex w-full items-start gap-2.5 border-b border-[var(--color-border)] px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-[var(--color-bg-secondary)]"
                        >
                          <span className="min-w-0 flex-1">
                            <b className="block truncate text-xs font-semibold text-[var(--color-text-primary)]">
                              {source.display_name || source.source_id}
                            </b>
                            <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-[var(--color-text-secondary)]">
                              {source.user_notes ||
                                t("blueprint.sourceUnderstanding")}
                            </p>
                          </span>
                          <span
                            className={`shrink-0 rounded px-1.5 text-[10px] font-semibold leading-[18px] ${
                              source.current_intelligence_version_id
                                ? TONE_CHIP.done
                                : TONE_CHIP.wait
                            }`}
                          >
                            {source.current_intelligence_version_id
                              ? t("blueprint.board.sourceUnderstood")
                              : t("blueprint.board.sourcePending")}
                          </span>
                        </button>
                      ))}
                      {!research.length && !sources.length && (
                        <p className="px-3 py-4 text-center text-xs text-[var(--color-text-tertiary)]">
                          {t("blueprint.researchEmpty")}
                        </p>
                      )}
                    </div>
                  ),
                },
              ]}
            />
          )}
        </div>
      </div>
    </div>
  );
}
