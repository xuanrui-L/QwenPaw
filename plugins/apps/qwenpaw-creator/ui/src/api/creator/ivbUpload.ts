/**
 * Publish an exported interactive bundle to the platform.
 *
 * The bundle is produced by Creator's own export endpoint, but the upload
 * leaves from this browser straight to the platform: its only credential is
 * the `qwenpaw_console_token` cookie the subdomain gateway checks, and it
 * ignores every bearer - the same reason the feedback POST is same-origin.
 *
 * The platform runs a synchronous content check (绿网 / CSI) on intake, then
 * unpacks and publishes asynchronously. It hands back a `progress_url` to
 * redirect to; the Platform "my works" page owns polling to success/failed,
 * so Creator submits and steps aside.
 */

export const IVB_UPLOAD_PATH = "/api/v1/qwenpaw/ivb/uploads";

/** The ZIP cap the platform accepts, enforced here to fail before the upload. */
export const IVB_BUNDLE_LIMIT_BYTES = 200 * 1024 * 1024;

export interface IvbUploadResult {
  upload_id: string;
  project_id: string | null;
  status: string;
  version: string | null;
  play_url: string | null;
  progress_url: string | null;
  error_code: string | null;
  error_message: string | null;
}

function trimmed(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Upload one bundle ZIP and return the platform's task record.
 *
 * `idempotencyKey` should be fresh per attempt: the platform folds repeat
 * submissions of the same user + key into one task, so reusing a key after
 * editing the package would silently return the earlier upload.
 */
export async function uploadInteractiveBundle(options: {
  bundle: Blob;
  projectId?: string;
  idempotencyKey?: string;
}): Promise<IvbUploadResult> {
  const { bundle, projectId } = options;
  if (!bundle || bundle.size === 0) {
    throw new Error("互动包为空，无法上传");
  }
  if (bundle.size > IVB_BUNDLE_LIMIT_BYTES) {
    throw new Error("互动包超过 200MB，平台不接受");
  }

  const form = new FormData();
  form.append(
    "file",
    bundle,
    projectId ? `${projectId}-interactive.zip` : "interactive.zip",
  );
  if (projectId) form.append("project_id", projectId);

  // Content-Type is left unset on purpose: the browser fills in the multipart
  // boundary, and hardcoding "multipart/form-data" here would drop it.
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }

  const response = await fetch(IVB_UPLOAD_PATH, {
    method: "POST",
    credentials: "include",
    headers,
    body: form,
  });
  if (!response.ok) {
    throw new Error(
      response.status === 401
        ? "平台未识别本子域的登录会话（console_token 缺失或已过期）"
        : `上传失败：HTTP ${response.status}`,
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  const data =
    payload && typeof payload === "object" && "data" in payload
      ? (payload as { data: unknown }).data
      : payload;
  if (!data || typeof data !== "object") {
    throw new Error("平台响应不是对象");
  }
  const record = data as Record<string, unknown>;
  const uploadId = trimmed(record.upload_id);
  if (!uploadId) throw new Error("平台未返回 upload_id");
  return {
    upload_id: uploadId,
    project_id: trimmed(record.project_id) || null,
    status: trimmed(record.status) || "received",
    version: trimmed(record.version) || null,
    play_url: trimmed(record.play_url) || null,
    progress_url: trimmed(record.progress_url) || null,
    error_code: trimmed(record.error_code) || null,
    error_message: trimmed(record.error_message) || null,
  };
}

/** A fresh key per upload attempt; callers should not reuse one across edits. */
export function newUploadIdempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
