import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Button, Input, message, Modal, Tabs } from "antd";
import {
  Box,
  Clapperboard,
  Download,
  FileText,
  Film,
  Image as ImageIcon,
  Link2,
  Music2,
  Paperclip,
  Search,
  Upload,
  Wand2,
} from "lucide-react";
import {
  getArtifactVersionMediaUrl,
  getAssetVersionMediaUrl,
  ingestAssetFile,
  ingestAssetValue,
} from "@/api/creator";
import type {
  ArtifactVersionDocument,
  ProjectDocument,
  SourceAssetVersionDocument,
  VisualEntityDocument,
  VisualVariantDocument,
} from "@/contracts/creator";
import { navigate, useParams, useSearchParams } from "@/routing/navigation";
import {
  useReviewFieldFocus,
  useReviewMediaFocus,
} from "@/routing/reviewFocus";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import AssetMediaPreview from "@/components/assets/AssetMediaPreview";
import DocumentUnderstanding from "@/components/assets/DocumentUnderstanding";
import PageLoadError from "@/components/PageLoadError";
import PageSkeleton from "@/components/PageSkeleton";
import { selectPrimaryTimeline } from "@/selectors/timelineElementSelectors";
import { visualVariantLabel } from "@/lib/visualVariants";

type FilterKey =
  | "all"
  | "source"
  | "artifact"
  | "visual"
  | "image"
  | "video"
  | "audio";
type AssetItem = {
  id: string;
  ref: string;
  kind: "source" | "artifact" | "visual";
  name: string;
  cardName?: string;
  description: string;
  mediaKind: string;
  mediaType: string;
  previewUrl?: string;
  createdAt?: string;
  stale?: boolean;
  durationSeconds?: number | null;
  checksum?: string;
  ownerRef?: string;
  entityId?: string;
  variantId?: string;
  variantOrder?: number;
  variantLabel?: string;
  variantState?: "active" | "history" | "unselected";
  provenanceRefs: string[];
  metadata: Record<string, unknown>;
  raw:
    | SourceAssetVersionDocument
    | ArtifactVersionDocument
    | VisualEntityDocument;
};

type AssetItemGroup = {
  key: string;
  label: string | null;
  badge?: string;
  countLabel?: string;
  items: AssetItem[];
};

const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "source", label: "来源素材" },
  { key: "artifact", label: "生成产物" },
  { key: "visual", label: "视觉设定" },
  { key: "image", label: "图片" },
  { key: "video", label: "视频" },
  { key: "audio", label: "音频" },
];

function fileMedia(
  project: ProjectDocument,
  fileId: string | null,
): { kind: string; type: string } {
  const type = fileId
    ? project.assets.files_by_id[fileId]?.media_type || ""
    : "";
  const kind = type.startsWith("image/")
    ? "image"
    : type.startsWith("video/")
    ? "video"
    : type.startsWith("audio/")
    ? "audio"
    : type.startsWith("text/")
    ? "text"
    : "other";
  return { kind, type };
}

const MIME_EXTENSION_MAP: Record<string, string> = {
  "video/mp4": ".mp4",
  "video/quicktime": ".mov",
  "video/webm": ".webm",
  "video/x-matroska": ".mkv",
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "audio/mpeg": ".mp3",
  "audio/wav": ".wav",
  "audio/ogg": ".ogg",
};

function downloadName(name: string, mediaType: string): string {
  const base = name.trim() || "download";
  if (/\.[a-zA-Z0-9]{1,10}$/.test(base)) return base;
  const ext =
    MIME_EXTENSION_MAP[mediaType.split(";")[0].trim().toLowerCase()] ?? "";
  return ext ? `${base}${ext}` : base;
}

function artifactMedia(
  project: ProjectDocument,
  artifact: ArtifactVersionDocument,
): { kind: string; type: string } {
  const file = fileMedia(project, artifact.file_id);
  if (file.kind !== "other") return file;
  const hint = `${artifact.kind} ${artifact.name}`.toLocaleLowerCase();
  if (hint.includes("video") || hint.includes("render"))
    return { kind: "video", type: file.type };
  if (hint.includes("audio")) return { kind: "audio", type: file.type };
  if (hint.includes("image") || hint.includes("storyboard"))
    return { kind: "image", type: file.type };
  return file;
}

