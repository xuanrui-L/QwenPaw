import type {
  ProjectDocument,
  SpecialistRunStatus,
  TaskStatus,
  TaskView,
} from "@/contracts/creator";
import i18n from "@/i18n";

const TASK_KIND_LABELS: Record<TaskView["kind"], string> = {
  asset_ingest: i18n.t("presentation.taskKinds.asset_ingest"),
  asset_import: i18n.t("presentation.taskKinds.asset_import"),
  source_intelligence: i18n.t("presentation.taskKinds.source_intelligence"),
  image_generation: i18n.t("presentation.taskKinds.image_generation"),
  r2v_generation: i18n.t("presentation.taskKinds.r2v_generation"),
  ai_edit_plan: i18n.t("presentation.taskKinds.ai_edit_plan"),
  ai_edit_execute: i18n.t("presentation.taskKinds.ai_edit_execute"),
  compose: i18n.t("presentation.taskKinds.compose"),
};

export function taskKindLabel(kind: string): string {
  return (
    TASK_KIND_LABELS[kind as TaskView["kind"]] ??
    i18n.t("presentation.taskExecution")
  );
}

const STATUS_LABELS: Record<string, string> = {
  IDLE: i18n.t("presentation.statuses.IDLE"),
  QUEUED: i18n.t("presentation.statuses.QUEUED"),
  QUEUED_CAPACITY: i18n.t("presentation.statuses.QUEUED_CAPACITY"),
  RUNNING: i18n.t("presentation.statuses.RUNNING"),
  RUNNING_MODEL: i18n.t("presentation.statuses.RUNNING_MODEL"),
  WAITING_RUNTIME: i18n.t("presentation.statuses.WAITING_RUNTIME"),
  WAITING_AUTHORIZATION: i18n.t("presentation.statuses.WAITING_AUTHORIZATION"),
  WAITING_USER_INPUT: i18n.t("presentation.statuses.WAITING_USER_INPUT"),
  WAITING_EXECUTION_AUTH: i18n.t(
    "presentation.statuses.WAITING_EXECUTION_AUTH",
  ),
  PENDING_REVIEW: i18n.t("presentation.statuses.PENDING_REVIEW"),
  RESUMING: i18n.t("presentation.statuses.RESUMING"),
  INTERRUPT_REQUESTED: i18n.t("presentation.statuses.INTERRUPT_REQUESTED"),
  SUCCEEDED: i18n.t("presentation.statuses.SUCCEEDED"),
  BLOCKED: i18n.t("presentation.statuses.BLOCKED"),
  FAILED: i18n.t("presentation.statuses.FAILED"),
  STALE: i18n.t("presentation.statuses.STALE"),
  CANCELLED: i18n.t("presentation.statuses.CANCELLED"),
  QUARANTINED: i18n.t("presentation.statuses.QUARANTINED"),
  ERROR: i18n.t("presentation.statuses.ERROR"),
};

export function creatorStatusLabel(
  status: SpecialistRunStatus | TaskStatus | string | null | undefined,
): string {
  return status
    ? (STATUS_LABELS[status] ?? i18n.t("presentation.processing"))
    : i18n.t("presentation.dash");
}

function elementName(
  project: ProjectDocument | null | undefined,
  elementId: string,
): string | null {
  if (!project) return null;
  for (const timeline of Object.values(project.timelines.items)) {
    const element = timeline.elements_by_id[elementId];
    if (element)
      return element.label || i18n.t("presentation.targets.timelineContent");
  }
  return null;
}

