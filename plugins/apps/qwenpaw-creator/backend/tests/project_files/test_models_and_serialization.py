# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Project model serialization and normalization of incoming edits."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from services.project_files import (
    CanonicalJsonError,
    Project,
    canonical_json_bytes,
    load_project_document,
    load_project_json,
    project_document_etag,
    project_etag,
    project_file_bytes,
)
from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectToolError,
    AgentProjectTools,
)
from services.project_files.candidate_normalization import (
    normalize_project_candidate,
)
from services.project_files.models import (
    ElementLocation,
    EntityCollection,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
    VisualCastLineup,
    VisualEntity,
    VisualVariant,
    is_pet_content_type,
    is_subtitle_overlay,
)
from services.project_files.store import ProjectStore

pytestmark = pytest.mark.unit


def _edit_project() -> Project:
    raw = Project.new(
        project_id="project-edit",
        name="Edit Project",
        scenario="video_edit",
        now=datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc),
    ).model_dump(mode="json")
    raw["assets"] = {
        "files_by_id": {
            "file-source": {
                "file_id": "file-source",
                "kind": "source_original",
                "relative_uri": "assets/sources/source-1/version-1/original.mp4",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "media_type": "video/mp4",
                "created_at": "2026-07-15T08:00:00Z",
            },
        },
        "source_versions_by_id": {
            "source-version-1": {
                "version_id": "source-version-1",
                "logical_asset_id": "logical-source-1",
                "name": "source.mp4",
                "file_id": "file-source",
                "checksum": "a" * 64,
                "media_kind": "video",
                "media_type": "video/mp4",
                "duration_seconds": 10,
                "created_at": "2026-07-15T08:00:00Z",
            },
        },
    }
    raw["sources"] = {
        "sources": {
            "items": {
                "source-1": {
                    "source_id": "source-1",
                    "display_name": "Source",
                    "logical_asset_id": "logical-source-1",
                    "selected_asset_version_id": "source-version-1",
                },
            },
            "order": ["source-1"],
        },
    }
    raw["timelines"]["items"]["timeline:main"]["elements_by_id"] = {
        "element-1": {
            "element_id": "element-1",
            "label": "Edit Element",
            "enabled": True,
            "span": {"start_tick": 0, "duration_tick": 3000},
            "location": {},
            "z_index": 0,
            "creation": {
                "type": "edit",
                "intent": "选择素材高光",
                "reason": "素材理解确认该范围有关键动作",
                "original_sound": "preserve",
                "source_intelligence_version_id": None,
            },
            "outputs": {},
            "render_source": {
                "type": "source_asset_version",
                "version_id": "source-version-1",
                "source_in_tick": 0,
                "source_out_tick": 3000,
                "playback_rate": 1,
                "loop": False,
            },
            "provenance_refs": [],
        },
    }
    return Project.model_validate(raw)


def test_project_new_has_complete_valid_defaults_and_utc_time():
    project = Project.new(
        project_id="project-001",
        name="Project",
        now=datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc),
    )

    assert project.schema_version == 10
    assert project.generation == 0
    assert project.created_at.tzinfo == timezone.utc
    assert project.timelines.order == ["timeline:main"]
    assert project.timelines.items["timeline:main"].elements_by_id == {}
    assert project.assets.files_by_id == {}


def _variant_project() -> Project:
    project = Project.new(project_id="project-variants", name="Variants")
    project.visual.entities = EntityCollection(
        items={
            "char:hero": VisualEntity(
                entity_id="char:hero",
                kind="character",
                name="Hero",
                required_variant_ids=["variant:peak", "variant:fallen"],
                variants=EntityCollection(
                    items={
                        "variant:peak": VisualVariant(
                            variant_id="variant:peak",
                        ),
                        "variant:fallen": VisualVariant(
                            variant_id="variant:fallen",
                        ),
                    },
                    order=["variant:peak", "variant:fallen"],
                ),
            ),
        },
        order=["char:hero"],
    )
    project.timelines.items["timeline:main"].elements_by_id[
        "element:hero"
    ] = TimelineElement(
        element_id="element:hero",
        span=TimelineSpan(start_tick=0, duration_tick=1_000),
        location=ElementLocation(),
        creation=R2VCreation(
            character_refs=["char:hero"],
            visual_variant_refs={"char:hero": "variant:fallen"},
        ),
    )
    return Project.model_validate(project.model_dump(mode="json"))


