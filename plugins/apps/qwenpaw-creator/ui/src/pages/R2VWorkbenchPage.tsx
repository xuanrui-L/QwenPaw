import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Input, Select, message } from "antd";
import {
  AlertTriangle,
  ArrowLeft,
  Clapperboard,
  Image as ImageIcon,
  Video,
  Wand2,
} from "lucide-react";
import { navigate, useParams } from "@/routing/navigation";
import {
  useProjectSnapshotStore,
  type ProjectEditOperation,
} from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { selectPrimaryTimeline } from "@/selectors/timelineElementSelectors";
import { getArtifactVersionMediaUrl, getResolvedModels } from "@/api/creator";
import { projectJsonPointer } from "@/lib/projectJsonPointer";
import PageSkeleton from "@/components/PageSkeleton";
import PageLoadError from "@/components/PageLoadError";
import ShotList from "@/components/workbench/ShotList";
import ArtifactVersionChips from "@/components/workbench/ArtifactVersionChips";
import type {
  ArtifactSlotDocument,
  ArtifactVersionDocument,
  ProjectDocument,
  TaskView,
} from "@/contracts/creator";

const { TextArea } = Input;

type ReferenceField = "scene" | "characters" | "props" | "sources";

const FIELD_LABEL: Record<ReferenceField, string> = {
  scene: "场景",
  characters: "角色",
  props: "道具",
  sources: "素材",
};

function Panel({
  title,
  badge,
  children,
}: {
  title: string;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-card)]">
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2.5">
        <h4 className="text-xs font-bold text-[var(--color-text-secondary)]">
          {title}
        </h4>
        {badge}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function PromptTextArea({
  label,
  value,
  field,
  path,
  onCommit,
}: {
  label: string;
  value: string;
  field: string;
  path: string;
  onCommit: (value: string) => Promise<unknown>;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <div
      data-creator-field={field}
      data-creator-path={path}
      data-creator-field-label={label}
    >
      <p className="mb-1 text-[11px] font-medium text-[var(--color-text-tertiary)]">
        {label}
      </p>
      <TextArea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          if (draft === value) return;
          void onCommit(draft).catch(() => setDraft(value));
        }}
        autoSize={{ minRows: 2, maxRows: 10 }}
        placeholder={`生成${label}后可在此编辑…`}
        className="!rounded-lg !border-[var(--color-border)] !bg-[var(--color-bg-secondary)] !text-xs"
      />
    </div>
  );
}

function versionsOfSlot(
  project: ProjectDocument,
  slot: ArtifactSlotDocument | null,
): ArtifactVersionDocument[] {
  if (!slot) return [];
  return slot.version_ids
    .map((versionId) => project.assets.artifact_versions_by_id[versionId])
    .filter((version): version is ArtifactVersionDocument => Boolean(version));
}

function mediaUrlOf(
  project: ProjectDocument,
  version: ArtifactVersionDocument | null,
  mediaPrefix: string,
): string | null {
  if (!version) return null;
  const file = project.assets.files_by_id[version.file_id];
  return file?.media_type.startsWith(mediaPrefix)
    ? getArtifactVersionMediaUrl(version.version_id)
    : null;
}

function visualEntityName(project: ProjectDocument, ref: string): string {
  const entityId = ref.replace(/^visual-entity:/, "");
  return project.visual.entities.items[entityId]?.name ?? ref;
}

function referenceVersionName(
  project: ProjectDocument,
  versionId: string,
): string {
  return (
    project.assets.artifact_versions_by_id[versionId]?.name ??
    project.assets.source_versions_by_id[versionId]?.name ??
    versionId
  );
}

