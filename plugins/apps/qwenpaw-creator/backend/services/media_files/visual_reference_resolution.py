# -*- coding: utf-8 -*-
"""Resolve R2V visual references through Element-to-Variant bindings."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from domain.errors import ValidationError
from services.project_files.models import (
    Project,
    R2VCreation,
    TimelineElement,
    VisualEntity,
)


def _owner_entity_id(owner_ref: str | None) -> str | None:
    if owner_ref is None:
        return None
    for prefix in ("visual-entity:", "asset:"):
        if owner_ref.startswith(prefix):
            return owner_ref.removeprefix(prefix)
    return owner_ref


def project_ref_media_kind(project: Project, version_id: str) -> str:
    """``"video"`` only when the reference verifiably resolves to a video.

    Unresolvable ids (e.g. synthetic ``continuity:*`` markers) report
    ``"image"``: the caller's continuity bookkeeping decides their real
    kind, and anything the index does not call a video has always been
    addressed through the image sequence.
    """

    source = project.assets.source_versions_by_id.get(version_id)
    if source is not None:
        media_type = source.media_type
    else:
        artifact = project.assets.artifact_versions_by_id.get(version_id)
        if artifact is None:
            return "image"
        indexed = project.assets.files_by_id.get(artifact.file_id)
        if indexed is None:
            return "image"
        media_type = indexed.media_type
    return "video" if media_type.casefold().startswith("video/") else "image"


def number_references_by_media(
    project: Project,
    version_ids: Iterable[str],
    *,
    force_video: Iterable[str] = (),
) -> list[tuple[str, str, int]]:
    """``(version_id, kind, ordinal)`` in send order, per-type sequences.

    Providers address references in two independent sequences counted per
    media type (``图N`` / ``视频N`` on the Wan family; the canonical
    ``[Image N]`` authoring form renders into the image one), so ordinals
    are tallied separately for images and videos. An all-image sequence
    numbers exactly like the old positional one, so only plans carrying
    video references observe any difference — and there positional
    numbering pointed markers at the wrong or nonexistent reference.
    Single source of truth for the runtime role block, the continuity
    suffix indices, and the preview's ``imageIndex``/``videoIndex``; the
    three must never re-implement this independently or they drift into
    colliding ``[Image N]`` markers inside the paid request.

    ``force_video`` names ids the Asset Index cannot resolve but whose
    submit-time media type is known — the synthetic continuity tail clip
    — so they tally into the video sequence instead of the fallback.
    """

    forced = set(force_video)
    numbered: list[tuple[str, str, int]] = []
    image_ordinal = 0
    video_ordinal = 0
    # Deduplicate first: the submit resolver sends each id once, so the
    # ordinals here must follow the same unique sequence or a repeated
    # reference would inflate both counts.
    for raw_id in dict.fromkeys(str(item) for item in version_ids):
        version_id = str(raw_id)
        if (
            version_id in forced
            or project_ref_media_kind(
                project,
                version_id,
            )
            == "video"
        ):
            video_ordinal += 1
            numbered.append((version_id, "video", video_ordinal))
        else:
            image_ordinal += 1
            numbered.append((version_id, "image", image_ordinal))
    return numbered


def _entity_ids(creation: R2VCreation) -> list[str]:
    """Return the stable semantic order used by prompt reference mappings.

    Preserve the long-lived provider-facing order: characters establish
    identity first, the scene anchors the world, and props follow. This order
    is part of the authored ``[Image N]`` contract; changing it for aesthetic
    preference would silently swap responsibilities in stored video prompts.
    """

    return list(
        dict.fromkeys(
            [
                *creation.character_refs,
                *(
                    [creation.scene_ref]
                    if creation.scene_ref is not None
                    else []
                ),
                *creation.prop_refs,
            ],
        ),
    )


def _artifact_variant_id(
    project: Project,
    entity: VisualEntity,
    version_id: str,
) -> str | None:
    artifact = project.assets.artifact_versions_by_id.get(version_id)
    if artifact is None:
        return None
    generated_matches = [
        variant_id
        for variant_id in entity.variants.order
        if version_id
        in entity.variants.items[variant_id].generated_artifact_version_ids
    ]
    if (
        _owner_entity_id(artifact.owner_ref) != entity.entity_id
        and not generated_matches
    ):
        return None
    metadata_variant = artifact.metadata.get("variantId")
    if isinstance(metadata_variant, str) and (
        metadata_variant in generated_matches
        or (
            _owner_entity_id(artifact.owner_ref) == entity.entity_id
            and metadata_variant in entity.variants.items
        )
    ):
        return metadata_variant
    return generated_matches[0] if len(generated_matches) == 1 else None


def _resolved_variant_id(
    project: Project,
    creation: R2VCreation,
    entity: VisualEntity,
    explicit_version_ids: Iterable[str],
) -> str | None:
    bound = creation.visual_variant_refs.get(entity.entity_id)
    if bound is not None:
        return bound
    if len(entity.variants.order) == 1:
        return entity.variants.order[0]
    candidates = list(
        dict.fromkeys(
            candidate
            for version_id in explicit_version_ids
            if (
                candidate := _artifact_variant_id(
                    project,
                    entity,
                    version_id,
                )
            )
        ),
    )
    return candidates[0] if len(candidates) == 1 else None


def _lineup_anchor_version_ids(
    project: Project,
    creation: R2VCreation,
) -> list[str]:
    """Selected cast-lineup images referenced by this element.

    The lineup is the group anchor for relative consistency (scale
    ratios, shared style baseline, spatial order), so it must lead the
    reference chain ahead of individual identity anchors. A referenced
    lineup without a generated image contributes nothing yet — the
    element keeps working while visual development catches up.
    """

    anchors: list[str] = []
    for ref in creation.cast_lineup_refs:
        lineup = project.visual.cast_lineups.items.get(ref)
        if lineup is None:
            # Mirrors the entity invariant below: a validated Project
            # cannot reach here, so fail loudly for mutated models.
            raise ValidationError(f"R2V 引用的阵容图不存在: {ref}")
        if lineup.selected_artifact_version_id:
            anchors.append(lineup.selected_artifact_version_id)
    return anchors


def resolve_r2v_visual_reference_version_ids(
    project: Project,
    creation: R2VCreation,
    explicit_version_ids: Iterable[str],
) -> tuple[str, ...]:
    """Return exact references with bound Variant selections first.

    Agent-specified references are authoritative: a non-empty explicit
    list is used exactly as written (deduplicated, order preserved) so
    the planning agent — not a default chain — decides which images
    constrain generation and owns the provider's reference budget. Only
    an element with no explicit references falls back to the automatic
    chain: cast-lineup group anchors lead, then per-entity identity
    anchors. A bound character or prop never consumes an ArtifactVersion
    owned by another Variant, so a normal shot cannot pull a different
    costume/identity state. A bound scene may reference several of its own
    Variants at once (a dusk-to-night lighting transition is authored
    intent, not a mixup). Ambiguous legacy Elements are left unchanged
    rather than guessed; the Plan coverage checkpoint exposes those missing
    bindings to the user.
    """

    explicit = list(dict.fromkeys(explicit_version_ids))
    if explicit:
        for version_id in explicit:
            for entity_id in _entity_ids(creation):
                entity = project.visual.entities.items.get(entity_id)
                if entity is None:
                    raise ValidationError(
                        f"R2V 视觉引用实体不存在: {entity_id}",
                    )
                bound = creation.visual_variant_refs.get(entity_id)
                if bound is None:
                    continue
                owned_variant = _artifact_variant_id(
                    project,
                    entity,
                    version_id,
                )
                if (
                    owned_variant is not None
                    and owned_variant != bound
                    # Scenes legitimately carry multiple time-of-day /
                    # weather Variants into a single shot; only identity
                    # carriers (character, prop) are locked to the bound
                    # Variant to prevent cross-state mixups.
                    and entity.kind != "scene"
                ):
                    raise ValidationError(
                        f"显式参考 {version_id} 属于实体 {entity_id} 的 "
                        f"Variant {owned_variant}，与该 Element 绑定的 "
                        f"Variant {bound} 冲突；请改用绑定 Variant 的版本"
                        "或调整 visual_variant_refs",
                    )
        return tuple(explicit)
    selected: list[str] = []
    for entity_id in _entity_ids(creation):
        entity = project.visual.entities.items.get(entity_id)
        if entity is None:
            # A validated Project guarantees this invariant. Keep a controlled
            # failure for callers holding a manually mutated in-memory model;
            # silently skipping would generate without a required identity.
            raise ValidationError(
                f"R2V 视觉引用实体不存在: {entity_id}",
            )
        variant_id = _resolved_variant_id(
            project,
            creation,
            entity,
            (),
        )
        if variant_id is not None:
            version_id = entity.variants.items[
                variant_id
            ].selected_artifact_version_id
        else:
            version_id = (
                entity.selected_artifact_version_id
                if not entity.variants.order
                else None
            )
        if version_id is not None:
            selected.append(version_id)

    lineup_anchors = _lineup_anchor_version_ids(project, creation)
    return tuple(dict.fromkeys([*lineup_anchors, *selected]))


def video_reference_plan(
    project: Project,
    element: TimelineElement,
) -> tuple[str, ...]:
    """Reserve Image 1 for this element's current storyboard selection.

    Older projects may also list that storyboard explicitly among video
    references. A new selection replaces this semantic slot; it must not
    leave an older version in Image 2 and shift every identity reference.
    Only this output slot is excluded. Storyboards from other elements and
    unknown references retain their authored positions and validation.
    """
    from services.media_files.element_adapter import (
        element_output_slot_id,
        selected_element_output,
    )

    creation = element.creation
    if not isinstance(creation, R2VCreation):
        raise ValidationError("视频参考序列仅适用于 R2V Element")
    output = element.outputs.get("storyboard")
    slot_id = (
        output.slot_id
        if output is not None
        else element_output_slot_id(element.element_id, "storyboard")
    )
    slot = project.assets.artifact_slots_by_id.get(slot_id)
    own_versions = set(slot.version_ids if slot is not None else ())
    artifact_versions = project.assets.artifact_versions_by_id
    own_versions.update(
        version_id
        for version_id, artifact in artifact_versions.items()
        if artifact.slot_id == slot_id
    )
    selected = selected_element_output(project, element, "storyboard")
    storyboard_id = selected[1] if selected is not None else None
    from models.video_task_plans import uses_storyboard

    include_storyboard = uses_storyboard(creation)
    if storyboard_id:
        own_versions.add(storyboard_id)
    # Resolve before filtering: an explicitly storyboard-only plan must not
    # accidentally become an empty list that activates automatic references.
    additional = resolve_r2v_visual_reference_version_ids(
        project,
        creation,
        creation.video_reference_version_ids,
    )
    return (
        *([storyboard_id] if storyboard_id and include_storyboard else []),
        *(
            version_id
            for version_id in additional
            if version_id not in own_versions
        ),
    )


def preview_r2v_reference_order(
    project: Project,
    element_id: str,
    *,
    stage: str = "video",
    image_model_name: str = "",
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Preview authored media in the same order used in the request."""
    from models.video_task_plans import uses_storyboard

    from services.media_files.element_adapter import (
        find_timeline_element,
        selected_element_output,
    )

    timeline, element = find_timeline_element(project, element_id)
    creation = element.creation
    if not isinstance(creation, R2VCreation):
        raise ValidationError(
            f"Element creation.type={creation.type} 不使用 [Image N] 参考序列",
        )
    if stage not in {"storyboard", "video"}:
        raise ValidationError("参考图阶段必须为 storyboard 或 video")
    selected_storyboard = selected_element_output(
        project,
        element,
        "storyboard",
    )
    storyboard_id = (
        selected_storyboard[1] if selected_storyboard is not None else None
    )
    dropped: tuple[str, ...] = ()
    if stage == "storyboard":
        version_ids, dropped = storyboard_reference_plan(
            project,
            creation,
            image_model_name=image_model_name,
        )
    else:
        version_ids = video_reference_plan(project, element)
    references: list[dict[str, Any]] = []
    plan = creation.generation_plan if stage == "video" else None
    if plan is not None and plan.predecessor_element_id is not None:
        predecessor = timeline.elements_by_id.get(plan.predecessor_element_id)
        prev_slot = project.assets.artifact_slots_by_id.get(
            f"element:{plan.predecessor_element_id}:main",
        )
        version_id = (
            prev_slot.selected_version_id if prev_slot is not None else None
        )
        extending = plan.method == "video_extend"
        references.append(
            {
                "index": 1,
                "imageIndex": None if extending else 1,
                "videoIndex": 1 if extending else None,
                "versionId": (version_id or "") if extending else "",
                "kind": "continuity",
                "available": bool(version_id),
                "name": (
                    f"{predecessor.label or predecessor.element_id} · 前段视频"
                    if extending and predecessor is not None
                    else f"{plan.predecessor_element_id} · 前段末帧"
                ),
            }
        )
        if extending:
            version_ids = ()
    if (
        stage == "video"
        and storyboard_id is None
        and uses_storyboard(creation)
    ):
        # Video execution requires this element's selected storyboard. Keep
        # its semantic position while authoring, without inventing a version
        # ID or allowing an identity reference to impersonate [Image 1].
        references.append(
            {
                "index": len(references) + 1,
                "imageIndex": len(references) + 1,
                "videoIndex": None,
                "versionId": "",
                "kind": "storyboard",
                "available": False,
                "name": "分镜图（待生成）",
            },
        )
    reserved_images = sum(row["imageIndex"] is not None for row in references)
    wire_ordinals = {
        version_id: (
            media_kind,
            ordinal + reserved_images if media_kind == "image" else ordinal,
        )
        for version_id, media_kind, ordinal in number_references_by_media(
            project,
            version_ids,
        )
    }
    for index, version_id in enumerate(version_ids, start=len(references) + 1):
        source = project.assets.source_versions_by_id.get(version_id)
        artifact = project.assets.artifact_versions_by_id.get(version_id)
        version = source if source is not None else artifact
        if stage == "video" and version_id == storyboard_id:
            kind = "storyboard"
        elif source is not None:
            kind = "source"
        else:
            kind = "artifact"
        available = version is not None
        if stage == "storyboard" and available and project_root is not None:
            # Use the actual submit resolver, one position at a time. A
            # missing middle image remains an unavailable slot, never a
            # filtered list that silently changes later [Image N] values.
            from services.media_files.image_execution import (
                _resolve_version_references,
            )
            from domain.errors import CreatorError

            try:
                _resolve_version_references(
                    project=project,
                    project_root=project_root,
                    version_ids=(version_id,),
                )
            except (CreatorError, KeyError):
                available = False
        wire_kind, wire_ordinal = wire_ordinals.get(
            version_id,
            ("image", None),
        )
        references.append(
            {
                "index": index,
                "imageIndex": (wire_ordinal if wire_kind == "image" else None),
                "videoIndex": (wire_ordinal if wire_kind == "video" else None),
                "versionId": version_id,
                "kind": kind,
                "available": available,
                "name": (
                    version.name
                    if version is not None and version.name
                    else "参考图片"
                ),
            },
        )
    from models.image.base import image_reference_limit
    from models.reference_markers import canonical_marker_indices

    limit = (
        image_reference_limit(image_model_name)
        if stage == "storyboard"
        else None
    )
    indices = (
        canonical_marker_indices(creation.storyboard_prompt)
        if stage == "storyboard"
        else ()
    )
    invalid_indices = sorted(
        {index for index in indices if not 1 <= index <= len(references)},
    )
    return {
        "elementId": element_id,
        "stage": stage,
        "storyboardSelected": storyboard_id is not None,
        "references": references,
        "referenceLimit": limit,
        "budgetDroppedVersionIds": list(dropped),
        "invalidMarkerIndices": invalid_indices,
        "ready": (
            all(item["available"] for item in references)
            and not invalid_indices
            and (
                stage != "storyboard"
                or not references
                or (limit is not None and len(references) <= limit)
            )
        ),
    }


