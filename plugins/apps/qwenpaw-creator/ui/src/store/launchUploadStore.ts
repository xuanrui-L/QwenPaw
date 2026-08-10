import { create } from "zustand";

export type LaunchUploadPhase = "uploading" | "messaging" | "done" | "error";

interface LaunchUploadState {
  /** Project currently ingesting its launch attachments, if any. */
  projectId: string | null;
  phase: LaunchUploadPhase;
  total: number;
  done: number;
  failed: number;
  /** File names currently in flight (bounded by the upload pool size). */
  activeNames: string[];
  begin: (projectId: string, total: number) => void;
  fileStarted: (name: string) => void;
  fileFinished: (name: string, ok: boolean) => void;
  messaging: () => void;
  finish: (ok: boolean) => void;
  reset: () => void;
}

/**
 * Launch-time attachment ingest progress, decoupled from the composer's
 * component lifecycle: the composer navigates to the project page right
 * after the Project exists, and the background continuation reports here
 * so the workspace can show "uploading n/m" instead of a silent IDLE.
 */
export const useLaunchUploadStore = create<LaunchUploadState>((set) => ({
  projectId: null,
  phase: "done",
  total: 0,
  done: 0,
  failed: 0,
  activeNames: [],
  begin: (projectId, total) =>
    set({
      projectId,
      total,
      done: 0,
      failed: 0,
      phase: "uploading",
      activeNames: [],
    }),
  fileStarted: (name) =>
    set((state) => ({ activeNames: [...state.activeNames, name] })),
  fileFinished: (name, ok) =>
    set((state) => ({
      done: state.done + 1,
      failed: state.failed + (ok ? 0 : 1),
      activeNames: state.activeNames.filter((item) => item !== name),
    })),
  messaging: () => set({ phase: "messaging", activeNames: [] }),
  finish: (ok) => set({ phase: ok ? "done" : "error", activeNames: [] }),
  reset: () =>
    set({
      projectId: null,
      phase: "done",
      total: 0,
      done: 0,
      failed: 0,
      activeNames: [],
    }),
}));
