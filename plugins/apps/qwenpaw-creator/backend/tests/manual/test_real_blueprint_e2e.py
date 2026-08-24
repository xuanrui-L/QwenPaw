# -*- coding: utf-8 -*-
"""Manual real-key acceptance for the project-blueprint slice.

Drives the REAL HTTP surface (create → v9 snapshot → narrative structure
patch → real Qwen synopsis drafting → work graph → interactive-bundle
fail-closed gate) against an isolated CREATOR_DATA_ROOT, using the real
DashScope key. Opt-in::

    DASHSCOPE_API_KEY=... pytest tests/manual/test_real_blueprint_e2e.py -q -m manual_real
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytestmark = pytest.mark.manual_real

requires_dashscope_key = pytest.mark.skipif(
    not os.environ.get("DASHSCOPE_API_KEY"),
    reason="requires DASHSCOPE_API_KEY",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path / "creator-root"))
    monkeypatch.setenv(
        "CREATOR_MODEL_CONFIG_PATH",
        str(tmp_path / "creator-root" / "config" / "model_config.json"),
    )
    monkeypatch.setenv("TEXT_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
    monkeypatch.setenv("TEXT_MODEL_NAME", "qwen-plus")
    (tmp_path / "creator-root" / "config").mkdir(parents=True, exist_ok=True)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.dependencies import creator_error_handler
    from api.router import router as creator_router
    from domain.errors import CreatorError
    from services.project_files.facade import (
        clear_creator_file_service_registry,
    )

    clear_creator_file_service_registry()
    app = FastAPI()
    app.add_exception_handler(CreatorError, creator_error_handler)
    app.include_router(creator_router)
    with TestClient(app) as test_client:
        yield test_client
    clear_creator_file_service_registry()



def _pointer_get(document: dict, pointer: str):
    from services.project_files.json_pointer import MISSING

    node = document
    for token in pointer.strip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return MISSING
            node = node[token]
        elif isinstance(node, list):
            if token == "-" or int(token) >= len(node):
                return MISSING
            node = node[int(token)]
        else:
            return MISSING
    return node


def _op(project: dict, op: str, path: str, value):
    from services.project_files.json_pointer import hash_json_value

    return {
        "op": op,
        "path": path,
        "value": value,
        "expectedValueHash": hash_json_value(_pointer_get(project, path)),
    }

def _patch(client, project_id: str, snapshot: dict, operations: list) -> dict:
    response = client.patch(
        f"/projects/{project_id}/project",
        json={
            "clientCommandId": uuid.uuid4().hex,
            "editSessionId": "e2e",
            "baseGeneration": snapshot["generation"],
            "baseEtag": snapshot["etag"],
            "operations": operations,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(client, project_id: str) -> dict:
    response = client.get(f"/projects/{project_id}/project")
    assert response.status_code == 200, response.text
    payload = response.json()
    payload["etag"] = response.headers.get("ETag", "").strip('"') or payload.get(
        "etag",
        "",
    )
    return payload


@requires_dashscope_key
def test_blueprint_slice_end_to_end_with_real_model(client) -> None:
    # 1. Create a story project through the real endpoint.
    created = client.post(
        "/projects",
        json={
            "clientRequestId": uuid.uuid4().hex,
            "name": f"雾山谜案-e2e-{uuid.uuid4().hex[:6]}",
            "description": "互动短剧端到端验收",
            "scenario": "short_drama",
            "aspectRatio": "9:16",
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["projectId"]

    # 2. Snapshot must already be schema v9 with narrative fields.
    snapshot = _snapshot(client, project_id)
    project = snapshot["project"] if "project" in snapshot else snapshot
    assert project["schema_version"] == 9
    assert project["narrative_edges"] == []
    primary_id = project["timelines"]["order"][0]

    # 3. Real model call: draft a two-episode structure synopsis.
    import asyncio

    from models.text_model import chat_completion

    draft = asyncio.run(
        chat_completion(
            "为一部两集互动短剧《雾山谜案》分别用一句话写两集梗概，"
            '只输出 JSON：{"ep1": "...", "ep2": "..."}',
            temperature=0.3,
        ),
    )
    start = draft.find("{")
    end = draft.rfind("}")
    synopses = json.loads(draft[start : end + 1])
    assert synopses["ep1"].strip() and synopses["ep2"].strip()

    # 4. Persist the drafted structure through the real patch channel:
    #    title/synopsis on the primary node, a second timeline, one edge.
    second_id = "tl:ep2"
    _patch(
        client,
        project_id,
        snapshot,
        [
            _op(project, "replace", f"/timelines/items/{primary_id}/title", "第1集 · 雾夜来信"),
            _op(project, "replace", f"/timelines/items/{primary_id}/synopsis", synopses["ep1"].strip()),
            _op(project, "add", f"/timelines/items/{second_id}", {
                "timeline_id": second_id,
                "title": "第2集 · 旧宅疑云",
                "synopsis": synopses["ep2"].strip(),
            }),
            _op(project, "add", "/timelines/order/1", second_id),
            _op(project, "add", "/narrative_edges/0", {
                "edge_id": "edge:1",
                "source_timeline_id": primary_id,
                "target_timeline_id": second_id,
                "label": "选择 · 进入旧宅",
                "prompt": "是否进入旧宅？",
            }),
        ],
    )
    updated = _snapshot(client, project_id)
    updated_project = (
        updated["project"] if "project" in updated else updated
    )
    assert (
        updated_project["timelines"]["items"][second_id]["synopsis"]
        == synopses["ep2"].strip()
    )
    assert len(updated_project["narrative_edges"]) == 1

    # 5. Work graph derives without error on the branching project.
    graph = client.get(f"/projects/{project_id}/work-graph")
    assert graph.status_code == 200, graph.text

    # 6. Interactive bundle fails closed before any final cut exists.
    bundle = client.get(f"/projects/{project_id}/interactive-bundle")
    assert bundle.status_code == 409, bundle.text