// Keep one semantic card per Variant and underlying content. The same image
// can legitimately be attached to two legacy Variants; keep both assignments
// visible instead of hiding the data-quality issue.
function dedupeByChecksum(items: AssetItem[]): AssetItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (!item.checksum) return true;
    const key = `${item.variantId ?? ""}:${item.checksum}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function visualVariantForVersion(
  project: ProjectDocument,
  versionId: string,
): {
  entity: VisualEntityDocument;
  variant: VisualVariantDocument;
} | null {
  const artifact = project.assets.artifact_versions_by_id[versionId];
  const metadataVariantId =
    typeof artifact?.metadata.variantId === "string"
      ? artifact.metadata.variantId
      : null;
  const ownerEntityId = (artifact?.owner_ref ?? "").replace(
    /^(?:visual-entity|asset):/,
    "",
  );
  const ownerEntity = project.visual.entities.items[ownerEntityId];
  if (
    ownerEntity &&
    metadataVariantId &&
    ownerEntity.variants.items[metadataVariantId]
  ) {
    return {
      entity: ownerEntity,
      variant: ownerEntity.variants.items[metadataVariantId],
    };
  }
  for (const entityId of project.visual.entities.order) {
    const entity = project.visual.entities.items[entityId];
    if (!entity) continue;
    for (const variantId of entity.variants.order) {
      const variant = entity.variants.items[variantId];
      if (variant?.generated_artifact_version_ids.includes(versionId)) {
        return { entity, variant };
      }
    }
  }
  return null;
}

function visualVariantCardName(variant: VisualVariantDocument): string {
  const label = visualVariantLabel(variant, 36);
  return label.split(/[：:]/, 1)[0]?.trim() || label;
}

function assetItems(project: ProjectDocument): AssetItem[] {
  const sources = Object.values(project.assets.source_versions_by_id).map(
    (source): AssetItem => ({
      id: source.version_id,
      ref: `asset-version:${source.version_id}`,
      kind: "source",
      name: source.name || source.version_id,
      description: String(
        source.metadata.description ||
          source.metadata.user_notes ||
          "用户导入的来源素材",
      ),
      mediaKind: source.media_kind,
      mediaType: source.media_type,
      previewUrl: ["image", "video", "audio"].includes(source.media_kind)
        ? getAssetVersionMediaUrl(source.version_id)
        : undefined,
      createdAt: source.created_at,
      durationSeconds: source.duration_seconds,
      checksum: source.checksum,
      provenanceRefs: source.provenance_refs,
      metadata: source.metadata,
      raw: source,
    }),
  );
  const artifacts = Object.values(project.assets.artifact_versions_by_id).map(
    (artifact): AssetItem => {
      const media = artifactMedia(project, artifact);
      const visualVariant = visualVariantForVersion(
        project,
        artifact.version_id,
      );
      return {
        id: artifact.version_id,
        ref: `artifact-version:${artifact.version_id}`,
        kind: "artifact",
        name: artifact.name || artifact.version_id,
        description: artifact.stale
          ? artifact.stale_reason || "该产物依赖的 Project 内容已经变化"
          : `${artifact.kind} · generation ${artifact.based_on_generation}`,
        mediaKind: media.kind,
        mediaType: media.type,
        previewUrl: ["image", "video", "audio"].includes(media.kind)
          ? getArtifactVersionMediaUrl(artifact.version_id)
          : undefined,
        createdAt: artifact.created_at,
        stale: artifact.stale,
        durationSeconds: artifact.duration_seconds,
        checksum: artifact.checksum,
        ownerRef: artifact.owner_ref,
        entityId: visualVariant?.entity.entity_id,
        variantId: visualVariant?.variant.variant_id,
        variantLabel: visualVariant
          ? `${visualVariant.entity.name} · ${visualVariantCardName(
              visualVariant.variant,
            )}`
          : undefined,
        variantState: visualVariant
          ? visualVariant.variant.selected_artifact_version_id ===
            artifact.version_id
            ? "active"
            : "history"
          : undefined,
        provenanceRefs: artifact.provenance_refs,
        metadata: artifact.metadata,
        raw: artifact,
      };
    },
  );
  const visuals = project.visual.entities.order.flatMap(
    (entityId): AssetItem[] => {
      const entity = project.visual.entities.items[entityId];
      if (!entity) return [];
      const variants = entity.variants.order.length
        ? entity.variants.order
            .map((variantId) => entity.variants.items[variantId])
            .filter((variant): variant is VisualVariantDocument =>
              Boolean(variant),
            )
        : [null];
      return variants.map((variant, variantIndex): AssetItem => {
        const selectedVersionId =
          variant?.selected_artifact_version_id ??
          (!variant ? entity.selected_artifact_version_id : null);
        const artifact = selectedVersionId
          ? project.assets.artifact_versions_by_id[selectedVersionId]
          : undefined;
        const media = artifact
          ? artifactMedia(project, artifact)
          : { kind: "image", type: "" };
        return {
          id: variant
            ? `${entity.entity_id}@${variant.variant_id}`
            : entity.entity_id,
          ref: variant
            ? `visual-variant:${entity.entity_id}@${variant.variant_id}`
            : `visual-entity:${entity.entity_id}`,
          kind: "visual",
          name: entity.name,
          cardName: variant ? visualVariantCardName(variant) : entity.name,
          description:
            variant?.requirements ||
            entity.description ||
            entity.continuity ||
            `${entity.kind} 视觉设定`,
          mediaKind: media.kind,
          mediaType: media.type,
          previewUrl: artifact
            ? getArtifactVersionMediaUrl(artifact.version_id)
            : undefined,
          stale: artifact?.stale,
          checksum: artifact?.checksum,
          ownerRef: artifact?.owner_ref,
          entityId: entity.entity_id,
          variantId: variant?.variant_id,
          variantOrder: variantIndex,
          variantLabel: variant ? visualVariantCardName(variant) : undefined,
          variantState: variant
            ? artifact
              ? "active"
              : "unselected"
            : undefined,
          // Surface the references the generation model actually saw — e.g. the
          // web-grounding photo a scene design was composed from. The artifact
          // is the ground truth; the variant's reference_asset_version_ids is
          // the configured intent, which the provenance_refs mirror at run time.
          provenanceRefs: artifact?.provenance_refs ?? [],
          metadata: {
            kind: entity.kind,
            continuity: entity.continuity,
            variants: entity.variants.order.length,
            variant_id: variant?.variant_id,
            generated_artifact_version_ids:
              variant?.generated_artifact_version_ids ?? [],
            selected_artifact_version_id: selectedVersionId,
          },
          raw: entity,
        };
      });
    },
  );
  return dedupeByChecksum([...visuals, ...sources, ...artifacts]).sort(
    (left, right) => {
      return (
        (right.createdAt || "").localeCompare(left.createdAt || "") ||
        left.name.localeCompare(right.name)
      );
    },
  );
}

function visualItemGroups(
  project: ProjectDocument,
  items: AssetItem[],
): AssetItemGroup[] {
  const itemsByEntity = new Map<string, AssetItem[]>();
  const unassigned: AssetItem[] = [];
  for (const item of items) {
    if (!item.entityId) {
      unassigned.push(item);
      continue;
    }
    const entityItems = itemsByEntity.get(item.entityId) ?? [];
    entityItems.push(item);
    itemsByEntity.set(item.entityId, entityItems);
  }

  const characterGroups: AssetItemGroup[] = [];
  const sceneItems: AssetItem[] = [];
  const propItems: AssetItem[] = [];
  for (const entityId of project.visual.entities.order) {
    const entity = project.visual.entities.items[entityId];
    const entityItems = itemsByEntity.get(entityId);
    if (!entity || !entityItems?.length) continue;
    entityItems.sort(
      (left, right) =>
        (left.variantOrder ?? 0) - (right.variantOrder ?? 0) ||
        (left.cardName || left.name).localeCompare(
          right.cardName || right.name,
        ),
    );
    if (entity.kind === "character") {
      const requiredCount = entity.required_variant_ids.length;
      const definedCount = entity.variants.order.length;
      characterGroups.push({
        key: `character:${entityId}`,
        label: entity.name,
        badge: "角色",
        countLabel:
          requiredCount > 0
            ? definedCount === requiredCount
              ? `${definedCount} 个造型`
              : `${definedCount}/${requiredCount} 个造型`
            : "1 项设定",
        items: entityItems,
      });
    } else if (entity.kind === "scene") {
      sceneItems.push(...entityItems);
    } else {
      propItems.push(...entityItems);
    }
  }

  return [
    ...characterGroups,
    ...(sceneItems.length
      ? [
          {
            key: "visual-scenes",
            label: "场景",
            countLabel: `${sceneItems.length} 项设定`,
            items: sceneItems,
          },
        ]
      : []),
    ...(propItems.length
      ? [
          {
            key: "visual-props",
            label: "道具",
            countLabel: `${propItems.length} 项设定`,
            items: propItems,
          },
        ]
      : []),
    ...(unassigned.length
      ? [
          {
            key: "visual-other",
            label: "其他设定",
            countLabel: `${unassigned.length} 项设定`,
            items: unassigned,
          },
        ]
      : []),
  ];
}

function kindLabel(item: AssetItem): string {
  if (item.kind === "source")
    return item.mediaKind === "document" ? "来源 · 文档" : "来源";
  if (item.kind === "artifact") return "产物";
  const entity = item.raw as VisualEntityDocument;
  return entity.kind === "character"
    ? "角色"
    : entity.kind === "scene"
    ? "场景"
    : "道具";
}

function mediaIcon(kind: string) {
  if (kind === "video") return Film;
  if (kind === "audio") return Music2;
  if (kind === "image") return ImageIcon;
  if (kind === "text" || kind === "document") return FileText;
  return Box;
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  if (value === null || value === undefined) return "—";
  return JSON.stringify(value);
}

/** Resolve a provenance/ref string to a previewable thumbnail + label. */
function resolveProvenanceRef(
  project: ProjectDocument,
  ref: string,
): { name: string; url: string; kind: "image" | "video"; ref: string } | null {
  if (ref.startsWith("asset-version:")) {
    const id = ref.slice("asset-version:".length);
    const version = project.assets.source_versions_by_id[id];
    if (!version) return null;
    return {
      name: version.name || id,
      url: getAssetVersionMediaUrl(id),
      kind: version.media_kind === "video" ? "video" : "image",
      ref,
    };
  }
  if (ref.startsWith("artifact-version:")) {
    const id = ref.slice("artifact-version:".length);
    const version = project.assets.artifact_versions_by_id[id];
    if (!version) return null;
    const media = artifactMedia(project, version);
    if (!media) return null;
    return {
      name: version.name || id,
      url: getArtifactVersionMediaUrl(id),
      kind: media.kind === "video" ? "video" : "image",
      ref,
    };
  }
  if (ref.startsWith("visual-entity:")) {
    const entityId = ref.slice("visual-entity:".length);
    const entity = project.visual.entities.items[entityId];
    // A multi-Variant entity has no safe implicit selection. Legacy entity
    // provenance therefore stays unresolved until it names visual-variant:.
    const versionId = entity
      ? entity.variants.order.length === 1
        ? entity.variants.items[entity.variants.order[0]]
            ?.selected_artifact_version_id ?? null
        : entity.variants.order.length === 0
        ? entity.selected_artifact_version_id
        : null
      : null;
    const version = versionId
      ? project.assets.artifact_versions_by_id[versionId]
      : undefined;
    if (!version) return null;
    return {
      name: entity?.name || entityId,
      url: getArtifactVersionMediaUrl(versionId!),
      kind: "image",
      ref,
    };
  }
  if (ref.startsWith("visual-variant:")) {
    const identity = ref.slice("visual-variant:".length);
    const separator = identity.lastIndexOf("@");
    if (separator < 1) return null;
    const entityId = identity.slice(0, separator);
    const variantId = identity.slice(separator + 1);
    const entity = project.visual.entities.items[entityId];
    const versionId =
      entity?.variants.items[variantId]?.selected_artifact_version_id ?? null;
    if (!entity || !versionId) return null;
    return {
      name: `${entity.name} / ${visualVariantLabel(
        entity.variants.items[variantId],
      )}`,
      url: getArtifactVersionMediaUrl(versionId),
      kind: "image",
      ref,
    };
  }
  return null;
}

interface PromptTarget {
  pointer: string;
  value: string;
  label: string;
}

function visualEntityPromptTarget(
  entity: VisualEntityDocument,
  versionId: string | null,
  requestedVariantId?: string,
): PromptTarget | null {
  const variantId =
    (requestedVariantId &&
      entity.variants.items[requestedVariantId] &&
      requestedVariantId) ||
    (versionId &&
      entity.variants.order.find(
        (candidate) =>
          entity.variants.items[
            candidate
          ]?.generated_artifact_version_ids.includes(versionId),
      )) ||
    entity.variants.order[0];
  const variant = variantId ? entity.variants.items[variantId] : null;
  if (!variant) return null;
  return {
    pointer: `/visual/entities/items/${entity.entity_id}/variants/items/${variant.variant_id}/prompt`,
    value: variant.prompt,
    label: "生成 Prompt",
  };
}

function generationPromptTarget(
  project: ProjectDocument,
  selected: AssetItem,
): PromptTarget | null {
  if (selected.kind === "visual") {
    const entity = selected.raw as VisualEntityDocument;
    return visualEntityPromptTarget(
      entity,
      selected.variantId
        ? entity.variants.items[selected.variantId]
            ?.selected_artifact_version_id ?? null
        : entity.selected_artifact_version_id,
      selected.variantId,
    );
  }
  if (selected.kind !== "artifact") return null;
  const ownerRef = selected.ownerRef ?? "";
  if (ownerRef.startsWith("visual-entity:") || ownerRef.startsWith("asset:")) {
    const visualVariant = visualVariantForVersion(project, selected.id);
    const entity =
      visualVariant?.entity ??
      project.visual.entities.items[
        ownerRef.replace(/^(visual-entity:|asset:)/, "")
      ];
    return entity
      ? visualEntityPromptTarget(
          entity,
          selected.id,
          visualVariant?.variant.variant_id,
        )
      : null;
  }
  if (ownerRef.startsWith("element:")) {
    const elementId = ownerRef.slice("element:".length);
    const timeline = selectPrimaryTimeline(project);
    const element = timeline?.elements_by_id[elementId];
    if (!timeline || !element) return null;
    const base = `/timelines/items/${timeline.timeline_id}/elements_by_id/${elementId}/creation`;
    if (element.creation.type === "r2v") {
      const artifact = selected.raw as ArtifactVersionDocument;
      const isVideo =
        selected.mediaKind === "video" || `${artifact.kind}`.includes("video");
      return isVideo
        ? {
            pointer: `${base}/video_prompt`,
            value: element.creation.video_prompt,
            label: "视频生成 Prompt",
          }
        : {
            pointer: `${base}/storyboard_prompt`,
            value: element.creation.storyboard_prompt,
            label: "分镜图生成 Prompt",
          };
    }
    if (element.creation.type === "overlay") {
      return {
        pointer: `${base}/prompt`,
        value: element.creation.prompt,
        label: "生成 Prompt",
      };
    }
  }
  return null;
}

/** Editable generation-prompt block; key is bound to the pointer so drafts reset when the selection changes. */
function GenerationPromptEditor({
  target,
  onSave,
  saving,
}: {
  target: PromptTarget;
  onSave: (target: PromptTarget, next: string) => Promise<void>;
  saving: boolean;
}) {
  const [draft, setDraft] = useState(target.value);
  const dirty = draft !== target.value;
  return (
    <div
      data-creator-path={target.pointer}
      data-creator-field-label={target.label}
      className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-3"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1 text-xs font-semibold text-[var(--color-text-secondary)]">
          <Wand2 className="h-3.5 w-3.5" />
          {target.label}
        </span>
        <div className="flex gap-1.5">
          {dirty && (
            <Button size="small" onClick={() => setDraft(target.value)}>
              还原
            </Button>
          )}
          <Button
            size="small"
            type="primary"
            disabled={!dirty}
            loading={saving}
            onClick={() => void onSave(target, draft)}
          >
            保存
          </Button>
        </div>
      </div>
      <Input.TextArea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        autoSize={{ minRows: 3, maxRows: 10 }}
        placeholder="该产物的生成 Prompt"
        className="!text-xs"
      />
      <p className="mt-1.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
        修改后保存即写入项目；下次生成将使用新 Prompt。
      </p>
    </div>
  );
}

export default function AssetsPage() {
  const { id = "" } = useParams();
  const query = useSearchParams();
  const project = useProjectSnapshotStore((state) =>
    state.projectId === id ? state.project : null,
  );
  const syncStatus = useProjectSnapshotStore((state) => state.syncStatus);
  const syncError = useProjectSnapshotStore((state) => state.syncError);
  const pollOnce = useProjectSnapshotStore((state) => state.pollOnce);
  const patchProject = useProjectSnapshotStore((state) => state.patch);
  const patching = useProjectSnapshotStore((state) => state.patching);
  const refreshTasks = useCreatorTaskViewStore((state) => state.refresh);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [search, setSearch] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [inputKind, setInputKind] = useState<"url" | "text">("url");
  const [inputName, setInputName] = useState("");
  const [inputValue, setInputValue] = useState("");
  const selectedId = query.get("asset");
  const reviewMode = query.get("review") === "1";
  const reviewField = query.get("field");
  const reviewPulse = query.get("reviewPulse");
  const versionFromUrl = query.get("version");
  useReviewFieldFocus({
    path: `/project/${id}/assets`,
    field: reviewField,
    enabled: reviewMode,
    pulse: reviewPulse,
  });
  // "View generation detail" for portrait-image reviews has no field pointer;
  // flash the detail preview anchored by the version awaiting review.
  useReviewMediaFocus({
    versionId: versionFromUrl,
    enabled: reviewMode && !reviewField,
    pulse: reviewPulse,
  });
  const allItems = useMemo(
    () => (project ? assetItems(project) : []),
    [project],
  );
  const items = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return allItems.filter((item) => {
      const filterMatch =
        filter === "all" ||
        filter === item.kind ||
        filter === item.mediaKind ||
        (filter === "artifact" &&
          item.kind === "visual" &&
          item.variantState === "active");
      const searchMatch =
        !needle ||
        `${item.name} ${item.description} ${item.ref}`
          .toLocaleLowerCase()
          .includes(needle);
      return filterMatch && searchMatch;
    });
  }, [allItems, filter, search]);
  const itemGroups = useMemo(() => {
    if (filter === "visual") return visualItemGroups(project, items);
    return [
      {
        key: `flat:${filter}`,
        label: null,
        items,
      },
    ];
  }, [filter, items, project]);
  const selected =
    allItems.find((item) => item.id === selectedId) ||
    allItems.find(
      (item) =>
        item.kind === "visual" &&
        item.entityId === selectedId &&
        item.variantState === "active",
    ) ||
    null;

  useEffect(() => {
    useCreatorInteractionStore.getState().select(selected?.ref || null);
  }, [selected?.ref]);

  const selectItem = (item: AssetItem | null) => {
    navigate(
      item
        ? `/project/${id}/assets?asset=${encodeURIComponent(item.id)}`
        : `/project/${id}/assets`,
    );
  };
  const refreshAfterIngest = async () => {
    await Promise.allSettled([pollOnce(id), refreshTasks(id)]);
  };
  const uploadFile = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      await ingestAssetFile(id, file, "ATTACH_SOURCE");
      message.success("素材已提交，入库完成后会自动出现在这里");
      await refreshAfterIngest();
    } catch (error) {
      // Keep a persistent inline banner besides the transient toast so the
      // rejection reason stays readable (acceptance B6).
      const text = error instanceof Error ? error.message : "上传失败";
      setUploadError(text);
      message.error(text, 6);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };
  const addValue = async () => {
    if (!inputName.trim() || !inputValue.trim()) {
      message.warning("请填写名称和内容");
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      await ingestAssetValue(id, {
        kind: inputKind,
        name: inputName.trim(),
        value: inputValue.trim(),
        postIngestAction: "ATTACH_SOURCE",
      });
      message.success(
        inputKind === "url" ? "链接已提交入库" : "文本素材已提交入库",
      );
      setAddOpen(false);
      setInputName("");
      setInputValue("");
      await refreshAfterIngest();
    } catch (error) {
      const text = error instanceof Error ? error.message : "添加失败";
      setUploadError(text);
      message.error(text, 6);
    } finally {
      setUploading(false);
    }
  };

  if (!project) {
    if (syncStatus === "invalid" || syncStatus === "not_found") {
      return (
        <PageLoadError
          message={syncError || "Project 无法读取"}
          retry={() => void pollOnce(id)}
        />
      );
    }
    return <PageSkeleton type="grid" />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--color-bg-layout)]">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)]/70 px-5 py-3 backdrop-blur">
        <div>
          <h2 className="text-base font-semibold text-[var(--color-text-primary)]">
            素材与产物
          </h2>
          <p className="mt-0.5 text-xs text-[var(--color-text-secondary)]">
            当前项目中的来源素材、生成结果与视觉设定；时间线内容会引用这里的具体版本。
          </p>
        </div>
        <div
          data-onboarding-id="assets-upload"
          className="flex items-center gap-2"
        >
          <input
            ref={fileInputRef}
            type="file"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void uploadFile(file);
            }}
          />
          <Button
            size="small"
            icon={<Link2 className="h-3.5 w-3.5" />}
            onClick={() => setAddOpen(true)}
          >
            添加链接或文本
          </Button>
          <Button
            size="small"
            loading={uploading}
            icon={<Upload className="h-3.5 w-3.5" />}
            onClick={() => fileInputRef.current?.click()}
          >
            上传素材
          </Button>
        </div>
      </header>

      {uploadError && (
        <div
          role="alert"
          data-creator-module="asset-upload-error"
          className="flex shrink-0 items-start justify-between gap-3 border-b border-red-200 bg-red-50 px-5 py-2 text-xs text-red-700"
        >
          <span className="leading-5">{uploadError}</span>
          <button
            type="button"
            aria-label="关闭错误提示"
            onClick={() => setUploadError(null)}
            className="shrink-0 font-semibold text-red-500 hover:text-red-700"
          >
            ×
          </button>
        </div>
      )}

      <div
        data-onboarding-id="assets-filters"
        className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-primary)] px-5 py-2.5"
      >
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((candidate) => (
            <button
              key={candidate.key}
              type="button"
              onClick={() => setFilter(candidate.key)}
              className={`rounded-full px-3 py-1 text-xs transition ${
                filter === candidate.key
                  ? "bg-[var(--color-accent)] text-white"
                  : "border border-[var(--color-border)] bg-white text-[var(--color-text-secondary)] hover:border-[var(--color-accent)]/50"
              }`}
            >
              {candidate.label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex w-56 items-center rounded-lg border border-[var(--color-border)] bg-white px-2.5">
          <Search className="h-3.5 w-3.5 text-[var(--color-text-tertiary)]" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索名称或 ID"
            className="min-w-0 flex-1 border-0 bg-transparent px-2 py-1.5 text-xs outline-none"
          />
        </div>
        <span className="text-[11px] text-[var(--color-text-tertiary)]">
          {items.length} 项
        </span>
      </div>

      <main className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_340px] gap-4 overflow-hidden p-4">
        <section
          data-onboarding-id="assets-grid"
          className="min-h-0 overflow-y-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-3"
        >
          {items.length > 0 ? (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(190px,1fr))] gap-3">
              {itemGroups.map((group) => (
                <Fragment key={group.key}>
                  {group.label && (
                    <div
                      data-asset-group={group.key}
                      className="col-span-full flex items-center gap-2 border-b border-[var(--color-border)] pb-1.5 pt-1 text-xs font-semibold text-[var(--color-text-secondary)]"
                    >
                      {group.badge && (
                        <span className="rounded bg-[var(--color-bg-secondary)] px-1.5 py-0.5 text-[10px] font-bold text-[var(--color-text-tertiary)]">
                          {group.badge}
                        </span>
                      )}
                      <span>{group.label}</span>
                      {group.countLabel && (
                        <span className="font-normal text-[var(--color-text-tertiary)]">
                          {group.countLabel}
                        </span>
                      )}
                    </div>
                  )}
                  {group.items.map((item) => {
                    const Icon = mediaIcon(item.mediaKind);
                    return (
                      <button
                        key={`${item.kind}:${item.id}`}
                        type="button"
                        data-creator-module="asset-card"
                        data-creator-module-id={item.id}
                        onClick={() => selectItem(item)}
                        className={`group overflow-hidden rounded-xl border bg-[var(--color-bg-card)] text-left transition ${
                          selected?.id === item.id
                            ? "border-[var(--color-accent)] shadow-[0_0_0_1px_var(--color-accent)]"
                            : "border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:shadow-sm"
                        }`}
                      >
                        <div className="relative flex h-32 items-center justify-center overflow-hidden bg-[var(--color-bg-secondary)]">
                          <AssetMediaPreview
                            name={item.name}
                            mediaType={item.mediaKind}
                            previewUrl={item.previewUrl}
                            state={
                              item.previewUrl
                                ? "ready"
                                : item.kind === "visual"
                                ? "planned"
                                : "unavailable"
                            }
                            mediaClassName="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.02]"
                            placeholderClassName="flex flex-col items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]"
                          />
                          {!item.previewUrl && (
                            <Icon className="pointer-events-none absolute h-6 w-6 -translate-y-3 text-[var(--color-text-tertiary)]" />
                          )}
                          <span className="absolute left-2 top-2 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-bold text-white">
                            {kindLabel(item)}
                          </span>
                          <div className="absolute right-2 top-2 flex flex-col items-end gap-1">
                            {item.variantState && (
                              <span
                                className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                                  item.variantState === "active"
                                    ? "bg-emerald-500 text-white"
                                    : item.variantState === "history"
                                    ? "bg-black/60 text-white"
                                    : "bg-amber-500 text-white"
                                }`}
                              >
                                {item.variantState === "active"
                                  ? "使用中"
                                  : item.variantState === "history"
                                  ? "历史"
                                  : "未选择"}
                              </span>
                            )}
                            {item.stale && (
                              <span className="rounded bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold text-white">
                                过期
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="p-3">
                          <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
                            {item.kind === "visual" && filter === "visual"
                              ? item.cardName || item.name
                              : item.name}
                          </h3>
                          <p className="mt-1 line-clamp-2 min-h-8 text-[11px] leading-4 text-[var(--color-text-secondary)]">
                            {item.description}
                          </p>
                          {item.kind === "artifact" && item.variantLabel && (
                            <p className="mt-2 truncate text-[10px] font-medium text-[var(--color-text-secondary)]">
                              {item.variantLabel}
                            </p>
                          )}
                          <p className="mt-2 truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">
                            {item.id}
                          </p>
                        </div>
                      </button>
                    );
                  })}
                </Fragment>
              ))}
            </div>
          ) : (
            <div className="flex h-full min-h-64 flex-col items-center justify-center text-center text-[var(--color-text-tertiary)]">
              <Paperclip className="mb-3 h-8 w-8 opacity-50" />
              <p className="text-sm font-medium text-[var(--color-text-secondary)]">
                当前筛选下没有素材
              </p>
              <p className="mt-1 text-xs">
                上传来源素材，或让 Agent 根据时间轴生成画面与成片。
              </p>
            </div>
          )}
        </section>

        <aside
          data-onboarding-id="assets-detail"
          className="min-h-0 overflow-y-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)]"
        >
          {selected ? (
            <div>
              <div
                data-review-media-anchor={versionFromUrl ?? undefined}
                className="flex aspect-video items-center justify-center overflow-hidden bg-black"
              >
                {selected.mediaKind === "audio" && selected.previewUrl ? (
                  <audio
                    src={selected.previewUrl}
                    controls
                    className="w-[86%]"
                  />
                ) : (
                  <AssetMediaPreview
                    name={selected.name}
                    mediaType={selected.mediaKind}
                    previewUrl={selected.previewUrl}
                    state={
                      selected.previewUrl
                        ? "ready"
                        : selected.kind === "visual"
                        ? "planned"
                        : "unavailable"
                    }
                    controls
                    mediaClassName="h-full w-full object-contain"
                    placeholderClassName="text-xs text-white/55"
                  />
                )}
              </div>
              <div className="space-y-4 p-4">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-[var(--color-accent-soft)] px-2 py-0.5 text-[10px] font-semibold text-[var(--color-accent)]">
                      {kindLabel(selected)}
                    </span>
                    {selected.stale && (
                      <span className="text-[10px] font-semibold text-amber-600">
                        已过期
                      </span>
                    )}
                  </div>
                  <h3 className="mt-2 text-base font-semibold text-[var(--color-text-primary)]">
                    {selected.name}
                  </h3>
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-secondary)]">
                    {selected.description}
                  </p>
                </div>
                <dl className="space-y-2 text-xs">
                  {[
                    ["引用", selected.ref],
                    ["媒体", selected.mediaType || selected.mediaKind],
                    [
                      "时长",
                      selected.durationSeconds == null
                        ? "—"
                        : `${selected.durationSeconds.toFixed(2)}s`,
                    ],
                    ["Owner", selected.ownerRef || "—"],
                    [
                      "创建时间",
                      selected.createdAt
                        ? new Date(selected.createdAt).toLocaleString("zh-CN")
                        : "—",
                    ],
                    ["Checksum", selected.checksum || "—"],
                  ].map(([label, value]) => (
                    <div
                      key={label}
                      className="grid grid-cols-[64px_minmax(0,1fr)] gap-2"
                    >
                      <dt className="text-[var(--color-text-tertiary)]">
                        {label}
                      </dt>
                      <dd className="break-all font-mono text-[11px] text-[var(--color-text-secondary)]">
                        {value}
                      </dd>
                    </div>
                  ))}
                </dl>
                {selected.kind === "source" &&
                  selected.mediaKind === "document" && (
                    <DocumentUnderstanding
                      projectId={id}
                      assetId={
                        (selected.raw as SourceAssetVersionDocument)
                          .logical_asset_id
                      }
                      intelligenceVersionId={
                        // Bind the panel to this exact source version; the
                        // versionless endpoint would show the current
                        // version's understanding on older cards.
                        Object.values(
                          project.assets.intelligence_versions_by_id,
                        ).find(
                          (record) =>
                            record.source_asset_version_id === selected.id,
                        )?.intelligence_version_id ?? null
                      }
                    />
                  )}
                {(() => {
                  if (!project) return null;
                  const resolved = selected.provenanceRefs
                    .map((ref) => resolveProvenanceRef(project, ref))
                    .filter(Boolean) as NonNullable<
                    ReturnType<typeof resolveProvenanceRef>
                  >[];
                  if (!resolved.length) return null;
                  return (
                    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-3">
                      <div className="mb-2 text-[11px] font-semibold text-[var(--color-text-secondary)]">
                        引用参考图
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {resolved.map((entry) => {
                          const target = allItems.find(
                            (item) => item.ref === entry.ref,
                          );
                          return (
                            <button
                              key={entry.ref}
                              type="button"
                              title={entry.name}
                              onClick={() => target && selectItem(target)}
                              className="group flex w-24 flex-col gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-1 text-left transition hover:border-[var(--color-accent)]"
                            >
                              <div className="aspect-video w-full overflow-hidden rounded bg-black/20">
                                {entry.kind === "video" ? (
                                  <video
                                    src={entry.url}
                                    className="h-full w-full object-cover"
                                    muted
                                  />
                                ) : (
                                  <img
                                    src={entry.url}
                                    alt={entry.name}
                                    className="h-full w-full object-cover"
                                    loading="lazy"
                                  />
                                )}
                              </div>
                              <span className="truncate text-[10px] text-[var(--color-text-tertiary)] group-hover:text-[var(--color-accent)]">
                                {entry.name}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  );
                })()}
                {Object.keys(selected.metadata).length > 0 && (
                  <details className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-3 text-xs">
                    <summary className="cursor-pointer font-semibold text-[var(--color-text-secondary)]">
                      元数据
                    </summary>
                    <dl className="mt-2 space-y-1.5">
                      {Object.entries(selected.metadata).map(([key, value]) => (
                        <div
                          key={key}
                          className="grid grid-cols-[100px_minmax(0,1fr)] gap-2"
                        >
                          <dt className="truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">
                            {key}
                          </dt>
                          <dd className="break-all text-[11px] text-[var(--color-text-secondary)]">
                            {displayValue(value)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                )}
                {(() => {
                  if (!project) return null;
                  const promptTarget = generationPromptTarget(
                    project,
                    selected,
                  );
                  if (!promptTarget) return null;
                  return (
                    <GenerationPromptEditor
                      key={promptTarget.pointer}
                      target={promptTarget}
                      saving={patching}
                      onSave={async (target, next) => {
                        try {
                          await patchProject(id, [
                            {
                              op: "replace",
                              path: target.pointer,
                              before: target.value,
                              value: next,
                            },
                          ]);
                          message.success("生成 Prompt 已保存");
                        } catch (error) {
                          message.error(
                            `保存失败：${(error as Error).message}`,
                          );
                        }
                      }}
                    />
                  );
                })()}
                {selected.mediaKind === "video" &&
                  selected.ownerRef?.startsWith("element:") && (
                    <Button
                      block
                      icon={<Clapperboard className="h-3.5 w-3.5" />}
                      onClick={() =>
                        navigate(
                          `/project/${id}/plan/element/${encodeURIComponent(
                            selected.ownerRef!.slice("element:".length),
                          )}`,
                        )
                      }
                    >
                      进入该视频的 R2V 工作台
                    </Button>
                  )}
                <div className="flex gap-2">
                  {selected.previewUrl && (
                    <Button
                      icon={<Download className="h-3.5 w-3.5" />}
                      onClick={() => {
                        const filename = downloadName(
                          selected.name,
                          selected.mediaType,
                        );
                        fetch(selected.previewUrl!)
                          .then((res) => {
                            if (!res.ok)
                              throw new Error(`下载失败（${res.status}）`);
                            return res.blob();
                          })
                          .then((blob) => {
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            a.href = url;
                            a.download = filename;
                            a.click();
                            URL.revokeObjectURL(url);
                          })
                          .catch((error) => {
                            message.error(
                              error instanceof Error
                                ? error.message
                                : "下载失败",
                            );
                          });
                      }}
                    >
                      下载
                    </Button>
                  )}
                  <Button className="flex-1" onClick={() => selectItem(null)}>
                    关闭
                  </Button>
                </div>
              </div>
            </div>
          ) : (
            <div className="flex h-full min-h-64 flex-col items-center justify-center px-8 text-center">
              <Box className="mb-3 h-8 w-8 text-[var(--color-text-tertiary)] opacity-50" />
              <p className="text-sm font-medium text-[var(--color-text-secondary)]">
                选择一项查看详情
              </p>
              <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
                这里显示 Project 内实际保存的版本、引用与产物状态。
              </p>
            </div>
          )}
        </aside>
      </main>

      <Modal
        title="添加来源素材"
        open={addOpen}
        confirmLoading={uploading}
        okText="提交入库"
        cancelText="取消"
        onOk={() => void addValue()}
        onCancel={() => setAddOpen(false)}
      >
        <Tabs
          activeKey={inputKind}
          onChange={(key) => setInputKind(key as "url" | "text")}
          items={[
            { key: "url", label: "链接" },
            { key: "text", label: "文本" },
          ]}
        />
        <div className="space-y-3">
          <Input
            value={inputName}
            onChange={(event) => setInputName(event.target.value)}
            placeholder="素材名称"
          />
          <Input.TextArea
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            autoSize={{ minRows: inputKind === "url" ? 2 : 6, maxRows: 10 }}
            placeholder={
              inputKind === "url" ? "https://…" : "粘贴脚本、要求或其他文本素材"
            }
          />
        </div>
      </Modal>
    </div>
  );
}
