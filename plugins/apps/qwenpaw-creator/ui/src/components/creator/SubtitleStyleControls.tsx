import { useTranslation } from "react-i18next";

import type { SubtitleStyle } from "@/contracts/creator";

/**
 * Colour / font-size / outline overrides for the standard subtitle bar.
 *
 * Mirrors the backend ``SubtitleStyle`` model: every control keeps a
 * "default" choice mapped to ``null`` so an untouched project persists no
 * style at all and renders exactly as before. The values drive the
 * deterministic plain-subtitle renderer (PIL burn-in) and its live preview;
 * designed 花字 keeps its own per-footage styling.
 */

/** Swatches offered for the subtitle text colour. */
const COLOR_SWATCHES: string[] = [
  "#FFFFFF",
  "#FFE066",
  "#7DD3FC",
  "#86EFAC",
  "#FCA5A5",
  "#C4B5FD",
  "#1F2937",
];

/** Font-size multiplier choices; ``null`` is the fitted base size. */
const SIZE_CHOICES: Array<{ id: number | null; key: string }> = [
  { id: 0.85, key: "home.subtitleStyleSizeSmall" },
  { id: null, key: "home.subtitleStyleSizeStandard" },
  { id: 1.25, key: "home.subtitleStyleSizeLarge" },
];

/** Outline-width multiplier choices; ``null`` is the base outline. */
const STROKE_CHOICES: Array<{ id: number | null; key: string }> = [
  { id: 0, key: "home.subtitleStyleStrokeNone" },
  { id: null, key: "home.subtitleStyleStrokeStandard" },
  { id: 1.8, key: "home.subtitleStyleStrokeThick" },
];

interface SubtitleStyleControlsProps {
  value: SubtitleStyle | null;
  onChange: (style: SubtitleStyle | null) => void;
}

export function SubtitleStyleControls({
  value,
  onChange,
}: SubtitleStyleControlsProps) {
  const { t } = useTranslation();

  const color = value?.color ?? null;
  const fontScale = value?.fontScale ?? null;
  const strokeScale = value?.strokeScale ?? null;

  /** Merge one override, dropping the whole object back to ``null``. */
  const emit = (patch: Partial<SubtitleStyle>) => {
    const next: SubtitleStyle = {
      color: patch.color !== undefined ? patch.color : color,
      fontScale: patch.fontScale !== undefined ? patch.fontScale : fontScale,
      strokeScale:
        patch.strokeScale !== undefined ? patch.strokeScale : strokeScale,
    };
    const isDefault =
      next.color == null && next.fontScale == null && next.strokeScale == null;
    onChange(isDefault ? null : next);
  };

  const pillBase =
    "cursor-pointer rounded-full border px-2.5 py-1 text-[11px] leading-4 transition-colors";
  const pillOn =
    "border-[var(--color-text-primary)] bg-[rgba(43,27,0,0.06)] font-semibold text-[var(--color-text-primary)] dark:bg-white/10";
  const pillOff =
    "border-transparent bg-[rgba(43,27,0,0.03)] text-[var(--color-text-secondary)] hover:bg-[rgba(43,27,0,0.06)] dark:bg-white/5 dark:hover:bg-white/10";

  return (
    <div className="flex flex-col gap-2">
      <span className="text-[11px] font-semibold text-[var(--color-text-tertiary)]">
        {t("home.subtitleStyle")}
      </span>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {/* Colour */}
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-[var(--color-text-tertiary)]">
            {t("home.subtitleStyleColor")}
          </span>
          <button
            type="button"
            aria-label={t("home.subtitleStyleColorDefault")}
            aria-pressed={color == null}
            onClick={() => emit({ color: null })}
            className={`${pillBase} ${color == null ? pillOn : pillOff}`}
          >
            {t("home.subtitleStyleColorDefault")}
          </button>
          {COLOR_SWATCHES.map((swatch) => (
            <button
              key={swatch}
              type="button"
              aria-label={swatch}
              aria-pressed={color === swatch}
              onClick={() => emit({ color: swatch })}
              className={`h-5 w-5 cursor-pointer rounded-full border transition-transform ${
                color === swatch
                  ? "border-[var(--color-text-primary)] scale-110"
                  : "border-[var(--color-border)] hover:scale-105"
              }`}
              style={{ backgroundColor: swatch }}
            />
          ))}
        </div>

        {/* Font size */}
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-[var(--color-text-tertiary)]">
            {t("home.subtitleStyleSize")}
          </span>
          {SIZE_CHOICES.map((choice) => {
            const selected = fontScale === choice.id;
            return (
              <button
                key={choice.key}
                type="button"
                aria-pressed={selected}
                onClick={() => emit({ fontScale: choice.id })}
                className={`${pillBase} ${selected ? pillOn : pillOff}`}
              >
                {t(choice.key)}
              </button>
            );
          })}
        </div>

        {/* Outline */}
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-[var(--color-text-tertiary)]">
            {t("home.subtitleStyleStroke")}
          </span>
          {STROKE_CHOICES.map((choice) => {
            const selected = strokeScale === choice.id;
            return (
              <button
                key={choice.key}
                type="button"
                aria-pressed={selected}
                onClick={() => emit({ strokeScale: choice.id })}
                className={`${pillBase} ${selected ? pillOn : pillOff}`}
              >
                {t(choice.key)}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default SubtitleStyleControls;
