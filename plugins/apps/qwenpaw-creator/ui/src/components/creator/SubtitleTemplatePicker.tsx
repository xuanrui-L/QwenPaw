import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";

import { creatorRequest } from "@/api/creator/client";
import type { SubtitleStyle } from "@/contracts/creator";

/**
 * Preview parameters of one subtitle style pack, exactly as the backend's
 * deterministic renderer paints them. Mirrors one item of the backend
 * ``subtitle_pack_preview_specs()`` payload.
 */
export interface SubtitlePackPreviewSpec {
  id: string | null;
  fontFamily: string;
  fontWeight: number;
  letterSpacing: string;
  lineHeight: number;
  strokeScale: number;
  tiltDeg: number;
  plate: string;
  plateRadius: string;
  glow: boolean;
  entrance: string;
  loop: boolean;
}

/**
 * Fallback mirror of backend ``subtitle_pack_preview_specs()`` (same order,
 * same values): the cards still paint the renderer's look when the preview
 * endpoint is unreachable — tests, stale hosts. The backend stays the single
 * source of truth; this table follows it.
 */
export const SUBTITLE_TEMPLATE_PACKS: SubtitlePackPreviewSpec[] = [
  {
    id: "variety_pop",
    fontFamily: "'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif",
    fontWeight: 800,
    letterSpacing: "0.02em",
    lineHeight: 1.25,
    strokeScale: 1.35,
    tiltDeg: 0,
    plate: "",
    plateRadius: "0.28em",
    glow: false,
    entrance: "pop",
    loop: true,
  },
  {
    id: "cinematic",
    fontFamily: "'Songti SC','Noto Serif CJK SC','SimSun',serif",
    fontWeight: 500,
    letterSpacing: "0.18em",
    lineHeight: 1.5,
    strokeScale: 0.6,
    tiltDeg: 0,
    plate: "",
    plateRadius: "0.28em",
    glow: false,
    entrance: "slide",
    loop: false,
  },
  {
    id: "clean_talk",
    fontFamily: "'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif",
    fontWeight: 600,
    letterSpacing: "0.01em",
    lineHeight: 1.35,
    strokeScale: 0.4,
    tiltDeg: 0,
    plate: "rgba(14,16,20,0.42)",
    plateRadius: "0.34em",
    glow: false,
    entrance: "soft_fade",
    loop: false,
  },
  {
    id: "warm_note",
    fontFamily: "'Kaiti SC','STKaiti','Noto Serif CJK SC',serif",
    fontWeight: 500,
    letterSpacing: "0.04em",
    lineHeight: 1.45,
    strokeScale: 0.5,
    tiltDeg: -1.6,
    plate: "rgba(255,248,232,0.5)",
    plateRadius: "0.18em",
    glow: false,
    entrance: "type_on",
    loop: false,
  },
  {
    id: "neon_music",
    fontFamily: "'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif",
    fontWeight: 700,
    letterSpacing: "0.06em",
    lineHeight: 1.3,
    strokeScale: 0.8,
    tiltDeg: 0,
    plate: "rgba(10,8,24,0.42)",
    plateRadius: "0.28em",
    glow: true,
    entrance: "pop",
    loop: true,
  },
];

/** The "smart recommend" card: the historical plain-subtitle look. */
export const SMART_SUBTITLE_PREVIEW: SubtitlePackPreviewSpec = {
  id: null,
  fontFamily:
    "'Noto Sans CJK SC','Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif",
  fontWeight: 600,
  letterSpacing: "0em",
  lineHeight: 1.35,
  strokeScale: 1,
  tiltDeg: 0,
  plate: "",
  plateRadius: "0.28em",
  glow: false,
  entrance: "soft_fade",
  loop: false,
};

const PACK_I18N_KEYS: Record<string, { nameKey: string; descKey: string }> = {
  variety_pop: {
    nameKey: "home.subtitleTemplateVarietyPop",
    descKey: "home.subtitleTemplateVarietyPopDesc",
  },
  cinematic: {
    nameKey: "home.subtitleTemplateCinematic",
    descKey: "home.subtitleTemplateCinematicDesc",
  },
  clean_talk: {
    nameKey: "home.subtitleTemplateCleanTalk",
    descKey: "home.subtitleTemplateCleanTalkDesc",
  },
  warm_note: {
    nameKey: "home.subtitleTemplateWarmNote",
    descKey: "home.subtitleTemplateWarmNoteDesc",
  },
  neon_music: {
    nameKey: "home.subtitleTemplateNeonMusic",
    descKey: "home.subtitleTemplateNeonMusicDesc",
  },
};