def storyboard_reference_plan(
    project: Project,
    creation: R2VCreation,
    *,
    additional_version_ids: Iterable[str] = (),
    image_model_name: str = "",
    max_reference_images: int | None = None,
    prompt: str | None = None,
    has_explicit_urls: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The same ordered image inputs for editor preview and new submission.

    Authored version lists and canonical markers pin reference identity. Only
    the legacy, unnumbered automatic chain retains its recorded budget trim.
    An oversized authored plan stays intact so admission can reject it.
    """
    from models.image.base import image_reference_limit
    from models.reference_markers import canonical_marker_indices

    explicit = (
        *creation.storyboard_reference_version_ids,
        *additional_version_ids,
    )
    ids = resolve_r2v_visual_reference_version_ids(project, creation, explicit)
    limit = (
        image_reference_limit(image_model_name)
        if max_reference_images is None
        else max_reference_images
    )
    if (
        limit is not None
        and 0 <= limit < len(ids)
        and not explicit
        and not has_explicit_urls
        and not canonical_marker_indices(
            creation.storyboard_prompt if prompt is None else prompt,
        )
    ):
        return ids[:limit], ids[limit:]
    return ids, ()


__all__ = [
    "preview_r2v_reference_order",
    "resolve_r2v_visual_reference_version_ids",
    "storyboard_reference_plan",
    "video_reference_plan",
]