def test_r2v_variant_binding_must_target_a_referenced_entity_and_variant():
    project = _variant_project()
    raw = project.model_dump(mode="json")
    creation = raw["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "element:hero"
    ]["creation"]

    creation["visual_variant_refs"] = {
        "char:hero": "variant:missing",
    }
    with pytest.raises(
        ValidationError,
        match="element element:hero: .*missing variant variant:missing",
    ):
        Project.model_validate(raw)

    creation["visual_variant_refs"] = {
        "char:other": "variant:fallen",
    }
    with pytest.raises(
        ValidationError,
        match=(
            "element element:hero: .*unreferenced entity char:other; "
            "add it to this creation's character_refs"
        ),
    ):
        Project.model_validate(raw)


def test_visual_variants_must_be_declared_in_required_variant_ids():
    with pytest.raises(
        ValidationError,
        match="must be declared in required_variant_ids",
    ):
        VisualEntity(
            entity_id="char:hero",
            kind="character",
            name="Hero",
            required_variant_ids=[],
            variants=EntityCollection(
                items={
                    "variant:peak": VisualVariant(variant_id="variant:peak"),
                },
                order=["variant:peak"],
            ),
        )


def test_canonical_serialization_is_stable_human_readable_and_round_trips():
    project = _edit_project()
    first = project_file_bytes(project)
    second = project_file_bytes(
        Project.model_validate(project.model_dump(mode="json")),
    )

    assert first == second
    assert first.endswith(b"\n")
    assert not first.startswith(b"\xef\xbb\xbf")
    assert first.index(b'"schema_version"') < first.index(b'"project_id"')
    assert load_project_json(first) == project
    assert project_etag(load_project_json(first)) == project_etag(project)


def test_legacy_document_etag_survives_in_memory_schema_migration():
    project = Project.new(
        project_id="project-legacy",
        name="Legacy",
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )
    project.visual.entities = EntityCollection(
        items={
            "char:hero": VisualEntity(
                entity_id="char:hero",
                kind="character",
                name="Hero",
                required_variant_ids=["variant:peak"],
                variants=EntityCollection(
                    items={
                        "variant:peak": VisualVariant(
                            variant_id="variant:peak",
                        ),
                    },
                    order=["variant:peak"],
                ),
            ),
        },
        order=["char:hero"],
    )
    raw = Project.model_validate(project.model_dump(mode="json")).model_dump(
        mode="json",
    )
    raw["schema_version"] = 3
    # A v3 document predates the interactive presentation field entirely: it is
    # one of the fields a migration adds in memory, so the source document the
    # historical runtime hashed cannot have carried it.
    raw.pop("interactive_presentation", None)
    for added in ("name_source", "narrative_edges"):
        raw.pop(added, None)
    del raw["visual"]["entities"]["items"]["char:hero"]["required_variant_ids"]
    # Legacy documents predate the character voice field entirely.
    del raw["visual"]["entities"]["items"]["char:hero"]["voice"]
    # Production-stage controls also did not exist in the persisted schema.
    del raw["settings"]["production_stage"]
    del raw["settings"]["script_approval_fingerprint"]
    # Neither did the project-level segment-continuity switch.
    del raw["settings"]["video_continuity"]

    migrated = load_project_document(raw)

    assert migrated.schema_version == 10
    assert migrated.settings.production_stage == "media"
    assert migrated.settings.script_approval_fingerprint is None
    assert migrated.visual.entities.items[
        "char:hero"
    ].required_variant_ids == ["variant:peak"]
    assert project_document_etag(raw, project=migrated) == (
        # Pinned against the current schema dump: bump when Project gains
        # fields, the mechanism under test is that migration-added fields
        # stay out of the source-document hash.
        "sha256:d867595d1540363575bb637229cc1881899de8e4856de6571514e429b37e385d"
    )
    assert project_document_etag(raw, project=migrated) != project_etag(
        migrated,
    )


def test_dynamic_map_keys_are_sorted_but_business_order_is_preserved():
    value = {"z": 1, "a": 2, "order": ["z", "a"]}
    assert canonical_json_bytes(value) == b'{"a":2,"order":["z","a"],"z":1}'


