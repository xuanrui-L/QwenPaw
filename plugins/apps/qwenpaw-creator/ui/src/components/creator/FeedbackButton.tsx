import { useRef, useState } from "react";
import { Loader2, MessageSquareText } from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  FeedbackContext,
  FeedbackScreenshot,
} from "@/api/creator/feedback";
import { captureFeedbackScreenshot } from "@/lib/feedbackScreenshot";
import FeedbackModal from "./FeedbackModal";

export default function FeedbackButton(context: FeedbackContext) {
  const { t } = useTranslation();
  const trigger = useRef<HTMLButtonElement>(null);
  const [capturing, setCapturing] = useState(false);
  const [session, setSession] = useState<{
    screenshot: FeedbackScreenshot | null;
    context: FeedbackContext;
  } | null>(null);

  async function openFeedback() {
    if (capturing || session) return;
    setCapturing(true);
    const screenshot = await captureFeedbackScreenshot().catch(() => null);
    setSession({ screenshot, context });
    setCapturing(false);
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        data-agent-feedback
        aria-haspopup="dialog"
        aria-expanded={!!session}
        aria-busy={capturing}
        aria-disabled={capturing}
        aria-label={capturing ? t("feedback.capturing") : undefined}
        onClick={() => void openFeedback()}
        className="creator-feedback-trigger"
      >
        <span className="relative flex">
          <MessageSquareText size={13} />
          {capturing && (
            <span
              data-html2canvas-ignore
              className="absolute inset-0 bg-[var(--color-bg-card)]"
            >
              <Loader2 size={13} className="animate-spin" />
            </span>
          )}
        </span>
        {t("feedback.action")}
      </button>
      {session && (
        <FeedbackModal
          context={session.context}
          initialScreenshot={session.screenshot}
          onClose={() => {
            setSession(null);
            trigger.current?.focus();
          }}
        />
      )}
    </>
  );
}
