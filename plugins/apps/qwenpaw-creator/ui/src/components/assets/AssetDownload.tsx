import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button, Modal, QRCode, Spin, Tooltip } from "antd";
import { Download, QrCode } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  generateAssetDownload,
  type AssetDownloadLink,
  type DownloadAsset,
  type DownloadVersion,
  type WholeFilm,
} from "@/api/creator/assetDownload";
import type { ProjectDocument } from "@/contracts/creator";
import { selectWholeFilmEpisodeVersions } from "@/selectors/blueprintSelectors";
import i18n from "@/i18n";

export function downloadableVersion(
  project: ProjectDocument | null,
  kind: DownloadVersion["kind"],
  versionId?: string | null,
  displayName?: string,
): DownloadVersion | null {
  if (!project || !versionId) return null;
  const assets = project.assets;
  const source =
    kind === "source" ? assets.source_versions_by_id[versionId] : undefined;
  const artifact =
    kind === "artifact" ? assets.artifact_versions_by_id[versionId] : undefined;
  const version = source ?? artifact;
  if (!version) return null;
  const file = version.file_id
    ? assets.files_by_id[version.file_id]
    : undefined;
  if (version.file_id && !file) return null;
  const type = file?.media_type || source?.media_type || "";
  if (
    !/^(image|video|audio)\//.test(type) &&
    !["image", "video", "audio"].includes(source?.media_kind ?? "")
  )
    return null;
  if (type === "image/svg+xml") return null;
  const versions =
    artifact && assets.artifact_slots_by_id[artifact.slot_id]?.version_ids;
  const index = versions?.indexOf(versionId) ?? -1;
  const label = displayName || version.name || versionId;
  const name =
    versions && versions.length > 1 && index >= 0
      ? `${label} · v${index + 1}`
      : label;
  return { kind, versionId, name };
}

export function downloadableWholeFilm(
  project: ProjectDocument,
): WholeFilm | null {
  const versionIds = selectWholeFilmEpisodeVersions(project);
  if (!versionIds) return null;
  return {
    kind: "whole-film",
    versionIds,
    name: i18n.t("assetDownload.wholeFilmName", {
      name: project.name,
      count: versionIds.length,
    }),
  };
}

type DialogState =
  | { asset: DownloadAsset; status: "loading" | "expired" }
  | { asset: DownloadAsset; status: "error"; message: string }
  | { asset: DownloadAsset; status: "ready"; link: AssetDownloadLink };

const DownloadContext = createContext<((asset: DownloadAsset) => void) | null>(
  null,
);
export function useAssetDownload() {
  return useContext(DownloadContext);
}
// A URLSearchParams-encoded fragment is ASCII. QR version 40-L holds 2,953 bytes.
const MAX_QR_BYTES = 2953;

