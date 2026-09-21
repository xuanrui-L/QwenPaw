import { creatorRequest, jsonBody } from "./client";

/**
 * Platform one-click model configuration.
 *
 * Creator runs as a container on a QwenPaw subdomain, and the platform hands
 * out its model key to the browser only: the credential endpoint authenticates
 * with the `console_token` cookie that the subdomain gateway checks against the
 * deployment owner. The Creator backend cannot call it - it holds no platform
 * identity, and the platform explicitly refuses its login JWT - so the key
 * travels browser -> Creator backend, which is the only place able to persist
 * it into the model configuration.
 */

export const PLATFORM_CREDENTIALS_PATH = "/api/v1/qwenpaw/creator-credentials";
export const PLATFORM_CREDITS_USAGE_PATH =
  "/api/v1/qwenpaw/creator/credits-usage";

export interface PlatformCredentials {
  user_id?: string;
  deployment_id?: string;
  api_key: string;
  /** True when this call minted the key rather than returning an existing one. */
  api_key_created?: boolean;
  display_available_credits?: number | string | null;
  balance_credits?: number | string | null;
  chat_completions_url: string;
  /** Redundant with `api_key`; never consumed, so nothing can double the Bearer. */
  auth_header?: string;
}

export interface PlatformApplySection {
  section: string;
  model_name: string;
  ready: boolean;
  /** True when the preset replaced whatever the section held before. */
  replaced?: boolean;
}

export interface PlatformApplyResult {
  ok: boolean;
  base_url: string;
  sections: PlatformApplySection[];
}

function requireText(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`平台响应缺少 ${field}`);
  }
  return value.trim();
}

/**
 * Read the credentials of this deployment.
 *
 * A bare same-origin fetch on purpose: the platform ignores Authorization here
 * (its credential is the cookie, not Creator's bearer), and `credentials` must
 * opt the cookie back in for callers that default to `same-origin` semantics.
 */
export async function fetchPlatformCredentials(): Promise<PlatformCredentials> {
  const response = await fetch(PLATFORM_CREDENTIALS_PATH, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      response.status === 401
        ? "平台未识别本子域的登录会话（console_token 缺失或已过期）"
        : `平台凭据获取失败：HTTP ${response.status}`,
    );
  }
  const payload: unknown = await response.json().catch(() => null);
  // Documented shape wraps the fields in `data`; tolerate a bare object so a
  // gateway-side simplification does not break the button.
  const body =
    payload && typeof payload === "object" && "data" in payload
      ? (payload as { data: unknown }).data
      : payload;
  if (!body || typeof body !== "object") {
    throw new Error("平台响应不是对象");
  }
  const record = body as Record<string, unknown>;
  return {
    ...record,
    api_key: requireText(record.api_key, "api_key"),
    chat_completions_url: requireText(
      record.chat_completions_url,
      "chat_completions_url",
    ),
  } as PlatformCredentials;
}

/** Point every proxy-served section at the platform with the issued key. */
export function applyPlatformCredentials(
  credentials: Pick<PlatformCredentials, "api_key" | "chat_completions_url">,
): Promise<PlatformApplyResult> {
  return creatorRequest("/models/platform-autoconfigure", {
    method: "POST",
    body: jsonBody({
      api_key: credentials.api_key,
      chat_completions_url: credentials.chat_completions_url,
    }),
  });
}

/** Per-model Credits rollup from `credits-usage.by_model`. */
export interface CreditsUsageByModel {
  model_id: string;
  model_type?: string;
  settled_credits: number;
  call_count?: number;
}

/**
 * The subset of `GET /creator/credits-usage` the ring needs.
 *
 * Only the aggregate and the per-model rollup are read here; the paged
 * `items[]` detail is left for whoever later wants a transaction list, so an
 * unexpected shape in those rows cannot break this fetch.
 */
export interface CreditsUsage {
  total_settled_credits: number;
  total_calls?: number;
  by_model: CreditsUsageByModel[];
}

function asNumber(value: unknown): number {
  const parsed = typeof value === "string" ? Number(value) : (value as unknown);
  return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : 0;
}

/**
 * Read this deployment's Credits consumption.
 *
 * Same-origin cookie fetch, mirroring `fetchPlatformCredentials`: the platform
 * authenticates with `console_token` and ignores Creator's bearer, so the
 * backend cannot proxy this. Every failure path throws - it is the caller (the
 * store) that decides to swallow the error and keep the rest of the chrome
 * alive, because off-platform this endpoint is simply unreachable.
 */
export async function fetchCreditsUsage(
  signal?: AbortSignal,
): Promise<CreditsUsage> {
  const response = await fetch(PLATFORM_CREDITS_USAGE_PATH, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Credits 用量获取失败：HTTP ${response.status}`);
  }
  const payload: unknown = await response.json().catch(() => null);
  const body =
    payload && typeof payload === "object" && "data" in payload
      ? (payload as { data: unknown }).data
      : payload;
  if (!body || typeof body !== "object") {
    throw new Error("Credits 用量响应不是对象");
  }
  const record = body as Record<string, unknown>;
  const rawModels = Array.isArray(record.by_model) ? record.by_model : [];
  const byModel: CreditsUsageByModel[] = rawModels
    .filter((row): row is Record<string, unknown> =>
      Boolean(row && typeof row === "object"),
    )
    .map((row) => ({
      model_id: String(row.model_id ?? "未知模型"),
      model_type:
        typeof row.model_type === "string" ? row.model_type : undefined,
      settled_credits: asNumber(row.settled_credits),
      call_count: row.call_count == null ? undefined : asNumber(row.call_count),
    }));
  return {
    total_settled_credits: asNumber(record.total_settled_credits),
    total_calls:
      record.total_calls == null ? undefined : asNumber(record.total_calls),
    by_model: byModel,
  };
}
