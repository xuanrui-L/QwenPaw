# -*- coding: utf-8 -*-
# pylint: disable=unused-argument,redefined-outer-name
"""Plugin-bundled inspiration example behavior."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from uuid import uuid4

import pytest

from api import example_routes
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.unit

_CREATE_PAYLOAD = {
    "clientRequestId": "example-fixture-request-1",
    "name": "乌鸦喝水示例",
    "description": "做一个乌鸦喝水的卡通短视频",
    "scenario": "short_drama",
    "aspectRatio": "16:9",
    "resolution": "720P",
    "contentType": None,
}

_EXAMPLE_ID = "crow-short-drama"


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _export_project_archive(client) -> tuple[str, bytes]:
    """Create a real Project, export it, then remove it from the data root."""

    created = await client.post("/projects", json=_CREATE_PAYLOAD)
    assert created.status_code == 201
    project_id = created.json()["projectId"]
    exported = await client.get(
        f"/projects/{project_id}/export",
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert exported.status_code == 200
    deleted = await client.delete(
        f"/projects/{project_id}",
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert deleted.status_code == 204
    return project_id, exported.content


@pytest.fixture()
def bundled_example(app, api_runtime_root, tmp_path, monkeypatch):
    """Stage one bundled example built from a real exported Project."""

    async def scenario():
        async with _client(app) as client:
            return await _export_project_archive(client)

    project_id, archive = asyncio.run(scenario())
    examples_dir = tmp_path / "bundled-examples"
    examples_dir.mkdir()
    (examples_dir / f"{_EXAMPLE_ID}.zip").write_bytes(archive)
    (examples_dir / "manifest.json").write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "id": _EXAMPLE_ID,
                        "title": "短剧制作",
                        "description": "做一个乌鸦喝水的卡通短视频",
                        "projectId": project_id,
                        "archive": f"{_EXAMPLE_ID}.zip",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(example_routes, "examples_root", lambda: examples_dir)
    return project_id


def test_examples_list_reports_bundled_catalogue(app, bundled_example):
    async def scenario():
        async with _client(app) as client:
            return await client.get("/examples")

    listed = asyncio.run(scenario())

    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [_EXAMPLE_ID]
    assert items[0]["projectId"] == bundled_example
    assert items[0]["installed"] is False


def test_open_materializes_without_surfacing_in_project_list(
    app,
    api_runtime_root,
    bundled_example,
):
    async def scenario():
        async with _client(app) as client:
            opened = await client.post(f"/examples/{_EXAMPLE_ID}/open")
            listed_projects = await client.get("/projects")
            listed_examples = await client.get("/examples")
            snapshot = await client.get(
                f"/projects/{bundled_example}/project",
            )
            return opened, listed_projects, listed_examples, snapshot

    opened, listed_projects, listed_examples, snapshot = asyncio.run(
        scenario(),
    )

    assert opened.status_code == 200
    assert opened.json() == {"projectId": bundled_example, "installed": True}
    # The materialized example carries the marker and stays off the shelf...
    marker = api_runtime_root / bundled_example / ".builtin-example"
    assert marker.is_file()
    assert listed_projects.json()["items"] == []
    assert listed_examples.json()["items"][0]["installed"] is True
    # ...while id-addressed routes serve it like any other Project.
    assert snapshot.status_code == 200
    assert snapshot.json()["projectId"] == bundled_example


def test_open_is_idempotent(app, api_runtime_root, bundled_example):
    async def scenario():
        async with _client(app) as client:
            first = await client.post(f"/examples/{_EXAMPLE_ID}/open")
            second = await client.post(f"/examples/{_EXAMPLE_ID}/open")
            return first, second

    first, second = asyncio.run(scenario())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    staging = api_runtime_root / ".example-staging"
    assert not staging.exists() or not [
        entry for entry in staging.iterdir() if entry.is_dir()
    ]


def test_open_unknown_example_is_not_found(app, bundled_example):
    async def scenario():
        async with _client(app) as client:
            return await client.post("/examples/no-such-example/open")

    response = asyncio.run(scenario())

    assert response.status_code == 404


def test_manifest_entries_without_archives_are_hidden(
    app,
    api_runtime_root,
    tmp_path,
    monkeypatch,
):
    examples_dir = tmp_path / "broken-examples"
    examples_dir.mkdir()
    (examples_dir / "manifest.json").write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "id": "ghost",
                        "title": "缺归档",
                        "description": "archive 不存在",
                        "projectId": "project-000000000000",
                        "archive": "ghost.zip",
                    },
                    {
                        "id": "escape",
                        "title": "路径逃逸",
                        "description": "archive 含路径",
                        "projectId": "project-000000000001",
                        "archive": "../escape.zip",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(example_routes, "examples_root", lambda: examples_dir)

    async def scenario():
        async with _client(app) as client:
            listed = await client.get("/examples")
            opened = await client.post("/examples/ghost/open")
            return listed, opened

    listed, opened = asyncio.run(scenario())

    assert listed.json()["items"] == []
    assert opened.status_code == 404


def test_corrupt_bundled_archive_is_an_integrity_error(
    app,
    api_runtime_root,
    tmp_path,
    monkeypatch,
):
    examples_dir = tmp_path / "corrupt-examples"
    examples_dir.mkdir()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.txt", "boom")
    (examples_dir / "bad.zip").write_bytes(payload.getvalue())
    (examples_dir / "manifest.json").write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "id": "bad",
                        "title": "损坏归档",
                        "description": "路径穿越",
                        "projectId": "project-000000000002",
                        "archive": "bad.zip",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(example_routes, "examples_root", lambda: examples_dir)

    async def scenario():
        async with _client(app) as client:
            return await client.post("/examples/bad/open")

    response = asyncio.run(scenario())

    # Bundled archives are plugin-controlled, so damage is 503 not 400.
    assert response.status_code == 503
    assert "损坏" in response.json()["message"]
    assert not (api_runtime_root / "project-000000000002").exists()
