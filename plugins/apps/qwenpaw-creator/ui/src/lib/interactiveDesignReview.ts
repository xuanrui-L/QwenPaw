import type {
  FileProjectReviewRecord,
  ModelConfigData,
} from "@/contracts/creator";

/** Compatibility for runtimes that publish interactive HTML with a required
 * review even under YOLO. Never extend this to scripts, deletions, execution
 * approvals, or mixed authoring changes. */
export function isInteractiveDesignReview(
  review: FileProjectReviewRecord,
): boolean {
  if (review.status !== "PENDING") return false;
  const pending = review.operations.filter(
    (operation) => operation.decision === "PENDING",
  );
  if (!pending.length) return false;
  const groups = new Map<string, boolean>();
  for (const operation of pending) {
    if (
      !["create", "update"].includes(operation.kind) ||
      operation.after == null
    )
      return false;
    const match = (operation.json_pointer ?? "").match(
      /^(\/interactive_presentation|\/timelines\/items\/[^/]+\/elements_by_id\/[^/]+\/creation)\/(motion(?:\/(?:html|format|fps|loop|design_notes|emotion|entrance|exit))?|design_prompt)$/u,
    );
    if (!match) return false;
    if (match[2] === "motion") {
      const value = operation.after;
      if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
      const motion = value as Record<string, unknown>;
      if (
        motion.format !== "html_css" ||
        typeof motion.html !== "string" ||
        !motion.html.trim()
      )
        return false;
    }
    if (
      match[2] === "motion/html" &&
      (typeof operation.after !== "string" || !operation.after.trim())
    )
      return false;
    // Regenerations can publish only changed HTML/metadata rather than the
    // whole object. A prompt-only authoring edit is not a generated result.
    groups.set(
      match[1],
      Boolean(groups.get(match[1])) || match[2].startsWith("motion"),
    );
  }
  return [...groups.values()].every(Boolean);
}

export function usesYoloReview(config: ModelConfigData): boolean {
  return (
    config?.executionAuthorization?.mode === "allow_all" &&
    config?.creationCheckpoints?.mode === "skip" &&
    config?.mediaReview?.mode === "auto_approve"
  );
}

export function interactiveReviewKey(review: FileProjectReviewRecord) {
  return JSON.stringify([
    review.review_id,
    review.operations
      .filter((op) => op.decision === "PENDING")
      .map((op) => [op.operation_id, op.before_hash, op.after_hash])
      .sort((a, b) => a[0].localeCompare(b[0])),
  ]);
}
