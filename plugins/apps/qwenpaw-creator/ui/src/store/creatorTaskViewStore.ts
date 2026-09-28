import { create } from "zustand";
import type {
  CreatorEvent,
  SpecialistRunView,
  TaskView,
} from "@/contracts/creator";
import { cancelTask, listSpecialistRuns, listTasks } from "@/api/creator";

interface CreatorTaskViewState {
  projectId: string | null;
  runs: SpecialistRunView[];
  tasks: TaskView[];
  loading: boolean;
  error: string | null;
  refresh: (projectId: string) => Promise<void>;
  consumeEvent: (event: CreatorEvent) => void;
  cancel: (taskId: string) => Promise<void>;
  reset: () => void;
}

// Every refresh reads two independently durable collections. Multiple SSE
// lifecycle events can start overlapping reads, so a slower, older snapshot
// must never replace the result of a newer refresh (for example, restoring a
// WAITING_AUTHORIZATION run after SUCCEEDED has already been observed).
let refreshGeneration = 0;

function reconcileCancelledMediaRuns(
  runs: SpecialistRunView[],
  tasks: TaskView[],
): SpecialistRunView[] {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  return runs.map((run) => {
    // A media executor cannot continue once all its Tasks are cancelled.
    // Task and Run heads can arrive in separate polls (or from older servers
    // that omitted Run cleanup). Chat delegations may handle a cancelled
    // child tool and continue, so their own terminal event stays authoritative.
    if (
      !run.metadata.commandType ||
      run.metadata.parentActionId ||
      ![
        "QUEUED",
        "QUEUED_CAPACITY",
        "RUNNING_MODEL",
        "WAITING_RUNTIME",
        "WAITING_AUTHORIZATION",
      ].includes(run.status) ||
      run.taskRefs.length === 0 ||
      !run.taskRefs.every((id) => {
        const task = byId.get(id);
        return task?.specialistRunId === run.id && task.status === "CANCELLED";
      })
    )
      return run;
    return { ...run, status: "CANCELLED" };
  });
}

export const useCreatorTaskViewStore = create<CreatorTaskViewState>(
  (set, get) => ({
    projectId: null,
    runs: [],
    tasks: [],
    loading: false,
    error: null,
    refresh: async (projectId) => {
      const generation = ++refreshGeneration;
      set({ projectId, loading: true, error: null });
      try {
        const [runs, tasks] = await Promise.all([
          listSpecialistRuns(projectId),
          listTasks(projectId),
        ]);
        if (generation !== refreshGeneration || get().projectId !== projectId)
          return;
        set({
          runs: reconcileCancelledMediaRuns(runs.items, tasks.items),
          tasks: tasks.items,
          loading: false,
        });
      } catch (error) {
        if (generation !== refreshGeneration || get().projectId !== projectId)
          return;
        set({ loading: false, error: (error as Error).message });
        throw error;
      }
    },
    consumeEvent: (event) => {
      if (event.projectId !== get().projectId) return;
      const run = event.data.run as SpecialistRunView | undefined;
      const task = event.data.task as TaskView | undefined;
      if (run || task)
        set((state) => {
          const tasks = task
            ? [task, ...state.tasks.filter((item) => item.id !== task.id)]
            : state.tasks;
          const runs = run
            ? [run, ...state.runs.filter((item) => item.id !== run.id)]
            : state.runs;
          return { tasks, runs: reconcileCancelledMediaRuns(runs, tasks) };
        });
    },
    cancel: async (taskId) => {
      const initialProjectId = get().projectId;
      if (!initialProjectId) return;
      const updated = await cancelTask(initialProjectId, taskId);
      set((state) => {
        if (state.projectId !== initialProjectId) return {};
        const tasks = state.tasks.map((item) =>
          item.id === taskId ? updated : item,
        );
        return { tasks, runs: reconcileCancelledMediaRuns(state.runs, tasks) };
      });
    },
    reset: () => {
      refreshGeneration += 1;
      set({
        projectId: null,
        runs: [],
        tasks: [],
        loading: false,
        error: null,
      });
    },
  }),
);
