# -*- coding: utf-8 -*-
# flake8: noqa: E501
# The upload doubles keep the real signature while asserting only the parts
# each case is about, so most of their parameters are deliberately unread.
# pylint: disable=protected-access,unused-argument
from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image

from models import media_transport
from models.media_transport import (
    SEEDANCE_REFERENCE_IMAGE_MAX_BYTES,
    _fetch_dashscope_upload_policy,
    _mask_key,
    _upload_local_file_to_dashscope_temp_sync,
    read_reference_media,
    reference_media_data_url,
)


def test_media_transport_api_key_mask_never_reveals_a_secret_fragment() -> (
    None
):
    secret = "sk-sensitive-prefix-and-private-suffix"

    assert _mask_key(secret) == "[redacted]"
    assert _mask_key("") == "(empty)"


def test_upload_policy_log_never_contains_temporary_credentials(
    monkeypatch,
) -> None:
    temporary_secret = "temporary-policy-signature-must-not-reach-logs"
    emitted: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        media_transport.logger,
        "info",
        lambda *args, **_kwargs: emitted.append(args),
    )

    class PolicyClient:
        def get(self, *_args, **_kwargs):
            response = _FakeResponse(
                {
                    "data": {
                        "max_file_size_mb": 1024,
                        "upload_dir": "dashscope-instant/account/date/id",
                        "upload_host": "https://upload.example.test",
                        "oss_access_key_id": "temporary-access",
                        "signature": temporary_secret,
                        "policy": "temporary-policy",
                    },
                },
            )
            response.text = temporary_secret
            return response

    policy = _fetch_dashscope_upload_policy(
        PolicyClient(),
        api_key="provider-key",
        model_name="qwen-image-2.0-pro",
        size=128,
    )

    assert policy["signature"] == temporary_secret
    assert temporary_secret not in repr(emitted)
    assert "provider-key" not in repr(emitted)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color="blue").save(output, format="PNG")
    return output.getvalue()


class _FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeUploadClient:
    """Shared policy-GET + upload-POST stub for the dashscope temp endpoint."""

    def __init__(self, observed: dict):
        self.observed = observed

    def __call__(self, **kwargs):
        self.observed["client_kwargs"] = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, *, params, headers):
        self.observed["policy_request"] = (url, params, headers)
        return _FakeResponse(
            {
                "data": {
                    "max_file_size_mb": 1024,
                    "upload_dir": "dashscope-instant/account/date/id",
                    "upload_host": "https://upload.example.test",
                    "oss_access_key_id": "temporary-access",
                    "signature": "temporary-signature",
                    "policy": "temporary-policy",
                },
            },
        )

    def post(self, url, *, data, files):
        filename, file_source, media_type = files["file"]
        is_bytes = isinstance(file_source, (bytes, bytearray))
        self.observed["upload"] = {
            "url": url,
            "data": dict(data),
            "filename": filename,
            "media_type": media_type,
            "file_source": file_source,
            "is_bytes": is_bytes,
            # the handle is only readable while the upload is in flight
            "first_byte": None if is_bytes else file_source.read(1),
        }
        return _FakeResponse()


def test_dashscope_temporary_upload_streams_file_handle_instead_of_loading_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    video = tmp_path / "large-local-video.mp4"
    with video.open("wb") as stream:
        stream.seek(64 * 1024 * 1024 - 1)
        stream.write(b"\0")
    observed: dict = {}
    monkeypatch.setattr(
        "models.media_transport.httpx.Client",
        _FakeUploadClient(observed),
    )

    url = _upload_local_file_to_dashscope_temp_sync(
        video,
        api_key="test-api-key",
        model_name="qwen3.7-plus",
        media_type="video/mp4",
    )

    assert url.startswith("oss://dashscope-instant/account/date/id/")
    assert observed["policy_request"][1] == {
        "action": "getPolicy",
        "model": "qwen3.7-plus",
    }
    upload = observed["upload"]
    assert upload["is_bytes"] is False
    assert upload["first_byte"] == b"\0"
    assert upload["filename"].endswith(".mp4")
    assert upload["media_type"] == "video/mp4"
    assert upload["data"]["success_action_status"] == "200"


def test_reference_media_data_url_inlines_png_for_seedance() -> None:
    content = _png_bytes()

    data_url = reference_media_data_url(content, "reference.png")

    prefix = "data:image/png;base64,"
    assert data_url.startswith(prefix)
    assert base64.b64decode(data_url[len(prefix) :]) == content
    # Without an extension the media type is sniffed from the magic bytes.
    assert reference_media_data_url(content, "reference").startswith(prefix)


