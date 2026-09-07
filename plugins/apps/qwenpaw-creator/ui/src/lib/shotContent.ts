import type { ShotDocument } from "@/contracts/creator";

/** Keep legacy spoken content visible in the single shot content editor. */
export function shotContentText(
  shot: Pick<ShotDocument, "description" | "dialogue">,
  dialogueLabel = "对白",
): string {
  const description = shot.description ?? "";
  const dialogue = shot.dialogue?.trim();
  if (!dialogue || description.includes(dialogue)) return description;
  return `${description}${
    description ? "\n\n" : ""
  }${dialogueLabel}：${dialogue}`;
}

/** The visible text is authoritative; synchronization derives spoken metadata. */
export function editShotContent(shot: ShotDocument, description: string): void {
  shot.description = description;
  shot.dialogue = "";
}