def test_parser_rejects_duplicate_keys_non_object_root_and_non_finite_numbers():
    with pytest.raises(CanonicalJsonError, match="duplicate"):
        load_project_json('{"schema_version":1,"schema_version":1}')
    with pytest.raises(CanonicalJsonError, match="root"):
        load_project_json("[]")
    with pytest.raises(CanonicalJsonError, match="invalid JSON number"):
        load_project_json('{"value": NaN}')


@pytest.mark.parametrize(
    "relative_uri",
    ["assets", "../outside.bin", "/assets/file.bin", r"assets\\file.bin"],
)
def test_indexed_file_uri_must_name_a_file_below_assets(relative_uri):
    raw = _edit_project().model_dump(mode="json")
    raw["assets"]["files_by_id"]["file-source"]["relative_uri"] = relative_uri

    with pytest.raises(ValidationError, match="relative_uri"):
        Project.model_validate(raw)


def test_project_json_is_plain_json_with_no_runtime_state():
    payload = json.loads(project_file_bytes(_edit_project()))

    assert "runtime" not in payload
    assert "reviews" not in payload


def test_fabricated_artifact_slots_are_rejected():
    """Hand-written slots (unknown kind or empty shell) must not validate.

    Reproduces the 2026-08 incident: the model fabricated video slots via
    jq_project (kind ``r2v_video``, no versions) to claim completion, and
    the real pipeline write-back later collided with them.
    """

    raw = Project.new(project_id="project-1", name="Initial").model_dump(
        mode="json",
    )
    raw["assets"]["artifact_slots_by_id"]["element:el:x:main"] = {
        "slot_id": "element:el:x:main",
        "kind": "r2v_video",
        "owner_ref": "element:el:x",
        "version_ids": [],
        "selected_version_id": None,
        "metadata": {},
    }
    with pytest.raises(ValidationError, match="unknown kind"):
        Project.model_validate(raw)

    # A known kind with no versions is still an empty shell no pipeline
    # ever writes.
    raw["assets"]["artifact_slots_by_id"]["element:el:x:main"][
        "kind"
    ] = "element_video"
    with pytest.raises(ValidationError, match="no artifact"):
        Project.model_validate(raw)


def test_visual_style_anchor_refs_validate_and_reject_self_reference():
    """`visual:<entity>:<variant>` anchors commit before any base art exists;
    only a real target passes and a self-anchor is refused outright."""

    project = _variant_project()
    hero = project.visual.entities.items["char:hero"]
    anchor = "visual:char:hero:variant:peak"
    fallen = hero.variants.items["variant:fallen"]

    fallen.reference_artifact_version_ids = [anchor]
    Project.model_validate(project.model_dump(mode="json"))

    peak = hero.variants.items["variant:peak"]
    peak.reference_artifact_version_ids = [anchor]
    with pytest.raises(ValueError, match="references itself"):
        Project.model_validate(project.model_dump(mode="json"))
    peak.reference_artifact_version_ids = []

    # An anchor at a nonexistent variant degrades to a concrete-id check.
    fallen.reference_artifact_version_ids = ["visual:char:hero:variant:nope"]
    with pytest.raises(ValueError, match="visual artifact reference"):
        Project.model_validate(project.model_dump(mode="json"))


def test_visual_style_anchor_cycles_are_rejected_at_commit():
    """A→B→A gates both scenes forever with empty repair queues, so the
    commit refuses the cycle outright (self-anchors are caught earlier)."""

    project = _variant_project()
    hero = project.visual.entities.items["char:hero"]
    hero.variants.items["variant:peak"].reference_artifact_version_ids = [
        "visual:char:hero:variant:fallen",
    ]
    hero.variants.items["variant:fallen"].reference_artifact_version_ids = [
        "visual:char:hero:variant:peak",
    ]
    with pytest.raises(ValueError, match="form a cycle"):
        Project.model_validate(project.model_dump(mode="json"))


def test_pet_content_type_accepts_both_spellings():
    """The launch form writes ``pet_video``, the composer writes ``pets``.

    Classification must accept both or every UI-created pet project would
    have its OS bubbles counted as subtitles; the styling side keeps its own
    narrower check on purpose, so this is the only place the pair is stated.
    """

    assert is_pet_content_type("pets")
    assert is_pet_content_type("pet_video")
    assert not is_pet_content_type("short_drama")
    assert not is_pet_content_type(None)


