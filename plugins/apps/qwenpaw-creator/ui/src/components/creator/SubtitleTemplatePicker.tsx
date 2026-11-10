import { useTranslation } from "react-i18next";

/** One JianYing-like subtitle style pack offered under the "auto" policy. */
export interface SubtitleTemplatePack {
  /** Stable id persisted as ``settings.subtitle_template``. */
  id: string;
  nameKey: string;
  descKey: string;
  /** Tailwind classes painting the mini glyph preview. */
  previewClass: string;
  glyphClass: string;
}

/** Mirrors backend ``SUBTITLE_TEMPLATE_PACKS`` order and ids. */
export const SUBTITLE_TEMPLATE_PACKS: SubtitleTemplatePack[] = [
  {
    id: "variety_pop",
    nameKey: "home.subtitleTemplateVarietyPop",
    descKey: "home.subtitleTemplateVarietyPopDesc",
    previewClass: "bg-gradient-to-br from-amber-300 to-rose-400",
    glyphClass: "font-black italic -rotate-6 text-white",
  },
  {
    id: "cinematic",
    nameKey: "home.subtitleTemplateCinematic",
    descKey: "home.subtitleTemplateCinematicDesc",
    previewClass: "bg-gradient-to-br from-slate-700 to-slate-900",
    glyphClass: "font-serif tracking-widest text-slate-100",
  },
  {
    id: "clean_talk",
    nameKey: "home.subtitleTemplateCleanTalk",
    descKey: "home.subtitleTemplateCleanTalkDesc",
    previewClass: "bg-white border border-slate-200",
    glyphClass: "font-medium text-slate-800",
  },
  {
    id: "warm_note",
    nameKey: "home.subtitleTemplateWarmNote",
    descKey: "home.subtitleTemplateWarmNoteDesc",
    previewClass: "bg-gradient-to-br from-amber-100 to-orange-200",
    glyphClass: "font-semibold [font-family:cursive] rotate-2 text-amber-900",
  },
  {
    id: "neon_music",
    nameKey: "home.subtitleTemplateNeonMusic",
    descKey: "home.subtitleTemplateNeonMusicDesc",
    previewClass: "bg-gradient-to-br from-fuchsia-600 to-cyan-500",
    glyphClass:
      "font-bold text-white [text-shadow:0_0_6px_rgba(255,255,255,0.9)]",
  },
];

interface SubtitleTemplatePickerProps {
  value: string | null;
  onChange: (id: string | null) => void;
}

/** Horizontal JianYing-style subtitle template strip shown for "auto". */
export function SubtitleTemplatePicker({
  value,
  onChange,
}: SubtitleTemplatePickerProps) {
  const { t } = useTranslation();

  const cards: Array<{
    id: string | null;
    name: string;
    desc: string;
    previewClass?: string;
    glyphClass?: string;
  }> = [
    {
      id: null,
      name: t("home.subtitleTemplateSmart"),
      desc: t("home.subtitleTemplateSmartDesc"),
    },
    ...SUBTITLE_TEMPLATE_PACKS.map((pack) => ({
      id: pack.id,
      name: t(pack.nameKey),
      desc: t(pack.descKey),
      previewClass: pack.previewClass,
      glyphClass: pack.glyphClass,
    })),
  ];

  return (
    <div className="flex items-stretch gap-2 overflow-x-auto pb-1">
      {cards.map((card) => {
        const selected = value === card.id;
        return (
          <button
            key={card.id ?? "smart"}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(card.id)}
            className={`flex min-w-[104px] flex-1 cursor-pointer flex-col gap-1 rounded-lg border p-2 text-left transition-colors ${
              selected
                ? "border-[var(--color-text-primary)] bg-[rgba(43,27,0,0.06)] dark:bg-white/10"
                : "border-transparent bg-[rgba(43,27,0,0.03)] hover:bg-[rgba(43,27,0,0.06)] dark:bg-white/5 dark:hover:bg-white/10"
            }`}
          >
            <span
              className={`flex h-9 items-center justify-center rounded-md ${
                card.previewClass ?? "bg-[rgba(43,27,0,0.05)] dark:bg-white/10"
              }`}
            >
              <span
                className={`text-lg leading-none ${
                  card.glyphClass ??
                  "font-semibold text-[var(--color-text-secondary)]"
                }`}
              >
                字
              </span>
            </span>
            <span className="text-xs font-semibold text-[var(--color-text-primary)]">
              {card.name}
            </span>
            <span className="text-[11px] leading-4 text-[var(--color-text-tertiary)]">
              {card.desc}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export default SubtitleTemplatePicker;
