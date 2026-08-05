import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

export type AssetPreviewState =
  "planned" | "processing" | "ready" | "failed" | "unavailable";

function placeholderLabel(
  state: AssetPreviewState,
  loadFailed: boolean,
  t: (key: string) => string,
): string {
  if (loadFailed) return t("assetPreview.previewUnavailable");
  if (state === "planned") return t("assetPreview.pendingGeneration");
  if (state === "processing") return t("assetPreview.ingesting");
  if (state === "failed") return t("assetPreview.ingestFailed");
  return t("assetPreview.noPreview");
}

export default function AssetMediaPreview({
  name,
  mediaType,
  previewUrl,
  state,
  controls = false,
  mediaClassName,
  placeholderClassName,
}: {
  name: string;
  mediaType: string;
  previewUrl?: string;
  state: AssetPreviewState;
  controls?: boolean;
  mediaClassName: string;
  placeholderClassName: string;
}) {
  const [loadFailed, setLoadFailed] = useState(false);
  const { t } = useTranslation();

  useEffect(() => setLoadFailed(false), [previewUrl]);

  const canRender = state === "ready" && Boolean(previewUrl) && !loadFailed;
  if (canRender && mediaType === "video") {
    return (
      <video
        src={previewUrl}
        aria-label={`${name} ${t("assetPreview.video")}`}
        controls={controls}
        muted={!controls}
        playsInline
        preload="metadata"
        onError={() => setLoadFailed(true)}
        className={mediaClassName}
      />
    );
  }
  if (canRender && mediaType === "image") {
    return (
      <img
        src={previewUrl}
        alt={name}
        onError={() => setLoadFailed(true)}
        className={mediaClassName}
      />
    );
  }
  return (
    <span
      data-asset-preview-state={loadFailed ? "unavailable" : state}
      className={placeholderClassName}
    >
      {placeholderLabel(state, loadFailed, t)}
    </span>
  );
}
