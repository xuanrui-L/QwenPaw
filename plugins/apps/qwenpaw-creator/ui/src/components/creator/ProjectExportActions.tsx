import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Popover, Tooltip, message } from "antd";
import {
  ChevronDown,
  Download,
  FileOutput,
  Package,
  Upload,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectDocument } from "@/contracts/creator";
import {
  AssetDownloadButton,
  downloadableVersion,
  downloadableWholeFilm,
  useAssetDownload,
} from "@/components/assets/AssetDownload";
import {
  prepareWholeFilm,
  wholeFilmFileUrl,
  type DownloadAsset,
} from "@/api/creator/assetDownload";
import {
  getArtifactVersionMediaUrl,
  getInteractiveBundleUrl,
  newUploadIdempotencyKey,
  uploadInteractiveBundle,
} from "@/api/creator";
import { selectTimelineFilmVersionId } from "@/selectors/blueprintSelectors";
import {
  selectLiveTimelineIds,
  selectNarrativeShape,
} from "@/selectors/timelineElementSelectors";
import {
  ExportProgressCard,
  saveExportFile,
  type ExportProgressState,
} from "@/components/creator/ProjectImportExport";

/**
 * Filesystem-safe download stem, mirroring the backend `_display_stem` rule:
 * neutralize characters illegal on Windows/macOS/Linux or URL-corrupting
 * (`#`/`%`) while keeping the title legible (single spaces and CJK are valid
 * in a filename); slice by code points so an emoji is never cut in half; and
 * guard Windows reserved names (CON/NUL/COM1…) that fail even with an
 * extension.
 */
const WINDOWS_RESERVED = /^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$/i;

