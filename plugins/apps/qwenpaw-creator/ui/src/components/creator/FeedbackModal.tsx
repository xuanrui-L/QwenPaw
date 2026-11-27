import { useEffect, useId, useRef, useState } from "react";
import type { ClipboardEvent, FormEvent, ReactNode } from "react";
import { CreatorHttpError, openHostLink } from "@/api/creator/client";
import { Button, Image, Modal } from "antd";
import { Check, Plus, Send, X } from "lucide-react";
import { Trans, useTranslation } from "react-i18next";
import {
  FEEDBACK_IMAGE_ACCEPT,
  FEEDBACK_LIMIT,
  MAX_FEEDBACK_SCREENSHOTS,
  PLATFORM_NOTIFICATIONS_URL,
  feedbackErrorMessage,
  formatFeedbackText,
  makeFeedbackScreenshot,
  needsFeedbackAccount,
  submitFeedback,
  type FeedbackAttempt,
  type FeedbackContext,
  type FeedbackScreenshot,
} from "@/api/creator/feedback";
import creatorGlyph from "@/assets/design/logo-glyph-orange.png";
import "./FeedbackModal.css";

function PlatformLink({
  href,
  children,
}: {
  href: string;
  children?: ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        if (
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey &&
          openHostLink(href)
        ) {
          event.preventDefault();
        }
      }}
    >
      {children}
    </a>
  );
}

