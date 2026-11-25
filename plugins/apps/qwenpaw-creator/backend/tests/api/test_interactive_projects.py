# -*- coding: utf-8 -*-
"""Interactive title provenance and active task status in project lists."""

from __future__ import annotations
import pytest
from services.project_files.models import Project
from test_projects import _create_payload


@pytest.mark.parametrize("source", ["user", "auto"])
def test_project_create_persists_title_provenance(
    app,
    api_runtime_root,
    run_scenario,
    source,
):
    payload = _create_payload(
        "title-source-" + source,
        "项目标题",
        nameSource=source,
    )

    async def scenario(client):
        return await client.post("/projects", json=payload)

    response = run_scenario(app, scenario)
    assert response.status_code == 201
    project = Project.model_validate_json(
        (
            api_runtime_root / response.json()["projectId"] / "project.json"
        ).read_text(),
    )
    assert project.name_source == source


def test_project_list_answers_active_task_badge_for_the_whole_page(
    app,
    api_runtime_root,
    run_scenario,
):
    """One response carries the badge: no per-Project /tasks fan-out.

    The home page used to issue GET /tasks once per Project and render nothing
    until the slowest returned. ``hasActiveTask`` is answered by the listing
    itself, so the card state must match the durable Task status exactly.
    """

    from domain.enums import TaskKind, TaskStatus
    from services.runtime_files.execution_models import TaskRecord
    from services.runtime_files.execution_store import ProjectExecutionStore

    async def scenario(client):
        busy = await client.post(
            "/projects",
            json=_create_payload("project-list-badge-busy", "有在跑的任务"),
        )
        quiet = await client.post(
            "/projects",
            json=_create_payload("project-list-badge-quiet", "没有任务"),
        )
        store = ProjectExecutionStore(api_runtime_root)
        store.create_task(
            TaskRecord(
                task_id="task-badge",
                project_id=busy.json()["projectId"],
                kind=TaskKind.IMAGE_GENERATION,
                status=TaskStatus.QUEUED,
                request_fingerprint="sha256:" + "b" * 64,
            ),
        )
        return busy, quiet, await client.get("/projects")

    busy, quiet, listed = run_scenario(app, scenario)
    assert busy.status_code == 201 and quiet.status_code == 201
    flags = {
        item["name"]: item["hasActiveTask"] for item in listed.json()["items"]
    }
    assert flags == {"有在跑的任务": True, "没有任务": False}


def test_saved_branch_cannot_export_before_there_is_a_final_cut(
    app,
    run_scenario,
):
    from services.project_files.json_pointer import MISSING, hash_json_value

    async def scenario(client):
        created = await client.post(
            "/projects",
            json=_create_payload("branching-before-final", "Branching"),
        )
        assert created.status_code == 201
        project_id = created.json()["projectId"]
        project_url = f"/projects/{project_id}/project"
        snapshot_response = await client.get(project_url)
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        project = snapshot["project"]
        assert project["narrative_edges"] == []
        first_id = project["timelines"]["order"][0]
        second_id = "timeline:ending"
        ending = {
            **project["timelines"]["items"][first_id],
            "timeline_id": second_id,
            "title": "结局",
        }
        edge = {
            "edge_id": "edge:ending",
            "source_timeline_id": first_id,
            "target_timeline_id": second_id,
            "label": "进入结局",
            "prompt": "是否进入结局？",
        }
        operations = [
            {
                "op": "add",
                "path": name,
                "value": value,
                "expectedValueHash": hash_json_value(MISSING),
            }
            for name, value in (
                (f"/timelines/items/{second_id}", ending),
                ("/timelines/order/1", second_id),
                ("/narrative_edges/0", edge),
            )
        ]
        patched = await client.patch(
            project_url,
            json={
                "clientCommandId": "branch-before-final",
                "editSessionId": "branch-check",
                "baseGeneration": snapshot["generation"],
                "baseEtag": snapshot["etag"],
                "operations": operations,
            },
        )
        assert patched.status_code == 200, patched.text
        saved = (await client.get(project_url)).json()["project"]
        assert saved["narrative_edges"] == [{**edge, "tone": None}]
        return await client.get(f"/projects/{project_id}/interactive-bundle")

    missing = run_scenario(app, scenario)
    assert missing.status_code == 409, missing.text