export function creatorTargetLabel(
  ref: string,
  project?: ProjectDocument | null,
): string {
  if (!ref || ref === "project")
    return i18n.t("presentation.targets.currentProject");
  if (ref === "project:assets")
    return i18n.t("presentation.targets.assetsAndResults");
  if (ref === "project:plan") return i18n.t("presentation.targets.videoPlan");
  if (ref.startsWith("element:"))
    return (
      elementName(project, ref.slice("element:".length)) ??
      i18n.t("presentation.targets.timelineContent")
    );
  if (ref.startsWith("timeline:"))
    return i18n.t("presentation.targets.mainTimeline");
  if (ref.startsWith("source:")) {
    const sourceId = ref.slice("source:".length);
    return (
      project?.sources.sources.items[sourceId]?.display_name ||
      i18n.t("presentation.targets.currentSource")
    );
  }
  if (ref.startsWith("asset:")) {
    const logicalAssetId = ref.slice("asset:".length);
    const entity = project?.visual?.entities?.items?.[logicalAssetId];
    if (entity?.name) return entity.name;
    return (
      Object.values(project?.assets.source_versions_by_id ?? {}).find(
        (version) => version.logical_asset_id === logicalAssetId,
      )?.name || i18n.t("presentation.targets.currentSource")
    );
  }
  if (ref.startsWith("visual-entity:")) {
    const entityId = ref.slice("visual-entity:".length);
    return (
      project?.visual?.entities?.items?.[entityId]?.name ||
      i18n.t("presentation.targets.visualSetting")
    );
  }
  if (ref.startsWith("asset-version:")) {
    return (
      project?.assets.source_versions_by_id[ref.slice("asset-version:".length)]
        ?.name || i18n.t("presentation.targets.sourceVersion")
    );
  }
  if (ref.startsWith("artifact-version:")) {
    return (
      project?.assets.artifact_versions_by_id[
        ref.slice("artifact-version:".length)
      ]?.name || i18n.t("presentation.targets.genResult")
    );
  }
  if (ref.startsWith("file:")) return i18n.t("presentation.targets.sourceFile");
  if (ref.startsWith("artifact:"))
    return i18n.t("presentation.targets.genResult");
  return i18n.t("presentation.targets.currentProject");
}

export function creatorToolLabel(name: string): string {
  const labels: Record<string, string> = {
    read_project: i18n.t("presentation.tools.read_project"),
    read_project_file: i18n.t("presentation.tools.read_project_file"),
    jq_project: i18n.t("presentation.tools.jq_project"),
    elements_at: i18n.t("presentation.tools.elements_at"),
    delegate_to_agent: i18n.t("presentation.tools.delegate_to_agent"),
    analyze_source_media: i18n.t("presentation.tools.analyze_source_media"),
    source_intelligence: i18n.t("presentation.tools.source_intelligence"),
    ai_edit: i18n.t("presentation.tools.ai_edit"),
    r2v_generation: i18n.t("presentation.tools.r2v_generation"),
    image_generation: i18n.t("presentation.tools.image_generation"),
    read_file: i18n.t("presentation.tools.read_file"),
    write_file: i18n.t("presentation.tools.write_file"),
    edit_file: i18n.t("presentation.tools.edit_file"),
    append_file: i18n.t("presentation.tools.append_file"),
    grep_search: i18n.t("presentation.tools.grep_search"),
    glob_search: i18n.t("presentation.tools.glob_search"),
    ast_search: i18n.t("presentation.tools.ast_search"),
    plan: i18n.t("presentation.tools.plan"),
    final: i18n.t("presentation.tools.final"),
    finalize_video: i18n.t("presentation.tools.finalize_video"),
    yield_until_runtime_event: i18n.t(
      "presentation.tools.yield_until_runtime_event",
    ),
    complete_current_change: i18n.t(
      "presentation.tools.complete_current_change",
    ),
    ground_prompt_context: i18n.t("presentation.tools.ground_prompt_context"),
    transcribe_source_audio: i18n.t(
      "presentation.tools.transcribe_source_audio",
    ),
    commit_source_intelligence: i18n.t(
      "presentation.tools.commit_source_intelligence",
    ),
  };
  return labels[name] ?? i18n.t("presentation.processing");
}

export function creatorRoleLabel(name: string): string {
  const labels: Record<string, string> = {
    source_intelligence_agent: i18n.t(
      "presentation.roles.source_intelligence_agent",
    ),
    visual_development_agent: i18n.t(
      "presentation.roles.visual_development_agent",
    ),
    v_generation_director: i18n.t("presentation.roles.v_generation_director"),
    ai_editing_director: i18n.t("presentation.roles.ai_editing_director"),
    r2v_generation_director: i18n.t(
      "presentation.roles.r2v_generation_director",
    ),
    story_planning_agent: i18n.t("presentation.roles.story_planning_agent"),
    unit_planning_routing_agent: i18n.t(
      "presentation.roles.unit_planning_routing_agent",
    ),
    review_consistency_agent: i18n.t(
      "presentation.roles.review_consistency_agent",
    ),
  };
  return labels[name] ?? i18n.t("presentation.specialistProduction");
}