/** i18n key of one card's name; unknown and null ids read as smart recommend. */
export function subtitlePackNameKey(packId: string | null): string {
  return PACK_I18N_KEYS[packId ?? ""]?.nameKey ?? "home.subtitleTemplateSmart";
}

/** i18n key of one card's scene description. */
export function subtitlePackDescKey(packId: string | null): string {
  return (
    PACK_I18N_KEYS[packId ?? ""]?.descKey ?? "home.subtitleTemplateSmartDesc"
  );
}

// ── same formulas as backend motion_templates ──
function captionEm(value: number): string {
  let text = value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (text.startsWith("0.")) text = text.slice(1);
  return text ? `${text}em` : "0em";
}

function captionShadowStack(
  stroke: number,
  glow: boolean,
  colour: string,
): string {
  const parts: string[] = [];
  if (stroke > 0) {
    parts.push(`0 ${captionEm(0.025 * stroke)} ${captionEm(0.05 * stroke)} #000d`);
    parts.push(`0 ${captionEm(0.04 * stroke)} ${captionEm(0.14 * stroke)} #000b`);
  }
  if (glow) {
    const halo = colour.startsWith("#") ? colour : "#ffffff";
    parts.push(`0 0 .06em ${halo}`, `0 0 .18em ${halo}88`, `0 0 .38em ${halo}55`);
  }
  return parts.length ? parts.join(",") : "none";
}

