import { useTranslation } from "react-i18next";

import type {
  MotionGraphicDocument,
  ProjectSettingsDocument,
  SubtitleAnimationPolicy,
  SubtitleStyle,
  SubtitleStyleUpdateRequest,
} from "@/contracts/creator";
import SubtitleAnimationControls, {
  CAPTION_ENTRANCE_CHOICES,
  CAPTION_EXIT_CHOICES,
} from "./SubtitleAnimationControls";
import SubtitleStyleControls from "./SubtitleStyleControls";
import SubtitleTemplatePicker from "./SubtitleTemplatePicker";
import { SUBTITLE_TEMPLATE_PACKS } from "./SubtitleTemplatePicker";

/**
 * The subtitle style bar outside the creation flow.
 *
 * Every control here writes through one endpoint, so picking a pack changes
 * the project setting, the burned-in caption documents and each subtitle's
 * 动效描述 in the same step — the triangle the creation form only promises.
 * Nothing is kept in local state: the project document stays the only source
 * of what is currently in effect.
 */

interface SubtitleStyleSectionProps {
  settings: ProjectSettingsDocument;
  /**
   * Captions the animation picker should address (subtitle detail view).
   * Left out, the animation choice reaches every caption too.
   */
  elementIds?: string[];
  /** What the addressed captions play today; ``null`` = pack default. */
  animation?: SubtitleAnimationPolicy | null;
  /**
   * Whether the addressed captions' look comes from the deterministic caption
   * template.  Left out, the bar is shown for a project as a whole.  False
   * hides the motion picker: a picker that can never move its target is worse
   * than a sentence that says who owns the look.
   */
  motionOwned?: boolean;
  busy?: boolean;
  onApply: (request: SubtitleStyleUpdateRequest) => void;
}

/** Map the persisted snake_case overrides onto the control's camel shape. */
export function subtitleStyleFromSettings(
  settings: ProjectSettingsDocument,
): SubtitleStyle | null {
  const stored = settings.subtitle_style;
  if (!stored) return null;
  return {
    color: stored.color ?? null,
    fontScale: stored.font_scale ?? null,
    strokeScale: stored.stroke_scale ?? null,
  };
}

/**
 * Whether the caption style bar may redraw this document at all.
 *
 * Mirrors backend ``caption_owned_template``: only the deterministic
 * ``caption_card`` template is derived data.  A VLM ``html_js`` lettering, a
 * blueprint 花字 or an authored card owns a look no style pack overwrites, so
 * the route refuses to touch it and reports it as skipped.
 */
export function captionDocumentOwnedByStyle(
  motion: MotionGraphicDocument | null | undefined,
): boolean {
  return Boolean(
    motion &&
      motion.format === "html_css" &&
      motion.motif === "caption_card" &&
      motion.template_version != null,
  );
}

/**
 * What one caption document plays today, read the way the renderer reads it.
 *
 * Entrance and exit are document fields; the ambient loop is deliberately not
 * a document field (a tiled document would replay the entrance), so it lives
 * in the ``caption_animation=`` stamp the backend writes into design notes.
 * A document this pipeline never drew carries no stamp and reports nothing,
 * which keeps the pickers honest about "follow the style pack".
 */
export function subtitleAnimationFromDocument(
  motion: MotionGraphicDocument | null | undefined,
): SubtitleAnimationPolicy | null {
  if (!motion) return null;
  if (motion.format !== "html_css" || motion.motif !== "caption_card") {
    return null;
  }
  const stamp = /caption_animation=([^\s\n]*)/.exec(
    motion.design_notes ?? "",
  )?.[1];
  if (!stamp) return null;
  const [entrance, exit, loop] = stamp.split("/");
  // A token outside the renderer's vocabulary can only come from a damaged or
  // future document; casting it through would light up a pill the picker
  // cannot send back, so unknown parts simply read as "follow the pack".
  return {
    entrance:
      CAPTION_ENTRANCE_CHOICES.find((choice) => choice.id === entrance)?.id ??
      null,
    exit:
      CAPTION_EXIT_CHOICES.find((choice) => choice.id === exit)?.id ?? null,
    loop: loop === "1",
  };
}

export function SubtitleStyleSection({
  settings,
  elementIds,
  animation = null,
  motionOwned = true,
  busy = false,
  onApply,
}: SubtitleStyleSectionProps) {
  const { t } = useTranslation();
  const packId = settings.subtitle_template ?? null;
  const pack = SUBTITLE_TEMPLATE_PACKS.find((item) => item.id === packId);

  /**
   * A style pack is a project-wide promise, so only the motion picker may
   * address a single caption; scoping the pack itself would leave the other
   * subtitles describing — and drawing — the previous look.
   */
  const apply = (request: SubtitleStyleUpdateRequest, perElement = false) => {
    if (busy) return;
    onApply(
      perElement && elementIds?.length
        ? { ...request, elementIds }
        : request,
    );
  };

  return (
    <div
      data-subtitle-style-section
      className="flex flex-col gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-container)] p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-[var(--color-text-primary)]">
          {t("home.subtitleTemplateLabel")}
        </span>
        <span className="text-[11px] text-[var(--color-text-tertiary)]">
          {t("plan.subtitleStyleCurrent", {
            name: pack ? t(pack.nameKey) : t("plan.subtitleStyleSmart"),
          })}
        </span>
      </div>

      <SubtitleTemplatePicker
        value={packId}
        onChange={(id) => apply({ subtitleTemplate: id })}
      />

      <SubtitleStyleControls
        value={subtitleStyleFromSettings(settings)}
        onChange={(style) => apply({ subtitleStyle: style })}
      />

      {motionOwned ? (
        <SubtitleAnimationControls
          value={animation ?? null}
          onChange={(next) => apply({ animation: next }, true)}
          disabled={busy}
        />
      ) : (
        <span className="text-[11px] text-[var(--color-text-tertiary)]">
          {t("plan.subtitleStyleMotionUnowned")}
        </span>
      )}
      {elementIds?.length ? (
        <span className="text-[11px] text-[var(--color-text-tertiary)]">
          {t("plan.subtitleStyleElementScope")}
        </span>
      ) : null}

      <button
        type="button"
        disabled={busy}
        onClick={() => apply({ syncPrompts: true })}
        className="cursor-pointer self-start rounded-md border border-[var(--color-border)] px-2.5 py-1 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[rgba(43,27,0,0.04)] disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-white/10"
      >
        {t("plan.subtitleStyleSyncPrompts")}
      </button>
    </div>
  );
}

export default SubtitleStyleSection;
