# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""An old approved version cannot bypass a generation/review in flight."""

import pytest

from api import interactive_bundle_routes as routes
from domain.errors import ConflictError
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import ProjectExecutionStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("state", ["idle", "QUEUED", "RUNNING", "review"])
def test_export_waits_for_inflight_work(tmp_path, monkeypatch, state):
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id="bundle", name="Bundle"))
    if state in {"QUEUED", "RUNNING"}:
        execution = ProjectExecutionStore(services.root)
        execution.create_task(
            TaskRecord(
                project_id="bundle",
                task_id="regenerate",
                kind="interaction_draft",
                request_fingerprint="sha256:test",
            ),
        )
        if state == "RUNNING":
            execution.transition_task(
                "bundle",
                "regenerate",
                expected_status="QUEUED",
                status="RUNNING",
            )
    monkeypatch.setattr(
        routes,
        "active_media_review_slots",
        lambda _pid: frozenset({"selected-video"})
        if state == "review"
        else frozenset(),
    )
    # Assembly/content validity is exercised by the bundle suite. This
    # probe checks the admission boundary even when old content is valid.
    monkeypatch.setattr(
        routes,
        "assemble_interactive_bundle",
        lambda *_args, **_kwargs: b"approved-content",
    )
    if state == "idle":
        assert routes._assemble("bundle", services) == b"approved-content"
    else:
        with pytest.raises(ConflictError, match="generation and media review"):
            routes._assemble("bundle", services)
