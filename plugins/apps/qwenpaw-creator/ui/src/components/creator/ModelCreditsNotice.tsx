import { useTranslation } from "react-i18next";
import { Tooltip } from "antd";
import { CircleAlert, X } from "lucide-react";
import { useModelCreditsStore } from "@/store/modelCreditsStore";

/**
 * Navigation-bar Credits notice.
 *
 * An empty balance is refused by the provider before any model is reached, so
 * the condition belongs to the account rather than to the project whose failure
 * tray happened to record it. Without this pill every other screen - the
 * project list included - still offered "生成" as the next click while no model
 * call anywhere could succeed.
 *
 * Display only: jumping to the project that proved it cannot fix it, and the
 * tray there owns the one action that can, because resuming needs that
 * project's session. The pill is therefore a plain element rather than a link -
 * which also matters for the tooltip, since antd needs a real DOM node to
 * measure: a component that does not forward a ref leaves it without a bounding
 * box, and the bubble then appears at the document origin on mouse-leave.
 */
export default function ModelCreditsNotice() {
  const { t } = useTranslation();
  const notice = useModelCreditsStore((state) => state.notice);
  const clear = useModelCreditsStore((state) => state.clear);

  if (!notice) return null;

  return (
    <div
      data-model-credits-notice
      className="flex min-w-0 shrink-0 items-center gap-1"
    >
      <Tooltip title={t("nav.creditsExhaustedHint")} placement="bottom">
        <span
          data-model-credits-pill
          className="inline-flex h-[31px] min-w-0 cursor-default items-center gap-1.5 rounded-full border border-[var(--color-danger)] bg-[var(--color-danger-soft)] px-3 text-xs font-bold text-[var(--color-danger)]"
        >
          <CircleAlert className="h-3.5 w-3.5 shrink-0" />
          <span className="hidden truncate md:inline">
            {t("nav.creditsExhausted")}
          </span>
        </span>
      </Tooltip>
      <button
        type="button"
        onClick={clear}
        aria-label={t("nav.creditsDismiss")}
        title={t("nav.creditsDismiss")}
        className="icon-button shrink-0"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}
