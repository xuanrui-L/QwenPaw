import { create } from "zustand";

/**
 * Account-level "the model provider will not serve us" notice, surfaced in
 * every navigation bar.
 *
 * An exhausted Credits balance is not a property of one project: the provider
 * refuses before reaching any model, in every project, until a human redeems
 * more. The per-project failure tray therefore only spoke where the refusal
 * happened, so opening the project list hid the only actionable fact on screen
 * - and every other project looked perfectly healthy.
 *
 * This store is a frontend mirror of a server condition, not a second truth.
 * It is written from the session stream, mirrored to localStorage so a route
 * change or a reload cannot lose it, and dropped by the first run that reaches
 * its end (which proves the balance recovered) or by an explicit dismissal.
 * The failure tray still owns every retry control; this surface only states the
 * condition, and carries the project id so a future action has a subject.
 */

const STORAGE_KEY = "qwenpaw-creator:model-credits:v1";

/** Session error code the runtime reports for a refused Credits spend. */
export const MODEL_QUOTA_ERROR_CODE = "MODEL_QUOTA_EXCEEDED";

export interface CreditsNotice {
  /** Project whose run last proved the balance is empty. */
  projectId: string;
  /** ISO timestamp of the first refusal seen for that project. */
  at: string;
}

interface ModelCreditsState {
  notice: CreditsNotice | null;
  markExhausted: (projectId: string) => void;
  clear: () => void;
}

// Lazy lookup: tests replace window.localStorage; don't capture at load.
function getStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

function readPersisted(): CreditsNotice | null {
  try {
    const raw = getStorage()?.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CreditsNotice> | null;
    return typeof parsed?.projectId === "string"
      ? { projectId: parsed.projectId, at: String(parsed.at ?? "") }
      : null;
  } catch {
    return null;
  }
}

function persist(notice: CreditsNotice | null) {
  const storage = getStorage();
  if (!storage) return;
  try {
    if (notice) storage.setItem(STORAGE_KEY, JSON.stringify(notice));
    else storage.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode): degrade to session-only state.
  }
}

/**
 * True when *code* names the one failure no Agent turn can talk the provider
 * out of. Kept next to the store so the session reducer and the tray agree on
 * one string instead of each carrying a literal.
 */
export function isQuotaErrorCode(code: unknown): code is string {
  return code === MODEL_QUOTA_ERROR_CODE;
}

export const useModelCreditsStore = create<ModelCreditsState>((set, get) => ({
  notice: readPersisted(),
  markExhausted: (projectId) => {
    if (!projectId) return;
    // Re-reporting the same project keeps the original timestamp, so the
    // notice cannot look freshly raised while a run keeps failing.
    if (get().notice?.projectId === projectId) return;
    const notice: CreditsNotice = {
      projectId,
      at: new Date().toISOString(),
    };
    set({ notice });
    persist(notice);
  },
  clear: () => {
    if (!get().notice) return;
    set({ notice: null });
    persist(null);
  },
}));

/**
 * Plain entry points for the session reducer.
 *
 * The event reducer is a state machine over one project's session and this
 * notice is cross-project, so the calls stay as small as they can be and the
 * store itself remains the only thing that touches localStorage.
 */
export function markCreditsExhausted(projectId: string): void {
  useModelCreditsStore.getState().markExhausted(projectId);
}

export function clearCreditsNotice(): void {
  useModelCreditsStore.getState().clear();
}