@pytest.mark.parametrize(
    ("text", "vibe", "caption_role", "content_type", "expected"),
    [
        # An empty card is a decoration, never a subtitle.
        ("", "chill", "dialogue", "short_drama", False),
        # A declared role wins even inside a pet project: the author asked
        # for a subtitle card, not a bubble.
        ("台词", "chill", "dialogue", "pet_video", True),
        # The pet-OS bubble leaves its role empty and gets a dedicated
        # layout, so bulk subtitle work must not claim it.
        ("本喵要发光", "chill", None, "pet_video", False),
        ("本喵要发光", "chill", None, "pets", False),
        # A migrated interview summary keeps its vibe tag but is a subtitle.
        ("五年增长了十二倍", "summary", None, "pets", True),
        # A blank role stored while caption_role was still a free string is
        # not a declaration, and matches how the styling path reads the role.
        ("本喵要发光", "chill", "", "pets", False),
        ("这句要烧进成片", "chill", "", "short_drama", True),
        # Ordinary dramatic copy with no role is still a subtitle.
        ("那句台词", "chill", None, "short_drama", True),
    ],
)
def test_subtitle_predicate_is_the_single_classification(
    text: str,
    vibe: str,
    caption_role: str | None,
    content_type: str | None,
    expected: bool,
):
    assert (
        is_subtitle_overlay(
            text=text,
            vibe=vibe,
            caption_role=caption_role,
            content_type=content_type,
        )
        is expected
    )


def _character(entity_id: str, **extra) -> VisualEntity:
    return VisualEntity(
        entity_id=entity_id,
        kind="character",
        name=entity_id.split(":")[-1].title(),
        required_variant_ids=list(
            (extra.get("variants") or EntityCollection()).order,
        ),
        **extra,
    )


def _project_with_cast() -> Project:
    project = Project.new(project_id="project-lineup", name="Lineup")
    project.visual.entities = EntityCollection(
        items={
            "char:hero": _character("char:hero"),
            "char:rival": _character("char:rival"),
        },
        order=["char:hero", "char:rival"],
    )
    return project


def test_cast_lineup_roundtrips_with_validation():
    project = _project_with_cast()
    project.visual.cast_lineups = EntityCollection(
        items={
            "lineup:main": VisualCastLineup(
                lineup_id="lineup:main",
                name="主卡司定妆照",
                character_refs=["char:hero", "char:rival"],
                relative_notes="hero:rival ≈ 190:175cm",
            ),
        },
        order=["lineup:main"],
    )

    validated = Project.model_validate(project.model_dump(mode="json"))
    lineup = validated.visual.cast_lineups.items["lineup:main"]
    assert lineup.character_refs == ["char:hero", "char:rival"]


def test_cast_lineup_rejects_missing_characters():
    project = _project_with_cast()
    project.visual.cast_lineups = EntityCollection(
        items={
            "lineup:main": VisualCastLineup(
                lineup_id="lineup:main",
                name="主卡司定妆照",
                character_refs=["char:hero", "char:ghost"],
            ),
        },
        order=["lineup:main"],
    )

    with pytest.raises(
        ValidationError,
        match="missing character char:ghost",
    ):
        Project.model_validate(project.model_dump(mode="json"))


def test_canonical_variant_must_be_one_of_the_entitys_variants():
    variants = EntityCollection(
        items={"var:default": VisualVariant(variant_id="var:default")},
        order=["var:default"],
    )
    entity = _character(
        "char:hero",
        variants=variants,
        canonical_variant_id="var:default",
    )
    assert entity.canonical_variant_id == "var:default"

    with pytest.raises(ValidationError, match="canonical_variant_id"):
        _character(
            "char:hero",
            variants=variants,
            canonical_variant_id="var:ghost",
        )


def test_legacy_projects_without_lineups_still_validate():
    raw = Project.new(project_id="p", name="n").model_dump(mode="json")
    raw["visual"].pop("cast_lineups", None)

    validated = Project.model_validate(raw)
    assert validated.visual.cast_lineups.order == []


def _variant_entities(**variant_extra) -> dict:
    return {
        "items": {
            "char:hero": {
                "entity_id": "char:hero",
                "kind": "character",
                "name": "Hero",
                "required_variant_ids": ["var:default"],
                "variants": {
                    "items": {
                        "var:default": {
                            "variant_id": "var:default",
                            **variant_extra,
                        },
                    },
                    "order": ["var:default"],
                },
            },
        },
        "order": ["char:hero"],
    }


