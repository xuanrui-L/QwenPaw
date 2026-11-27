# -*- coding: utf-8 -*-
"""An explicit video selection accepts existing bytes for current inputs.

Keep generation provenance intact. The slot records the author's acceptance,
which expires when that element's inputs or selected storyboard change.
"""

from __future__ import annotations

import hashlib
import json

from .models import ArtifactVersion, Project, TimelineElement


def video_selection_fingerprint(
    element: TimelineElement,
    storyboard_version_id: str | None,
    *,
    legacy_schema_version: int | None = None,
) -> str:
    creation = element.creation.model_dump(mode="json")
    if legacy_schema_version is not None:
        # Historical receipts omitted fields that did not exist yet.
        # Only omit defaults, so an authored change still expires a receipt.
        introduced = {"generation_plan": None, "predecessor_element_id": None}
        if legacy_schema_version <= 10:
            introduced.update(
                force_storyboard=False,
                transition_intent=None,
                storyboard_intent=None,
                last_frame_version_id=None,
            )
        if legacy_schema_version <= 9:
            introduced["generate_audio"] = True
        for field, default in introduced.items():
            if creation.get(field) == default:
                creation.pop(field, None)
    payload = {
        "creation": creation,
        "duration_tick": element.span.duration_tick,
        "storyboard_version_id": storyboard_version_id,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
    )


def accepted_video_selection_is_current(
    project: Project,
    artifact: ArtifactVersion,
) -> bool | None:
    """None means no explicit acceptance; False means its inputs changed."""

    slot = project.assets.artifact_slots_by_id.get(artifact.slot_id)
    if (
        slot is None
        or slot.kind != "element_video"
        or slot.selected_version_id != artifact.version_id
    ):
        return None
    acceptance = slot.metadata.get("selectionAcceptance")
    if (
        not isinstance(acceptance, dict)
        or acceptance.get("versionId") != artifact.version_id
    ):
        return None
    element_id = slot.owner_ref.removeprefix("element:")
    element = next(
        (
            timeline.elements_by_id[element_id]
            for timeline in project.timelines.items.values()
            if element_id in timeline.elements_by_id
        ),
        None,
    )
    if element is None:
        return False
    storyboard = project.assets.artifact_slots_by_id.get(
        f"element:{element_id}:storyboard",
    )
    version_id = storyboard.selected_version_id if storyboard else None
    fingerprint = acceptance.get("inputFingerprint")
    if fingerprint == video_selection_fingerprint(element, version_id):
        return True
    return any(
        fingerprint
        == video_selection_fingerprint(
            element,
            version_id,
            legacy_schema_version=schema_version,
        )
        for schema_version in (11, 10, 9)
    )
