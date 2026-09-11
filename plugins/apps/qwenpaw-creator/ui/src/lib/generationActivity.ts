import type { TaskView } from "@/contracts/creator";

/** In-flight generation for a work-graph node — regenerating while the
    provider is already painting is ambiguous, so pills must wait. */
export function nodeGenerating(
  tasks: TaskView[],
  nodeId: string | null,
): boolean {
  if (!nodeId) return false;
  const cut = nodeId.indexOf(":");
  const prefix = nodeId.slice(0, cut);
  const rest = nodeId.slice(cut + 1);
  const expected: [TaskView["kind"], string] | null =
    prefix === "visual"
      ? ["image_generation", `asset:${rest.split(":")[0]}`]
      : prefix === "lineup"
      ? ["image_generation", `lineup:${rest}`]
      : prefix === "storyboard"
      ? ["image_generation", `element:${rest}`]
      : prefix === "video"
      ? ["r2v_generation", `element:${rest}`]
      : null;
  if (!expected) return false;
  return tasks.some(
    (task) =>
      task.kind === expected[0] &&
      task.targetRef === expected[1] &&
      (task.status === "RUNNING" || task.status === "QUEUED"),
  );
}
