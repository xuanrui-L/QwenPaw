import type {
  AgentStatusBarView,
  CreatorSessionView,
  ProjectDocument,
  TaskView,
} from "@/contracts/creator";
import type { SubagentActivity } from "@/store/creatorSessionStore";
import type { ToolCallPresentation } from "@/lib/creatorMessagePresentation";
import {
  creatorRoleLabel,
  creatorTargetLabel,
  getRoleRunningLabel,
  getToolRunningLabel,
  taskKindLabel,
} from "./creatorPresentation";
import { taskProgressPercent } from "./taskPresentation";

// 与 AgentStatusBar/agentWorkingSelectors 一致的"Agent 正在工作"会话口径。
const WORKING_SESSION_STATUSES = new Set([
  "RUNNING",
  "RESUMING",
  "WAITING_RUNTIME",
  "INTERRUPT_REQUESTED",
]);

const ACTIVE_TASK_STATUSES = new Set(["QUEUED", "RUNNING"]);

export type AgentLiveState = "working" | "stopping" | "waiting" | "idle";

export interface AgentLiveStatus {
  state: AgentLiveState;
  label: string;
  /** 仅当存在可量化进度（如素材入库）时给出 0-100；否则为 null 不展示进度。 */
  progressPercent: number | null;
}

export interface AgentLiveStatusInput {
  session: CreatorSessionView | null;
  agentStatusBar: AgentStatusBarView | null;
  stopping: boolean;
  hasQueuedInput: boolean;
  isReplaying: boolean;
  subagentActivities: Record<string, SubagentActivity>;
  toolCalls: ToolCallPresentation[];
  tasks: TaskView[];
  project: ProjectDocument | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function toolTargetRef(
  args: Record<string, unknown> | undefined,
  fallbackRefs: string[] = [],
): string {
  const direct = args?.targetRef ?? args?.target_ref;
  if (typeof direct === "string" && direct) return direct;
  return fallbackRefs[0] ?? "";
}

/** 目标名过长时在名字内部截断，保留句尾的“分镜图/画面”等关键词。 */
const MAX_TARGET_NAME_LENGTH = 10;

function clampTargetName(name: string): string {
  return name.length > MAX_TARGET_NAME_LENGTH
    ? `${name.slice(0, MAX_TARGET_NAME_LENGTH - 1)}…`
    : name;
}

/** 把 creatorTargetLabel 的兜底文案视为"未解析"，避免出现「时间线内容」分镜图。 */
function resolvedTargetName(
  ref: string,
  project: ProjectDocument | null,
): string | null {
  if (!ref) return null;
  const label = creatorTargetLabel(ref, project);
  const fallbacks = new Set([
    "当前项目",
    "时间线内容",
    "当前素材",
    "素材版本",
    "生成结果",
    "素材文件",
  ]);
  return fallbacks.has(label) ? null : clampTargetName(label);
}

function imageGenerationLabel(
  ref: string,
  project: ProjectDocument | null,
): string {
  const name = resolvedTargetName(ref, project);
  if (ref.startsWith("element:") || ref.startsWith("timeline:"))
    return name ? `正在生成「${name}」分镜图…` : "正在生成分镜图…";
  if (ref.startsWith("asset") || ref.startsWith("artifact"))
    return name ? `正在生成「${name}」画面…` : "正在生成视觉资产…";
  return "正在生成画面…";
}

function r2vGenerationLabel(
  ref: string,
  project: ProjectDocument | null,
): string {
  const name = resolvedTargetName(ref, project);
  return name ? `正在生成「${name}」视频…` : "正在生成视频…";
}

function runningToolLabel(
  tool: string,
  args: Record<string, unknown> | undefined,
  fallbackRefs: string[],
  project: ProjectDocument | null,
): string | null {
  if (tool === "image_generation")
    return imageGenerationLabel(toolTargetRef(args, fallbackRefs), project);
  if (tool === "r2v_generation")
    return r2vGenerationLabel(toolTargetRef(args, fallbackRefs), project);
  return getToolRunningLabel(tool);
}

function subagentRoleName(activity: SubagentActivity): string {
  return activity.roleDisplayName || creatorRoleLabel(activity.role);
}

/** 角色级子状态：优先用预设文案，未知角色降级为「角色」工作中。 */
function roleWorkingLabel(activity: SubagentActivity): string {
  const runningLabel = getRoleRunningLabel(activity.role);
  if (runningLabel) return runningLabel;
  return `「${subagentRoleName(activity)}」工作中…`;
}

/** 未完成子 Agent 里最新一个仍在执行的工具（含所属活动的 targetRefs 兜底）。 */
function activeSubagentToolLabel(
  activities: Record<string, SubagentActivity>,
  project: ProjectDocument | null,
): string | null {
  // 内部项目工具对用户无意义，跳过以避免显示"正在修改项目"等模糊状态。
  const internalProjectTools = new Set([
    "jq_project",
    "read_project",
    "read_project_file",
    "elements_at",
  ]);
  let latestSeq = -1;
  let latestLabel: string | null = null;
  Object.values(activities).forEach((activity) => {
    if (activity.completed) return;
    Object.values(activity.tools).forEach((tool) => {
      if (tool.status !== "started" || tool.firstEventSeq <= latestSeq) return;
      if (internalProjectTools.has(tool.tool)) return;
      const label = runningToolLabel(
        tool.tool,
        tool.arguments,
        activity.targetRefs,
        project,
      );
      if (!label) return;
      latestSeq = tool.firstEventSeq;
      latestLabel = label;
    });
  });
  return latestLabel;
}

/** 主 Agent 最新一个仍在执行的工具；delegate 交由角色文案表达。 */
function activeMainToolLabel(
  toolCalls: ToolCallPresentation[],
  activities: Record<string, SubagentActivity>,
  project: ProjectDocument | null,
): string | null {
  // 内部项目工具对用户无意义，跳过以避免显示"正在修改项目"等模糊状态。
  const internalProjectTools = new Set([
    "jq_project",
    "read_project",
    "read_project_file",
    "elements_at",
  ]);
  for (let index = toolCalls.length - 1; index >= 0; index -= 1) {
    const call = toolCalls[index];
    if (call.status !== "started") continue;
    if (call.tool === "delegate_to_agent") {
      const activity = activities[call.actionId];
      // Subagent still running → show its working label.
      if (activity && !activity.completed) return roleWorkingLabel(activity);
      // Subagent already finished (incl. cancelled) → defer to the activity
      // card, which renders the terminal state. Returning null here avoids
      // showing "正在安排" for a delegation that has already ended, which
      // would contradict the card's "已取消/已完成" status.
      if (activity && activity.completed) return null;
      // No subagent activity yet → the delegation was just issued; show a
      // brief "正在安排" until the specialist reports in.
      const args = isRecord(call.arguments) ? call.arguments : undefined;
      const role = typeof args?.role === "string" ? args.role : "";
      return role
        ? `正在安排「${creatorRoleLabel(role)}」…`
        : "正在分配任务…";
    }
    if (internalProjectTools.has(call.tool)) continue;
    const label = runningToolLabel(call.tool, call.arguments, [], project);
    if (label) return label;
  }
  return null;
}

/** 运行中的 Runtime 任务（素材入库/视频生成等长任务）。 */
function activeTask(tasks: TaskView[]): TaskView | null {
  const running = tasks.filter((task) => ACTIVE_TASK_STATUSES.has(task.status));
  if (running.length === 0) return null;
  return [...running].sort((left, right) =>
    (left.updatedAt ?? "").localeCompare(right.updatedAt ?? ""),
  )[running.length - 1];
}

function activeTaskLabel(
  task: TaskView,
  project: ProjectDocument | null,
): string {
  const name = resolvedTargetName(task.targetRef, project);
  const kind = taskKindLabel(task.kind);
  return name ? `「${name}」${kind}中…` : `${kind}中…`;
}

function firstIncompleteActivity(
  activities: Record<string, SubagentActivity>,
): SubagentActivity | null {
  const pending = Object.values(activities)
    .filter((activity) => !activity.completed)
    .sort((left, right) => right.firstEventSeq - left.firstEventSeq);
  return pending[0] ?? null;
}

/** 仅认可可量化进度：数值型任务进度（如素材入库），否则用后端 completed/total。 */
function quantifiedProgressPercent(
  tasks: TaskView[],
  agentStatusBar: AgentStatusBarView | null,
): number | null {
  const measurable = tasks
    .filter(
      (task) =>
        ACTIVE_TASK_STATUSES.has(task.status) &&
        typeof task.progress === "number",
    )
    .sort((left, right) =>
      (left.updatedAt ?? "").localeCompare(right.updatedAt ?? ""),
    );
  const latest = measurable[measurable.length - 1];
  if (latest) return taskProgressPercent(latest.progress);
  const completed = agentStatusBar?.progress.completed;
  const total = agentStatusBar?.progress.total;
  if (typeof completed === "number" && typeof total === "number" && total > 0)
    return Math.max(0, Math.min(100, Math.round((completed / total) * 100)));
  return null;
}

export function deriveAgentLiveStatus(
  input: AgentLiveStatusInput,
): AgentLiveStatus {
  const {
    session,
    agentStatusBar,
    stopping,
    hasQueuedInput,
    isReplaying,
    subagentActivities,
    toolCalls,
    tasks,
    project,
  } = input;

  if (stopping || session?.status === "INTERRUPT_REQUESTED")
    return {
      state: "stopping",
      label: "正在停止所有 Agent…",
      progressPercent: null,
    };

  // 重放期间显示"加载中…"，抑制"工作中"动画
  if (isReplaying)
    return {
      state: "idle",
      label: "加载中…",
      progressPercent: null,
    };

  // CANCELLED/ERROR are terminal. IDLE remains actionable: an optimistic
  // queued message must be visible while the backend transitions to RUNNING.
  if (
    session?.status === "CANCELLED" ||
    session?.status === "ERROR" ||
    (session?.status === "IDLE" && !hasQueuedInput)
  )
    return {
      state: "idle",
      label: "待命中，可随时输入修改意图。",
      progressPercent: null,
    };

  const runningTask = activeTask(tasks);
  const working =
    (agentStatusBar?.activity?.runningTaskCount ?? 0) > 0 ||
    Boolean(session && WORKING_SESSION_STATUSES.has(session.status)) ||
    Boolean(runningTask) ||
    Object.values(subagentActivities).some((activity) => !activity.completed) ||
    hasQueuedInput;

  if (working) {
    const progressPercent = quantifiedProgressPercent(tasks, agentStatusBar);
    const label =
      activeSubagentToolLabel(subagentActivities, project) ??
      activeMainToolLabel(toolCalls, subagentActivities, project) ??
      (runningTask ? activeTaskLabel(runningTask, project) : null) ??
      (() => {
        const activity = firstIncompleteActivity(subagentActivities);
        return activity ? roleWorkingLabel(activity) : null;
      })() ??
      agentStatusBar?.progress.label ??
      (hasQueuedInput && session?.status !== "RUNNING"
        ? "指令已发出，等待响应…"
        : "正在思考…");
    return {
      state: "working",
      label,
      progressPercent:
        progressPercent != null && progressPercent < 100
          ? progressPercent
          : null,
    };
  }

  if (session?.status === "WAITING_USER_INPUT")
    return {
      state: "waiting",
      label: "等待补充信息，请继续输入。",
      progressPercent: null,
    };
  if (session?.status === "WAITING_EXECUTION_AUTH")
    return {
      state: "waiting",
      label: "等待执行确认。",
      progressPercent: null,
    };
  if (session?.status === "PENDING_REVIEW")
    return {
      state: "waiting",
      label: "等待审阅 Agent 改动。",
      progressPercent: null,
    };

  return {
    state: "idle",
    label: "待命中，可随时输入修改意图。",
    progressPercent: null,
  };
}
