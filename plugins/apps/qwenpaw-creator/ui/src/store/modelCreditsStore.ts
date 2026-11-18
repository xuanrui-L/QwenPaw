import { create } from "zustand";
import { getCreatorSession } from "@/api/creator/sessions";
import type { CreatorSessionView } from "@/contracts/creator";

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
  /** A successful run cleared a visible refusal, so newer history can restore it. */
  clearedNotice?: true;
}

interface PersistedCreditsHistory extends Partial<CreditsRecovery> {
  dismissed?: CreditsNotice;
}

interface PendingLegacyDismissal {
  notice: CreditsNotice;
  refusal?: { source: CreditsSource; live: boolean };
}

// Only an explicit click on an old, unbound notice can have a cursor in flight.
let pendingLegacyDismissal: PendingLegacyDismissal | null = null;

interface ModelCreditsState {
  notice: CreditsNotice | null;
  recovery: CreditsRecovery | null;
  dismissed: CreditsNotice | null;
  dismissalVersion: number;
  markExhausted: (
    projectId: string,
    source?: CreditsSource,
    live?: boolean,
  ) => void;
  clear: (readSession?: () => CreatorSessionView | null) => void;
}

// Lazy lookup: tests replace window.localStorage; don't capture at load.
function getStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

function readStored(key: string): PersistedCreditsHistory | null {
  try {
    const raw = getStorage()?.getItem(key);
    return raw ? (JSON.parse(raw) as PersistedCreditsHistory | null) : null;
  } catch {
    return null;
  }
}

function storedNotice(
  value?: Partial<CreditsNotice> | null,
): CreditsNotice | null {
  return typeof value?.projectId === "string"
    ? {
        projectId: value.projectId,
        at: String(value.at ?? ""),
        source: value.source,
      }
    : null;
}

function readHistory() {
  const value = readStored(RECOVERY_KEY);
  const success = storedNotice(value);
  const recovery: CreditsRecovery | null =
    success && value?.clearedNotice === true
      ? { ...success, clearedNotice: true }
      : success;
  return { recovery, dismissed: storedNotice(value?.dismissed) };
}

