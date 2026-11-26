import i18n from "@/i18n";
import { creatorAuthenticatedUrl, creatorRequest } from "./client";

export interface DownloadVersion {
  kind: "source" | "artifact";
  versionId: string;
  name: string;
}

export interface WholeFilm {
  kind: "whole-film";
  versionIds: string[];
  name: string;
}

export type DownloadAsset = DownloadVersion | WholeFilm;

type PreparedFilm = {
  status: "ready";
  relativePath: string;
  key: string;
  sizeBytes: number;
};

export async function prepareWholeFilm(
  projectId: string,
  film: WholeFilm,
  signal: AbortSignal,
) {
  const path = `/projects/${encodeURIComponent(projectId)}/whole-film`;
  for (;;) {
    signal.throwIfAborted();
    const result = await creatorRequest<PreparedFilm | { status: "preparing" }>(
      path,
      {
        method: "POST",
        body: JSON.stringify({ versionIds: film.versionIds }),
        signal,
      },
    );
    if (result.status === "ready") return result;
    if (result.status !== "preparing")
      throw new Error(i18n.t("assetDownload.invalidResponse"));
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1500));
  }
}

export function wholeFilmFileUrl(projectId: string, key: string) {
  return creatorAuthenticatedUrl(
    `/projects/${encodeURIComponent(projectId)}/whole-film/${encodeURIComponent(
      key,
    )}`,
  );
}

export interface AssetDownloadLink {
  url: string;
  directUrl: string;
  expiresAt: number;
}

const PLATFORM_UPLOAD = "/api/v1/qwenpaw/creator/assets/oss-urls";
const DOWNLOAD_PAGE =
  "https://platform-oss.agentscope.io/qwenpaw-creator/download/index.html";

function isWebUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    return ["https:", "http:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
}

export async function generateAssetDownload(
  projectId: string,
  asset: DownloadAsset,
  signal: AbortSignal,
): Promise<AssetDownloadLink> {
  const source =
    asset.kind === "whole-film"
      ? await prepareWholeFilm(projectId, asset, signal)
      : await creatorRequest<{ relativePath: string }>(
          `/projects/${encodeURIComponent(projectId)}/media/${
            asset.kind
          }/${encodeURIComponent(asset.versionId)}/oss-source`,
          { signal },
        );
  if ("sizeBytes" in source && Number(source.sizeBytes) > 500 * 1024 * 1024) {
    throw new Error(i18n.t("assetDownload.wholeFilmTooLarge"));
  }
  // Like feedback and credits, this platform call uses the deployment's
  // browser cookie. The Creator API bearer belongs only to the local lookup.
  const response = await fetch(PLATFORM_UPLOAD, {
    method: "POST",
    credentials: "include",
    signal,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      relative_path: source.relativePath,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(
      response.status === 401
        ? i18n.t("assetDownload.signIn")
        : typeof error?.message === "string" && error.message
        ? error.message
        : i18n.t("assetDownload.platformError", { status: response.status }),
    );
  }
  const body = await response.json().catch(() => null);
  const data = body?.data;
  if (
    !isWebUrl(data?.preview_url) ||
    !isWebUrl(data?.download_url) ||
    !["image", "video", "file"].includes(data.type) ||
    typeof data.name !== "string" ||
    !data.name ||
    !Number.isSafeInteger(data.size) ||
    data.size < 0 ||
    !Number.isFinite(data.expires_in) ||
    data.expires_in <= 0
  ) {
    throw new Error(i18n.t("assetDownload.invalidResponse"));
  }
  const fragment = new URLSearchParams({
    type: data.type,
    name: data.name,
    size: String(data.size),
    src: data.preview_url,
    url: data.download_url,
  });
  return {
    url: `${DOWNLOAD_PAGE}#${fragment}`,
    directUrl: data.download_url,
    expiresAt: Date.now() + data.expires_in * 1000,
  };
}
