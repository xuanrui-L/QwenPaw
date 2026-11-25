# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,unused-argument
"""Asynchronous image submission and polling for DashScope and OpenAI."""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
import pytest

import models.image.dashscope_provider as dashscope_provider  # noqa: PLR0402  pylint: disable=consider-using-from-import
import models.image.openai_provider as openai_provider  # noqa: PLR0402  pylint: disable=consider-using-from-import
from models.image.base import REFERENCE_DOWNLOAD_ERROR_CODE
from models.image.dashscope_provider import DashScopeImageModel
from models.image.openai_provider import (
    OpenAIImageModel,
    build_reference_image_files,
)
from utils.exceptions import ModelError

pytestmark = pytest.mark.unit


def _model(
    base_url: str = dashscope_provider.DEFAULT_BASE_URL,
    timeout: int = 240,
) -> DashScopeImageModel:
    return DashScopeImageModel(
        model_name="qwen-image-3.0-pro",
        api_key="test-key",
        base_url=base_url,
        timeout=timeout,
    )


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request(
            "POST",
            "https://dashscope.aliyuncs.com/api/v1/x",
        ),
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


# ── async-rejected accounts fall back to the sync transport ───────────────


def test_async_rejection_falls_back_to_sync_and_caches(monkeypatch) -> None:
    """403 'does not support asynchronous calls' → sync resubmit, cached.

    Observed live on 2026-08-06: the account-level API rejected the async
    header outright, so the transport must retry without it and remember
    the discovery instead of probing on every image.
    """
    monkeypatch.setattr(DashScopeImageModel, "_async_unsupported", False)
    model = _model()
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("x-dashscope-async") == "enable":
            return httpx.Response(
                403,
                text="current user api does not support asynchronous calls",
            )
        return httpx.Response(
            200,
            json={"output": {"choices": [{"message": {}}]}},
        )

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            return await model._request(client, "p", "16:9", [])

    first = asyncio.run(scenario())
    assert first.status_code == 200
    assert len(calls) == 2  # async probe + sync resubmit
    assert DashScopeImageModel._async_unsupported is True

    second = asyncio.run(scenario())
    assert second.status_code == 200
    assert len(calls) == 3  # cached: straight to sync, no probe
    # An unrelated 403 must not be mistaken for the async rejection.
    denied = _response(403, {"message": "invalid api key"})
    assert DashScopeImageModel._async_rejected(denied) is False


# ── poll loop ──────────────────────────────────────────────────────────────


def _succeeded_payload() -> dict:
    return {
        "output": {
            "task_id": "t-1",
            "task_status": "SUCCEEDED",
            "results": {
                "choices": [
                    {
                        "message": {
                            "content": [{"image": "https://oss/img.png"}],
                        },
                    },
                ],
            },
        },
    }


def _poll(
    model: DashScopeImageModel,
    transport: httpx.MockTransport,
) -> httpx.Response:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await model._poll_task(client, "t-1")

    return asyncio.run(scenario())


def test_poll_rides_out_pending_and_transient_errors(monkeypatch) -> None:
    monkeypatch.setattr(dashscope_provider, "POLL_INTERVAL_SECONDS", 0)
    steps = iter(["PENDING", "boom", "503", "RUNNING", "SUCCEEDED"])

    def handler(request: httpx.Request) -> httpx.Response:
        step = next(steps)
        if step == "boom":
            raise httpx.ReadTimeout("poll hiccup", request=request)
        if step == "503":
            return httpx.Response(503, json={})
        if step == "SUCCEEDED":
            return httpx.Response(200, json=_succeeded_payload())
        return httpx.Response(
            200,
            json={"output": {"task_id": "t-1", "task_status": step}},
        )

    response = _poll(_model(), httpx.MockTransport(handler))
    assert response.json()["output"]["task_status"] == "SUCCEEDED"


def test_failed_task_raises_a_deterministic_model_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "t-1",
                    "task_status": "FAILED",
                    "code": "DataInspectionFailed",
                    "message": "safety rejection",
                },
            },
        )

    with pytest.raises(ModelError) as caught:
        _poll(_model(), httpx.MockTransport(handler))
    assert "FAILED" in str(caught.value)
    assert "safety rejection" in str(caught.value)
    # Deterministic wording: must NOT look transient to the scheduler.
    assert "timed out" not in str(caught.value).lower()