def test_reference_media_data_url_rejects_oversized_media() -> None:
    oversized = b"\0" * SEEDANCE_REFERENCE_IMAGE_MAX_BYTES

    with pytest.raises(RuntimeError, match="30MB"):
        reference_media_data_url(oversized, "reference.png")


def test_reference_media_uses_bounded_safe_remote_download(
    monkeypatch,
) -> None:
    observed = {}

    def download(url, *, max_bytes, timeout):
        observed.update(url=url, max_bytes=max_bytes, timeout=timeout)
        return _png_bytes()

    monkeypatch.setattr("models.media_transport.safe_download_bytes", download)

    content, filename = asyncio.run(
        read_reference_media(
            "https://public.example/reference.png",
            max_bytes=1234,
        ),
    )

    assert content == _png_bytes()
    assert filename == "reference.png"
    assert observed == {
        "url": "https://public.example/reference.png",
        "max_bytes": 1234,
        "timeout": 60.0,
    }


def test_reference_media_checks_local_size_before_reading(tmp_path) -> None:
    reference = tmp_path / "too-large.png"
    reference.write_bytes(b"123456")

    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        asyncio.run(read_reference_media(reference.as_uri(), max_bytes=5))


@pytest.mark.parametrize(
    ("values", "expected_status"),
    [
        (("", "", ""), "absent"),
        (("id", "secret", "https://oss.example.test"), "ready"),
        (("id", "", ""), "invalid"),
    ],
)
def test_creator_oss_readiness_has_explicit_three_states(
    monkeypatch,
    values,
    expected_status,
) -> None:
    access_id, access_secret, endpoint = values
    monkeypatch.setattr(
        media_transport.model_config,
        "get_oss_access_key_id",
        lambda: access_id,
    )
    monkeypatch.setattr(
        media_transport.model_config,
        "get_oss_access_key_secret",
        lambda: access_secret,
    )
    monkeypatch.setattr(
        media_transport.model_config,
        "get_oss_endpoint",
        lambda: endpoint,
    )
    monkeypatch.setattr(
        media_transport.model_config,
        "get_oss_bucket",
        lambda default: default,
    )
    monkeypatch.setattr(
        media_transport.model_config,
        "get_oss_public_base_url",
        lambda: "",
    )

    readiness = media_transport.creator_oss_readiness()

    assert readiness["status"] == expected_status


# ── AgentScope model-proxy media upload ────────────────────────────────────
# The proxy's sk-as key has no identity on the Bailian temporary-upload API, so
# these cover the only transport that can hand it a reference it can fetch.


class _PlatformUploadClient:
    """POST stub for the proxy's own ``/v1/media/uploads`` endpoint."""

    def __init__(self, observed, payload=None, status=200, text=""):
        self.observed = observed
        self.payload = payload
        self.status = status
        self.text = text

    def post(self, url, **kwargs):
        self.observed["url"] = url
        self.observed["kwargs"] = kwargs
        response = _FakeResponse(self.payload)
        response.status_code = self.status
        response.text = self.text
        return response


@pytest.mark.parametrize(
    "base_url",
    (
        "https://platform-pre.agentscope.io/v1",
        "https://platform-pre.agentscope.io/v1/",
        # The image section also accepts the full generation endpoint as base.
        "https://platform-pre.agentscope.io/v1"
        "/services/aigc/multimodal-generation/generation",
    ),
)
def test_platform_upload_endpoint_is_rebuilt_from_the_origin(base_url) -> None:
    assert (
        media_transport.platform_media_upload_url(base_url)
        == "https://platform-pre.agentscope.io/v1/media/uploads"
    )


def test_post_platform_upload_uses_multipart_and_returns_the_public_url() -> (
    None
):
    observed: dict = {}
    client = _PlatformUploadClient(
        observed,
        payload={
            "items": [
                {
                    "url": "https://bucket.oss-accelerate.aliyuncs.com"
                    "/proxy-media/a.png",
                    "mime": "image/png",
                    "bytes": 84,
                },
            ],
        },
    )

    url = media_transport._post_platform_media_upload(
        client,
        base_url="https://platform-pre.agentscope.io/v1",
        api_key="sk-as-test",
        filename="a.png",
        file_source=b"whatever",
        media_type="image/png",
    )

    assert (
        url == "https://bucket.oss-accelerate.aliyuncs.com/proxy-media/a.png"
    )
    assert observed["url"] == (
        "https://platform-pre.agentscope.io/v1/media/uploads"
    )
    # Multipart, not a Base64 body: reference frames run to megabytes and the
    # provider's non-media routes sit behind a much smaller body limit.
    assert "file" in observed["kwargs"]["files"]
    assert (
        observed["kwargs"]["headers"]["Authorization"] == "Bearer sk-as-test"
    )