function plateWithOpacity(
  plate: string,
  opacity: number | null | undefined,
): string {
  if (!plate) return "";
  if (opacity === null || opacity === undefined) return plate;
  const alpha = Math.max(0, Math.min(1, opacity));
  if (alpha <= 0) return "";
  const match = /^rgba?\((\d+),(\d+),(\d+)/.exec(plate);
  if (!match) return plate;
  return `rgba(${match[1]},${match[2]},${match[3]},${alpha.toFixed(3)})`;
}

const ENTRANCE_ANIMATION: Record<string, string> = {
  pop: "capPop .28s cubic-bezier(.34,1.56,.64,1) both",
  slide: "capSlide .3s ease-out both",
  type_on: "capType .5s linear both",
};
// Same source as backend ``_caption_entrance_css`` plus the ambient breath
// loop; remounting the words node (hover / select) replays the entrance.
const PREVIEW_KEYFRAMES =
  "@keyframes capPop{from{opacity:.6;transform:scale(.88)}" +
  "to{opacity:1;transform:scale(1)}}" +
  "@keyframes capSlide{from{opacity:.6;transform:translateY(.45em)}" +
  "to{opacity:1;transform:translateY(0)}}" +
  "@keyframes capType{from{opacity:.85;clip-path:inset(0 62% 0 0)}" +
  "to{opacity:1;clip-path:inset(0 0 0 0)}}" +
  "@keyframes capAppear{from{opacity:.72}to{opacity:1}}" +
  "@keyframes capBreath{0%,100%{transform:scale(1)}12%{transform:scale(1.022)}" +
  "25%{transform:scale(1)}37%{transform:scale(1.018)}50%{transform:scale(1)}" +
  "62%{transform:scale(1.022)}75%{transform:scale(1)}87%{transform:scale(1.014)}}";
// A stand-in video frame: the previews must sit on footage-like darkness,
// otherwise plate alpha and glow read nothing like the final pixels.
const PREVIEW_FRAME_BACKGROUND =
  "radial-gradient(120% 90% at 20% 10%, rgba(255,196,120,0.55), transparent 60%)," +
  "radial-gradient(90% 80% at 85% 80%, rgba(64,120,190,0.5), transparent 65%)," +
  "linear-gradient(160deg, #4a5568 0%, #232a36 55%, #10141b 100%)";

interface SubtitleTemplatePickerProps {
  value: string | null;
  onChange: (id: string | null) => void;
  /** Live draft of the style window's four control groups; previews follow. */
  style?: SubtitleStyle | null;
}

/** Horizontal JianYing-style subtitle template strip shown for "auto". */
export function SubtitleTemplatePicker({
  value,
  onChange,
  style = null,
}: SubtitleTemplatePickerProps) {
  const { t } = useTranslation();
  const [packSpecs, setPackSpecs] = useState<SubtitlePackPreviewSpec[] | null>(
    null,
  );
  const [replay, setReplay] = useState<Record<string, number>>({});

  useEffect(() => {
    let alive = true;
    creatorRequest<{ items?: SubtitlePackPreviewSpec[] }>(
      "/subtitle-packs/preview",
      {},
      { timeoutMs: 8000 },
    )
      .then((body) => {
        const items = (body?.items ?? []).filter((item) => item && item.id);
        if (alive && items.length > 0) setPackSpecs(items);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const specs = useMemo(
    () => [SMART_SUBTITLE_PREVIEW, ...(packSpecs ?? SUBTITLE_TEMPLATE_PACKS)],
    [packSpecs],
  );

  const colour = style?.color || "#ffffff";
  const fontScale = style?.fontScale ?? 1;
  const strokeScale = style?.strokeScale ?? 1;
  const bgOpacity = style?.backgroundOpacity ?? null;

  return (
    <div className="flex items-stretch gap-2 overflow-x-auto pb-1">
      <style>{PREVIEW_KEYFRAMES}</style>
      {specs.map((spec) => {
        const cardKey = spec.id ?? "smart";
        const selected = value === spec.id;
        const stroke = Math.max(0, spec.strokeScale * strokeScale);
        const plate = plateWithOpacity(spec.plate, bgOpacity);
        const wordsStyle: CSSProperties = {
          color: colour,
          fontFamily: spec.fontFamily,
          fontSize: `${(15 * fontScale).toFixed(1)}px`,
          fontWeight: spec.fontWeight,
          lineHeight: spec.lineHeight,
          letterSpacing: spec.letterSpacing,
          textShadow: captionShadowStack(stroke, spec.glow, colour),
          ...(plate
            ? {
                display: "inline-block",
                padding: ".28em .6em",
                background: plate,
                borderRadius: spec.plateRadius || "0.28em",
                maxWidth: "100%",
              }
            : { width: "100%" }),
          animation:
            (ENTRANCE_ANIMATION[spec.entrance] ?? "capAppear .14s linear both") +
            (spec.loop ? ",capBreath 9.6s ease-in-out .45s infinite" : ""),
        };
        const family = spec.fontFamily.includes("Songti")
          ? t("home.subtitlePreviewFamilySerif")
          : spec.fontFamily.includes("Kaiti")
            ? t("home.subtitlePreviewFamilyHand")
            : t("home.subtitlePreviewFamilySans");
        const plateAlpha = parseFloat(spec.plate.split(",")[3] ?? "0") || 0;
        const plateText = !spec.plate
          ? t("home.subtitlePreviewPlateNone")
          : bgOpacity === 0
            ? t("home.subtitlePreviewPlateOff")
            : t("home.subtitlePreviewPlatePct", {
                percent: Math.round((bgOpacity ?? plateAlpha) * 100),
              });
        return (
          <button
            key={cardKey}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(spec.id)}
            onMouseEnter={() =>
              setReplay((current) => ({
                ...current,
                [cardKey]: (current[cardKey] ?? 0) + 1,
              }))
            }
            className={`flex min-w-[168px] flex-1 cursor-pointer flex-col gap-1 rounded-lg border p-2 text-left transition-colors ${
              selected
                ? "border-[var(--color-text-primary)] bg-[rgba(43,27,0,0.06)] dark:bg-white/10"
                : "border-transparent bg-[rgba(43,27,0,0.03)] hover:bg-[rgba(43,27,0,0.06)] dark:bg-white/5 dark:hover:bg-white/10"
            }`}
          >
            <span
              className="relative block h-24 overflow-hidden rounded-md"
              style={{ background: PREVIEW_FRAME_BACKGROUND }}
            >
              <span
                className="absolute inset-[6%] z-[1] flex items-center justify-center"
                style={
                  spec.tiltDeg
                    ? { transform: `rotate(${spec.tiltDeg}deg)` }
                    : undefined
                }
              >
                <span
                  key={replay[cardKey] ?? 0}
                  data-subtitle-preview={cardKey}
                  className="text-center"
                  style={wordsStyle}
                >
                  {t("home.subtitlePreviewSampleA")}
                  <br />
                  {t("home.subtitlePreviewSampleB")}
                </span>
              </span>
            </span>
            <span className="text-xs font-semibold text-[var(--color-text-primary)]">
              {t(subtitlePackNameKey(spec.id))}
            </span>
            <span className="text-[11px] leading-4 text-[var(--color-text-tertiary)]">
              {t(subtitlePackDescKey(spec.id))}
            </span>
            <span className="font-mono text-[10px] leading-[14px] text-[var(--color-text-tertiary)]">
              {t("home.subtitlePreviewMeta", {
                family,
                weight: spec.fontWeight,
                spacing: spec.letterSpacing,
                stroke: stroke.toFixed(2),
                plate: plateText,
                entrance: spec.entrance,
                loop: spec.loop ? t("home.subtitlePreviewLoop") : "",
              })}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export default SubtitleTemplatePicker;