export default function R2VWorkbenchPage() {
  const { id = "", elementId = "" } = useParams();
  const project = useProjectSnapshotStore((state) =>
    state.projectId === id ? state.project : null,
  );
  const syncStatus = useProjectSnapshotStore((state) => state.syncStatus);
  const syncError = useProjectSnapshotStore((state) => state.syncError);
  const patchProject = useProjectSnapshotStore((state) => state.patch);
  const pollOnce = useProjectSnapshotStore((state) => state.pollOnce);
  const tasks = useCreatorTaskViewStore((state) => state.tasks);
  const refreshTasks = useCreatorTaskViewStore((state) => state.refresh);
  const timeline = useMemo(() => selectPrimaryTimeline(project), [project]);
  const element = timeline?.elements_by_id[elementId] ?? null;
  const creation = element?.creation.type === "r2v" ? element.creation : null;
  const [viewedSbId, setViewedSbId] = useState<string | null>(null);
  const [viewedVideoId, setViewedVideoId] = useState<string | null>(null);
  const [resolvedVideoModel, setResolvedVideoModel] = useState<string | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    getResolvedModels()
      .then((resolved) => {
        if (!cancelled) setResolvedVideoModel(resolved.video.model || null);
      })
      .catch(() => {
        /* best-effort: fall back to recipe.model below */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    useCreatorInteractionStore
      .getState()
      .select(element ? `element:${element.element_id}` : null);
  }, [element]);
  useEffect(() => {
    setViewedSbId(null);
    setViewedVideoId(null);
  }, [elementId]);

  const planPath = `/project/${id}/plan`;
  const backToPlan = useCallback(
    () =>
      navigate(
        element
          ? `${planPath}?element=${encodeURIComponent(element.element_id)}`
          : planPath,
      ),
    [element, planPath],
  );
  const focusAgent = useCallback(
    (prompt: string) => {
      useCreatorInteractionStore.getState().select(`element:${elementId}`);
      useAgentDockUiStore.getState().setOpen(true);
      useAgentDockUiStore.getState().setTab("conversation");
      useAgentDockUiStore.getState().setDraft(prompt);
    },
    [elementId],
  );

  if (!project || !timeline) {
    if (syncStatus === "invalid" || syncStatus === "not_found") {
      return (
        <PageLoadError
          message={syncError || "Project 无法读取"}
          retry={() => void pollOnce(id)}
        />
      );
    }
    return <PageSkeleton type="editor" />;
  }
  if (!element || !creation) {
    return (
      <div className="flex h-full items-center justify-center bg-[var(--color-bg-layout)] px-6">
        <div className="max-w-sm text-center">
          <p className="text-sm font-semibold text-[var(--color-text-primary)]">
            {element
              ? "该时间线内容不是 AI 生成画面，没有独立工作台"
              : "时间线中找不到这项内容"}
          </p>
          <button
            type="button"
            onClick={() => navigate(planPath, true)}
            className="mt-4 rounded border border-[var(--color-border)] bg-white px-3 py-1.5 text-sm font-medium text-[var(--color-text-primary)] hover:bg-[var(--color-bg-secondary)]"
          >
            返回方案
          </button>
        </div>
      </div>
    );
  }

  const elementLabel = element.label || element.element_id;
  const elementPointer = (...segments: Array<string | number>) =>
    projectJsonPointer(
      "timelines",
      "items",
      timeline.timeline_id,
      "elements_by_id",
      element.element_id,
      ...segments,
    );
  const patchOps = (operations: ProjectEditOperation[]) =>
    patchProject(id, operations).catch((error) => {
      message.error((error as Error).message);
      throw error;
    });
  const patchField = (
    segments: Array<string | number>,
    before: unknown,
    value: unknown,
  ) =>
    patchOps([
      { op: "replace", path: elementPointer(...segments), before, value },
    ]);

  const slotOf = (name: string): ArtifactSlotDocument | null => {
    const output = element.outputs[name];
    return output
      ? project.assets.artifact_slots_by_id[output.slot_id] ?? null
      : null;
  };
  const storyboardSlot = slotOf("storyboard");
  const videoSlot =
    slotOf("video") ??
    slotOf("main") ??
    Object.keys(element.outputs)
      .filter((name) => name !== "storyboard")
      .map(slotOf)
      .find(Boolean) ??
    null;
  const storyboardVersions = versionsOfSlot(project, storyboardSlot);
  const videoVersions = versionsOfSlot(project, videoSlot);
  const effectiveSbId =
    viewedSbId ??
    storyboardSlot?.selected_version_id ??
    storyboardVersions.at(-1)?.version_id ??
    null;
  const effectiveVideoId =
    viewedVideoId ??
    videoSlot?.selected_version_id ??
    videoVersions.at(-1)?.version_id ??
    null;
  const viewedStoryboard =
    storyboardVersions.find(
      (version) => version.version_id === effectiveSbId,
    ) ?? null;
  const viewedVideo =
    videoVersions.find((version) => version.version_id === effectiveVideoId) ??
    null;
  const storyboardUrl = mediaUrlOf(project, viewedStoryboard, "image/");
  const videoUrl = mediaUrlOf(project, viewedVideo, "video/");
  const setCurrentVersion = (
    slot: ArtifactSlotDocument,
    version: ArtifactVersionDocument,
  ) =>
    patchOps([
      {
        op: "replace",
        path: projectJsonPointer(
          "assets",
          "artifact_slots_by_id",
          slot.slot_id,
          "selected_version_id",
        ),
        before: slot.selected_version_id,
        value: version.version_id,
      },
    ]);

  const elementRef = `element:${element.element_id}`;
  const videoTask = [...tasks]
    .filter((task: TaskView) => task.targetRef === elementRef)
    .sort(
      (left, right) =>
        Date.parse(right.updatedAt || right.createdAt || "") -
        Date.parse(left.updatedAt || left.createdAt || ""),
    )[0];
  const videoGenerating =
    videoTask?.status === "RUNNING" || videoTask?.status === "QUEUED";
  const videoFailed =
    videoTask &&
    ["FAILED", "CANCELLED", "QUARANTINED"].includes(videoTask.status);
  const videoTaskMessage = (() => {
    if (!videoTask) return "";
    if (videoGenerating) return "任务已提交，等待最新状态";
    const detail =
      videoTask.error?.message ||
      videoTask.error?.detail ||
      videoTask.error?.code;
    return typeof detail === "string" && detail ? detail : "视频生成失败";
  })();

  const spanSeconds = element.span.duration_tick / timeline.ticks_per_second;
  const totalDuration = creation.shots.order.length
    ? creation.shots.order.reduce(
        (total, shotId) =>
          total + (creation.shots.items[shotId]?.duration_seconds ?? 0),
        0,
      )
    : spanSeconds;
  const overLimit = totalDuration > spanSeconds;

  // 输入引用：从 R2V creation 的引用字段汇总，与 origin/main 的 resolvedRefs 对应。
  const materialVersionIds = [
    ...new Set([
      ...creation.storyboard_reference_version_ids,
      ...creation.video_reference_version_ids,
    ]),
  ];
  const inputRefs: Array<{ ref: string; field: ReferenceField; name: string }> =
    [
      ...(creation.scene_ref
        ? [
            {
              ref: creation.scene_ref,
              field: "scene" as const,
              name: visualEntityName(project, creation.scene_ref),
            },
          ]
        : []),
      ...creation.character_refs.map((ref) => ({
        ref,
        field: "characters" as const,
        name: visualEntityName(project, ref),
      })),
      ...creation.prop_refs.map((ref) => ({
        ref,
        field: "props" as const,
        name: visualEntityName(project, ref),
      })),
      ...materialVersionIds.map((versionId) => ({
        ref: `artifact-version:${versionId}`,
        field: "sources" as const,
        name: referenceVersionName(project, versionId),
      })),
    ];

  const entityOptions = (kind: "scene" | "character" | "prop") =>
    Object.values(project.visual.entities.items)
      .filter((entity) => entity.kind === kind)
      .map((entity) => ({
        value: `visual-entity:${entity.entity_id}`,
        label: entity.name,
      }));
  const materialOptions = [
    ...Object.values(project.assets.source_versions_by_id).map((version) => ({
      value: version.version_id,
      label: version.name,
    })),
    ...Object.values(project.assets.artifact_versions_by_id)
      .filter((version) => version.owner_ref !== elementRef)
      .map((version) => ({
        value: version.version_id,
        label: version.name,
      })),
  ];
  const changeMaterialReferences = (next: string[]) =>
    patchOps([
      {
        op: "replace",
        path: elementPointer("creation", "storyboard_reference_version_ids"),
        before: creation.storyboard_reference_version_ids,
        value: next,
      },
      {
        op: "replace",
        path: elementPointer("creation", "video_reference_version_ids"),
        before: creation.video_reference_version_ids,
        value: next,
      },
    ]);

  const addShot = () => {
    const shotId = `shot-${Date.now()}`;
    return patchOps([
      {
        op: "add",
        path: elementPointer("creation", "shots", "items", shotId),
        missingBefore: true,
        value: {
          shot_id: shotId,
          description: "",
          camera: "⊙ 静止",
          framing: "中景",
          duration_seconds: 3,
        },
      },
      {
        op: "replace",
        path: elementPointer("creation", "shots", "order"),
        before: creation.shots.order,
        value: [...creation.shots.order, shotId],
      },
    ]);
  };
  const deleteShot = (shot: { shot_id: string }) =>
    patchOps([
      {
        op: "remove",
        path: elementPointer("creation", "shots", "items", shot.shot_id),
        before: creation.shots.items[shot.shot_id],
      },
      {
        op: "replace",
        path: elementPointer("creation", "shots", "order"),
        before: creation.shots.order,
        value: creation.shots.order.filter((item) => item !== shot.shot_id),
      },
    ]);

  return (
    <div
      data-r2v-workbench={element.element_id}
      className="flex h-full flex-col overflow-hidden bg-[var(--color-bg-layout)]"
    >
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)]/60 px-5 py-3 backdrop-blur">
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={backToPlan}
            className="icon-button shrink-0"
            aria-label="返回视频方案"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
              {`视频方案 / ${elementLabel} / 制作工作台`}
            </h2>
            <p className="mt-0.5 truncate text-xs text-[var(--color-text-secondary)]">
              AI 生成画面子界面，继承分镜、引用资产和产物版本。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="small"
            icon={<Wand2 className="h-3 w-3" />}
            onClick={() =>
              focusAgent(
                `请为「${elementLabel}」生成分镜图 Prompt，先读取当前分镜与创作意图。`,
              )
            }
            className="!text-xs"
          >
            生成分镜 Prompt
          </Button>
          <Button
            size="small"
            icon={<ImageIcon className="h-3 w-3" />}
            onClick={() =>
              focusAgent(
                `请为「${elementLabel}」生成分镜图，基于当前分镜图 Prompt 与引用资产。`,
              )
            }
            className="!text-xs"
          >
            生成分镜图
          </Button>
          <Button
            size="small"
            icon={<Clapperboard className="h-3 w-3" />}
            onClick={() =>
              focusAgent(
                `请为「${elementLabel}」生成视频 Prompt，覆盖全部分镜与运镜要求。`,
              )
            }
            className="!text-xs"
          >
            生成视频 Prompt
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<Video className="h-3 w-3" />}
            loading={videoGenerating}
            disabled={videoGenerating}
            onClick={() =>
              focusAgent(
                `请为「${elementLabel}」生成视频，基于当前分镜与视频 Prompt 完成制作。`,
              )
            }
            className="!text-xs"
          >
            {videoGenerating ? "视频生成中" : "生成视频"}
          </Button>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-h-0 space-y-3 overflow-y-auto pr-1">
          <Panel
            title={`Shot 列表（${creation.shots.order.length}）`}
            badge={
              <span
                className={`flex items-center gap-1 text-[11px] font-medium ${
                  overLimit
                    ? "text-[var(--color-danger)]"
                    : "text-[var(--color-text-tertiary)]"
                }`}
              >
                {overLimit && <AlertTriangle className="h-3 w-3" />}
                合计 {totalDuration}s / 区间 {spanSeconds}s
              </span>
            }
          >
            <ShotList
              shots={creation.shots}
              elementId={element.element_id}
              shotPointer={(shotId, field) =>
                elementPointer("creation", "shots", "items", shotId, field)
              }
              onPatchField={(shotId, field, before, value) =>
                patchField(
                  ["creation", "shots", "items", shotId, field],
                  before,
                  value,
                )
              }
              onAdd={addShot}
              onDelete={deleteShot}
            />
          </Panel>

          <Panel
            title="分镜Prompt与分镜图"
            badge={
              <ArtifactVersionChips
                versions={storyboardVersions}
                currentId={storyboardSlot?.selected_version_id}
                viewingId={effectiveSbId}
                onView={setViewedSbId}
              />
            }
          >
            <div className="space-y-3">
              {viewedStoryboard &&
                storyboardSlot &&
                viewedStoryboard.version_id !==
                  storyboardSlot.selected_version_id && (
                  <div className="flex items-center justify-between rounded-lg border border-[var(--color-warning)]/25 bg-[var(--color-warning-soft)] px-2.5 py-1.5">
                    <span className="text-[11px] text-[var(--color-warning)]">
                      可切换为当前分镜版本
                    </span>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() =>
                        void setCurrentVersion(storyboardSlot, viewedStoryboard)
                      }
                      className="!text-[11px]"
                    >
                      设为当前
                    </Button>
                  </div>
                )}
              <PromptTextArea
                label="分镜图 Prompt"
                value={creation.storyboard_prompt}
                field={`element:${element.element_id}/creation/storyboard_prompt`}
                path={elementPointer("creation", "storyboard_prompt")}
                onCommit={(value) =>
                  patchField(
                    ["creation", "storyboard_prompt"],
                    creation.storyboard_prompt,
                    value,
                  )
                }
              />
              {storyboardUrl ? (
                <img
                  src={storyboardUrl}
                  alt="分镜图"
                  className="w-full rounded-lg border border-[var(--color-border)]"
                />
              ) : (
                <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
                  尚无分镜图
                </div>
              )}
              <PromptTextArea
                label="视频 Prompt"
                value={creation.video_prompt}
                field={`element:${element.element_id}/creation/video_prompt`}
                path={elementPointer("creation", "video_prompt")}
                onCommit={(value) =>
                  patchField(
                    ["creation", "video_prompt"],
                    creation.video_prompt,
                    value,
                  )
                }
              />
            </div>
          </Panel>
        </div>

        <aside className="min-h-0 space-y-3 overflow-y-auto pr-1">
          <Panel
            title="视频结果"
            badge={
              <ArtifactVersionChips
                versions={videoVersions}
                currentId={videoSlot?.selected_version_id}
                viewingId={effectiveVideoId}
                onView={setViewedVideoId}
              />
            }
          >
            <div className="space-y-2">
              {videoUrl && viewedVideo ? (
                <video
                  key={viewedVideo.version_id}
                  src={videoUrl}
                  controls
                  className="w-full rounded-lg border border-[var(--color-border)]"
                />
              ) : (
                <div className="flex h-32 flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
                  {videoGenerating ? (
                    <>
                      <span className="font-medium text-[var(--color-warning)]">
                        R2V 任务生成中…
                      </span>
                      <span>{videoTaskMessage}</span>
                      <Button
                        size="small"
                        onClick={() =>
                          void Promise.all([refreshTasks(id), pollOnce(id)])
                        }
                        className="!text-[11px]"
                      >
                        手动刷新
                      </Button>
                    </>
                  ) : videoFailed ? (
                    <span className="px-3 text-center text-[var(--color-danger)]">
                      {videoTaskMessage}
                    </span>
                  ) : (
                    "尚未生成视频"
                  )}
                </div>
              )}
              {viewedVideo &&
                videoSlot &&
                viewedVideo.version_id !== videoSlot.selected_version_id && (
                  <div className="flex items-center justify-between rounded-lg border border-[var(--color-warning)]/25 bg-[var(--color-warning-soft)] px-2.5 py-1.5">
                    <span className="text-[11px] text-[var(--color-warning)]">
                      可切换为当前视频版本
                    </span>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() =>
                        void setCurrentVersion(videoSlot, viewedVideo)
                      }
                      className="!text-[11px]"
                    >
                      设为当前
                    </Button>
                  </div>
                )}
              {viewedVideo?.stale && (
                <p className="text-[10px] text-[var(--color-warning)]">
                  该结果基于旧版方案，需要重新生成。
                </p>
              )}
            </div>
          </Panel>

          <div className="grid grid-cols-3 gap-2">
            {[
              { label: "时长", value: `${totalDuration}s` },
              { label: "画幅", value: project.settings.aspect_ratio },
              {
                label: "模型",
                value: resolvedVideoModel ?? creation.recipe?.model ?? "R2V",
              },
            ].map((cell) => (
              <div
                key={cell.label}
                className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-card)] p-3 text-center"
              >
                <p className="text-[10px] text-[var(--color-text-tertiary)]">
                  {cell.label}
                </p>
                <p
                  title={cell.value}
                  className="mt-1 truncate text-xs font-semibold text-[var(--color-text-primary)]"
                >
                  {cell.value}
                </p>
              </div>
            ))}
          </div>

          <Panel title={`输入引用（${inputRefs.length}）`}>
            {inputRefs.length === 0 ? (
              <p className="text-xs text-[var(--color-text-tertiary)]">
                暂无引用资产。
              </p>
            ) : (
              <div className="space-y-1.5">
                {inputRefs.map((item) => (
                  <button
                    key={item.ref}
                    type="button"
                    onClick={() =>
                      useCreatorInteractionStore.getState().select(item.ref)
                    }
                    className="flex w-full items-center gap-2 rounded-lg bg-[var(--color-bg-secondary)]/60 px-2.5 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-secondary)]"
                  >
                    <span className="shrink-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-1.5 py-px text-[10px] text-[var(--color-text-tertiary)]">
                      {FIELD_LABEL[item.field]}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-[var(--color-accent)]">
                      @{item.name}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </Panel>

          <Panel title="资产绑定">
            <div className="space-y-3">
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-text-tertiary)]">
                  场景
                </p>
                <Select
                  size="small"
                  className="!w-full"
                  value={creation.scene_ref || undefined}
                  onChange={(value) =>
                    void patchField(
                      ["creation", "scene_ref"],
                      creation.scene_ref,
                      value ?? null,
                    )
                  }
                  allowClear
                  placeholder="选择场景"
                  options={entityOptions("scene")}
                />
              </div>
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-text-tertiary)]">
                  角色
                </p>
                <Select
                  size="small"
                  mode="multiple"
                  className="!w-full"
                  value={creation.character_refs}
                  onChange={(value) =>
                    void patchField(
                      ["creation", "character_refs"],
                      creation.character_refs,
                      value,
                    )
                  }
                  placeholder="选择角色"
                  options={entityOptions("character")}
                />
              </div>
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-text-tertiary)]">
                  道具
                </p>
                <Select
                  size="small"
                  mode="multiple"
                  className="!w-full"
                  value={creation.prop_refs}
                  onChange={(value) =>
                    void patchField(
                      ["creation", "prop_refs"],
                      creation.prop_refs,
                      value,
                    )
                  }
                  placeholder="选择道具"
                  options={entityOptions("prop")}
                />
              </div>
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-text-tertiary)]">
                  素材
                </p>
                <Select
                  size="small"
                  mode="multiple"
                  className="!w-full"
                  value={materialVersionIds}
                  onChange={(value) => void changeMaterialReferences(value)}
                  placeholder="选择素材"
                  options={materialOptions}
                />
              </div>
            </div>
          </Panel>
        </aside>
      </div>
    </div>
  );
}
