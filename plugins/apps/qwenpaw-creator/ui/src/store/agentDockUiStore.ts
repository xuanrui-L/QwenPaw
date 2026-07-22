import { create } from "zustand";

export interface SelectionAttachment {
  text: string;
  ref: string | null;
  field: string | null;
  /** Exact RFC 6901 pointer to the canonical value in project.json, when available. */
  path?: string | null;
  start: number;
  end: number;
  label: string;
  /** Timeline selections use ticks; text selections leave these fields absent. */
  kind?: "text" | "timeline_point" | "timeline_range";
  timelineId?: string;
  startTick?: number;
  endTick?: number;
  elementIds?: string[];
}

export type AgentDockTab = "conversation" | "activity" | "review";

interface AgentDockUiState {
  open: boolean;
  tab: AgentDockTab;
  width: number;
  height: number;
  runFilter: string;
  draft: string;
  selection: SelectionAttachment | null;
  allowExpandDetails: boolean;
  setOpen: (open: boolean) => void;
  setTab: (tab: AgentDockTab) => void;
  setSize: (width: number, height: number) => void;
  setRunFilter: (filter: string) => void;
  setDraft: (draft: string) => void;
  setSelection: (selection: SelectionAttachment | null) => void;
  setAllowExpandDetails: (allow: boolean) => void;
  reset: () => void;
}

export const useAgentDockUiStore = create<AgentDockUiState>((set) => ({
  open: true,
  tab: "conversation",
  width: 440,
  height: 620,
  runFilter: "all",
  draft: "",
  selection: null,
  allowExpandDetails: false,
  setOpen: (open) => set({ open }),
  setTab: (tab) => set({ tab, open: true }),
  setSize: (width, height) =>
    set({ width: Math.max(440, width), height: Math.max(320, height) }),
  setRunFilter: (runFilter) => set({ runFilter }),
  setDraft: (draft) => set({ draft }),
  setSelection: (selection) =>
    set({ selection, tab: "conversation", open: true }),
  setAllowExpandDetails: (allowExpandDetails) => set({ allowExpandDetails }),
  reset: () =>
    set({
      open: true,
      tab: "conversation",
      width: 440,
      height: 620,
      runFilter: "all",
      draft: "",
      selection: null,
      allowExpandDetails: false,
    }),
}));