function ScreenshotTile({
  screenshot,
  index,
  onRemove,
}: {
  screenshot: FeedbackScreenshot;
  index: number;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const [url, setUrl] = useState("");
  useEffect(() => {
    const preview = URL.createObjectURL(screenshot.file);
    setUrl(preview);
    return () => URL.revokeObjectURL(preview);
  }, [screenshot.file]);

  return (
    <div className="creator-feedback-tile">
      {url && (
        <Image
          src={url}
          alt={t("feedback.image", { index })}
          width="100%"
          height="100%"
        />
      )}
      <button
        type="button"
        className="creator-feedback-remove"
        aria-label={t("feedback.removeImage", { index })}
        onClick={onRemove}
      >
        <X size={13} />
      </button>
    </div>
  );
}

export default function FeedbackModal({
  context,
  initialScreenshot,
  onClose,
}: {
  context: FeedbackContext;
  initialScreenshot: FeedbackScreenshot | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const description = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(true);
  const [text, setText] = useState("");
  const [email, setEmail] = useState("");
  const [screenshots, setScreenshots] = useState(
    initialScreenshot ? [initialScreenshot] : [],
  );
  const [imageError, setImageError] = useState("");
  const attempt = useRef<FeedbackAttempt>({});
  const [sendError, setSendError] = useState<Error | null>(null);
  const [status, setStatus] = useState<"editing" | "sending" | "sent">(
    "editing",
  );
  const busy = status === "sending";
  const sent = status === "sent";
  const errorCode = sendError instanceof CreatorHttpError ? sendError.code : "";
  const unconfirmed = errorCode === "FEEDBACK_UNCONFIRMED";
  const screenshotNote = screenshots.some((shot) => shot.partialPreview)
    ? t("feedback.partialPreview")
    : !initialScreenshot && !screenshots.length
    ? t("feedback.captureFailed")
    : "";
  const receiptTitle = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (sent) receiptTitle.current?.focus();
  }, [sent]);
  const excessLength =
    formatFeedbackText(text, email, context).length - FEEDBACK_LIMIT;
  const tooLong = excessLength > 0;

  function addImages(files: File[]) {
    if (busy || unconfirmed || !files.length) return;
    const added: FeedbackScreenshot[] = [];
    let error = "";
    for (const file of files) {
      if (screenshots.length + added.length >= MAX_FEEDBACK_SCREENSHOTS) {
        error = t("feedback.imageLimit", { count: MAX_FEEDBACK_SCREENSHOTS });
        break;
      }
      try {
        added.push(makeFeedbackScreenshot(file));
      } catch {
        error = t("feedback.invalidImage");
      }
    }
    setScreenshots((current) => [...current, ...added]);
    setImageError(error);
  }

  function paste(event: ClipboardEvent) {
    const files = Array.from(event.clipboardData.files);
    if (!files.length) {
      for (const item of Array.from(event.clipboardData.items)) {
        const file = item.kind === "file" ? item.getAsFile() : null;
        if (file) files.push(file);
      }
    }
    if (files.some((file) => file.type.startsWith("image/"))) {
      event.preventDefault();
      addImages(files);
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || tooLong || busy || unconfirmed) return;
    setStatus("sending");
    setSendError(null);
    try {
      await submitFeedback(
        {
          ...context,
          feedback: text,
          contactEmail: email,
          screenshots,
        },
        attempt.current,
      );
      setStatus("sent");
    } catch (exc) {
      setStatus("editing");
      setSendError(
        exc instanceof Error ? exc : new Error(t("feedback.failed")),
      );
    }
  }

  return (
    <Modal
      open={open}
      centered
      width={sent ? 384 : 504}
      className={
        sent
          ? "creator-feedback-modal creator-feedback-success"
          : "creator-feedback-modal"
      }
      footer={null}
      title={
        sent ? (
          <div className="creator-feedback-success-heading">
            <span className="creator-feedback-success-icon" aria-hidden="true">
              <Check size={26} strokeWidth={2.5} />
            </span>
            <h2 ref={receiptTitle} tabIndex={-1}>
              {t("feedback.sent")}
            </h2>
          </div>
        ) : (
          <div className="creator-feedback-heading">
            <h2>{t("feedback.title")}</h2>
            <p>{t("feedback.intro")}</p>
            <span aria-hidden="true" className="creator-feedback-mark">
              <img src={creatorGlyph} alt="" />
            </span>
          </div>
        )
      }
      closeIcon={<X size={18} />}
      closable={{ disabled: busy, "aria-label": t("common.close") }}
      mask={{ closable: false }}
      keyboard={!busy}
      onCancel={() => {
        if (!busy) setOpen(false);
      }}
      afterOpenChange={(visible) => {
        if (visible) description.current?.focus();
      }}
      focusable={{ focusTriggerAfterClose: false }}
      afterClose={onClose}
    >
      {sent ? (
        <p className="creator-feedback-receipt">
          <Trans
            i18nKey="feedback.viewReplies"
            components={{
              replies: <PlatformLink href={PLATFORM_NOTIFICATIONS_URL} />,
            }}
          />
        </p>
      ) : (
        <form className="creator-feedback-form" onSubmit={send} onPaste={paste}>
          <div className="creator-feedback-sheet">
            <fieldset disabled={busy || unconfirmed}>
              <div className="creator-feedback-field">
                <label htmlFor={id + "-description"}>
                  {t("feedback.description")}{" "}
                  <span>{t("feedback.requiredMark")}</span>
                </label>
                <textarea
                  ref={description}
                  id={id + "-description"}
                  required
                  rows={4}
                  maxLength={FEEDBACK_LIMIT}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder={t("feedback.placeholder")}
                  aria-invalid={tooLong || undefined}
                />
                {tooLong && (
                  <p className="creator-feedback-error" role="alert">
                    {t("feedback.tooLong", { count: excessLength })}
                  </p>
                )}
              </div>
              <div
                className="creator-feedback-field"
                role="group"
                aria-labelledby={id + "-screenshots"}
              >
                <div className="creator-feedback-label-row">
                  <span id={id + "-screenshots"}>
                    {t("feedback.screenshots")}
                  </span>
                  <small>
                    {screenshots.length} / {MAX_FEEDBACK_SCREENSHOTS}
                  </small>
                </div>
                <div className="creator-feedback-images">
                  {screenshots.map((screenshot, index) => (
                    <ScreenshotTile
                      key={screenshot.key}
                      screenshot={screenshot}
                      index={index + 1}
                      onRemove={() => {
                        setScreenshots((current) =>
                          current.filter((item) => item.key !== screenshot.key),
                        );
                        setImageError("");
                      }}
                    />
                  ))}
                  {screenshots.length < MAX_FEEDBACK_SCREENSHOTS && (
                    <button
                      type="button"
                      className="creator-feedback-add"
                      onClick={() => fileInput.current?.click()}
                    >
                      <Plus size={19} strokeWidth={1.7} />
                      <span>{t("feedback.addImage")}</span>
                    </button>
                  )}
                </div>
                <input
                  type="file"
                  multiple
                  hidden
                  ref={fileInput}
                  accept={FEEDBACK_IMAGE_ACCEPT}
                  aria-label={t("feedback.addImage")}
                  onChange={(e) => {
                    addImages(Array.from(e.target.files ?? []));
                    e.target.value = "";
                  }}
                />
                {imageError && (
                  <p className="creator-feedback-error" role="alert">
                    {imageError}
                  </p>
                )}
                {screenshotNote && (
                  <p className="creator-feedback-note" role="status">
                    {screenshotNote}
                  </p>
                )}
              </div>
              <div className="creator-feedback-field">
                <label htmlFor={id + "-email"}>
                  {t("feedback.email")} <span>{t("common.optional")}</span>
                </label>
                <input
                  type="email"
                  autoComplete="email"
                  id={id + "-email"}
                  maxLength={254}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                />
              </div>
              {sendError && (
                <p className="creator-feedback-error" role="alert">
                  {feedbackErrorMessage(sendError)}{" "}
                  {unconfirmed && (
                    <PlatformLink href={PLATFORM_NOTIFICATIONS_URL}>
                      {t("feedback.checkNotifications")}
                    </PlatformLink>
                  )}
                  {errorCode === "PLATFORM_HOST_UNSUPPORTED" && (
                    <PlatformLink href="https://platform.agentscope.io/feedback">
                      {t("feedback.webForm")}
                    </PlatformLink>
                  )}
                  {needsFeedbackAccount(sendError) && (
                    <PlatformLink href="/settings/community">
                      {t("feedback.connectAccount")}
                    </PlatformLink>
                  )}
                </p>
              )}
            </fieldset>
          </div>
          <div className="creator-feedback-footer">
            <Button
              type="primary"
              htmlType="submit"
              icon={<Send size={14} />}
              loading={busy}
              disabled={!text.trim() || tooLong || busy || unconfirmed}
            >
              {t("feedback.submit")}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
