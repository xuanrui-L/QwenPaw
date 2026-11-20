import { useEffect, useMemo, useState } from "react";
import { Dropdown, message } from "antd";
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
          t("blueprint.episodeN", { n: index + 1 }),
      })),
    [project, t],
  );
  const hasFilm = films.some((film) => film.versionId);

  const downloadFilm = async (film: (typeof films)[number]) => {
    if (!film.versionId) return;
    const url = getArtifactVersionMediaUrl(film.versionId);
    const base =
      safeFileName(film.name || project.name || "") || t("blueprint.finalCut");
    const filename = `${base}.mp4`;
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
    } catch {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
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
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
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
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            ...(shape === "branching"
              ? []
              : films.length > 1
              ? [
                  {
                    type: "group" as const,
                    key: "films",
                    label: t("blueprint.downloadFinal"),
                    children: films.map((film) => ({
                      key: film.timelineId,
                      label: film.name,
                      icon: <Download className="h-3.5 w-3.5" />,
                      disabled: !film.versionId,
                      onClick: () => void downloadFilm(film),
                    })),
                  },
                ]
              : [
                  {
                    key: "download",
                    label: t("blueprint.downloadFinal"),
                    icon: <Download className="h-3.5 w-3.5" />,
                    disabled: !hasFilm,
                    onClick: () => films[0] && void downloadFilm(films[0]),
                  },
                ]),
            {
              key: "export",
              label: exporting
                ? t("blueprint.exporting")
                : t("blueprint.exportProject"),
              icon: <FileOutput className="h-3.5 w-3.5" />,
              disabled: exporting,
              onClick: () => void exportProject(),
            },
          ],
        }}
      >
        <button
          type="button"
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
      </Dropdown>
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