const TOOL_RUNNING_LABELS: Record<string, string> = {
  read_project: i18n.t("presentation.toolRunning.read_project"),
  read_project_file: i18n.t("presentation.toolRunning.read_project_file"),
  jq_project: i18n.t("presentation.toolRunning.jq_project"),
  elements_at: i18n.t("presentation.toolRunning.elements_at"),
  ground_prompt_context: i18n.t(
    "presentation.toolRunning.ground_prompt_context",
  ),
  analyze_source_media: i18n.t("presentation.toolRunning.analyze_source_media"),
  source_intelligence: i18n.t("presentation.toolRunning.source_intelligence"),
  transcribe_source_audio: i18n.t(
    "presentation.toolRunning.transcribe_source_audio",
  ),
  commit_source_intelligence: i18n.t(
    "presentation.toolRunning.commit_source_intelligence",
  ),
  ai_edit: i18n.t("presentation.toolRunning.ai_edit"),
  read_file: i18n.t("presentation.toolRunning.read_file"),
  write_file: i18n.t("presentation.toolRunning.write_file"),
  edit_file: i18n.t("presentation.toolRunning.edit_file"),
  append_file: i18n.t("presentation.toolRunning.append_file"),
  grep_search: i18n.t("presentation.toolRunning.grep_search"),
  glob_search: i18n.t("presentation.toolRunning.glob_search"),
  ast_search: i18n.t("presentation.toolRunning.ast_search"),
  plan: i18n.t("presentation.toolRunning.plan"),
  final: i18n.t("presentation.toolRunning.final"),
  finalize_video: i18n.t("presentation.toolRunning.finalize_video"),
  yield_until_runtime_event: i18n.t(
    "presentation.toolRunning.yield_until_runtime_event",
  ),
  complete_current_change: i18n.t(
    "presentation.toolRunning.complete_current_change",
  ),
};

export function getToolRunningLabel(name: string): string | null {
  return TOOL_RUNNING_LABELS[name] ?? null;
}

export function getRoleRunningLabel(name: string): string | null {
  const roleLabel = creatorRoleLabel(name);
  if (!roleLabel || roleLabel === i18n.t("presentation.productionAssistant"))
    return null;
  return i18n.t("presentation.roleRunningSuffix", { role: roleLabel });
}

export function getEstimatedDuration(toolName: string): string | null {
  const durations: Record<string, string> = {
    image_generation: i18n.t(
      "presentation.estimatedDurations.image_generation",
    ),
    r2v_generation: i18n.t("presentation.estimatedDurations.r2v_generation"),
    analyze_source_media: i18n.t(
      "presentation.estimatedDurations.analyze_source_media",
    ),
    ai_edit: i18n.t("presentation.estimatedDurations.ai_edit"),
    finalize_video: i18n.t("presentation.estimatedDurations.finalize_video"),
    plan: i18n.t("presentation.estimatedDurations.plan"),
    grep_search: i18n.t("presentation.estimatedDurations.grep_search"),
    glob_search: i18n.t("presentation.estimatedDurations.glob_search"),
    ast_search: i18n.t("presentation.estimatedDurations.ast_search"),
  };
  return durations[toolName] ?? null;
}

export function creatorEventLabel(type: string): string {
  const labels: Record<string, string> = {
    "workspace.project_committed": i18n.t(
      "presentation.events.workspace.project_committed",
    ),
    "workspace.project_changed": i18n.t(
      "presentation.events.workspace.project_changed",
    ),
    "review.created": i18n.t("presentation.events.review.created"),
    "review.applied": i18n.t("presentation.events.review.applied"),
    "review.resolved": i18n.t("presentation.events.review.resolved"),
    "task.queued": i18n.t("presentation.events.task.queued"),
    "task.started": i18n.t("presentation.events.task.started"),
    "task.completed": i18n.t("presentation.events.task.completed"),
    "task.failed": i18n.t("presentation.events.task.failed"),
  };
  if (labels[type]) return labels[type];
  if (type.startsWith("workspace."))
    return i18n.t("presentation.eventFallbacks.workspace");
  if (type.startsWith("review."))
    return i18n.t("presentation.eventFallbacks.review");
  if (type.startsWith("task."))
    return i18n.t("presentation.eventFallbacks.task");
  return i18n.t("presentation.projectActivity");
}

export function outputLabel(name: string): string {
  const labels: Record<string, string> = {
    storyboard: i18n.t("presentation.outputs.storyboard"),
    main: i18n.t("presentation.outputs.main"),
    overlay: i18n.t("presentation.outputs.overlay"),
    render: i18n.t("presentation.outputs.render"),
    audio: i18n.t("presentation.outputs.audio"),
  };
  return labels[name] ?? i18n.t("presentation.genResult");
}
