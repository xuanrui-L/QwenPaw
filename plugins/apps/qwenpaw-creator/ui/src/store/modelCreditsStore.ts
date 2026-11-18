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
const RECOVERY_KEY = "qwenpaw-creator:model-credits:last-success";

/** Session error code the runtime reports for a refused Credits spend. */
export const MODEL_QUOTA_ERROR_CODE = "MODEL_QUOTA_EXCEEDED";

export interface CreditsSource {
  sessionId: string;
  seq?: number;
  at?: string;
}

export interface CreditsNotice {
  /** Project whose run last proved the balance is empty. */
  projectId: string;
  /** ISO timestamp of the first refusal seen for that project. */
  at: string;
  source?: CreditsSource;
}

interface CreditsRecovery extends CreditsNotice {
  /** Keep comparing account-wide server evidence after a success cleared the notice. */
  clearedNotice?: true;
}

interface ModelCreditsState {
  notice: CreditsNotice | null;
  recovery: CreditsRecovery | null;
  markExhausted: (
    projectId: string,
    source?: CreditsSource,
    live?: boolean,
  ) => void;
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

function readPersisted(key: string): CreditsRecovery | null {
  try {
    const raw = getStorage()?.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CreditsRecovery> | null;
    return typeof parsed?.projectId === "string"
      ? {
          projectId: parsed.projectId,
          at: String(parsed.at ?? ""),
          source: parsed.source,
          ...(parsed.clearedNotice === true && { clearedNotice: true }),
        }
      : null;
  } catch {
    return null;
  }
}

function persist(key: string, notice: CreditsNotice | null) {
  const storage = getStorage();
  if (!storage) return;
  try {
    if (notice) storage.setItem(key, JSON.stringify(notice));
    else storage.removeItem(key);
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

// Python's ISO timestamps retain microseconds even when two events share a millisecond.
function serverMicros(at = ""): number {
  const remainder = at.match(/\.\d{3}(\d{1,3})/u)?.[1] ?? "";
  return Date.parse(at) * 1000 + Number(remainder.padEnd(3, "0"));
}

// Sequence numbers order events within a session. Across sessions the server
// timestamps order them. Legacy notices have only a browser timestamp, so
// use a live completion as evidence until a server observation is available.
function newerThanNotice(
  notice: CreditsNotice,
  projectId: string,
  source: CreditsSource,
  live: boolean,
): boolean {
  if (
    notice.projectId === projectId &&
    notice.source?.sessionId === source.sessionId &&
    notice.source.seq !== undefined &&
    source.seq !== undefined
  ) {
    return source.seq > notice.source.seq;
  }
  if (
    notice.projectId === projectId &&
    notice.source?.sessionId === source.sessionId &&
    notice.source.seq === undefined &&
    !notice.source.at &&
    source.seq !== undefined
  )
    return true;
  const previous = serverMicros(notice.source?.at);
  const incoming = serverMicros(source.at);
  return Number.isFinite(previous) && Number.isFinite(incoming)
    ? incoming > previous
    : live;
}

export const useModelCreditsStore = create<ModelCreditsState>((set, get) => ({
  notice: readPersisted(STORAGE_KEY),
  recovery: readPersisted(RECOVERY_KEY),
  markExhausted: (projectId, source, live = true) => {
    if (!projectId) return;
    const { notice: previous, recovery } = get();
    // When the original project replays its own refusal, it supplies the
    // server time that old localStorage records lack.
    const observedLegacyRefusal =
      previous?.projectId === projectId &&
      !previous.source &&
      source !== undefined &&
      Number.isFinite(serverMicros(source.at));
    if (
      source &&
      recovery &&
      !newerThanNotice(recovery, projectId, source, live)
    ) {
      if (
        observedLegacyRefusal &&
        recovery.source &&
        newerThanNotice(
          { ...previous, source },
          recovery.projectId,
          recovery.source,
          false,
        )
      )
        recordRecovery(recovery, previous);
      return;
    }
    if (
      previous &&
      !observedLegacyRefusal &&
      (source
        ? !newerThanNotice(previous, projectId, source, live)
        : previous.projectId === projectId)
    )
      return;
    const notice: CreditsNotice = {
      projectId,
      at:
        previous?.projectId === projectId
          ? previous.at
          : new Date().toISOString(),
      ...(source && { source }),
    };
    set({ notice });
    persist(STORAGE_KEY, notice);
  },
  clear: () => {
    const { notice, recovery } = get();
    if (!notice) return;
    // An explicit dismissal ends historical tracking for the cleared banner.
    if (recovery?.clearedNotice) {
      const next = { ...recovery };
      delete next.clearedNotice;
      set({ notice: null, recovery: next });
      persist(RECOVERY_KEY, next);
    } else {
      set({ notice: null });
    }
    persist(STORAGE_KEY, null);
  },
}));

/**
 * Plain entry points for the session reducer.
 *
 * The event reducer is a state machine over one project's session and this
 * notice is cross-project, so the calls stay as small as they can be and the
 * store itself remains the only thing that touches localStorage.
 */
export function tracksCreditsHistory(projectId: string): boolean {
  const { notice, recovery } = useModelCreditsStore.getState();
  return notice?.projectId === projectId || recovery?.clearedNotice === true;
}

function recordRecovery(
  recovery: CreditsRecovery,
  cleared?: CreditsNotice,
): void {
  if (cleared) {
    const next: CreditsRecovery = { ...recovery, clearedNotice: true };
    useModelCreditsStore.setState({ notice: null, recovery: next });
    persist(RECOVERY_KEY, next);
    persist(STORAGE_KEY, null);
  } else {
    useModelCreditsStore.setState({ recovery });
    persist(RECOVERY_KEY, recovery);
  }
}

export function markCreditsExhausted(
  projectId: string,
  source: CreditsSource,
  live: boolean,
): void {
  useModelCreditsStore.getState().markExhausted(projectId, source, live);
}

export function clearCreditsAfterCompletion(
  projectId: string,
  source: CreditsSource,
  live: boolean,
): void {
  const state = useModelCreditsStore.getState();
  const cleared =
    state.notice && newerThanNotice(state.notice, projectId, source, live)
      ? state.notice
      : undefined;
  if (
    !state.recovery ||
    newerThanNotice(state.recovery, projectId, source, live)
  ) {
    const recovery: CreditsRecovery = {
      ...state.recovery,
      projectId,
      at: source.at ?? "",
      source,
    };
    recordRecovery(recovery, cleared);
  } else if (cleared) {
    recordRecovery(state.recovery, cleared);
  }
}
