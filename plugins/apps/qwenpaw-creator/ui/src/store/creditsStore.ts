import { create } from "zustand";
import {
  fetchCreditsUsage,
  fetchPlatformCredentials,
  type CreditsUsageByModel,
} from "@/api/creator/platform";

/**
 * Navigation-bar Credits ring state.
 *
 * Two platform endpoints feed one small widget: the credential endpoint gives
 * the current balance (how much is left), and `credits-usage` gives what has
 * been spent broken down by model (the hover detail). They are fetched together
 * but settled independently - a failure on either leaves the other usable, and
 * neither may surface as an unhandled rejection, because off-platform this
 * subdomain's `/api/v1/qwenpaw/*` routes do not resolve at all.
 *
 * The ring fraction needs a denominator the platform never returns: there is no
 * "total quota" field. So the arc reads remaining / (consumed + remaining),
 * i.e. the share of everything allocated-so-far that is still spendable. That
 * is an assumption, documented here and in `ringFraction`, not a contract term.
 */

export type CreditsStatus = "idle" | "loading" | "ready" | "error";

// Re-exported so the ring imports its row shape from the store it already
// depends on, rather than reaching past it into the api layer.
export type { CreditsUsageByModel as CreditsUsageByModelView } from "@/api/creator/platform";

export interface CreditsState {
  status: CreditsStatus;
  /** Current available balance; null when the credential call failed. */
  available: number | null;
  /** Total spent in the account window; null when the usage call failed. */
  consumed: number | null;
  byModel: CreditsUsageByModel[];
  totalCalls?: number;
  /** True when the balance itself is unknown, so the ring cannot be drawn. */
  balanceKnown: boolean;
  load: () => Promise<void>;
  /** Force a re-read even after a successful load (tab focus / task done). */
  reload: () => Promise<void>;
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

// Only ever move forward from idle; a mount that already loaded stays loaded.
let inflight: Promise<void> | null = null;

export const useCreditsStore = create<CreditsState>((set, get) => ({
  status: "idle",
  available: null,
  consumed: null,
  byModel: [],
  totalCalls: undefined,
  balanceKnown: false,

  load: async () => {
    if (get().status === "loading") return inflight ?? undefined;
    if (get().status === "ready") return undefined;
    set({ status: "loading" });
    inflight = (async () => {
      // Each request is isolated: one throwing must not blank the other's data.
      const [credential, usage] = await Promise.allSettled([
        fetchPlatformCredentials(),
        fetchCreditsUsage(),
      ]);
      const available =
        credential.status === "fulfilled"
          ? toNumber(credential.value.display_available_credits)
          : null;
      const usageValue = usage.status === "fulfilled" ? usage.value : null;
      const balanceKnown = available !== null || usageValue !== null;
      set({
        status: balanceKnown ? "ready" : "error",
        available,
        consumed: usageValue?.total_settled_credits ?? null,
        byModel: usageValue?.by_model ?? [],
        totalCalls: usageValue?.total_calls,
        balanceKnown,
      });
    })().finally(() => {
      inflight = null;
    });
    return inflight;
  },

  reload: async () => {
    // load()'s ready-gate only exists to stop duplicate first-fetches; once the
    // balance has actually moved we must be able to re-read it. Reuse an
    // in-flight call rather than firing a parallel request.
    if (get().status === "loading") return inflight ?? undefined;
    set({ status: "idle" });
    return get().load();
  },
}));

/**
 * Fraction of the ring that is still available, in `[0, 1]`.
 *
 * Denominator is consumed + available (see the store note). Returns null when
 * the balance is unknown, so the caller renders a neutral placeholder rather
 * than a misleading full or empty ring.
 */
export function ringFraction(
  available: number | null,
  consumed: number | null,
): number | null {
  if (available === null) return null;
  const spent = consumed ?? 0;
  const total = available + spent;
  if (total <= 0) return 1;
  return Math.max(0, Math.min(1, available / total));
}
