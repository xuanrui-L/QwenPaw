# -*- coding: utf-8 -*-
"""Frozen pre-removal documents keep their ETag and survive a new commit."""

import json
from pathlib import Path

import pytest

from services.project_files.commit import ProjectCommitBoundary
from services.project_files.recovery import ProjectCommitRecoveryCoordinator
from services.project_files.serialization import (
    load_project_document,
    project_document_etag,
)
from services.project_files.store import ProjectStore

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures" / "legacy_r2v_shots_v9.json"
# Encoded by the c9b6d20d Project model before Shot removal.
LEGACY_ETAG = (
    "sha256:7a0eb24b6d0d1a939856921e6096869cac35a6e6499b90a0dcd4263cee753ef4"
)
PID = "legacy-r2v-contract"


def test_historical_etag_uses_old_field_order_without_restoring_shots():
    document = json.loads(FIXTURE.read_text())
    project = load_project_document(document)
    assert project_document_etag(document, project=project) == LEGACY_ETAG
    creation = (
        project.timelines.items["timeline:main"]
        .elements_by_id["clip"]
        .creation
    )
    assert "shots" not in creation.model_dump()
    assert creation.narrative == "Paper boat drifts."
    # JSON object key ordering must not change the persisted identity.
    reordered = json.loads(json.dumps(document, sort_keys=False))
    reordered["timelines"]["items"]["timeline:main"]["elements_by_id"]["clip"][
        "creation"
    ] = dict(
        reversed(
            list(
                document["timelines"]["items"]["timeline:main"][
                    "elements_by_id"
                ]["clip"]["creation"].items(),
            ),
        ),
    )
    assert project_document_etag(reordered) == LEGACY_ETAG


def test_first_legacy_edit_recovers_after_publication_crash(
    tmp_path,
    monkeypatch,
):
    store = ProjectStore(tmp_path.resolve())
    store.create(load_project_document(json.loads(FIXTURE.read_text())))
    store.project_path(PID).write_bytes(FIXTURE.read_bytes())
    before = store.read(PID)
    assert before.etag == LEGACY_ETAG
    candidate = before.project.model_dump(mode="json")
    candidate["description"] = "Edited after upgrading"
    boundary = ProjectCommitBoundary(store)

    def crash(**_kwargs):
        raise RuntimeError("simulated publication crash")

    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_record_review", crash)
        with pytest.raises(RuntimeError, match="publication crash"):
            boundary.commit(
                base=before,
                candidate=candidate,
                origin="frontend_edit",
                transaction_id="legacy-first-edit",
            )
    recovery = ProjectCommitRecoveryCoordinator(store).recover_project(PID)
    assert recovery.ok
    after = store.read(PID)
    assert after.project.description == "Edited after upgrading"
    assert (
        "shots"
        not in json.loads(store.project_path(PID).read_text())["timelines"][
            "items"
        ]["timeline:main"]["elements_by_id"]["clip"]["creation"]
    )