def test_request_log_keeps_routing_facts_never_the_reference_signature(
    monkeypatch,
) -> None:
    """A successful request must not write its reference grant to the log.

    Issue #8592: the INFO line printed the whole request body, so an OSS
    pre-signed reference URL (``OSSAccessKeyId``/``Signature``) reached
    creator.log even when the render succeeded, and that pair is a usable
    grant for anyone who can read the file.
    """
    signed = (
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png"
        "?OSSAccessKeyId=AK123&Expires=1700000000&Signature=SECRET"
    )
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {"choices": []}})

    monkeypatch.setattr(DashScopeImageModel, "_async_unsupported", True)
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger("model.image.dashscope")
    collector = _Collector(logging.INFO)
    target.addHandler(collector)
    try:

        async def scenario() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
            ) as client:
                return await model._request(client, "p", "1:1", [signed])

        model = DashScopeImageModel(
            model_name="qwen-image-3.0-pro",
            api_key="sk-secret-key-123456",
            base_url=dashscope_provider.DEFAULT_BASE_URL,
            timeout=30,
        )
        assert asyncio.run(scenario()).status_code == 200
    finally:
        target.removeHandler(collector)

    # The body still carries the grant (it is what the provider needs).
    embedded = bodies[0]["input"]["messages"][0]["content"][0]["image"]
    assert "Signature=SECRET" in embedded
    logged = "\n".join(record.getMessage() for record in records)
    assert "OSSAccessKeyId" not in logged
    assert "Signature" not in logged
    assert "sk-secret-key-123456" not in logged
    assert "model=qwen-image-3.0-pro" in logged
    assert "mode=generate" in logged
    assert "references=1" in logged


def test_failed_task_scrubs_reference_signature_and_stays_permanent() -> None:
    """HTTP 200 + FAILED "download 403" must match the sync classification.

    The task payload carries the upstream message verbatim, so the async
    path used to surface the raw pre-signed URL as a retryable error while
    the sync HTTP branch scrubbed it and refused to retry.
    """
    signed = (
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png"
        "?OSSAccessKeyId=AK123&Signature=SECRET"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "t-1",
                    "task_status": "FAILED",
                    "message": f"failed to download image: {signed} (status 403)",
                },
            },
        )

    with pytest.raises(ModelError) as caught:
        _poll(_model(), httpx.MockTransport(handler))
    message = str(caught.value)
    assert caught.value.retryable is False
    assert "AK123" not in message
    assert "SECRET" not in message
    # The surviving facts stay diagnosable.
    assert "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png" in message
    assert "(status 403)" in message
    assert "regenerate or reselect" in message


def test_failed_task_keeps_storage_5xx_download_failures_retryable() -> None:
    """A storage 5xx is transient, unlike the 403 grant expiry above.

    The sync HTTP branch only classifies a reference failure as permanent
    for a 4xx; a 5xx object-store failure stays retryable. The async
    terminal payload carries the same prose and must reach the same
    verdict so a recovered store can be retried.
    """
    signed = (
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png"
        "?OSSAccessKeyId=AK123&Signature=SECRET"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "t-1",
                    "task_status": "FAILED",
                    "message": f"failed to download image: {signed} (status 503)",
                },
            },
        )

    with pytest.raises(ModelError) as caught:
        _poll(_model(), httpx.MockTransport(handler))
    message = str(caught.value)
    assert caught.value.retryable is True
    assert caught.value.code != REFERENCE_DOWNLOAD_ERROR_CODE
    assert "AK123" not in message
    assert "SECRET" not in message
    assert "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png" in message
    assert "(status 503)" in message


def test_failed_task_scrubs_urls_but_keeps_other_failures_retryable() -> None:
    signed = (
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/a.png"
        "?OSSAccessKeyId=AK123&Signature=SECRET"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "t-1",
                    "task_status": "FAILED",
                    "message": f"internal error while reading {signed}",
                },
            },
        )

    with pytest.raises(ModelError) as caught:
        _poll(_model(), httpx.MockTransport(handler))
    message = str(caught.value)
    assert caught.value.retryable is True
    assert "AK123" not in message
    assert "SECRET" not in message
    assert "internal error while reading" in message


def test_deadline_exhaustion_reads_as_a_transient_timeout(monkeypatch) -> None:
    monkeypatch.setattr(dashscope_provider, "POLL_INTERVAL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"task_id": "t-1", "task_status": "RUNNING"}},
        )

    with pytest.raises(ModelError) as caught:
        _poll(_model(timeout=0), httpx.MockTransport(handler))
    # The scheduler's transient classifier keys on this wording.
    assert "timed out" in str(caught.value)


# ── decode handles the nested async-result payload ─────────────────────────


def test_decode_unwraps_async_and_sync_payloads(monkeypatch) -> None:
    model = _model()

    async def fake_download(url: str, model_name: str) -> str:
        return f"/generated/{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(
        dashscope_provider,
        "download_remote_image",
        fake_download,
    )
    assert asyncio.run(model._decode(_succeeded_payload())) == {
        "url": "/generated/img.png",
        "source_url": "",
    }
    sync_payload = {
        "output": {
            "choices": [
                {"message": {"content": [{"image": "https://oss/sync.png"}]}},
            ],
        },
    }
    assert asyncio.run(model._decode(sync_payload)) == {
        "url": "/generated/sync.png",
        "source_url": "",
    }


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 20000


def _openai_model(
    background_model: str = "gpt-5.2",
    base_url: str = "https://api.openai.com/v1",
    timeout: int = 240,
) -> OpenAIImageModel:
    return OpenAIImageModel(
        model_name="gpt-image-2",
        api_key="test-key",
        base_url=base_url,
        quality="low",
        timeout=timeout,
        background_model=background_model,
    )


