import { create } from "zustand";
import { getCreatorSession } from "@/api/creator/sessions";
import { creatorReadSequence } from "@/api/creator/client";
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
  observed?: { first: CreatorSessionView; current: CreatorSessionView };
  readFailed?: true;
}

// Used only after an explicit click on a legacy notice without a server source.
let pendingLegacyDismissal: PendingLegacyDismissal | null = null;

interface ModelCreditsState {
  notice: CreditsNotice | null;
  recovery: CreditsRecovery | null;
  dismissed: CreditsNotice | null;
  lastReadAtDismissal: number;
  markExhausted: (
    projectId: string,
    source?: CreditsSource,
    live?: boolean,
  ) => void;
  clear: (currentSession?: CreatorSessionView | null) => void;
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
  const known =
    notice.source?.sessionId === session.id ? notice.source : undefined;
  const refusedAt = known?.at ?? session.error?.details?.refusedAt;
  return {
    ...notice,
    source: {
      sessionId: session.id,
      seq: Math.max(session.lastEventSeq, known?.seq ?? 0),
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
  lastReadAtDismissal: -1,
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
  clear: (currentSession) => {
    const { notice, recovery, dismissed } = get();
    if (!notice) return;
    // Ignore this refusal (or older history) after a manual dismissal. An
    // unrelated project's snapshot and recovery do not acknowledge a new refusal.
    const closing =
      currentSession?.projectId === notice.projectId &&
      (!notice.source || notice.source.sessionId === currentSession.id)
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
      lastReadAtDismissal: creatorReadSequence(),
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
        bindLegacyDismissal(pending, session);
      })
      .catch(() => {
        if (pendingLegacyDismissal !== pending) return;
        // The next SSE event can still belong to a read started before the click.
        pending.readFailed = true;
        settleFailedDismissal(pending);
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
  const { notice, recovery, dismissed } = useModelCreditsStore.getState();
  return (
    notice?.projectId === projectId ||
    recovery?.clearedNotice === true ||
    (dismissed?.projectId === projectId &&
      (hasServerSource(dismissed) ||
        pendingLegacyDismissal?.notice === dismissed))
  );
}

function canReplayCreditsRefusal(
  projectId: string,
  source: CreditsSource,
): boolean {
  const { notice, dismissed } = useModelCreditsStore.getState();
  // A current quota-error snapshot may legitimately show the notice again.
  if (notice?.projectId === projectId) return true;
  return (
    tracksCreditsHistory(projectId) &&
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
    if (pending.readFailed) settleFailedDismissal(pending);
    return;
  }
  if (live || canReplayCreditsRefusal(projectId, source))
    state.markExhausted(projectId, source, live);
}

function bindLegacyDismissal(
  pending: PendingLegacyDismissal,
  session: CreatorSessionView,
): void {
  pendingLegacyDismissal = null;
  const state = useModelCreditsStore.getState();
  if (
    state.dismissed !== pending.notice ||
    session.projectId !== pending.notice.projectId
  )
    return;
  const dismissed = atSessionCursor(pending.notice, session);
  useModelCreditsStore.setState({ dismissed });
  persist(RECOVERY_KEY, { ...state.recovery, dismissed });
  if (pending.refusal)
    markCreditsExhausted(
      dismissed.projectId,
      pending.refusal.source,
      pending.refusal.live,
    );
}

function settleFailedDismissal(pending: PendingLegacyDismissal): void {
  // Partially replayed SSE can pair an old ERROR status with a later refusal.
  // Only session reads started after the click can confirm what is current.
  if (!pending.observed) return;
  const { first, current } = pending.observed;
  const refusal = pending.refusal;
  // Even this fallback snapshot was read after the click, so everything
  // beyond its cursor definitely happened after the dismissal.
  if ((refusal?.source.seq ?? -1) > first.lastEventSeq) {
    bindLegacyDismissal(pending, first);
    return;
  }
  const currentError =
    current.status === "ERROR" && isQuotaErrorCode(current.error?.code);
  if (
    refusal?.live &&
    currentError &&
    current.id === refusal.source.sessionId
  ) {
    const refusedAt = current.error?.details?.refusedAt;
    const matches =
      typeof refusedAt === "string"
        ? refusedAt === refusal.source.at
        : current.lastEventSeq === refusal.source.seq;
    if (matches && (refusal.source.seq ?? 0) <= current.lastEventSeq) {
      pendingLegacyDismissal = null;
      useModelCreditsStore
        .getState()
        .markExhausted(current.projectId, refusal.source, true);
      return;
    }
  }
  // A stopped retry can replay the old refusal as apparently live. Wait for
  // an accepted read of this project before releasing those events.
  if (
    current.status === "IDLE" ||
    current.status === "CANCELLED" ||
    (current.status === "ERROR" && !currentError)
  )
    bindLegacyDismissal(pending, first);
}

export function observeSessionAfterCreditsDismissal(
  session: CreatorSessionView,
): void {
  const pending = pendingLegacyDismissal;
  if (!pending || pending.notice.projectId !== session.projectId) return;
  if (pending.observed) pending.observed.current = session;
  else pending.observed = { first: session, current: session };
  if (pending.readFailed) settleFailedDismissal(pending);
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
    // Keep the click cursor for later history, even when another run recovers.
    const dismissed =
      state.dismissed &&
      pendingLegacyDismissal?.notice !== state.dismissed &&
      !hasServerSource(state.dismissed) &&
      afterDismissal(state.dismissed, projectId, source, live)
        ? null
        : state.dismissed;
    recordRecovery(recovery, cleared, dismissed);
  } else if (cleared) {
    recordRecovery(state.recovery, cleared);
  }
}