def test_identity_echoes_are_stripped_with_receipts():
    candidate = Project.new(
        project_id="project-1",
        name="Initial",
    ).model_dump(mode="json")
    candidate["visual"]["entities"] = _variant_entities(
        entityId="char:hero",
        variantId="var:default",
    )
    candidate["visual"]["entities"]["items"]["char:hero"][
        "entityId"
    ] = "char:hero"

    receipts = normalize_project_candidate(candidate)

    assert sorted(receipts) == [
        "/visual/entities/items/char:hero/entityId",
        "/visual/entities/items/char:hero/variants/items/var:default/"
        "entityId",
        "/visual/entities/items/char:hero/variants/items/var:default/"
        "variantId",
    ]
    # The candidate now validates cleanly: echoes carried zero information.
    Project.model_validate(candidate)


def test_real_snake_case_identity_fields_are_never_stripped():
    candidate = Project.new(
        project_id="project-1",
        name="Initial",
    ).model_dump(mode="json")
    candidate["visual"]["entities"] = _variant_entities()

    receipts = normalize_project_candidate(candidate)

    assert not receipts
    entity = candidate["visual"]["entities"]["items"]["char:hero"]
    assert entity["entity_id"] == "char:hero"
    assert entity["variants"]["items"]["var:default"]["variant_id"] == (
        "var:default"
    )


def test_jq_project_commits_echoed_variants_and_reports_normalization(
    tmp_path,
):
    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id="project-1", name="Initial"))
    tools = AgentProjectTools(
        store,
        context=AgentProjectToolContext(
            origin="runtime_task",
            caused_by_request_id="request-1",
            caused_by_message_seq=1,
        ),
    )
    tools.invoke("read_project", {"projectId": "project-1"})

    result = tools.invoke(
        "jq_project",
        {
            "projectId": "project-1",
            "program": ".visual.entities = $entities",
            "jsonArgs": {
                "entities": _variant_entities(entityId="char:hero"),
            },
        },
    )

    assert result["normalizedPointers"] == [
        "/visual/entities/items/char:hero/variants/items/var:default/"
        "entityId",
    ]
    variant = result["project"]["visual"]["entities"]["items"]["char:hero"][
        "variants"
    ]["items"]["var:default"]
    assert "entityId" not in variant

    # A mismatched echo is information-bearing and must still fail loudly.
    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            "jq_project",
            {
                "projectId": "project-1",
                "program": ".visual.entities = $entities",
                "jsonArgs": {
                    "entities": _variant_entities(
                        entityId="char:someone-else",
                    ),
                },
            },
        )
    assert caught.value.code == "JQ_PROJECT_SCHEMA_INVALID"
    assert "entityId" in str(caught.value)


def _make_timeline_with_shots_and_overlays(
    shots: list[tuple[str, int, int]],
    overlays: list[tuple[str, int, int]],
) -> dict:
    """Helper to create a timeline dict with shot and overlay elements."""
    elements_by_id = {}
    for shot_id, start, dur in shots:
        elements_by_id[shot_id] = {
            "element_id": shot_id,
            "span": {"start_tick": start, "duration_tick": dur},
            "creation": {"type": "edit"},
            "location": None,
            "z_index": 0,
            "outputs": {},
            "render_source": None,
        }
    for ov_id, start, dur in overlays:
        elements_by_id[ov_id] = {
            "element_id": ov_id,
            "span": {"start_tick": start, "duration_tick": dur},
            "creation": {"type": "overlay", "text": "test"},
            "location": {
                "coordinate_space": "normalized_canvas",
                "x": 0.5,
                "y": 0.5,
                "width": 1.0,
                "height": 0.2,
                "anchor_x": 0.5,
                "anchor_y": 0.5,
            },
            "z_index": 100,
            "outputs": {},
            "render_source": None,
        }
    return {
        "timeline_id": "timeline:main",
        "ticks_per_second": 1000,
        "elements_by_id": elements_by_id,
    }


