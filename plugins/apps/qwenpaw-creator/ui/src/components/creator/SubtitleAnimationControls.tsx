import { useTranslation } from "react-i18next";

import type { SubtitleAnimationPolicy } from "@/contracts/creator";

/**
 * Entrance / exit / loop picker for the standard subtitle bar.
 *
 * The three choices are exactly the vocabulary the caption renderer
 * understands (backend ``CAPTION_ENTRANCES`` and ``data-motion-exit``), so a
 * pick here is a promise the burned-in captions actually keep.  Leaving a
 * choice unset hands the decision to the selected style pack, which is why
 * the component never invents a default of its own.
 */

export type CaptionEntrance = NonNullable<SubtitleAnimationPolicy["entrance"]>;
export type CaptionExit = NonNullable<SubtitleAnimationPolicy["exit"]>;

/** Entrances the caption template can draw, in the order they are offered. */
export const CAPTION_ENTRANCE_CHOICES: Array<{
  id: CaptionEntrance;
  key: string;
}> = [
  { id: "soft_fade", key: "home.subtitleMotionEntranceFade" },
  { id: "pop", key: "home.subtitleMotionEntrancePop" },
  { id: "slide", key: "home.subtitleMotionEntranceSlide" },
  { id: "type_on", key: "home.subtitleMotionEntranceTypeOn" },
];

/** Exit choices; the composed master hands these to the caption track. */
export const CAPTION_EXIT_CHOICES: Array<{ id: CaptionExit; key: string }> = [
  { id: "none", key: "home.subtitleMotionExitNone" },
  { id: "soft_fade", key: "home.subtitleMotionExitFade" },
  { id: "shrink", key: "home.subtitleMotionExitShrink" },
];

const pillBase =
  "cursor-pointer rounded-full border px-2.5 py-1 text-[11px] leading-4 transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const pillOn =
  "border-[var(--color-text-primary)] bg-[rgba(43,27,0,0.06)] font-semibold text-[var(--color-text-primary)] dark:bg-white/10";
const pillOff =
  "border-transparent bg-[rgba(43,27,0,0.03)] text-[var(--color-text-secondary)] hover:bg-[rgba(43,27,0,0.06)] dark:bg-white/5 dark:hover:bg-white/10";
const labelClass = "text-[11px] text-[var(--color-text-tertiary)]";

interface PillRowProps<T extends string | boolean> {
  label: string;
  choices: Array<{ id: T; key: string }>;
  selected?: T | null;
  disabled?: boolean;
  onPick: (id: T) => void;
}

function PillRow<T extends string | boolean>({
  label,
  choices,
  selected,
  disabled,
  onPick,
}: PillRowProps<T>) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-1.5">
      <span className={labelClass}>{label}</span>
      {choices.map((choice) => (
        <button
          key={String(choice.id)}
          type="button"
          disabled={disabled}
          aria-pressed={selected === choice.id}
          onClick={() => onPick(choice.id)}
          className={`${pillBase} ${selected === choice.id ? pillOn : pillOff}`}
        >
          {t(choice.key)}
        </button>
      ))}
    </div>
  );
}

const LOOP_CHOICES: Array<{ id: boolean; key: string }> = [
  { id: false, key: "home.subtitleMotionLoopOff" },
  { id: true, key: "home.subtitleMotionLoopOn" },
];

interface SubtitleAnimationControlsProps {
  /** What the addressed captions currently play; ``null`` = pack default. */
  value: SubtitleAnimationPolicy | null;
  onChange: (animation: SubtitleAnimationPolicy) => void;
  disabled?: boolean;
}

export function SubtitleAnimationControls({
  value,
  onChange,
  disabled = false,
}: SubtitleAnimationControlsProps) {
  const { t } = useTranslation();
  const current = value ?? {};

  return (
    <div className="flex flex-col gap-2">
      <span className="text-[11px] font-semibold text-[var(--color-text-tertiary)]">
        {t("home.subtitleMotion")}
      </span>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <PillRow
          label={t("home.subtitleMotionEntrance")}
          choices={CAPTION_ENTRANCE_CHOICES}
          selected={value?.entrance}
          disabled={disabled}
          onPick={(entrance) => onChange({ ...current, entrance })}
        />
        <PillRow
          label={t("home.subtitleMotionExit")}
          choices={CAPTION_EXIT_CHOICES}
          selected={value?.exit}
          disabled={disabled}
          onPick={(exit) => onChange({ ...current, exit })}
        />
        <PillRow
          label={t("home.subtitleMotionLoop")}
          choices={LOOP_CHOICES}
          selected={value?.loop}
          disabled={disabled}
          onPick={(loop) => onChange({ ...current, loop })}
        />
      </div>
      {value == null && (
        <span className={labelClass}>
          {t("home.subtitleMotionPackDefault")}
        </span>
      )}
    </div>
  );
}

export default SubtitleAnimationControls;
