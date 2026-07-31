# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from services.project_files.migrations import (
    PROJECT_MIGRATIONS,
    migrate_project_document,
)
from services.project_files.models import Project
from services.project_files.serialization import (
    CanonicalJsonError,
    load_project_json,
)


def _raw_project() -> dict:
    return Project.new(project_id="project-1", name="Project").model_dump(
        mode="json",
    )


def _v1_project() -> dict:
    raw = _raw_project()
    raw["schema_version"] = 1
    return raw


def test_registered_migration_runs_before_strict_project_validation() -> None:
    raw = _v1_project()
    raw["schema_version"] = 0
    raw["legacy_name"] = raw.pop("name")

    def migrate_v0(document: dict) -> dict:
        document["schema_version"] = 1
        document["name"] = document.pop("legacy_name")
        return document

    def migrate_v1(document: dict) -> dict:
        document["schema_version"] = 2
        return document

    PROJECT_MIGRATIONS[0] = migrate_v0
    PROJECT_MIGRATIONS[1] = migrate_v1
    try:
        project = load_project_json(json.dumps(raw))
    finally:
        PROJECT_MIGRATIONS.pop(0, None)
        PROJECT_MIGRATIONS.pop(1, None)

    assert project.schema_version == 3
    assert project.name == "Project"
    assert project.timelines.order == ["timeline:main"]


def test_unregistered_or_future_schema_fails_closed() -> None:
    raw = _v1_project()
    raw["schema_version"] = 0
    with pytest.raises(CanonicalJsonError):
        load_project_json(json.dumps(raw))

    raw["schema_version"] = 4
    with pytest.raises(CanonicalJsonError):
        load_project_json(json.dumps(raw))


def test_migration_cannot_change_project_identity() -> None:
    raw = _v1_project()
    raw["schema_version"] = 0

    def invalid(document: dict) -> dict:
        document["schema_version"] = 1
        document["project_id"] = "different"
        return document

    PROJECT_MIGRATIONS[0] = invalid
    try:
        with pytest.raises(CanonicalJsonError):
            load_project_json(json.dumps(raw))
    finally:
        PROJECT_MIGRATIONS.pop(0, None)


def test_unregistered_v1_requires_explicit_import() -> None:
    raw = _v1_project()
    with pytest.raises(CanonicalJsonError) as caught:
        load_project_json(json.dumps(raw))
    assert "no Project migration is registered" in str(caught.value.__cause__)


def test_v2_variant_selections_and_bindings_migrate_deterministically() -> (
    None
):
    raw = _raw_project()
    raw["schema_version"] = 2
    raw["visual"]["entities"] = {
        "order": ["char:hero", "char:rival"],
        "items": {
            "char:hero": {
                "entity_id": "char:hero",
                "kind": "character",
                "name": "Hero",
                "description": "",
                "continuity": "",
                "variants": {
                    "order": ["var:peak", "var:fallen", "var:ambiguous"],
                    "items": {
                        "var:peak": {
                            "variant_id": "var:peak",
                            "requirements": "",
                            "prompt": "",
                            "reference_asset_version_ids": [],
                            "reference_artifact_version_ids": [],
                            "generated_artifact_version_ids": [
                                "artifact:peak-1",
                            ],
                        },
                        "var:fallen": {
                            "variant_id": "var:fallen",
                            "requirements": "",
                            "prompt": "",
                            "reference_asset_version_ids": [],
                            "reference_artifact_version_ids": [],
                            "generated_artifact_version_ids": [
                                "artifact:fallen-1",
                                "artifact:fallen-2",
                            ],
                        },
                        "var:ambiguous": {
                            "variant_id": "var:ambiguous",
                            "requirements": "",
                            "prompt": "",
                            "reference_asset_version_ids": [],
                            "reference_artifact_version_ids": [],
                            "generated_artifact_version_ids": [
                                "artifact:mislabeled",
                            ],
                        },
                    },
                },
                "selected_artifact_version_id": "artifact:fallen-1",
            },
            "char:rival": {
                "entity_id": "char:rival",
                "kind": "character",
                "name": "Rival",
                "description": "",
                "continuity": "",
                "variants": {
                    "order": ["var:fallen", "var:other"],
                    "items": {
                        "var:fallen": {
                            "variant_id": "var:fallen",
                            "requirements": "",
                            "prompt": "",
                            "reference_asset_version_ids": [],
                            "reference_artifact_version_ids": [],
                            "generated_artifact_version_ids": [],
                        },
                        "var:other": {
                            "variant_id": "var:other",
                            "requirements": "",
                            "prompt": "",
                            "reference_asset_version_ids": [],
                            "reference_artifact_version_ids": [],
                            "generated_artifact_version_ids": [],
                        },
                    },
                },
                "selected_artifact_version_id": None,
            },
        },
    }
    raw["assets"]["artifact_versions_by_id"] = {
        "artifact:peak-1": {
            "owner_ref": "asset:char:hero",
            "metadata": {"variantId": "var:peak"},
        },
        "artifact:fallen-1": {
            "owner_ref": "asset:char:hero",
            "metadata": {"variantId": "var:fallen"},
        },
        "artifact:fallen-2": {
            "owner_ref": "asset:char:hero",
            "metadata": {"variantId": "var:fallen"},
        },
        "artifact:mislabeled": {
            "owner_ref": "asset:char:hero",
            "metadata": {"variantId": "var:peak"},
        },
    }
    raw["timelines"]["items"]["timeline:main"]["elements_by_id"] = {
        "ep01": {
            "creation": {
                "type": "r2v",
                "character_refs": ["char:hero"],
                "scene_ref": None,
                "prop_refs": [],
                "storyboard_reference_version_ids": [
                    "artifact:fallen-1",
                ],
                "video_reference_version_ids": [],
            },
        },
        "ep02": {
            "creation": {
                "type": "r2v",
                "character_refs": ["char:rival"],
                "scene_ref": None,
                "prop_refs": [],
                "storyboard_reference_version_ids": [
                    "artifact:fallen-1",
                ],
                "video_reference_version_ids": [],
            },
        },
    }

    migrated = migrate_project_document(raw)

    hero = migrated["visual"]["entities"]["items"]["char:hero"]
    assert (
        hero["variants"]["items"]["var:peak"]["selected_artifact_version_id"]
        == "artifact:peak-1"
    )
    assert (
        hero["variants"]["items"]["var:fallen"]["selected_artifact_version_id"]
        == "artifact:fallen-1"
    )
    assert (
        hero["variants"]["items"]["var:ambiguous"][
            "selected_artifact_version_id"
        ]
        is None
    )
    assert hero["selected_artifact_version_id"] is None
    creation = migrated["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["ep01"]["creation"]
    assert creation["visual_variant_refs"] == {
        "char:hero": "var:fallen",
    }
    rival_creation = migrated["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["ep02"]["creation"]
    assert rival_creation["visual_variant_refs"] == {}
