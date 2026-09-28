import { creatorRequest, jsonBody } from "./client";

/**
 * User feedback about one generation stage, stored on the platform side.
 *
 * Two identities are in play and only one of them is ours to send. Creator's
 * backend speaks to its own API with a bearer, while the platform reads the
 * person from the `qwenpaw_console_token` cookie that the subdomain gateway
 * checks against the deployment owner - and it ignores Authorization here. So
 * this POST goes straight from the browser on a relative path, exactly like
 * the credential fetch, and there is no point putting a user id in the body:
 * the server writes the one it authenticated, and trusts nothing else.
 *
 * What the platform keeps about the run is a *pointer* into Creator's trace
 * (file + line + timestamp), never the jsonl body. A pointer is optional and
 * so is the lookup: a complaint with no trace behind it is still a signal,
 * while a failed observability query must not swallow what the user typed.
 */

export const CREATOR_FEEDBACK_PATH = "/api/v1/qwenpaw/creator/feedbacks";

/** Limits from the platform contract (project_id <=128, feedback <=4000). */
export const PROJECT_ID_LIMIT = 128;
export const FEEDBACK_LIMIT = 4000;

export interface TracePointer {
  trace_id?: string;
  trace_file?: string;
  trace_line?: number;
  trace_ts?: string;
}

export interface CreatorFeedbackDraft extends TracePointer {
  project_id: string;
  /** A stage code from Creator's ladder; free text on the platform side. */
  stage: string;
  feedback: string;
}

export interface CreatorFeedbackRecord extends CreatorFeedbackDraft {
  id: string;
  user_id?: string;
  deployment_id?: string;
  created_at?: string;
}

interface TraceQuery {
  projectId: string;
  /** Narrow to the failing records when the feedback is about an error. */
  status?: string;
  name?: string;
}

interface TraceRecord {
  traceId?: string;
  traceFile?: string;
  traceLine?: number;
  timestamp?: string;
}

function trimmed(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Newest trace record for a project, as the platform wants it.
 *
 * Returns an empty pointer rather than throwing: observability may be off for
 * this deployment, and that is no reason to lose a user's words.
 */
export async function fetchTracePointer(
  query: TraceQuery,
): Promise<TracePointer> {
  try {
    const params = new URLSearchParams({ limit: "1" });
    params.set("projectId", query.projectId);
    if (query.status) params.set("status", query.status);
    if (query.name) params.set("name", query.name);
    const body = await creatorRequest<{ items?: TraceRecord[] }>(
      `/observability/traces?${params.toString()}`,
    );
    // The endpoint sorts ascending by timestamp, so the last row is newest.
    const record = body?.items?.[body.items.length - 1];
    if (!record) return {};
    return {
      trace_id: trimmed(record.traceId) || undefined,
      trace_file: trimmed(record.traceFile) || undefined,
      trace_line:
        typeof record.traceLine === "number" ? record.traceLine : undefined,
      trace_ts: trimmed(record.timestamp) || undefined,
    };
  } catch {
    return {};
  }
}

/**
 * Submit one feedback record to the platform.
 *
 * `credentials: "include"` is load-bearing: the subdomain gateway answers this
 * path from the platform, whose only credential is the console cookie, and a
 * caller defaulting to same-origin semantics would drop it cross-site.
 */
export async function submitCreatorFeedback(
  draft: CreatorFeedbackDraft,
): Promise<CreatorFeedbackRecord> {
  const projectId = trimmed(draft.project_id);
  const stage = trimmed(draft.stage);
  const feedback = trimmed(draft.feedback);
  if (!projectId) throw new Error("缺少 project_id，无法提交反馈");
  if (projectId.length > PROJECT_ID_LIMIT) {
    throw new Error(`project_id 超过 ${PROJECT_ID_LIMIT} 字符`);
  }
  if (!stage) throw new Error("缺少生成阶段，无法提交反馈");
  if (!feedback) throw new Error("反馈内容为空");
  if (feedback.length > FEEDBACK_LIMIT) {
    throw new Error(`反馈内容超过 ${FEEDBACK_LIMIT} 字符`);
  }

  // Built field by field: the trimmed values are what gets sent, and a
  // pointer part that is absent stays absent instead of arriving as "".
  const body: CreatorFeedbackDraft = {
    project_id: projectId,
    stage,
    feedback,
    ...(draft.trace_id ? { trace_id: draft.trace_id } : {}),
    ...(draft.trace_file ? { trace_file: draft.trace_file } : {}),
    ...(typeof draft.trace_line === "number"
      ? { trace_line: draft.trace_line }
      : {}),
    ...(draft.trace_ts ? { trace_ts: draft.trace_ts } : {}),
  };
  const response = await fetch(CREATOR_FEEDBACK_PATH, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(
      response.status === 401
        ? "平台未识别本子域的登录会话（console_token 缺失或已过期）"
        : `反馈提交失败：HTTP ${response.status}`,
    );
  }
  const payload: unknown = await response.json().catch(() => null);
  const unwrapped =
    payload && typeof payload === "object" && "data" in payload
      ? (payload as { data: unknown }).data
      : payload;
  if (!unwrapped || typeof unwrapped !== "object") {
    throw new Error("平台响应不是对象");
  }
  return unwrapped as CreatorFeedbackRecord;
}

/**
 * Ask Creator to complete a feedback record, then hand it to the platform.
 *
 * The person types the reason and nothing else: the project, the stage and
 * the trace pointer are filled server-side, because only the backend can read
 * which jsonl line a run landed on. The submit still leaves from this browser
 * because the platform's credential is the cookie it holds.
 */
export async function submitFeedback(body: {
  projectId: string;
  stage?: string;
  feedback: string;
}): Promise<CreatorFeedbackRecord> {
  const draft = await creatorRequest<CreatorFeedbackDraft>("/feedbacks/draft", {
    method: "POST",
    body: jsonBody({
      project_id: body.projectId,
      stage: body.stage,
      feedback: body.feedback,
    }),
  });
  return submitCreatorFeedback(draft);
}
