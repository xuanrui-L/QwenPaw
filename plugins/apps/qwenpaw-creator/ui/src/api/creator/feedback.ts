import i18n from "@/i18n";
import {
  CreatorHttpError,
  creatorRequest,
  jsonBody,
  newClientId,
} from "./client";

export const PLATFORM_NOTIFICATIONS_URL =
  "https://platform.agentscope.io/notifications";
export const FEEDBACK_LIMIT = 65_536;
export const MAX_FEEDBACK_SCREENSHOTS = 8;
const SCREENSHOT_BYTE_LIMIT = 5 * 1024 * 1024;
const requestOptions = { timeoutMs: 60_000 };
const imageTypes = ["image/jpeg", "image/png", "image/gif", "image/webp"];
export const FEEDBACK_IMAGE_ACCEPT = imageTypes.join(",");

export interface FeedbackAttempt {
  accountId?: string;
  unconfirmed?: boolean;
}

export interface FeedbackScreenshot {
  partialPreview?: boolean;
  key: string;
  file: File;
}

export function makeFeedbackScreenshot(file: File): FeedbackScreenshot {
  if (
    !imageTypes.includes(file.type) ||
    !file.size ||
    file.size > SCREENSHOT_BYTE_LIMIT
  ) {
    throw new Error(i18n.t("feedback.invalidImage"));
  }
  return { key: newClientId("shot"), file };
}

export interface FeedbackContext {
  projectId: string;
  sessionId?: string;
  conversationId?: string;
}

interface FeedbackDraft {
  stage?: string;
  trace_id?: string;
  trace_file?: string;
  trace_line?: number;
  trace_ts?: string;
}

export function formatFeedbackText(
  description: string,
  email: string,
  context: FeedbackContext,
  draft: FeedbackDraft = {},
): string {
  const deployment = window.location.hostname.match(
    /^([a-z0-9-]+)\.qwenpaw\.platform\.agentscope\.io$/i,
  );
  const fields = {
    project_id: context.projectId,
    session_id: context.sessionId,
    conversation_id: context.conversationId,
    deployment_id: deployment?.[1],
    stage: draft.stage === "unknown" ? undefined : draft.stage,
    trace_id: draft.trace_id,
    trace_file: draft.trace_file,
    trace_line: draft.trace_line,
    trace_ts: draft.trace_ts,
  };
  const diagnostics = Object.entries(fields)
    .filter(([, value]) => value != null && value !== "")
    .map(([name, value]) => `${name}: ${value}`)
    .join("\n");
  return [
    description.trim(),
    email.trim() && `${i18n.t("feedback.email")}: ${email.trim()}`,
    `---\n\nQwenPaw Creator\n\n\`\`\`text\n${diagnostics}\n\`\`\``,
  ]
    .filter(Boolean)
    .join("\n\n");
}

export function needsFeedbackAccount(error: unknown): boolean {
  return (
    error instanceof CreatorHttpError &&
    [
      "PLATFORM_ACCOUNT_REQUIRED",
      "authorization_expired",
      "not_connected",
      "connection_changed",
      "FEEDBACK_ACCOUNT_CHANGED",
      "account_changed",
      "permission_not_granted",
    ].includes(error.code)
  );
}

export function feedbackErrorMessage(error: unknown): string {
  if (!(error instanceof CreatorHttpError)) {
    return error instanceof Error ? error.message : i18n.t("feedback.failed");
  }
  if (
    [
      "FEEDBACK_ACCOUNT_CHANGED",
      "connection_changed",
      "account_changed",
    ].includes(error.code)
  ) {
    return i18n.t("feedback.accountChanged");
  }
  if (needsFeedbackAccount(error)) {
    return i18n.t(
      error.code === "permission_not_granted"
        ? "feedback.permissionDenied"
        : "feedback.accountRequired",
    );
  }
  switch (error.code) {
    case "authorization_refreshed":
      return i18n.t("feedback.accountRefreshed");
    case "PLATFORM_HOST_UNSUPPORTED":
      return i18n.t("feedback.hostUnsupported");
    case "FEEDBACK_UNCONFIRMED":
      return i18n.t("feedback.unconfirmed");
    case "rate_limited":
      return i18n.t("feedback.rateLimited");
    case "network_unavailable":
    case "platform_unavailable":
    case "invalid_platform_response":
      return i18n.t("feedback.retry");
    default:
      return error.message;
  }
}