function safeFileName(value: string): string {
  const cleaned = value
    .normalize("NFKC")
    .replace(/[\\/:*?"<>|#%\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[ ._]+|[ ._]+$/g, "");
  const clipped = Array.from(cleaned).slice(0, 80).join("");
  return WINDOWS_RESERVED.test(clipped) ? `_${clipped}` : clipped;
}

function saveDownload(file: string | Blob, filename: string) {
  const link = document.createElement("a");
  link.href = typeof file === "string" ? file : URL.createObjectURL(file);
  link.download = filename;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    if (typeof file !== "string") URL.revokeObjectURL(link.href);
  }
}

/** Read the server's chosen filename back from a Content-Disposition header. */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1].trim());
    } catch {
      // malformed escape -- fall through to the plain form
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

function FilmDownloadRow({
  label,
  detail,
  asset,
  onDownload,
  onClose,
}: {
  label: string;
  detail?: string;
  asset: DownloadAsset | null;
  onDownload?: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const showScan = useAssetDownload();
  return (
    <tr className="hover:bg-[var(--color-bg-secondary)]">
      <th scope="row" className="px-2.5 py-1.5 text-left font-medium">
        <span className="line-clamp-2 break-words" title={label}>
          {label}
        </span>
        {detail && (
          <span className="block text-xs font-normal text-[var(--color-text-secondary)]">
            {detail}
          </span>
        )}
      </th>
      <td className="text-center">
        <Tooltip title={t("assetDownload.direct")} trigger={["hover", "focus"]}>
          <button
            type="button"
            aria-label={`${t("assetDownload.direct")} · ${label}`}
            disabled={!onDownload}
            onClick={() => {
              onClose();
              onDownload?.();
            }}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] text-[var(--color-text-secondary)] transition enabled:cursor-pointer enabled:hover:border-[var(--color-accent)] enabled:hover:text-[var(--color-accent)] focus-visible:outline-[var(--color-accent)] disabled:opacity-35 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
          >
            <Download size={15} aria-hidden="true" />
          </button>
        </Tooltip>
      </td>
      {showScan && (
        <td className="text-center">
          <AssetDownloadButton asset={asset} onOpen={onClose} />
        </td>
      )}
    </tr>
  );
}

/**
 * Project-level export home (design 83:13383 plan header): a 下载/导出
 * dropdown (final cut + project export) plus, on branching projects, the
 * highlighted 导出互动包 button.
 */
export default function ProjectExportActions({
  project,
}: {
  project: ProjectDocument;
}) {
  const { t } = useTranslation();
  const projectId = project.project_id;
  const showScan = useAssetDownload();
  const [panelOpen, setPanelOpen] = useState(false);
  const panelId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closePanel = () => {
    setPanelOpen(false);
    triggerRef.current?.focus({ preventScroll: true });
  };
  const shape = useMemo(() => selectNarrativeShape(project), [project]);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [exportProgress, setExportProgress] =
    useState<ExportProgressState | null>(null);
  const exporting = exportProgress?.status === "running";

  // Multi-episode projects expose each selected film by its timeline title.
  // Never infer that an individual episode represents the entire project.
  const films = useMemo(
    () =>
      selectLiveTimelineIds(project).map((timelineId, index) => ({
        timelineId,
        versionId: selectTimelineFilmVersionId(project, timelineId),
        name:
          project.timelines.items[timelineId].title ||
          t("assetDownload.episode", { n: index + 1 }),
      })),
    [project, t],
  );
  const hasFilm = films.some((film) => film.versionId);
  const wholeFilm = downloadableWholeFilm(project);
  const wholeRequest = useRef<AbortController | null>(null);
  useEffect(() => () => wholeRequest.current?.abort(), [projectId]);
  const downloadWholeFilm = async () => {
    if (!wholeFilm || wholeRequest.current) return;
    const controller = new AbortController();
    wholeRequest.current = controller;
    const dismiss = message.loading(t("assetDownload.preparingWhole"), 0);
    try {
      const result = await prepareWholeFilm(
        projectId,
        wholeFilm,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      saveDownload(
        wholeFilmFileUrl(projectId, result.key),
        `${safeFileName(project.name) || projectId}.mp4`,
      );
    } catch (error) {
      if (!controller.signal.aborted) message.error((error as Error).message);
    } finally {
      dismiss();
      if (wholeRequest.current === controller) wholeRequest.current = null;
    }
  };

  const downloadFilm = async (film: (typeof films)[number]) => {
    if (!film.versionId) return;
    const url = getArtifactVersionMediaUrl(film.versionId);
    const base =
      safeFileName(film.name || project.name || "") || t("blueprint.finalCut");
    const filename = `${base}.mp4`;
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      saveDownload(await response.blob(), filename);
    } catch {
      saveDownload(url, filename);
    }
  };

  const exportProject = async () => {
    if (exporting) return;
    setExportProgress({
      receivedBytes: 0,
      totalBytes: null,
      status: "running",
      phase: "packaging",
    });
    try {
      await saveExportFile(projectId, (receivedBytes, totalBytes) =>
        setExportProgress({
          receivedBytes,
          totalBytes,
          status: "running",
          phase: "downloading",
        }),
      );
      setExportProgress((state) =>
        state ? { ...state, status: "done" } : state,
      );
    } catch (error) {
      setExportProgress(null);
      message.error(
        t("blueprint.exportProjectFailed", {
          detail: (error as Error).message,
        }),
      );
    }
  };

  // The finished card lingers briefly, then clears itself.
  useEffect(() => {
    if (exportProgress?.status !== "done") return;
    const timer = window.setTimeout(() => setExportProgress(null), 5000);
    return () => window.clearTimeout(timer);
  }, [exportProgress]);

  // The bundle endpoint 409s until every branch has a final cut; both the
  // download and the publish path read the same bytes, so fetch once here.
  // The server names the archive (Content-Disposition); we read it back
  // rather than re-deriving a rule that could drift from the backend's.
  const fetchBundle = async (): Promise<{
    blob: Blob;
    filename: string;
  }> => {
    const response = await fetch(getInteractiveBundleUrl(projectId));
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      throw new Error(
        error?.error?.message ||
          error?.message ||
          error?.detail ||
          `HTTP ${response.status}`,
      );
    }
    const blob = await response.blob();
    const filename =
      filenameFromDisposition(response.headers.get("content-disposition")) ||
      `${safeFileName(project.name || "") || projectId}.zip`;
    return { blob, filename };
  };

  const exportBundle = async () => {
    setBundleBusy(true);
    try {
      const { blob, filename } = await fetchBundle();
      saveDownload(blob, filename);
    } catch (error) {
      message.error(
        `${t("blueprint.exportBundleFailed")}：${(error as Error).message}`,
      );
    } finally {
      setBundleBusy(false);
    }
  };

  // Publish to the platform, then hand the person off to progress_url; the
  // Platform side owns polling to success/failed, so this only submits.
  const uploadBundle = async () => {
    setUploadBusy(true);
    try {
      const { blob, filename } = await fetchBundle();
      const result = await uploadInteractiveBundle({
        bundle: blob,
        projectId,
        filename,
        idempotencyKey: newUploadIdempotencyKey(),
      });
      message.success(t("blueprint.uploadBundleSubmitted"));
      if (result.progress_url) {
        window.open(result.progress_url, "_blank", "noopener,noreferrer");
      }
    } catch (error) {
      message.error(
        `${t("blueprint.uploadBundleFailed")}：${(error as Error).message}`,
      );
    } finally {
      setUploadBusy(false);
    }
  };

  return (
    <>
      <Popover
        trigger="click"
        placement="bottomRight"
        arrow={false}
        open={panelOpen}
        onOpenChange={setPanelOpen}
        afterOpenChange={(open) => {
          if (open) panelRef.current?.focus({ preventScroll: true });
        }}
        destroyOnHidden
        content={
          <section
            id={panelId}
            ref={panelRef}
            tabIndex={-1}
            aria-label={t("blueprint.downloadOrExport")}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.stopPropagation();
                closePanel();
              }
            }}
            onBlur={(event) => {
              if (
                event.relatedTarget &&
                !event.currentTarget.contains(event.relatedTarget)
              ) {
                setPanelOpen(false);
              }
            }}
            className={`max-w-[calc(100vw-44px)] text-[var(--color-text-primary)] outline-none ${
              shape === "branching" ? "" : "w-[330px]"
            }`}
          >
            {shape !== "branching" && (
              <div className="max-h-[min(55vh,420px)] overflow-y-auto">
                <table className="w-full table-fixed border-collapse text-sm">
                  <thead className="sticky top-0 bg-[var(--color-bg-primary)] text-xs text-[var(--color-text-secondary)]">
                    <tr className="border-b border-[var(--color-border)]">
                      <th
                        scope="col"
                        className="px-2.5 pb-2 text-left font-medium"
                      >
                        {t("assetDownload.videos")}
                      </th>
                      <th
                        scope="col"
                        className="w-12 pb-2 text-center font-medium"
                      >
                        {t("assetDownload.download")}
                      </th>
                      {showScan && (
                        <th
                          scope="col"
                          className="w-12 pb-2 text-center font-medium"
                        >
                          {t("assetDownload.scan")}
                        </th>
                      )}
                    </tr>
                  </thead>
                  {shape === "linear" && (
                    <tbody className="border-b border-[var(--color-border)]">
                      <FilmDownloadRow
                        label={t("assetDownload.whole")}
                        detail={
                          wholeFilm
                            ? t("assetDownload.episodeCount", {
                                count: films.length,
                              })
                            : t("assetDownload.waitForEpisodes")
                        }
                        asset={wholeFilm}
                        onDownload={wholeFilm ? downloadWholeFilm : undefined}
                        onClose={closePanel}
                      />
                    </tbody>
                  )}
                  <tbody>
                    {films.map((film) => (
                      <FilmDownloadRow
                        key={film.timelineId}
                        label={film.name}
                        detail={
                          !film.versionId
                            ? t("blueprint.waitingForFinalCut")
                            : undefined
                        }
                        asset={downloadableVersion(
                          project,
                          "artifact",
                          film.versionId,
                          t("assetDownload.filmName", { name: film.name }),
                        )}
                        onDownload={
                          film.versionId
                            ? () => void downloadFilm(film)
                            : undefined
                        }
                        onClose={closePanel}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div
              className={
                shape === "branching"
                  ? ""
                  : "mt-2 border-t border-[var(--color-border)] pt-2"
              }
            >
              <button
                type="button"
                disabled={exporting}
                onClick={() => {
                  closePanel();
                  void exportProject();
                }}
                className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm hover:bg-[var(--color-bg-secondary)] disabled:opacity-50"
              >
                <FileOutput size={15} aria-hidden="true" />
                {t(
                  exporting ? "blueprint.exporting" : "blueprint.exportProject",
                )}
              </button>
            </div>
          </section>
        }
      >
        <button
          type="button"
          ref={triggerRef}
          aria-expanded={panelOpen}
          aria-controls={panelOpen ? panelId : undefined}
          data-download-render
          title={
            shape === "branching"
              ? t("blueprint.exportProject")
              : hasFilm
              ? t("blueprint.downloadFinalTitle")
              : t("blueprint.waitingForFinalCut")
          }
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)]"
        >
          <Download className="h-3.5 w-3.5" />
          {t("blueprint.downloadOrExport")}
          <ChevronDown className="h-3.5 w-3.5" />
        </button>
      </Popover>
      {shape === "branching" && (
        <button
          type="button"
          data-export-bundle
          disabled={bundleBusy || !project.narrative_edges?.length}
          title={t(
            project.narrative_edges?.length
              ? "blueprint.downloadBundleTitle"
              : "blueprint.branchesPending",
          )}
          onClick={() => void exportBundle()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-accent)]/50 bg-[var(--color-accent-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--color-accent)] transition hover:border-[var(--color-accent)] disabled:cursor-not-allowed disabled:opacity-70"
        >
          <Package className="h-3.5 w-3.5" />
          {bundleBusy ? t("blueprint.exporting") : t("blueprint.exportBundle")}
        </button>
      )}
      {shape === "branching" && (
        <button
          type="button"
          data-upload-bundle
          disabled={
            uploadBusy || bundleBusy || !project.narrative_edges?.length
          }
          title={t("blueprint.uploadBundleTitle")}
          onClick={() => void uploadBundle()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-1.5 text-xs font-semibold text-[var(--color-text-primary)] transition hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-secondary)] disabled:cursor-not-allowed disabled:opacity-70"
        >
          <Upload className="h-3.5 w-3.5" />
          {uploadBusy ? t("blueprint.uploading") : t("blueprint.uploadBundle")}
        </button>
      )}
      {exportProgress && (
        <ExportProgressCard
          projectName={project.name}
          progress={exportProgress}
          onDismiss={() => setExportProgress(null)}
        />
      )}
    </>
  );
}
