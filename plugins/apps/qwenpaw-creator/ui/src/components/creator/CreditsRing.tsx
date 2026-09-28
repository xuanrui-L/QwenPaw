import { Component, useEffect, type ReactNode } from "react";
import { Popover } from "antd";
import { useTranslation } from "react-i18next";
import {
  ringFraction,
  useCreditsStore,
  type CreditsUsageByModelView,
} from "@/store/creditsStore";

/**
 * Navigation-bar Credits ring.
 *
 * A quiet always-on gauge: how much of the allocated balance is left, coloured
 * green / amber / red by remaining share, with a per-model spend breakdown on
 * hover. It deliberately does NOT replace `ModelCreditsNotice` - that pill is
 * an event-driven "every call is now refused" alarm, this is a passive number.
 * They sit side by side and answer different questions.
 *
 * Resilience is the whole point of the shape below. Off-platform the
 * `credits-usage` and credential endpoints are unreachable, so: the store has
 * already swallowed those failures (never throws to here), the component draws
 * a neutral placeholder instead of a fake ring when the balance is unknown, and
 * an error boundary catches any rendering surprise so a broken ring can never
 * take the rest of the top bar down with it.
 */

const RING_SIZE = 26;
const STROKE = 3;
const RADIUS = (RING_SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

// Remaining-share thresholds -> theme token. Kept as data so the colour logic
// reads once instead of scattered across ternaries.
function tierColor(fraction: number): string {
  if (fraction <= 0) return "var(--color-danger)";
  if (fraction < 0.2) return "var(--color-danger)";
  if (fraction < 0.5) return "var(--color-warning)";
  return "var(--color-success)";
}

function formatCredits(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function RingDetails({
  available,
  consumed,
  byModel,
}: {
  available: number | null;
  consumed: number | null;
  byModel: CreditsUsageByModelView[];
}) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-[184px] flex-col gap-1 text-xs">
      <div className="flex items-center justify-between gap-4">
        <span className="text-[var(--color-text-secondary)]">
          {t("nav.creditsRemaining")}
        </span>
        <span className="font-semibold">
          {available === null ? "—" : formatCredits(available)}
        </span>
      </div>
      <div className="flex items-center justify-between gap-4">
        <span className="text-[var(--color-text-secondary)]">
          {t("nav.creditsConsumed")}
        </span>
        <span className="font-semibold">
          {consumed === null ? "—" : formatCredits(consumed)}
        </span>
      </div>
      {byModel.length > 0 && (
        <div className="my-1 h-px bg-[var(--color-border)]" />
      )}
      {byModel.map((row) => (
        <div
          key={row.model_id}
          className="flex items-center justify-between gap-4"
        >
          <span className="truncate text-[var(--color-text-secondary)]">
            {row.model_id}
          </span>
          <span className="shrink-0 tabular-nums">
            {formatCredits(row.settled_credits)}
          </span>
        </div>
      ))}
    </div>
  );
}

function CreditsRingInner() {
  const { t } = useTranslation();
  const status = useCreditsStore((state) => state.status);
  const available = useCreditsStore((state) => state.available);
  const consumed = useCreditsStore((state) => state.consumed);
  const byModel = useCreditsStore((state) => state.byModel);
  const load = useCreditsStore((state) => state.load);
  const reload = useCreditsStore((state) => state.reload);

  // Load in an effect, never during render: a synchronous store write here
  // would schedule a re-render while React is still rendering this component.
  // Re-read whenever the tab regains focus — Credits usually move while the
  // person is in another window, and returning is when they want the number
  // without a manual reload.
  useEffect(() => {
    void load();
    const onVisibility = () => {
      if (document.visibilityState === "visible") void reload();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [load, reload]);

  // Balance unknown (both endpoints failed, or still loading): a neutral grey
  // ring, not a misleading full/empty one. Still hoverable so nothing looks
  // dead, but it carries no numbers.
  const fraction = ringFraction(available, consumed);
  const color =
    fraction === null ? "var(--color-border-strong)" : tierColor(fraction);
  const dash = fraction === null ? 0 : CIRCUMFERENCE * fraction;

  const detail = (
    <RingDetails available={available} consumed={consumed} byModel={byModel} />
  );

  return (
    <Popover
      content={detail}
      title={t("nav.creditsRingTitle")}
      trigger="hover"
      placement="bottomRight"
      mouseEnterDelay={0.15}
    >
      {/* A plain span, not a component: antd measures the popover anchor via a
          ref, and a function child that does not forward one leaves the bubble
          stranded at the document origin. */}
      <span
        data-credits-ring
        className="inline-flex h-[31px] shrink-0 cursor-default items-center px-1"
        title={status === "error" ? t("nav.creditsRingUnavailable") : undefined}
      >
        <svg
          width={RING_SIZE}
          height={RING_SIZE}
          viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
          role="img"
          aria-label={t("nav.creditsRingAria")}
        >
          <circle
            cx={RING_SIZE / 2}
            cy={RING_SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke="var(--color-border)"
            strokeWidth={STROKE}
          />
          {fraction !== null && (
            <circle
              cx={RING_SIZE / 2}
              cy={RING_SIZE / 2}
              r={RADIUS}
              fill="none"
              stroke={color}
              strokeWidth={STROKE}
              strokeLinecap="round"
              strokeDasharray={`${dash} ${CIRCUMFERENCE - dash}`}
              transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
            />
          )}
        </svg>
      </span>
    </Popover>
  );
}

class CreditsRingBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override render() {
    // A crashed ring renders nothing: the top bar and every other control stay.
    return this.state.failed ? null : this.props.children;
  }
}

export default function CreditsRing() {
  return (
    <CreditsRingBoundary>
      <CreditsRingInner />
    </CreditsRingBoundary>
  );
}
