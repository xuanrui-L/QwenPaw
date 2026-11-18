import { useState } from "react";
import { Modal, message } from "antd";
import { HardDriveDownload } from "lucide-react";
import { useTranslation } from "react-i18next";
import { confirmProjectReclaim, CreatorHttpError } from "@/api/creator";
import type { ProjectStorageProfile } from "@/api/creator";
import { formatBytes } from "@/components/creator/ProjectImportExport";

interface StorageReclaimNoticeProps {
  projectId: string;
  projectName: string;
  storage: Pick<
    ProjectStorageProfile,
    | "pendingReclaimBytes"
    | "pendingTransactionCleanupBytes"
    | "historyKeptAt"
    | "reclaimConfirmedAt"
  >;
  reclaimCap: number;
  onConfirmed: () => void | Promise<void>;
}

/**
 * One Project's settled history grew past what the automatic sweep may take
 * on its own authority, so the sweep reported it and stepped back. This row
 * is the whole door: it names the bytes and offers the one press that both
 * records the confirmation and runs the sweeps now.
 *
 * A confirmed Project can retry an interrupted transaction deletion.
 */
export default function StorageReclaimNotice({
  projectId,
  projectName,
  storage,
  reclaimCap,
  onConfirmed,
}: StorageReclaimNoticeProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const retry = storage.reclaimConfirmedAt !== null;
  const pendingBytes = retry
    ? storage.pendingTransactionCleanupBytes ?? 0
    : storage.pendingReclaimBytes;
  if (busy || storage.historyKeptAt || pendingBytes <= (retry ? 0 : reclaimCap))
    return null;
  const size = formatBytes(pendingBytes);

  const reclaim = async () => {
    setBusy(true);
    try {
      const result = await confirmProjectReclaim(projectId);
      message.success(
        t("storage.confirmed", {
          size: formatBytes(result.releasedBytes + result.transactionBytes),
        }),
      );
    } catch (error) {
      const detail =
        error instanceof CreatorHttpError ? error.userMessage : String(error);
      message.error(t("storage.failed", { message: detail }));
    }
    try {
      await onConfirmed();
    } finally {
      setBusy(false);
    }
  };

  const askAndReclaim = () => {
    if (retry) {
      void reclaim();
      return;
    }
    Modal.confirm({
      title: t("storage.confirmTitle", { name: projectName, size }),
      content: t("storage.confirmBody"),
      okText: t("storage.confirmButton"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: () => {
        void reclaim();
      },
    });
  };

  const buttonLabel = t(
    retry ? "storage.retryButton" : "storage.confirmButton",
  );

  return (
    <div
      data-storage-reclaim-notice
      className="flex items-center justify-between gap-2 rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-soft)]/40 px-2 py-1.5"
    >
      <span className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] leading-4 text-[var(--color-warning)]">
        <HardDriveDownload className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">
          {t(retry ? "storage.retryHint" : "storage.pendingHint", { size })}
        </span>
      </span>
      <button
        type="button"
        aria-label={buttonLabel}
        onClick={(event) => {
          event.stopPropagation();
          askAndReclaim();
        }}
        className="shrink-0 cursor-pointer rounded-full border border-[var(--color-warning)]/40 bg-white/60 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--color-warning)] transition-colors hover:bg-white/90 disabled:cursor-wait dark:bg-white/10"
      >
        {buttonLabel}
      </button>
    </div>
  );
}