export function AssetDownloadProvider({
  projectId,
  review,
  children,
}: {
  projectId: string;
  review: boolean;
  children: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const pending = useRef<AbortController | null>(null);

  const close = () => {
    pending.current?.abort();
    pending.current = null;
    setDialog(null);
  };
  useEffect(() => {
    if (review) close();
    return () => {
      pending.current?.abort();
    };
  }, [projectId, review]);

  useEffect(() => {
    if (dialog?.status !== "ready") return;
    const expire = () => {
      if (Date.now() >= dialog.link.expiresAt) {
        setDialog((current) =>
          current === dialog
            ? { asset: dialog.asset, status: "expired" }
            : current,
        );
      }
    };
    const timer = window.setTimeout(
      expire,
      Math.max(0, dialog.link.expiresAt - Date.now()),
    );
    document.addEventListener("visibilitychange", expire);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", expire);
    };
  }, [dialog]);

  const open = async (asset: DownloadAsset) => {
    if (review || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setDialog({ asset, status: "loading" });
    try {
      const link = await generateAssetDownload(
        projectId,
        asset,
        controller.signal,
      );
      if (!controller.signal.aborted)
        setDialog({ asset, status: "ready", link });
    } catch (error) {
      if (!controller.signal.aborted) {
        setDialog({
          asset,
          status: "error",
          message:
            error instanceof Error ? error.message : t("assetDownload.failed"),
        });
      }
    } finally {
      if (pending.current === controller) pending.current = null;
    }
  };

  return (
    <DownloadContext.Provider value={review ? null : open}>
      {children}
      {!review && dialog && (
        <Modal
          open
          centered
          width={420}
          footer={null}
          title={t("assetDownload.title")}
          onCancel={close}
        >
          <div
            data-asset-download-dialog
            className="flex flex-col items-center gap-3 text-center"
          >
            <p className="max-w-full break-words text-sm font-medium">
              {dialog.asset.name}
            </p>
            {dialog.status === "loading" && (
              <div role="status" className="flex items-center gap-2 py-8">
                <Spin size="small" />
                {t(
                  dialog.asset.kind === "whole-film"
                    ? "assetDownload.joining"
                    : "assetDownload.uploading",
                )}
              </div>
            )}
            {dialog.status === "ready" && (
              <>
                {dialog.link.url.length <= MAX_QR_BYTES ? (
                  <QRCode
                    type="svg"
                    value={dialog.link.url}
                    size={336}
                    marginSize={4}
                    errorLevel="L"
                    color="#000"
                    bgColor="#fff"
                    aria-label={t("assetDownload.title")}
                    style={{
                      maxWidth: "100%",
                      height: "auto",
                      aspectRatio: "1",
                    }}
                  />
                ) : (
                  <p role="status">{t("assetDownload.tooLong")}</p>
                )}
                <a
                  href={dialog.link.directUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--color-accent)] underline underline-offset-4"
                >
                  <Download size={15} aria-hidden="true" />
                  {t("assetDownload.direct")}
                </a>
                <p className="text-xs text-[var(--color-text-secondary)]">
                  {t("assetDownload.expires", {
                    time: new Date(dialog.link.expiresAt).toLocaleString(
                      i18n.language,
                      {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      },
                    ),
                  })}
                </p>
              </>
            )}
            {(dialog.status === "error" || dialog.status === "expired") && (
              <>
                <p
                  role="alert"
                  className="text-sm text-[var(--color-text-secondary)]"
                >
                  {dialog.status === "error"
                    ? dialog.message
                    : t("assetDownload.expired")}
                </p>
                <Button onClick={() => void open(dialog.asset)}>
                  {t("assetDownload.retry")}
                </Button>
              </>
            )}
          </div>
        </Modal>
      )}
    </DownloadContext.Provider>
  );
}

export function AssetDownloadButton({
  asset,
  withLabel = false,
  onOpen,
}: {
  asset: DownloadAsset | null;
  withLabel?: boolean;
  onOpen?: () => void;
}) {
  const open = useAssetDownload();
  const { t } = useTranslation();
  if (!asset || !open) return null;
  const label = t("assetDownload.title");
  const button = (
    <button
      type="button"
      data-asset-download={
        asset.kind === "whole-film" ? asset.kind : asset.versionId
      }
      aria-haspopup="dialog"
      aria-label={`${label} · ${asset.name}`}
      className={`inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] text-xs font-semibold text-[var(--color-text-secondary)] transition hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] focus-visible:outline-2 focus-visible:outline-[var(--color-accent)] ${
        withLabel
          ? "px-3 py-1.5"
          : "h-8 w-8 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
      }`}
      onClick={(event) => {
        event.stopPropagation();
        onOpen?.();
        open(asset);
      }}
    >
      <QrCode size={15} aria-hidden="true" />
      {withLabel && label}
    </button>
  );
  return withLabel ? (
    button
  ) : (
    <Tooltip title={label} trigger={["hover", "focus"]}>
      {button}
    </Tooltip>
  );
}
