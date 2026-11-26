# -*- coding: utf-8 -*-
"""The feedback draft: a person's one sentence in, a platform record out.

Creator owns every field except the reason, so the endpoint has to fill them
without inventing any - and a missing or unreadable trace must never cost the
platform a feedback row.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import feedback_routes
from domain.errors import ValidationError

PROJECT = "project-c0d79742eca95833a25d7db621abe73e"


def _draft(body: dict) -> dict:
    return asyncio.run(feedback_routes.build_feedback_draft(body))


def test_a_short_body_comes_back_with_the_platform_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feedback_routes,
        "read_trace_records",
        lambda **_: [
            {
                "traceId": "trace-1",
                "traceFile": "creator-trace-2026-09-17.jsonl",
                "traceLine": 137,
                "timestamp": "2026-09-17T02:45:06.871221+00:00",
                "name": "creator.error.reported",
                "component": "model.image",
                "attributes": {"errorCode": "IMAGE_GENERATION_FAILED"},
            },
        ],
    )

    assert _draft(
        {"project_id": PROJECT, "feedback": "  生成失败看不懂  "}
    ) == {
        "project_id": PROJECT,
        "stage": "media_generation",
        "feedback": "生成失败看不懂",
        "trace_id": "trace-1",
        "trace_file": "creator-trace-2026-09-17.jsonl",
        "trace_line": 137,
        "trace_ts": "2026-09-17T02:45:06.871221+00:00",
    }


def test_draft_uses_recent_activity_when_no_errors_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    def fake(  # pylint: disable=unused-argument
        *,
        filters,
        limit,
    ):
        seen.append(dict(filters))
        return (
            []
            if filters.get("status")
            else [
                {
                    "name": "creator.export",
                    "traceId": "trace-2",
                    "traceLine": 3,
                    "traceFile": "creator-trace-2026-09-17.jsonl",
                    "timestamp": "2026-09-17T03:00:00+00:00",
                },
            ]
        )

    monkeypatch.setattr(feedback_routes, "read_trace_records", fake)
    draft = _draft({"project_id": PROJECT, "feedback": "太慢"})

    assert seen[0]["status"] == "error"
    assert seen[0]["projectId"] == PROJECT
    assert draft["trace_line"] == 3
    assert draft["stage"] == "exported"


def test_the_stage_walks_back_past_a_generic_newest_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The newest failure is a bare api/CONFLICT that names no step; the image
    # error one line earlier does. The stage reads back the real signal while
    # the pointer still cites the newest record, which is what actually
    # happened last.
    monkeypatch.setattr(
        feedback_routes,
        "read_trace_records",
        lambda **_: [
            {
                "traceId": "t-old",
                "traceFile": "creator-trace-2026-09-17.jsonl",
                "traceLine": 10,
                "timestamp": "2026-09-17T01:00:00+00:00",
                "name": "creator.error.reported",
                "component": "model.image",
                "attributes": {"errorCode": "IMAGE_GENERATION_FAILED"},
            },
            {
                "traceId": "t-new",
                "traceFile": "creator-trace-2026-09-17.jsonl",
                "traceLine": 20,
                "timestamp": "2026-09-17T02:00:00+00:00",
                "name": "creator.error.reported",
                "component": "api",
                "attributes": {"errorCode": "CONFLICT"},
            },
        ],
    )

    draft = _draft({"project_id": PROJECT, "feedback": "图片老失败"})
    assert draft["stage"] == "media_generation"
    assert draft["trace_line"] == 20


def test_an_absent_trace_still_yields_a_submittable_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty pointer parts are omitted rather than sent as "", and a caller that
    # names its step keeps that name.
    monkeypatch.setattr(feedback_routes, "read_trace_records", lambda **_: [])
    assert _draft(
        {
            "project_id": PROJECT,
            "stage": "composition",
            "feedback": "合成占了三分钟还没动",
        },
    ) == {
        "project_id": PROJECT,
        "stage": "composition",
        "feedback": "合成占了三分钟还没动",
    }


def test_an_unreadable_trace_never_eats_the_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kwargs: object) -> list:
        raise RuntimeError("trace directory is gone")

    monkeypatch.setattr(feedback_routes, "read_trace_records", boom)
    assert _draft({"project_id": PROJECT, "feedback": "x"})["feedback"] == "x"


@pytest.mark.parametrize(
    "body",
    [
        {"project_id": "", "feedback": "x"},
        {"project_id": "p" * 129, "feedback": "x"},
        {"project_id": PROJECT, "feedback": "   "},
        {
            "project_id": PROJECT,
            "feedback": "字" * (feedback_routes.FEEDBACK_LIMIT + 1),
        },
    ],
)
def test_bodies_the_platform_would_refuse_are_refused_first(
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
) -> None:
    monkeypatch.setattr(feedback_routes, "read_trace_records", lambda **_: [])
    with pytest.raises(ValidationError):
        _draft(body)


def test_feedback_pointer_resolves_to_the_durable_trace(tmp_path, monkeypatch):
    import json

    from schemas.observability import ObservabilityConfigData
    from services.observability import save_observability_config, trace_event

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    save_observability_config(ObservabilityConfigData(enabled=True))
    trace_event(
        "creator.test.feedback",
        component="test",
        projectId=PROJECT,
    )

    draft = _draft({"project_id": PROJECT, "feedback": "慢"})
    assert draft["trace_file"].startswith("creator-trace-")
    log = tmp_path / "observability" / "traces" / draft["trace_file"]
    record = json.loads(log.read_text().splitlines()[draft["trace_line"] - 1])
    assert record["traceId"] == draft["trace_id"]


PLATFORM = "/feedbacks/platform"


@pytest.fixture
def platform_host():
    app = FastAPI()
    service = AsyncMock()
    app.state.community_connection_service = service
    app.include_router(feedback_routes.router)
    with TestClient(app, headers={"X-Platform-Account": "alice"}) as client:
        yield client, service


def test_platform_attachment_and_creator_feedback(platform_host):
    client, service = platform_host
    service.status.return_value = {"account": {"id": "alice"}}
    assert client.get(f"{PLATFORM}/account").json() == {"id": "alice"}

    url = "https://example.org/shot.png"
    service.community_request.return_value = {"url": url}
    image = ("screenshot.png", b"image bytes", "image/png")
    result = client.post(f"{PLATFORM}/screenshots", files={"file": image})
    assert result.json() == {"url": url}
    service.community_request.assert_awaited_once_with(
        "POST",
        "/api/v1/feedback/screenshots",
        account_id="alice",
        files={"file": image},
        timeout=60,
    )

    body = {"description": "预览卡住", "screenshot_urls": [url]}
    assert client.post(PLATFORM, json=body).json() == {"submitted": True}
    service.community_request.assert_awaited_with(
        "POST",
        "/api/v1/feedback",
        account_id="alice",
        json={
            **body,
            "modules": ["qwenpaw_usage"],
            "qwenpaw_version_tag": "qwenpaw_creator",
        },
    )


@pytest.mark.parametrize("has_service", [False, True])
def test_unconnected_host_offers_account_setup(platform_host, has_service):
    client, service = platform_host
    service.status.return_value = {"account": None}
    if not has_service:
        del client.app.state.community_connection_service
    response = client.get(f"{PLATFORM}/account")
    assert response.status_code == 409
    assert response.json()["code"] == "PLATFORM_ACCOUNT_REQUIRED"


def test_invalid_feedback_stays_local(platform_host, monkeypatch):
    client, service = platform_host
    monkeypatch.setattr(feedback_routes, "SCREENSHOT_BYTE_LIMIT", 4)
    for mime, content in [("text/plain", b"x"), ("image/png", b"12345")]:
        response = client.post(
            f"{PLATFORM}/screenshots",
            files={"file": ("file", content, mime)},
        )
        assert response.status_code == 422
    assert client.post(PLATFORM, json={"description": "  "}).status_code == 422
    service.community_request.assert_not_awaited()