def test_overlay_local_ticks_corrected_to_global():
    """Overlay with shot-local ticks should be corrected to global ticks."""
    shots = [
        ("shot:01", 0, 8000),
        ("shot:02", 8000, 8000),
        ("shot:03", 16000, 8000),
    ]
    # Overlay meant for shot:03 but with local ticks [4000, 7500)
    # Should be corrected to [20000, 23500)
    overlays = [("subtitle:03b:6", 4000, 3500)]
    timeline = _make_timeline_with_shots_and_overlays(shots, overlays)
    candidate = {
        "timelines": {"items": {"timeline:main": timeline}},
    }

    from services.project_files.candidate_normalization import (
        correct_overlay_local_ticks,
    )

    receipts = correct_overlay_local_ticks(candidate)

    assert len(receipts) == 1
    assert "+16000 via shot:03" in receipts[0]
    elem = candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "subtitle:03b:6"
    ]
    assert elem["span"]["start_tick"] == 20000


def test_overlay_with_correct_global_ticks_not_modified():
    """Overlay with correct global ticks should not be modified."""
    shots = [
        ("shot:01", 0, 8000),
        ("shot:02", 8000, 8000),
    ]
    # Overlay with correct global ticks [9000, 12000) - intersects shot:02
    overlays = [("subtitle:02a", 9000, 3000)]
    timeline = _make_timeline_with_shots_and_overlays(shots, overlays)
    candidate = {
        "timelines": {"items": {"timeline:main": timeline}},
    }

    from services.project_files.candidate_normalization import (
        correct_overlay_local_ticks,
    )

    receipts = correct_overlay_local_ticks(candidate)

    assert len(receipts) == 0
    elem = candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "subtitle:02a"
    ]
    assert elem["span"]["start_tick"] == 9000


def test_ambiguous_overlay_not_corrected():
    """Overlay that matches multiple shots should not be corrected."""
    # Two shots with same duration - overlay could belong to either
    shots = [
        ("shot:01", 0, 5000),
        ("shot:02", 5000, 5000),
    ]
    # Overlay with local ticks [1000, 3000) - could be in either shot
    overlays = [("subtitle:ambiguous", 1000, 2000)]
    timeline = _make_timeline_with_shots_and_overlays(shots, overlays)
    candidate = {
        "timelines": {"items": {"timeline:main": timeline}},
    }

    from services.project_files.candidate_normalization import (
        correct_overlay_local_ticks,
    )

    receipts = correct_overlay_local_ticks(candidate)

    # Should not be corrected because both offsets produce valid intersections
    assert len(receipts) == 0
    elem = candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "subtitle:ambiguous"
    ]
    assert elem["span"]["start_tick"] == 1000


def test_existing_overlay_in_base_not_processed():
    """Overlays that exist in base should not be processed."""
    shots = [
        ("shot:01", 0, 8000),
        ("shot:02", 8000, 8000),
    ]
    # Overlay with local ticks that exists in base
    overlays = [("subtitle:existing", 1000, 2000)]
    timeline = _make_timeline_with_shots_and_overlays(shots, overlays)
    candidate = {
        "timelines": {"items": {"timeline:main": timeline}},
    }
    base = {
        "timelines": {
            "items": {
                "timeline:main": {
                    "elements_by_id": {"subtitle:existing": {}},
                },
            },
        },
    }

    from services.project_files.candidate_normalization import (
        correct_overlay_local_ticks,
    )

    receipts = correct_overlay_local_ticks(candidate, base=base)

    # Should not be corrected because it exists in base
    assert len(receipts) == 0
    elem = candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "subtitle:existing"
    ]
    assert elem["span"]["start_tick"] == 1000


def test_normalize_project_candidate_includes_tick_corrections():
    """normalize_project_candidate should include tick correction receipts."""
    shots = [
        ("shot:01", 0, 8000),
        ("shot:02", 8000, 8000),
    ]
    overlays = [("subtitle:02a", 1000, 2000)]  # local ticks for shot:02
    timeline = _make_timeline_with_shots_and_overlays(shots, overlays)
    candidate = {
        "timelines": {"items": {"timeline:main": timeline}},
        "visual": {"entities": {"items": {}, "order": []}},
    }
    base = {
        "timelines": {"items": {"timeline:main": {"elements_by_id": {}}}},
        "visual": {"entities": {"items": {}, "order": []}},
    }

    receipts = normalize_project_candidate(candidate, base=base)

    tick_receipts = [r for r in receipts if "+8000" in r]
    assert len(tick_receipts) == 1
    elem = candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "subtitle:02a"
    ]
    assert elem["span"]["start_tick"] == 9000
