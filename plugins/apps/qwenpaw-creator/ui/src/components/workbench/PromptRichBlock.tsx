import { useEffect, useRef, useState } from "react";
import { Button, Image, Input, Modal } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { Maximize2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import InlineReviewDiff from "@/components/agent/InlineReviewDiff";
import type { ShotDocument } from "@/contracts/creator";

const { TextArea } = Input;

export interface PromptRichToken {
  /** Authoritative [Image N] index this token answers to. */
  index: number;
  name: string;
  thumbUrl: string | null;
  kind: "storyboard" | "artifact" | "source" | "entity";
}

interface PromptSegment {
  /** null = unmarked prompt (single block); 0 = overview; N = 【Shot N】 */
  shotNumber: number | null;
  text: string;
}

const SHOT_HL_CLASS = "workbench-shot-hl";

/** 【Shot N】 is a text convention, not schema; prompts without it degrade to one block. */
function splitSegments(value: string): {
  marked: boolean;
  segments: PromptSegment[];
} {
  const parts = value.split(/【Shot (\d+)】/);
  if (parts.length === 1) {
    return { marked: false, segments: [{ shotNumber: null, text: value }] };
  }
  const segments: PromptSegment[] = [];
  const overview = parts[0].trim();
  if (overview) segments.push({ shotNumber: 0, text: overview });
  for (let i = 1; i < parts.length; i += 2) {
    segments.push({
      shotNumber: Number(parts[i]),
      text: (parts[i + 1] ?? "").trim(),
    });
  }
  return { marked: true, segments };
}

function shotRowElement(shotId: string): HTMLElement | null {
  const escaped =
    typeof CSS !== "undefined" && CSS.escape
      ? CSS.escape(shotId)
      : shotId.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  return document.querySelector<HTMLElement>(
    `[data-creator-module="shot-row"][data-creator-module-id="${escaped}"]`,
  );
}

/**
 * Prompt surface with two modes: a read-only reference preview that renders
 * [Image N] citations as thumbnail tokens and 【Shot N】 sections with shot
 * badges linked to the Shot list, and the original editable TextArea. The
 * TextArea (with its data-creator-* anchors and InlineReviewDiff) always
 * stays mounted so review focus and field tracking keep working.
 */
export default function PromptRichBlock({
  label,
  value,
  onChange,
  disabled = false,
  field,
  path,
  tokens,
  shots,
  collapseHeight = 230,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  field: string;
  path: string;
  tokens: PromptRichToken[];
  shots?: ShotDocument[];
  collapseHeight?: number;
  placeholder?: string;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"preview" | "raw">("preview");
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const richRef = useRef<HTMLDivElement>(null);
  // 全屏编辑器：本地草稿，「完成」才写回（与折叠预览互不干扰）。
  const [fullOpen, setFullOpen] = useState(false);
  const [fullDraft, setFullDraft] = useState("");
  const fullTextRef = useRef<TextAreaRef>(null);
  const openFullEditor = () => {
    setFullDraft(value);
    setFullOpen(true);
  };
  const insertTokenAtCaret = (index: number) => {
    const textarea = fullTextRef.current?.resizableTextArea?.textArea;
    const snippet = `[Image ${index}]`;
    if (!textarea) {
      setFullDraft((prev) => prev + snippet);
      return;
    }
    const start = textarea.selectionStart ?? fullDraft.length;
    const end = textarea.selectionEnd ?? start;
    const next = fullDraft.slice(0, start) + snippet + fullDraft.slice(end);
    setFullDraft(next);
    requestAnimationFrame(() => {
      textarea.focus();
      const caret = start + snippet.length;
      textarea.setSelectionRange(caret, caret);
    });
  };

  const charCount = value.replace(/\s/g, "").length;
  const { marked, segments } = splitSegments(value);
  const shotSegmentCount = segments.filter(
    (segment) => (segment.shotNumber ?? 0) > 0,
  ).length;
  const segMismatch =
    marked && shots && shots.length > 0 && shotSegmentCount !== shots.length;
  const collapsed = overflowing && !expanded;

  // Measure after render (and when a hidden tab becomes visible) to decide
  // whether the collapse affordance is needed; hidden panes report 0 height.
  useEffect(() => {
    const element = richRef.current;
    if (!element || mode !== "preview") return;
    const check = () => {
      if (element.scrollHeight === 0) return;
      setOverflowing(element.scrollHeight > collapseHeight + 40);
    };
    check();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(check);
    observer.observe(element);
    return () => observer.disconnect();
  }, [mode, value, collapseHeight]);

  const clearShotHighlight = () => {
    document
      .querySelectorAll(`.${SHOT_HL_CLASS}`)
      .forEach((el) => el.classList.remove(SHOT_HL_CLASS));
  };
  const shotOf = (shotNumber: number): ShotDocument | null =>
    shots?.[shotNumber - 1] ?? null;
  const highlightShot = (shotNumber: number) => {
    const shot = shotOf(shotNumber);
    if (shot) shotRowElement(shot.shot_id)?.classList.add(SHOT_HL_CLASS);
  };
  const scrollToShot = (shotNumber: number) => {
    const shot = shotOf(shotNumber);
    if (!shot) return;
    shotRowElement(shot.shot_id)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  };
  useEffect(() => clearShotHighlight, []);

  const renderInline = (text: string) =>
    text.split(/(\[Image \d+\])/).map((part, partIndex) => {
      const match = /^\[Image (\d+)\]$/.exec(part);
      if (!match) return <span key={partIndex}>{part}</span>;
      const index = Number(match[1]);
      const token = tokens.find((item) => item.index === index);
      if (!token) {
        return (
          <span
            key={partIndex}
            className="mx-0.5 inline-flex items-center rounded-full border border-dashed border-[var(--color-danger)]/50 bg-[var(--color-bg-primary)] px-2 py-0.5 align-[-3px] font-mono text-[9px] font-bold leading-none text-[var(--color-danger)]"
          >
            {t("r2v.tokenMissing", { index })}
          </span>
        );
      }
      return (
        <button
          key={partIndex}
          type="button"
          data-prompt-token={index}
          title={token.name}
          onClick={() =>
            token.thumbUrl ? setPreviewSrc(token.thumbUrl) : undefined
          }
          className="mx-0.5 inline-flex cursor-pointer select-none items-center gap-1 rounded-full border border-[color-mix(in_srgb,var(--color-accent)_35%,var(--color-border))] bg-[var(--color-bg-primary)] py-0.5 pl-0.5 pr-2 align-[-5px] text-[11px] leading-none shadow-xs transition-all hover:-translate-y-px hover:border-[var(--color-accent)] hover:shadow-[0_2px_8px_rgba(255,127,22,.18)]"
        >
          {token.thumbUrl && (
            <img
              src={token.thumbUrl}
              alt=""
              className="h-5 w-5 rounded-full border border-[var(--color-border)] object-cover"
            />
          )}
          <span className="font-mono text-[9px] font-bold text-[var(--color-accent)]">
            IMG {index}
          </span>
          <span className="max-w-[108px] truncate font-medium text-[var(--color-text-primary)]">
            {token.name}
          </span>
        </button>
      );
    });

  return (
    <div
      data-creator-field={field}
      data-creator-path={path}
      data-creator-field-label={label}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <p className="min-w-0 truncate text-[11px] font-medium text-[var(--color-text-tertiary)]">
          {label}
          {charCount > 0 && (
            <span className="ml-1.5 text-[10px] text-[var(--color-text-tertiary)]/80">
              {t("r2v.promptChars", { count: charCount })}
            </span>
          )}
        </p>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={openFullEditor}
            title={t("r2v.fullscreenEdit")}
            className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-2 py-0.5 text-[10px] text-[var(--color-text-tertiary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
          >
            <Maximize2 className="h-2.5 w-2.5" />
            {t("r2v.fullscreenEdit")}
          </button>
          <div className="flex overflow-hidden rounded-md border border-[var(--color-border)]">
            {(
              [
                { key: "preview", text: t("r2v.refPreview") },
                { key: "raw", text: t("r2v.editRaw") },
              ] as const
            ).map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setMode(item.key)}
                className={`px-2 py-0.5 text-[10px] transition-colors ${
                  mode === item.key
                    ? "bg-[var(--color-accent-soft)] font-semibold text-[var(--color-accent)]"
                    : "bg-[var(--color-bg-primary)] text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]"
                }`}
              >
                {item.text}
              </button>
            ))}
          </div>
        </div>
      </div>

      {mode === "preview" && (
        <>
          <div
            ref={richRef}
            className="relative overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-3 py-2.5 text-xs leading-[2] text-[var(--color-text-primary)]"
            style={collapsed ? { maxHeight: collapseHeight } : undefined}
          >
            {value.trim() ? (
              segments.map((segment, segmentIndex) => {
                const shotNumber = segment.shotNumber;
                const shot =
                  shotNumber && shotNumber > 0 ? shotOf(shotNumber) : null;
                return (
                  <div
                    key={segmentIndex}
                    data-prompt-segment={shotNumber ?? "all"}
                    className={`whitespace-pre-wrap break-words rounded-sm border-l-2 border-transparent ${
                      marked ? "pl-2.5" : ""
                    } ${segmentIndex > 0 ? "mt-1.5" : ""}`}
                  >
                    {marked && shotNumber === 0 && (
                      <span className="mr-1.5 inline-flex items-center rounded-full border border-dashed border-[var(--color-border-strong)] bg-[var(--color-bg-primary)] px-2 py-px align-[-2px] text-[10px] font-bold leading-normal text-[var(--color-text-secondary)]">
                        {t("r2v.segOverview")}
                      </span>
                    )}
                    {marked && shotNumber !== null && shotNumber > 0 && (
                      <button
                        type="button"
                        data-shot-link={shotNumber}
                        title={t("r2v.shotBadgeTitle")}
                        onMouseEnter={() => highlightShot(shotNumber)}
                        onMouseLeave={clearShotHighlight}
                        onClick={() => scrollToShot(shotNumber)}
                        className="mr-1.5 inline-flex items-center gap-1 rounded-full border border-[var(--color-border-strong)] bg-[var(--color-bg-primary)] px-2 py-px align-[-2px] text-[10px] font-bold leading-normal text-[var(--color-text-secondary)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
                      >
                        SHOT {shotNumber}
                        {shot && (
                          <span className="font-medium text-[var(--color-text-tertiary)]">
                            {[
                              shot.framing?.trim(),
                              shot.duration_seconds != null
                                ? `${shot.duration_seconds}s`
                                : null,
                            ]
                              .filter(Boolean)
                              .map((meta) => `· ${meta}`)
                              .join(" ")}
                          </span>
                        )}
                      </button>
                    )}
                    {renderInline(segment.text)}
                  </div>
                );
              })
            ) : (
              <span className="text-[var(--color-text-tertiary)]">
                {placeholder ?? t("r2v.generateAndEdit", { label })}
              </span>
            )}
            {collapsed && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 rounded-b-lg bg-gradient-to-b from-transparent to-[var(--color-bg-secondary)]" />
            )}
          </div>
          {overflowing && (
            <button
              type="button"
              onClick={() => setExpanded((prev) => !prev)}
              className="mt-1 block w-full text-center text-[10.5px] font-semibold text-[var(--color-accent)] hover:underline"
            >
              {expanded
                ? t("r2v.collapse")
                : t("r2v.expandAll", { count: charCount })}
            </button>
          )}
          {segMismatch && (
            <p className="mt-1.5 text-[10px] text-[var(--color-warning)]">
              {t("r2v.segMismatch", {
                segments: shotSegmentCount,
                shots: shots?.length ?? 0,
              })}
            </p>
          )}
        </>
      )}

      {/* Keep the TextArea mounted so data-creator anchors, review focus and
          controlled edits survive mode switches; only its visibility toggles. */}
      <div className={mode === "raw" ? "" : "hidden"}>
        <TextArea
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          autoSize={{ minRows: 3, maxRows: 16 }}
          placeholder={placeholder ?? t("r2v.generateAndEdit", { label })}
          className="!rounded-lg !border-[var(--color-border)] !bg-[var(--color-bg-secondary)] !text-xs"
        />
      </div>
      <InlineReviewDiff pointer={path} />

      {/* Controlled zoom preview for token thumbnails. */}
      {previewSrc && (
        <Image
          style={{ display: "none" }}
          src={previewSrc}
          preview={{
            visible: true,
            src: previewSrc,
            onVisibleChange: (visible) => {
              if (!visible) setPreviewSrc(null);
            },
          }}
        />
      )}

      {/* 全屏编辑器：大画布 + 右侧素材栏点击插入 [Image N]；「完成」才写回。 */}
      <Modal
        open={fullOpen}
        onCancel={() => setFullOpen(false)}
        width="min(960px, 94vw)"
        title={
          <span className="text-sm font-bold">
            {t("r2v.fullscreenEditTitle", { label })}
            <span className="ml-2 text-[11px] font-normal text-[var(--color-text-tertiary)]">
              {t("r2v.promptChars", {
                count: fullDraft.replace(/\s/g, "").length,
              })}
            </span>
          </span>
        }
        footer={
          <div className="flex justify-end gap-2">
            <Button size="small" onClick={() => setFullOpen(false)}>
              {t("r2v.fullscreenCancel")}
            </Button>
            <Button
              size="small"
              type="primary"
              disabled={disabled}
              onClick={() => {
                onChange(fullDraft);
                setFullOpen(false);
              }}
            >
              {t("r2v.fullscreenDone")}
            </Button>
          </div>
        }
        destroyOnHidden
      >
        <div className="flex min-h-0 gap-3">
          <TextArea
            ref={fullTextRef}
            value={fullDraft}
            disabled={disabled}
            onChange={(event) => setFullDraft(event.target.value)}
            autoSize={{ minRows: 18, maxRows: 24 }}
            className="!rounded-lg !border-[var(--color-border)] !bg-[var(--color-bg-primary)] !font-mono !text-xs !leading-[1.9]"
          />
          {tokens.length > 0 && (
            <div className="w-[200px] shrink-0 self-stretch overflow-y-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-2.5">
              <p className="text-[10.5px] font-bold text-[var(--color-text-secondary)]">
                {t("r2v.insertRefTitle")}
              </p>
              <p className="mb-2 mt-0.5 text-[9.5px] leading-relaxed text-[var(--color-text-tertiary)]">
                {t("r2v.insertRefHint")}
              </p>
              {tokens.map((token) => (
                <button
                  key={token.index}
                  type="button"
                  onClick={() => insertTokenAtCaret(token.index)}
                  className="mb-1.5 flex w-full items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-2 py-1.5 text-left transition-colors hover:border-[var(--color-accent)]"
                >
                  {token.thumbUrl ? (
                    <img
                      src={token.thumbUrl}
                      alt=""
                      className="h-6 w-8 rounded border border-[var(--color-border)] object-cover"
                    />
                  ) : (
                    <span className="flex h-6 w-8 items-center justify-center rounded border border-dashed border-[var(--color-border)] font-mono text-[8px] text-[var(--color-text-tertiary)]">
                      {token.index}
                    </span>
                  )}
                  <span className="min-w-0 flex-1 truncate text-[10.5px]">
                    <b className="font-mono text-[9px] text-[var(--color-accent)]">
                      [{token.index}]
                    </b>{" "}
                    {token.name}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}
