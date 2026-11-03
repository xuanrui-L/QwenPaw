import { useState } from "react";
import { Modal, message } from "antd";
import { HardDriveDownload } from "lucide-react";
import { useTranslation } from "react-i18next";
import { confirmProjectReclaim, CreatorHttpError } from "@/api/creator";
import { formatBytes } from "@/components/creator/ProjectImportExport";

interface StorageReclaimNoticeProps {
  projectId: string;
  projectName: string;
  pendingBytes: number;
  onConfirmed: () => void;
}

/**
 * One Project's settled history grew past what the automatic sweep may take
 * on its own authority, so the sweep reported it and stepped back. This row
 * is the whole door: it names the bytes and offers the one press that both
 * records the confirmation and runs the sweeps now.
 *
 * The notice never appears for a Project that already carries a confirmation
 * receipt, and it disappears again once the press frees the bytes.
 */
export default function StorageReclaimNotice({
  projectId,
  projectName,
  pendingBytes,
  onConfirmed,
}: StorageReclaimNoticeProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const size = formatBytes(pendingBytes);

  const askAndReclaim = () => {
    Modal.confirm({
      title: t("storage.confirmTitle", { name: projectName, size }),
      content: t("storage.confirmBody"),
      okText: t("storage.confirmButton"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: () => {
        // The press is the decision; the deletion is minutes for a 17 GB
        // Project and the receipt lands before it finishes. Waiting for the
        // whole sweep kept a spinning button on screen long after the space
        // was gone, so the notice steps aside at once and reports the real
        // delta when the sweep answers. A refused press brings it back.
        setDismissed(true);
        setBusy(true);
        onConfirmed();
        confirmProjectReclaim(projectId)
          .then((result) => {
            message.success(
              t("storage.confirmed", {
                size: formatBytes(
                  result.releasedBytes + result.transactionBytes,
                ),
              }),
            );
          })
          .catch((error) => {
            setDismissed(false);
            const detail =
              error instanceof CreatorHttpError
                ? error.userMessage
                : String(error);
            message.error(t("storage.failed", { message: detail }));
          })
          .finally(() => setBusy(false));
      },
    });
  };

  if (dismissed) return null;

  return (
    <div
      data-storage-reclaim-notice
      className="flex items-center justify-between gap-2 rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-soft)]/40 px-2 py-1.5"
    >
      <span className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] leading-4 text-[var(--color-warning)]">
        <HardDriveDownload className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">{t("storage.pendingHint", { size })}</span>
      </span>
      <button
        type="button"
        disabled={busy}
        aria-label={t("storage.confirmButton")}
        onClick={(event) => {
          event.stopPropagation();
          askAndReclaim();
        }}
        className="shrink-0 cursor-pointer rounded-full border border-[var(--color-warning)]/40 bg-white/60 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--color-warning)] transition-colors hover:bg-white/90 disabled:cursor-wait dark:bg-white/10"
      >
        {t("storage.confirmButton")}
      </button>
    </div>
  );
}