def _request(model: OpenAIImageModel, handler) -> httpx.Response:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            return await model._request(client, "p", "16:9", [])

    return asyncio.run(scenario())


# ── background submit + poll ───────────────────────────────────────────────


def _completed_payload() -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "type": "image_generation_call",
                "result": base64.b64encode(_PNG_BYTES).decode("ascii"),
            },
        ],
    }


def test_background_submits_then_rides_out_transient_polls(
    monkeypatch,
) -> None:
    monkeypatch.setattr(openai_provider, "RESPONSES_POLL_INTERVAL_SECONDS", 0)
    submits: list[str] = []
    steps = iter(["in_progress", "boom", "503", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            assert body["background"] is True
            assert body["model"] == "gpt-5.2"
            assert body["tools"][0]["type"] == "image_generation"
            assert body["tools"][0]["model"] == "gpt-image-2"
            submits.append("submit")
            return httpx.Response(
                200,
                json={"id": "resp_1", "status": "queued"},
            )
        step = next(steps)
        if step == "boom":
            raise httpx.ReadTimeout("poll hiccup", request=request)
        if step == "503":
            return httpx.Response(503, json={})
        if step == "completed":
            return httpx.Response(200, json=_completed_payload())
        return httpx.Response(200, json={"id": "resp_1", "status": step})

    response = _request(_openai_model(), handler)
    assert response.json()["status"] == "completed"
    assert submits == ["submit"]  # transient poll errors never resubmit


def test_background_failure_raises_with_the_provider_detail(
    monkeypatch,
) -> None:
    monkeypatch.setattr(openai_provider, "RESPONSES_POLL_INTERVAL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "resp_1", "status": "queued"},
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "failed",
                "error": {"message": "moderation_blocked"},
            },
        )

    with pytest.raises(ModelError) as caught:
        _request(_openai_model(), handler)
    assert "moderation_blocked" in str(caught.value)
    # Deterministic wording: must NOT look transient to the scheduler.
    assert "timed out" not in str(caught.value).lower()


def test_background_deadline_reads_as_a_transient_timeout(monkeypatch) -> None:
    monkeypatch.setattr(openai_provider, "RESPONSES_POLL_INTERVAL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "resp_1", "status": "queued"},
            )
        return httpx.Response(
            200,
            json={"id": "resp_1", "status": "in_progress"},
        )

    with pytest.raises(ModelError) as caught:
        _request(_openai_model(timeout=0), handler)
    # The scheduler's transient classifier keys on this wording.
    assert "timed out" in str(caught.value)


# ── decode handles both payload shapes ─────────────────────────────────────


def test_decode_unwraps_background_and_classic_payloads(monkeypatch) -> None:
    model = _openai_model()
    saved: dict = {}

    def fake_persist(img_bytes: bytes, model_name: str, source: str) -> str:
        saved["bytes"] = img_bytes
        return "/generated/bg.png"

    monkeypatch.setattr(openai_provider, "persist_image_bytes", fake_persist)
    assert asyncio.run(model._decode(_completed_payload())) == {
        "url": "/generated/bg.png",
        "source_url": "",
    }
    assert saved["bytes"] == _PNG_BYTES
    classic = {
        "data": [{"b64_json": base64.b64encode(_PNG_BYTES).decode("ascii")}],
    }
    assert asyncio.run(model._decode(classic)) == {
        "url": "/generated/bg.png",
        "source_url": "",
    }


# ---------------------------------------------------------------------------
# URL construction and reference upload fields (classic Images API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "prefix"),
    [
        # Both endpoint styles must resolve to a single /v1/images/...
        # path — never /v1/v1/images/....
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        (
            "https://routify.alibaba-inc.com/protocol/openai",
            "https://routify.alibaba-inc.com/protocol/openai/v1",
        ),
    ],
)
def test_image_urls_gain_exactly_one_v1_segment(
    endpoint: str,
    prefix: str,
) -> None:
    model = _openai_model(base_url=endpoint)
    assert model.generation_url == f"{prefix}/images/generations"
    assert model._url(["ref.png"]) == f"{prefix}/images/edits"


def test_reference_files_use_the_array_field_for_multiple_images(
    tmp_path,
) -> None:
    """Two or more references upload as image[]; a single one stays image.

    The gateway rejects a repeated bare ``image`` field with 400
    "Duplicate parameter: 'image'" (storyboard incident regression).
    """

    first = tmp_path / "ref-a.png"
    second = tmp_path / "ref-b.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\na")
    second.write_bytes(b"\x89PNG\r\n\x1a\nb")

    multiple = asyncio.run(
        build_reference_image_files(
            [first.as_uri(), second.as_uri(), first.as_uri()],
        ),
    )
    # Distinct Project versions can share transport bytes. Position 3 must
    # remain position 3 instead of being collapsed by the upload adapter.
    assert [name for name, _ in multiple] == ["image[]", "image[]", "image[]"]

    single = asyncio.run(build_reference_image_files([first.as_uri()]))
    assert [name for name, _ in single] == ["image"]