@pytest.mark.parametrize(
    ("payload", "status", "text", "expected"),
    (
        ({"items": []}, 200, "", "no URL"),
        ({"items": [{"url": ""}]}, 200, "", "no URL"),
        (
            None,
            401,
            '{"error":{"code":"ASP.PROXY.API_KEY_MISSING"}}',
            "HTTP 401",
        ),
    ),
)
def test_post_platform_upload_fails_loudly_without_a_url(
    payload,
    status,
    text,
    expected,
) -> None:
    client = _PlatformUploadClient(
        {},
        payload=payload,
        status=status,
        text=text,
    )

    with pytest.raises(RuntimeError) as excinfo:
        media_transport._post_platform_media_upload(
            client,
            base_url="https://platform-pre.agentscope.io/v1",
            api_key="sk-as-secret-value",
            filename="a.png",
            file_source=b"whatever",
            media_type="image/png",
        )

    message = str(excinfo.value)
    assert expected in message
    # A failure this actionable must name itself, but never by echoing the
    # credential that is already in the request header.
    assert "sk-as-secret-value" not in message


def test_bytes_dispatcher_prefers_the_proxy_upload_channel(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_platform(
        content,
        filename,
        *,
        api_key,
        base_url,
        media_type,
    ):
        calls.append(("platform", media_type))
        return "https://cdn.test/proxy-media/a.png"

    async def fake_temp(content, filename, *, api_key, model_name):
        calls.append(("dashscope", model_name))
        return "oss://dashscope-instant/a.png"

    monkeypatch.setattr(
        media_transport,
        "upload_reference_bytes_to_platform_media",
        fake_platform,
    )
    monkeypatch.setattr(
        media_transport,
        "upload_reference_bytes_to_dashscope_temp",
        fake_temp,
    )

    url = asyncio.run(
        media_transport.upload_reference_bytes_for_provider(
            b"png-bytes",
            "a.png",
            api_key="sk-as-test",
            model_name="qwen-image-3.0",
            base_url="https://platform-pre.agentscope.io/v1",
        ),
    )

    assert url == "https://cdn.test/proxy-media/a.png"
    assert calls == [("platform", "image/png")]


def test_file_dispatcher_keeps_bailian_upload_for_dashscope(
    tmp_path,
    monkeypatch,
) -> None:
    media = tmp_path / "ref.png"
    media.write_bytes(b"png-bytes")
    calls: list[str] = []

    async def fake_platform(path, *, api_key, base_url, media_type):
        calls.append("platform")
        return "https://cdn.test/proxy-media/ref.png"

    async def fake_temp(path, *, api_key, model_name, media_type):
        calls.append("dashscope")
        return "oss://dashscope-instant/ref.png"

    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_platform_media",
        fake_platform,
    )
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_dashscope_temp",
        fake_temp,
    )

    url = asyncio.run(
        media_transport.upload_reference_file_for_provider(
            media,
            api_key="sk-test",
            model_name="wan2.7-r2v",
            media_type="image/png",
            base_url="https://dashscope.aliyuncs.com/api/v1",
        ),
    )

    assert url == "oss://dashscope-instant/ref.png"
    assert calls == ["dashscope"]


def test_agentscope_predicate_never_trusts_a_lookalike_url() -> None:
    config = media_transport.model_config

    assert config.is_agentscope_endpoint(
        "https://platform-pre.agentscope.io/v1",
    )
    assert config.is_agentscope_endpoint("agentscope.io/v1")
    # A path segment must not select a different media transport.
    assert not config.is_agentscope_endpoint(
        "https://evil.example/agentscope.io/v1",
    )
    assert not config.is_agentscope_endpoint(
        "https://dashscope.aliyuncs.com/api/v1",
    )
    assert not config.is_agentscope_endpoint("")
    # A saved protocol label covers a re-hosted gateway on another hostname.
    assert config.is_agentscope_gateway(
        protocol="AgentScope Platform",
        base_url="https://gateway.internal/v1",
    )
    assert not config.is_agentscope_gateway(
        protocol="DashScope（百炼）",
        base_url="https://dashscope.aliyuncs.com/api/v1",
    )
