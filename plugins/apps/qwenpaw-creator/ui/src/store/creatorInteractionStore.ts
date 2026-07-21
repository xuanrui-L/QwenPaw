import { create } from "zustand";
import type { RefSearchItem } from "@/contracts/creator";
import type { SelectionAttachment } from "./agentDockUiStore";

export type CreatorPanel = "plan" | "assets" | "other";

interface CreatorInteractionState {
  panel: CreatorPanel;
  selectedRef: string | null;
  editingField: string | null;
  selection: SelectionAttachment | null;
  extraRefs: RefSearchItem[];
  /**
   * When non-null, overrides the playhead-derived active element set shown in
   * ElementList with a specific set of element IDs (e.g. all elements of a
   * timeline lane the user clicked).  Set back to null to resume the default
   * "elements at the current playhead tick" behavior.
   */
  activeLaneElementIds: string[] | null;
  setPanel: (panel: CreatorPanel) => void;
  select: (ref: string | null) => void;
  setEditingField: (field: string | null) => void;
  setSelection: (selection: SelectionAttachment | null) => void;
  setExtraRefs: (refs: RefSearchItem[]) => void;
  setActiveLaneElementIds: (ids: string[] | null) => void;
  reset: () => void;
}

export const useCreatorInteractionStore = create<CreatorInteractionState>(
  (set) => ({
    panel: "other",
    selectedRef: null,
    editingField: null,
    selection: null,
    extraRefs: [],
    activeLaneElementIds: null,
    setPanel: (panel) => set({ panel }),
    select: (selectedRef) => set({ selectedRef }),
    setEditingField: (editingField) => set({ editingField }),
    setSelection: (selection) => set({ selection }),
    setExtraRefs: (extraRefs) => set({ extraRefs }),
    setActiveLaneElementIds: (activeLaneElementIds) =>
      set({ activeLaneElementIds }),
    reset: () =>
      set({
        panel: "other",
        selectedRef: null,
        editingField: null,
        selection: null,
        extraRefs: [],
        activeLaneElementIds: null,
      }),
  }),
);
