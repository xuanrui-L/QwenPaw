# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,unused-argument
"""Image provider capabilities, reference limits, and request formats."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx

from models import config as model_config
from models.image import (
    ArkImageModel,
    BFLImageModel,
    GeminiImageModel,
    IdeogramImageModel,
    MiniMaxImageModel,
    _detect_backend_from_names,
    ark_provider,
    bfl_provider,
    dashscope_provider,
    gemini_provider,
    ideogram_provider,
    image_backend_for_protocol,
    minimax_provider,
)
from models.image.base import image_reference_capability, image_reference_limit
from models.image.dashscope_provider import DashScopeImageModel
from models.image.openai_provider import OpenAIImageModel
from utils.exceptions import ModelError

_BASE_URL = "https://dashscope.test/api/v1/services/aigc/multimodal-generation/generation"
_TRANSLATE_URL = (
    "https://dashscope.test/api/v1/services/aigc/image2image/image-synthesis"
)


def _dashscope_model() -> DashScopeImageModel:
    return DashScopeImageModel(
        model_name="qwen-image-2.0-pro",
        api_key="sk-test",
        base_url=_BASE_URL,
        timeout=30,
    )


def _openai_model() -> OpenAIImageModel:
    return OpenAIImageModel(
        model_name="gpt-image-2",
        api_key="sk-test",
        base_url="https://openai.test/v1",
        quality="low",
        timeout=30,
    )


@contextmanager
def _tool_configs(configs: dict | None = None):
    token = model_config.set_request_tool_configs(configs or {})
    try:
        yield
    finally:
        model_config.reset_request_tool_configs(token)


def _run_translate(**kwargs) -> str:
    with _tool_configs():
        return asyncio.run(
            _dashscope_model().generate(
                "translate",
                mode="translate",
                reference_image_urls=["https://cdn.test/poster.png"],
                **kwargs,
            ),
        )


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ModelError, match="Unknown image mode"):
        asyncio.run(_dashscope_model().generate("cat", mode="remix"))


def test_mode_reference_count_validation() -> None:
    model = _dashscope_model()
    with pytest.raises(ModelError, match="1-3 reference images"):
        asyncio.run(model.generate("make it red", mode="edit"))
    with pytest.raises(ModelError, match="exactly 1 reference image"):
        asyncio.run(model.generate("translate", mode="translate"))


def test_openai_provider_rejects_edit_and_translate() -> None:
    model = _openai_model()
    for mode in ("edit", "translate"):
        with pytest.raises(ModelError, match="does not support"):
            asyncio.run(
                model.generate(
                    "poster",
                    mode=mode,
                    reference_image_urls=["https://cdn.test/poster.png"],
                ),
            )


def test_edit_body_keeps_images_first_and_disables_watermark() -> None:
    body = asyncio.run(
        _dashscope_model()._build_body(
            "convert to watercolor style",
            "16:9",
            ["https://cdn.test/a.png", "https://cdn.test/b.png"],
        ),
    )
    assert body["model"] == "qwen-image-2.0-pro"
    assert body["input"]["messages"][0]["content"] == [
        {"image": "https://cdn.test/a.png"},
        {"image": "https://cdn.test/b.png"},
        {"text": "convert to watercolor style"},
    ]
    assert body["parameters"] == {"size": "1664*928", "watermark": False}


def _reference_upload_hook() -> str:
    if hasattr(dashscope_provider, "upload_reference_bytes_for_provider"):
        return "upload_reference_bytes_for_provider"
    return "upload_reference_bytes_to_dashscope_temp"


def test_token_plan_reference_inlines_base64_not_upload(
    tmp_path,
    monkeypatch,
) -> None:
    """A Token Plan endpoint has no upload route: the public dashscope
    uploads answer its sk-sp- key 401 and the token-plan host serves no
    /api/v1/uploads, so a local reference is inlined as a Base64 data URL
    (which the multimodal-generation endpoint accepts) instead of uploaded.
    """
    from PIL import Image

    async def refuse_upload(*args, **kwargs):
        raise AssertionError("Token Plan reference reached the upload API")

    monkeypatch.setattr(
        dashscope_provider,
        _reference_upload_hook(),
        refuse_upload,
    )
    reference = tmp_path / "ref.png"
    Image.new("RGB", (8, 8), "red").save(reference, format="PNG")
    model = DashScopeImageModel(
        model_name="qwen-image-2.0-pro",
        api_key="sk-sp-test",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
        timeout=30,
    )
    resolved = asyncio.run(model._public_reference_url(reference.as_uri()))
    assert resolved.startswith("data:image/png;base64,")


def _token_plan_image_model() -> DashScopeImageModel:
    return DashScopeImageModel(
        model_name="qwen-image-2.0-pro",
        api_key="sk-sp-test",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
        timeout=30,
    )


def _noise_png_bytes() -> bytes:
    """Deterministic noise PNG: ~31KB raw, ~11KB as an optimised JPEG.

    The gap lets a test put the inline ceiling between the two, so the
    downscale path is exercised without building a 7MB fixture.
    """
    import io

    from PIL import Image

    output = io.BytesIO()
    Image.effect_noise((128, 128), 128).convert("RGB").save(
        output,
        format="PNG",
    )
    return output.getvalue()


def test_token_plan_reference_over_inline_ceiling_errors(
    tmp_path,
    monkeypatch,
) -> None:
    """A generate reference that cannot shrink under the 7MB Base64 ceiling
    fails with guidance rather than sending a doomed upload, and fails fast:
    the ceiling is a provider constant, so retrying cannot help."""
    from PIL import Image

    async def refuse_upload(*args, **kwargs):
        raise AssertionError("Token Plan reference reached the upload API")

    monkeypatch.setattr(
        dashscope_provider,
        _reference_upload_hook(),
        refuse_upload,
    )
    # Force the tiny test image over the ceiling without building a 7MB file.
    # 16 bytes is below even the smallest ladder rung, so no downscale can
    # rescue it and the refusal path is what runs.
    monkeypatch.setattr(
        dashscope_provider,
        "TOKEN_PLAN_INLINE_IMAGE_MAX_BYTES",
        16,
    )
    reference = tmp_path / "big.png"
    Image.new("RGB", (8, 8), "red").save(reference, format="PNG")
    model = _token_plan_image_model()
    with pytest.raises(ModelError, match="7MB") as excinfo:
        asyncio.run(model._public_reference_url(reference.as_uri()))
    assert excinfo.value.retryable is False


def test_token_plan_generate_reference_downscales_under_ceiling(
    tmp_path,
    monkeypatch,
) -> None:
    """A generate reference only carries subject and style, so shrinking it
    beats failing a request the provider could have served.

    The re-encoded payload is JPEG whatever the source was, so the data URL
    must not keep the source's PNG MIME: ``reference_media_data_url`` reads
    the type from the filename suffix first.
    """
    import base64

    async def refuse_upload(*args, **kwargs):
        raise AssertionError("Token Plan reference reached the upload API")

    monkeypatch.setattr(
        dashscope_provider,
        _reference_upload_hook(),
        refuse_upload,
    )
    monkeypatch.setattr(
        dashscope_provider,
        "TOKEN_PLAN_INLINE_IMAGE_MAX_BYTES",
        20_000,
    )
    raw = _noise_png_bytes()
    reference = tmp_path / "big.png"
    reference.write_bytes(raw)
    assert len(raw) > 20_000

    model = _token_plan_image_model()
    resolved = asyncio.run(model._public_reference_url(reference.as_uri()))

    assert resolved.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(resolved.split(",", 1)[1])
    assert len(payload) <= 20_000
    assert len(payload) < len(raw)


@pytest.mark.parametrize("mode", ["edit", "translate"])
def test_token_plan_edit_and_translate_references_never_downscale(
    tmp_path,
    monkeypatch,
    mode,
) -> None:
    """An edit/translate reference *is* the canvas the instruction operates
    on, so its pixels are never silently resampled even when the ladder
    could fit them: the request fails fast with guidance instead."""

    async def refuse_upload(*args, **kwargs):
        raise AssertionError("Token Plan reference reached the upload API")

    monkeypatch.setattr(
        dashscope_provider,
        _reference_upload_hook(),
        refuse_upload,
    )
    # The same bytes do downscale under mode=generate (see the test above),
    # so a refusal here can only come from the mode gate.
    monkeypatch.setattr(
        dashscope_provider,
        "TOKEN_PLAN_INLINE_IMAGE_MAX_BYTES",
        20_000,
    )
    reference = tmp_path / "big.png"
    reference.write_bytes(_noise_png_bytes())

    model = _token_plan_image_model()
    with pytest.raises(ModelError, match="7MB") as excinfo:
        asyncio.run(
            model._public_reference_url(reference.as_uri(), mode=mode),
        )
    assert excinfo.value.retryable is False
    assert f"mode={mode}" in str(excinfo.value)
    assert "不会被自动" in str(excinfo.value)


@respx.mock
def test_translate_submits_async_task_polls_and_downloads_result(
    monkeypatch,
) -> None:
    monkeypatch.delenv("IMAGE_TRANSLATE_MODEL_NAME", raising=False)
    monkeypatch.setattr(
        dashscope_provider,
        "_TRANSLATE_POLL_INTERVAL_SECONDS",
        0.0,
    )
    submit_route = respx.post(_TRANSLATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "output": {"task_id": "task-mt-1", "task_status": "PENDING"},
            },
        ),
    )
    respx.get("https://dashscope.test/api/v1/tasks/task-mt-1").mock(
        side_effect=[
            httpx.Response(200, json={"output": {"task_status": "RUNNING"}}),
            httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "image_url": "https://oss.test/translated.png",
                    },
                    "request_id": "req-1",
                },
            ),
        ],
    )
    downloaded: list[str] = []

    async def fake_download(url: str, model_name: str) -> str:
        downloaded.append(url)
        assert model_name == "qwen-mt-image"
        return "/generated/translated.png"

    monkeypatch.setattr(
        dashscope_provider,
        "download_remote_image",
        fake_download,
    )

    result = _run_translate(source_lang="zh", target_lang="en")

    assert result == {"url": "/generated/translated.png", "source_url": ""}
    assert downloaded == ["https://oss.test/translated.png"]
    request = submit_route.calls.last.request
    assert request.headers["X-DashScope-Async"] == "enable"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "qwen-mt-image",
        "input": {
            "image_url": "https://cdn.test/poster.png",
            "source_lang": "zh",
            "target_lang": "en",
            "ext": {"config": {"imageSegment": False}},
        },
    }


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("wan2.7-image-pro", 9),
        ("wan2.7-image", 9),
        ("wan2.6-image", 4),
        ("z-image-turbo", 0),
        (" WAN2.7-IMAGE-PRO ", 9),
        ("wan2.7", None),
        ("wan2.7-r2v", None),
        ("wan2.7-i2v", None),
        ("wan2.7-image-pro-unknown", None),
        ("qwen-image-3.0-pro", 3),
        ("qwen-image-2.0-pro-2026-06-22", 3),
        ("qwen-image-edit-plus-2025-12-15", 3),
        ("qwen-mt-image", 1),
        ("qwen-image-plus", 0),
        ("gpt-image-2-2026-04-21", 16),
        ("gpt-image-1-mini", 16),
        ("dall-e-2", 1),
        ("dall-e-3", 0),
        ("gemini-3-pro-image", 14),
        ("gemini-3.1-flash-image", 14),
        ("gemini-2.5-flash-image", 3),
        ("doubao-seedream-5-0-pro-260628", 10),
        ("doubao-seedream-4-5-251128", 14),
        ("flux-2-pro", 8),
        ("flux-2-klein-4b", 4),
        ("flux-2-klein-9b", 4),
        ("ideogram-v3", 1),
        ("ideogram-v4", 0),
        ("gemini-42-image", None),
        ("private-gateway-alias", None),
    ],
)
def test_official_reference_limits_are_model_specific(model_name, expected):
    assert image_reference_limit(model_name) == expected
    capability = image_reference_capability(model_name)
    if expected is None:
        assert capability is None
    else:
        assert capability is not None
        assert capability.documentation_url.startswith("https://")


def test_every_ui_image_preset_has_a_reference_contract() -> None:
    """A selectable model must not fail later as an unknown capability."""
    app_root = Path(__file__).resolve().parents[3]
    source = (
        app_root / "ui/src/components/creator/ModelConfigModal.tsx"
    ).read_text(encoding="utf-8")
    presets = source.split("const IMAGE_PRESETS:", 1)[1].split(
        "const VIDEO_PRESETS:",
        1,
    )[0]
    arrays = re.findall(r"models:\s*(\[[^\]]*\])", presets)
    assert arrays, "No image presets found; update this contract test"
    model_names = set(re.findall(r'"([^"\n]+)"', "".join(arrays)))
    missing = sorted(
        name
        for name in model_names
        if image_reference_capability(name) is None
    )
    assert (
        not missing
    ), f"Image presets missing reference capabilities: {missing}"


_PNG = b"\x89PNG fake image bytes"


class _StubResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _CapturingClient:
    def __init__(self, captured: dict, payload: dict | None = None):
        self._captured = captured
        self._payload = payload or {}

    async def post(self, url, headers=None, json=None, data=None, files=None):
        self._captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "data": data,
                "files": files,
            },
        )
        return _StubResponse(self._payload)


def _stub_reference_reading(monkeypatch, module) -> None:
    async def fake_read(_url, **_kwargs):
        return _PNG, "ref.png"

    monkeypatch.setattr(module, "read_reference_media", fake_read)
    monkeypatch.setattr(
        module,
        "validate_reference_image_bytes",
        lambda content: None,
    )


def _model(cls, name, base_url):
    return cls(model_name=name, api_key="k", base_url=base_url, timeout=30)


# ── backend detection ────────────────────────────────────


@pytest.mark.unit
def test_backend_detection() -> None:
    assert image_backend_for_protocol("google gemini") == "GEMINI"
    assert image_backend_for_protocol("volcano engine（火山引擎）") == "ARK"
    assert image_backend_for_protocol("black forest labs（flux）") == "BFL"
    assert image_backend_for_protocol("ideogram") == "IDEOGRAM"
    # Labels reach the matcher exactly as the UI stores them; it casefolds
    # internally, so the request-scoped writer and the persisted-config
    # fallback can both pass one through untouched.
    assert image_backend_for_protocol("MiniMax（国内站）") == "MINIMAX"
    assert image_backend_for_protocol("MiniMax（国际站）") == "MINIMAX"
    assert _detect_backend_from_names("image-01", "") == "MINIMAX"
    assert (
        _detect_backend_from_names("", "https://api.minimax.io") == "MINIMAX"
    )
    assert (
        _detect_backend_from_names("", "https://api.minimax.cn") == "MINIMAX"
    )
    assert _detect_backend_from_names("gemini-3-pro-image", "") == "GEMINI"
    assert _detect_backend_from_names("", "https://api.bfl.ai") == "BFL"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cls", "name", "base", "count"),
    [
        (GeminiImageModel, "gemini-2.5-flash-image", "https://g", 4),
        (ArkImageModel, "doubao-seedream-5-0-pro-260628", "https://a", 11),
        (BFLImageModel, "flux-2-pro", "https://b", 9),
        (IdeogramImageModel, "ideogram-v4", "https://i", 1),
        (MiniMaxImageModel, "image-01", "https://m", 2),
    ],
)
def test_over_budget_references_are_rejected(cls, name, base, count) -> None:
    with pytest.raises(ModelError, match="reference images"):
        _model(cls, name, base)._enforce_reference_budget(count)


# ── request shapes ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_gemini_request_shape(monkeypatch) -> None:
    _stub_reference_reading(monkeypatch, gemini_provider)
    captured: dict = {}
    model = _model(
        GeminiImageModel,
        "gemini-3-pro-image",
        "https://generativelanguage.googleapis.com/v1beta",
    )
    asyncio.run(
        model._request(
            _CapturingClient(captured),
            "a cat",
            "16:9",
            ["/generated/ref.png"],
        ),
    )
    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3-pro-image:generateContent"
    )
    assert captured["headers"]["x-goog-api-key"] == "k"
    parts = captured["json"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[-1] == {"text": "a cat"}
    config = captured["json"]["generationConfig"]
    assert config["responseModalities"] == ["TEXT", "IMAGE"]
    assert config["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}

    # gemini-2.5-flash-image has a fixed 1024px output: no imageSize.
    model = _model(GeminiImageModel, "gemini-2.5-flash-image", "https://g")
    asyncio.run(model._request(_CapturingClient(captured), "a cat", "1:1", []))
    assert captured["json"]["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
    }


@pytest.mark.unit
def test_ark_request_shape(monkeypatch) -> None:
    _stub_reference_reading(monkeypatch, ark_provider)
    captured: dict = {}
    model = _model(
        ArkImageModel,
        "doubao-seedream-5-0-pro-260628",
        "https://ark.cn-beijing.volces.com",
    )
    asyncio.run(
        model._request(
            _CapturingClient(captured),
            "海报",
            "16:9",
            ["/generated/a.png", "https://cdn.example/b.png"],
        ),
    )
    assert captured["url"] == (
        "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    )
    body = captured["json"]
    # Official 2K tier pixel example for 16:9; watermark off; url result.
    assert body["size"] == "2848x1600"
    assert body["response_format"] == "url"
    assert body["watermark"] is False
    assert "sequential_image_generation" not in body
    assert body["image"][0].startswith("data:image/png;base64,")
    assert body["image"][1] == "https://cdn.example/b.png"


@pytest.mark.unit
def test_bfl_submit_and_poll(monkeypatch) -> None:
    _stub_reference_reading(monkeypatch, bfl_provider)
    captured: dict = {}

    class _BFLClient(_CapturingClient):
        async def post(self, url, headers=None, json=None):
            await super().post(url, headers=headers, json=json)
            return _StubResponse(
                {"id": "req-1", "polling_url": "https://api.bfl.ai/poll"},
            )

        async def get(self, _url, headers=None, **_kwargs):
            self._captured["poll_headers"] = headers
            return _StubResponse(
                {
                    "status": "Ready",
                    "result": {"sample": "https://delivery.bfl.ai/s.png"},
                },
            )

    model = _model(BFLImageModel, "flux-2-pro", "https://api.bfl.ai")
    response = asyncio.run(
        model._request(
            _BFLClient(captured),
            "an owl",
            "16:9",
            ["/generated/a.png", "https://cdn.example/b.png"],
        ),
    )
    assert captured["url"] == "https://api.bfl.ai/v1/flux-2-pro"
    assert captured["headers"]["x-key"] == "k"
    body = captured["json"]
    assert (body["width"], body["height"]) == (2048, 1152)
    # First reference is bare base64; the public URL passes through.
    assert body["input_image"] == base64.b64encode(_PNG).decode()
    assert body["input_image_2"] == "https://cdn.example/b.png"
    assert captured["poll_headers"]["x-key"] == "k"
    assert response.json()["status"] == "Ready"


@pytest.mark.unit
def test_ideogram_request_shapes(monkeypatch) -> None:
    _stub_reference_reading(monkeypatch, ideogram_provider)
    captured: dict = {}
    model = _model(
        IdeogramImageModel,
        "ideogram-v3",
        "https://api.ideogram.ai",
    )
    asyncio.run(
        model._request(
            _CapturingClient(captured),
            "poster",
            "16:9",
            ["/generated/a.png"],
        ),
    )
    assert captured["url"] == "https://api.ideogram.ai/v1/ideogram-v3/generate"
    assert captured["headers"] == {"Api-Key": "k"}
    assert captured["data"] == {
        "prompt": "poster",
        "aspect_ratio": "16x9",
        "rendering_speed": "DEFAULT",
    }
    field, (_name, content, mime) = captured["files"][0]
    assert field == "character_reference_images"
    assert (content, mime) == (_PNG, "image/png")

    # ideogram-v4 documents neither aspect_ratio nor references.
    model = _model(
        IdeogramImageModel,
        "ideogram-v4",
        "https://api.ideogram.ai",
    )
    asyncio.run(
        model._request(_CapturingClient(captured), "poster", "16:9", []),
    )
    assert captured["data"] == {
        "text_prompt": "poster",
        "rendering_speed": "DEFAULT",
    }
    assert captured["files"] is None


@pytest.mark.unit
def test_minimax_request_shape(monkeypatch) -> None:
    _stub_reference_reading(monkeypatch, minimax_provider)
    captured: dict = {}
    model = _model(MiniMaxImageModel, "image-01", "https://api.minimax.io")
    asyncio.run(
        model._request(
            _CapturingClient(captured),
            "a cat",
            "16:9",
            ["/generated/ref.png"],
        ),
    )
    assert captured["url"] == "https://api.minimax.io/v1/image_generation"
    assert captured["headers"]["Authorization"] == "Bearer k"
    body = captured["json"]
    assert body["model"] == "image-01"
    assert body["aspect_ratio"] == "16:9"
    assert body["response_format"] == "url"
    assert body["n"] == 1
    # A local reference has to reach the endpoint as a data URL: MiniMax
    # reads neither a URL nor a media type out of bare base64 and answers a
    # parameter error before generating anything.
    encoded = base64.b64encode(_PNG).decode()
    assert body["subject_reference"] == [
        {
            "type": "character",
            "image_file": f"data:image/png;base64,{encoded}",
        },
    ]


@pytest.mark.unit
def test_minimax_keeps_a_public_reference_as_url(monkeypatch) -> None:
    # The documented form is a network URL; inlining a file MiniMax can fetch
    # itself would only inflate the request body.
    _stub_reference_reading(monkeypatch, minimax_provider)
    captured: dict = {}
    model = _model(MiniMaxImageModel, "image-01", "https://api.minimax.io")
    asyncio.run(
        model._request(
            _CapturingClient(captured),
            "a cat",
            "1:1",
            ["https://cdn.example.com/ref.jpg"],
        ),
    )
    assert captured["json"]["subject_reference"] == [
        {
            "type": "character",
            "image_file": "https://cdn.example.com/ref.jpg",
        },
    ]


@pytest.mark.unit
def test_minimax_decode_url_base64_and_base_resp(monkeypatch) -> None:
    model = _model(MiniMaxImageModel, "image-01", "https://api.minimax.io")
    downloaded: list = []

    async def fake_download(url, _name):
        downloaded.append(url)
        return "/generated/img.png"

    monkeypatch.setattr(
        minimax_provider,
        "download_remote_image",
        fake_download,
    )
    result = asyncio.run(
        model._decode({"data": {"image_urls": ["https://cdn/x.png"]}}),
    )
    assert result == {"url": "/generated/img.png", "source_url": ""}
    assert downloaded == ["https://cdn/x.png"]

    # base64 payload path is persisted without a network fetch.
    png = base64.b64encode(_PNG).decode()
    persisted: list = []
    monkeypatch.setattr(
        minimax_provider,
        "persist_image_bytes",
        lambda content, _name, _src: persisted.append(content)
        or "/generated/img.png",
    )
    assert asyncio.run(model._decode({"data": {"image_base64": [png]}})) == {
        "url": "/generated/img.png",
        "source_url": "",
    }

    # HTTP 200 carrying a non-zero base_resp is still a failure.
    with pytest.raises(ModelError, match="1026"):
        asyncio.run(
            model._decode(
                {
                    "data": {},
                    "base_resp": {"status_code": 1026, "status_msg": "nsfw"},
                },
            ),
        )
