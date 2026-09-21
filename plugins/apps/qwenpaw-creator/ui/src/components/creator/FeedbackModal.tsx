import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Input, Modal } from "antd";
import { submitFeedback } from "@/api/creator/feedback";
import { FEEDBACK_LIMIT } from "@/api/creator/feedback";

/**
 * Ask the person what went wrong, and only that.
 *
 * The record's other fields - project, stage, trace pointer - are completed by
 * Creator's `/feedbacks/draft`, so no question here is one the application
 * could already answer. The submit goes from this browser to the platform
 * because the platform reads the console cookie and ignores every bearer.
 */
export default function FeedbackModal({
  open,
  projectId,
  onClose,
}: {
  open: boolean;
  projectId: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  const reset = () => {
    setText("");
    setError("");
    setSent(false);
    onClose();
  };

  const send = async () => {
    const feedback = text.trim();
    if (!feedback || busy) return;
    setBusy(true);
    setError("");
    try {
      await submitFeedback({ projectId, feedback });
      setText("");
      setSent(true);
    } catch (exc) {
      setError((exc as Error).message || t("feedback.failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t("feedback.title")}
      okText={t("feedback.submit")}
      cancelText={t("common.cancel")}
      confirmLoading={busy}
      okButtonProps={{ disabled: sent || !text.trim() }}
      onOk={() => void send()}
      onCancel={reset}
      destroyOnClose
    >
      {sent ? (
        <Alert type="success" showIcon message={t("feedback.sent")} />
      ) : (
        <>
          <Input.TextArea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={t("feedback.placeholder")}
            autoSize={{ minRows: 4, maxRows: 10 }}
            maxLength={FEEDBACK_LIMIT}
          />
          {error && (
            <Alert
              type="error"
              showIcon
              message={error}
              style={{ marginTop: 12 }}
            />
          )}
        </>
      )}
    </Modal>
  );
}