// Keep a completed upload on a manual retry, scoped to the submitting account.
const uploadedScreenshots = new WeakMap<
  File,
  { accountId: string; url: string }
>();

async function uploadScreenshot(
  file: File,
  accountId: string,
): Promise<string> {
  const previous = uploadedScreenshots.get(file);
  if (previous?.accountId === accountId) return previous.url;
  const form = new FormData();
  form.append("file", file);
  const result = await creatorRequest<{ url: string }>(
    "/feedbacks/platform/screenshots",
    {
      method: "POST",
      headers: { "X-Platform-Account": accountId },
      body: form,
    },
    requestOptions,
  );
  if (typeof result?.url !== "string" || !/^https?:\/\//i.test(result.url)) {
    throw new Error(i18n.t("feedback.uploadFailed"));
  }
  uploadedScreenshots.set(file, { accountId, url: result.url });
  return result.url;
}

export async function submitFeedback(
  body: FeedbackContext & {
    feedback: string;
    contactEmail?: string;
    screenshots?: readonly FeedbackScreenshot[];
  },
  attempt: FeedbackAttempt,
): Promise<void> {
  if (attempt.unconfirmed)
    throw new CreatorHttpError(409, { code: "FEEDBACK_UNCONFIRMED" });
  if (!body.feedback.trim()) throw new Error(i18n.t("feedback.required"));
  const screenshots = body.screenshots ?? [];
  if (screenshots.length > MAX_FEEDBACK_SCREENSHOTS) {
    throw new Error(
      i18n.t("feedback.imageLimit", { count: MAX_FEEDBACK_SCREENSHOTS }),
    );
  }
  const draft = await creatorRequest<FeedbackDraft>(
    "/feedbacks/draft",
    {
      method: "POST",
      body: jsonBody({
        project_id: body.projectId,
        feedback: body.feedback.trim(),
      }),
    },
    requestOptions,
  );
  const description = formatFeedbackText(
    body.feedback,
    body.contactEmail ?? "",
    body,
    draft,
  );
  if (description.length > FEEDBACK_LIMIT) {
    throw new Error(
      i18n.t("feedback.tooLong", {
        count: description.length - FEEDBACK_LIMIT,
      }),
    );
  }
  const { id: accountId } = await creatorRequest<{ id: string }>(
    "/feedbacks/platform/account",
    {},
    { ...requestOptions, dedupe: false },
  );
  if (!accountId) throw new Error(i18n.t("feedback.failed"));
  if (attempt.accountId && attempt.accountId !== accountId) {
    throw new CreatorHttpError(409, { code: "FEEDBACK_ACCOUNT_CHANGED" });
  }
  attempt.accountId = accountId;
  const urls: string[] = [];
  for (const { file } of screenshots) {
    urls.push(await uploadScreenshot(file, accountId));
  }
  try {
    const result = await creatorRequest<{ submitted: boolean }>(
      "/feedbacks/platform",
      {
        method: "POST",
        headers: { "X-Platform-Account": accountId },
        body: jsonBody({
          description,
          ...(urls.length ? { screenshot_urls: urls } : {}),
        }),
      },
      requestOptions,
    );
    if (result?.submitted === true) return;
  } catch (error) {
    // A definite rejection can be retried. A lost receipt could be a posted ticket.
    if (
      error instanceof CreatorHttpError &&
      error.status >= 400 &&
      error.status < 500 &&
      error.status !== 408 &&
      error.code !== "connection_changed"
    )
      throw error;
  }
  attempt.unconfirmed = true;
  throw new CreatorHttpError(409, {
    code: "FEEDBACK_UNCONFIRMED",
    message: i18n.t("feedback.unconfirmed"),
  });
}