function persist(key: string, notice: PersistedCreditsHistory | null) {
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

function hasServerSource(notice: CreditsNotice): boolean {
  return notice.source?.seq !== undefined || Boolean(notice.source?.at);
}

function atSessionCursor(
  notice: CreditsNotice,
  session: CreatorSessionView,
): CreditsNotice {
  const refusedAt = session.error?.details?.refusedAt;
  return {
    ...notice,
    source: {
      sessionId: session.id,
      seq: session.lastEventSeq,
      ...(typeof refusedAt === "string" && { at: refusedAt }),
    },
  };
}

function afterDismissal(
  dismissed: CreditsNotice,
  projectId: string,
  source: CreditsSource,
  live: boolean,
): boolean {
  // A legacy timestamp came from the browser. A replay cannot place it
  // relative to a server event unless the dismissal has server evidence.
  if (!hasServerSource(dismissed)) return live;
  return newerThanNotice(dismissed, projectId, source, live);
}

export const useModelCreditsStore = create<ModelCreditsState>((set, get) => ({
  notice: storedNotice(readStored(STORAGE_KEY)),
  ...readHistory(),
  dismissalVersion: 0,
  markExhausted: (projectId, source, live = true) => {
    if (!projectId) return;
    const { notice: previous, recovery, dismissed } = get();
    // A poll can show a refusal before the live stream delivers the same event.
    if (
      live &&
      source &&
      dismissed?.projectId === projectId &&
      previous?.projectId !== projectId &&
      !afterDismissal(dismissed, projectId, source, true)
    )
      return;
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
  clear: (readSession) => {
    const { notice, recovery, dismissed, dismissalVersion } = get();
    if (!notice) return;
    const currentSession = readSession?.();
    // Ignore this refusal (or older history) after a manual dismissal. An
    // unrelated project's snapshot and recovery do not acknowledge a new refusal.
    const closing =
      !hasServerSource(notice) && currentSession?.projectId === notice.projectId
        ? atSessionCursor(notice, currentSession)
        : notice;
    const latest =
      !dismissed ||
      !closing.source ||
      afterDismissal(dismissed, closing.projectId, closing.source, true)
        ? closing
        : dismissed;
    const nextRecovery = recovery && { ...recovery };
    if (nextRecovery) delete nextRecovery.clearedNotice;
    set({
      notice: null,
      recovery: nextRecovery,
      dismissed: latest,
      dismissalVersion: dismissalVersion + 1,
    });
    persist(RECOVERY_KEY, { ...nextRecovery, dismissed: latest });
    persist(STORAGE_KEY, null);

    // Old notices have no server identity. Only a manual dismissal away from
    // their project needs to read that project's cursor once.
    if (pendingLegacyDismissal?.notice !== latest)
      pendingLegacyDismissal = null;
    if (latest !== closing || hasServerSource(latest)) return;
    const pending: PendingLegacyDismissal = { notice: latest };
    pendingLegacyDismissal = pending;
    // An open started before the click may have read a different event cursor.
    void getCreatorSession(latest.projectId, { dedupe: false })
      .then(({ session }) => {
        if (pendingLegacyDismissal !== pending) return;
        pendingLegacyDismissal = null;
        if (session.projectId !== latest.projectId) return;
        const state = get();
        if (state.dismissed === latest) {
          const bound = atSessionCursor(latest, session);
          set({ dismissed: bound });
          persist(RECOVERY_KEY, { ...state.recovery, dismissed: bound });
        }
        if (pending.refusal)
          markCreditsExhausted(
            latest.projectId,
            pending.refusal.source,
            pending.refusal.live,
          );
      })
      .catch(() => {
        if (pendingLegacyDismissal !== pending) return;
        pendingLegacyDismissal = null;
        // With no cursor, leave old history hidden. A live refusal that still
        // matches the active session error is independent evidence.
        const active = readSession?.();
        const refusal = pending.refusal;
        if (
          !refusal?.live ||
          active?.projectId !== latest.projectId ||
          active.id !== refusal.source.sessionId ||
          active.status !== "ERROR" ||
          !isQuotaErrorCode(active.error?.code)
        )
          return;
        const at = active.error?.details?.refusedAt;
        if (typeof at !== "string" || at === refusal.source.at)
          markCreditsExhausted(latest.projectId, refusal.source, true);
      });
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

function canReplayCreditsRefusal(
  projectId: string,
  source: CreditsSource,
): boolean {
  const { notice, recovery, dismissed } = useModelCreditsStore.getState();
  // A current quota-error snapshot may legitimately show the notice again.
  if (notice?.projectId === projectId) return true;
  return (
    recovery?.clearedNotice === true &&
    (!dismissed || afterDismissal(dismissed, projectId, source, false))
  );
}

function recordRecovery(
  recovery: CreditsRecovery,
  cleared?: CreditsNotice,
  dismissed = useModelCreditsStore.getState().dismissed,
): void {
  const next: CreditsRecovery = cleared
    ? { ...recovery, clearedNotice: true }
    : recovery;
  useModelCreditsStore.setState(
    cleared
      ? { notice: null, recovery: next, dismissed }
      : { recovery: next, dismissed },
  );
  persist(RECOVERY_KEY, dismissed ? { ...next, dismissed } : next);
  if (cleared) persist(STORAGE_KEY, null);
}

export function markCreditsExhausted(
  projectId: string,
  source: CreditsSource,
  live: boolean,
): void {
  const state = useModelCreditsStore.getState();
  const pending = pendingLegacyDismissal;
  if (
    pending &&
    pending.notice === state.dismissed &&
    pending.notice.projectId === projectId
  ) {
    const previous = pending.refusal?.source;
    if (
      !previous ||
      newerThanNotice(
        { ...pending.notice, source: previous },
        projectId,
        source,
        live,
      )
    )
      pending.refusal = { source, live };
    return;
  }
  if (live || canReplayCreditsRefusal(projectId, source))
    state.markExhausted(projectId, source, live);
}

export function markSnapshotCredits(
  projectId: string,
  source: CreditsSource,
  reopening = false,
): void {
  const { dismissed, markExhausted } = useModelCreditsStore.getState();
  // Reopening adopts the current server error. Background observations of the
  // same refusal must respect a dismissal made while the project is open.
  if (
    !reopening &&
    dismissed?.projectId === projectId &&
    !afterDismissal(dismissed, projectId, source, false)
  )
    return;
  markExhausted(projectId, source, false);
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
    // A stream opened before the click can deliver an older success as live.
    // Record the success, but keep the dismissal until its cursor is known.
    const dismissed =
      state.dismissed &&
      pendingLegacyDismissal?.notice !== state.dismissed &&
      afterDismissal(state.dismissed, projectId, source, live)
        ? null
        : state.dismissed;
    recordRecovery(recovery, cleared, dismissed);
  } else if (cleared) {
    recordRecovery(state.recovery, cleared);
  }
}
