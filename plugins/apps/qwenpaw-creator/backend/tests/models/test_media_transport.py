# -*- coding: utf-8 -*-
# flake8: noqa: E501
# The upload doubles keep the real signature while asserting only the parts
# each case is about, so most of their parameters are deliberately unread.
# pylint: disable=protected-access,unused-argument
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import socket
from urllib.parse import urlparse

import pytest
from PIL import Image

from models import config as model_config
from models import image as image_backend
from models import media_transport
from models import vlm_model
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


def test_file_dispatcher_inlines_for_a_plain_openai_gateway(
    tmp_path,
    monkeypatch,
) -> None:
    """A third endpoint shape: neither the proxy nor DashScope gets the bytes.

    Neither remote upload works here - the Bailian credential is not this
    gateway's, and it cannot fetch an ``oss://`` object - so reaching for one
    is not a slower route but a guaranteed failure of media understanding.
    """
    media = tmp_path / "ref.png"
    media.write_bytes(b"png-bytes")
    calls: list[str] = []

    async def forbidden_platform(path, **_kwargs):
        calls.append("platform")
        raise AssertionError("the proxy upload is not reachable here")

    async def forbidden_temp(path, **_kwargs):
        calls.append("dashscope")
        raise AssertionError("the Bailian upload is not reachable here")

    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_platform_media",
        forbidden_platform,
    )
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_dashscope_temp",
        forbidden_temp,
    )

    url = asyncio.run(
        media_transport.upload_reference_file_for_provider(
            media,
            api_key="",
            model_name="gpt-vision",
            media_type="image/png",
            base_url="https://api.openai.example/v1",
        ),
    )

    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"png-bytes"
    assert not calls


def test_reference_upload_key_predicate_mirrors_the_three_routes() -> None:
    """Every remote transport presents a credential; only inlining does not.

    A caller that guards on the key has to agree with the dispatcher, or an
    uncredentialed request reaches a remote upload and is answered 401.
    """
    needs = media_transport.reference_transport_needs_upload_key

    assert needs("https://platform-pre.agentscope.io/v1")
    assert needs("https://gateway.internal/v1", "AgentScope Platform")
    assert needs("https://dashscope.aliyuncs.com/api/v1")
    assert not needs("https://api.openai.example/v1")


MAAS_HOST_CASES = {
    # A regional MaaS deployment serves the DashScope API under its own host.
    "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1": True,
    "https://maas.aliyuncs.com": True,
    # The bare host is what config stores for some endpoints.
    "maas.aliyuncs.com": True,
    "https://dashscope.aliyuncs.com/compatible-mode/v1": False,
    # Lookalikes must not select the DashScope transport: doing so would hand
    # the bearer key to whoever owns the real host of the request.
    "https://evil.example/.maas.aliyuncs.com": False,
    "https://maas.aliyuncs.com.evil.com/api/v1": False,
    "https://user:maas.aliyuncs.com@evil.com/v1": False,
    "not a url at all": False,
    "": False,
}


def test_maas_endpoint_predicate_is_one_hostname_decision() -> None:
    """The MaaS match reads the host, and reads it the same way everywhere.

    It lives in ``model_config`` because the media transport and the image
    backend both have to agree on which endpoint owns the temporary upload;
    two private substring matches is how they started disagreeing.
    """

    for base_url, expected in MAAS_HOST_CASES.items():
        assert model_config.is_maas_endpoint(base_url) is expected, base_url
        assert image_backend._is_maas_endpoint(base_url) is expected, base_url


def test_dashscope_transport_covers_maas_without_trusting_lookalikes() -> None:
    """MaaS media travels by the temporary upload; lookalikes do not."""

    dashscope_transport_cases = {
        "https://dashscope.aliyuncs.com/compatible-mode/v1": True,
        "https://dashscope-intl.aliyuncs.com/api/v1": True,
        "https://api.openai.example/v1": False,
        "https://evil.example/.maas.aliyuncs.com": False,
        "https://maas.aliyuncs.com.evil.com/api/v1": False,
        "": False,
    }
    for base_url, expected in dashscope_transport_cases.items():
        selected = vlm_model._is_dashscope_provider(base_url, "DashScope")
        assert selected is expected, base_url

    # A MaaS host proves the wire format, not the upload identity, so the
    # protocol label has to be able to take the ``oss://`` route back away.
    maas = "https://ws-1.cn-beijing.maas.aliyuncs.com/api/v1"
    assert vlm_model._is_dashscope_provider(maas, "DashScope") is True
    assert vlm_model._is_dashscope_provider(maas, "Aliyun Token Plan") is False


def test_maas_config_reaches_the_shared_dispatcher_as_a_dashscope_transport(
    tmp_path,
    monkeypatch,
) -> None:
    """A MaaS video reference must not be priced against the VLM budget.

    The dispatcher is the only place the three transports are decided, so this
    goes through it rather than through a predicate again: the reported
    regression was a 21 MiB clip that ``uses_dashscope_transport`` filed under
    "generic gateway", sending it to the inline Base64 refusal instead of the
    1 GB temporary OSS that same endpoint has always accepted.
    """

    calls: list[dict] = []

    async def fake_temp_upload(path, **kwargs):
        del path
        calls.append(kwargs)
        return "oss://dashscope-instant/reference.mp4"

    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_dashscope_temp",
        fake_temp_upload,
    )
    maas = "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
    assert media_transport.reference_transport_needs_upload_key(maas) is True

    clip = tmp_path / "reference.mp4"
    clip.write_bytes(b"\x00")
    with clip.open("ab") as handle:
        handle.truncate(21 * 1024 * 1024)
    assert clip.stat().st_size > 20 * 1024 * 1024

    url = asyncio.run(
        media_transport.upload_reference_file_for_provider(
            clip,
            api_key="sk-test",
            model_name="wan2.5-i2v",
            media_type="video/mp4",
            base_url=maas,
            protocol="DashScope",
        ),
    )

    assert url == "oss://dashscope-instant/reference.mp4"
    assert calls[0]["media_type"] == "video/mp4"
    assert calls[0]["model_name"] == "wan2.5-i2v"


def test_token_plan_media_stays_on_the_inline_budget(tmp_path, monkeypatch):
    """Token Plan credentials must never be offered the Bailian upload API.

    Reproduced from the review: a small PNG under a Token Plan MaaS
    configuration used to travel inline and reach the model. Treating every
    ``maas.aliyuncs.com`` host as upload-capable sent it to
    ``dashscope.aliyuncs.com/api/v1/uploads`` instead, which answers that key
    401 and turns a working configuration into a ModelError.
    """

    async def refuse_upload(path, **kwargs):
        del path, kwargs
        raise AssertionError("a Token Plan key reached the upload API")

    maas = "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_dashscope_temp",
        refuse_upload,
    )
    monkeypatch.setattr(
        vlm_model,
        "upload_local_file_to_dashscope_temp",
        refuse_upload,
    )
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    url = asyncio.run(
        vlm_model.transport_local_media_url(
            image,
            api_key="sk-token-plan",
            model_name="qwen3-vl-plus",
            base_url=maas,
            protocol="Aliyun Token Plan",
        ),
    )

    assert str(url).startswith("data:image/png;base64,")


class _PinnedUploadClient:
    """Records the exact request a guarded upload puts on the wire."""

    def __init__(self, observed, peer=None, status=200, text=""):
        self.observed = observed
        self.peer = peer
        self.status = status
        self.text = text

    def __call__(self, **kwargs):
        self.observed["client_kwargs"] = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, **kwargs):
        self.observed["url"] = url
        self.observed["kwargs"] = kwargs
        response = _FakeResponse(
            {"items": [{"url": "https://cdn.example.test/a.wav"}]},
        )
        response.status_code = self.status
        response.text = self.text
        if self.peer is not None:
            response.extensions = {
                "network_stream": _GuardedUploadStream(self.peer),
            }
        return response


class _GuardedUploadStream:
    """Names the peer the socket actually ended up talking to."""

    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, key):
        return (self.peer, 443) if key == "server_addr" else None


_METADATA_ADDRESSES = {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}


def _is_metadata_host(host: str) -> bool:
    """Judge an address the way the route's policy does, not as a string.

    The transport hands the policy every resolver answer as a literal, and a
    literal has several spellings: the route refuses an IPv4-mapped IPv6 as
    the endpoint it names, so a membership test here would hide that. Which
    spellings the real policy refuses is pinned by
    ``tests/api/test_model_connection_probe.py``; this double only has to
    notice what it was given.
    """
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    spellings = (address, getattr(address, "ipv4_mapped", None))
    return any(
        spelling is not None
        and (spelling.is_link_local or str(spelling) in _METADATA_ADDRESSES)
        for spelling in spellings
    )


def _recording_guard():
    """Stand in for the route's policy, judging what it is handed."""
    seen: list = []

    def guard(url):
        seen.append(url)
        host = urlparse(url).hostname or ""
        if _is_metadata_host(host):
            raise ValueError(f"forbidden address: {host}")

    return guard, seen


def _stub_resolver(monkeypatch, answers, *, explode=False):
    looked_up: list = []

    def fake_getaddrinfo(host, port, **kwargs):
        del kwargs
        looked_up.append(host)
        if explode:
            raise AssertionError("an unguarded upload must not resolve")
        return [(2, 1, 6, "", (address, port)) for address in answers]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return looked_up


def _addressed(upload_url, address):
    """The URL form the pin sends: the literal address as authority."""
    host = f"[{address}]" if ":" in address else address
    return upload_url.replace("gateway.example.test", f"{host}:443", 1)


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "fe80::ec2:254", "::ffff:100.100.100.200"],
)
def test_guarded_upload_refuses_a_name_that_resolves_to_metadata(
    monkeypatch,
    address,
) -> None:
    """The policy judges a hostname as a hostname; the socket judges an address.

    Reproduced from the review: the strict upload checked only the URL string,
    so a configured host resolving to the cloud-metadata address still got the
    bearer token and the audio, and the connected peer was never read.
    """
    observed: dict = {}
    guard, seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed),
    )
    _stub_resolver(monkeypatch, [address])

    with pytest.raises(ValueError, match="forbidden address"):
        media_transport._upload_bytes_to_platform_media_sync(
            b"RIFF" + bytes(44),
            "probe.wav",
            api_key="sk-platform",
            base_url="https://gateway.example.test/v1",
            media_type="audio/wav",
            destination_guard=guard,
        )

    # Nothing reached the wire, and the resolution was checked as an address.
    assert "url" not in observed
    assert seen[0] == "https://gateway.example.test/v1/media/uploads"
    assert seen[-1] == _addressed(
        "https://gateway.example.test/v1/media/uploads",
        address,
    )


def test_guarded_upload_judges_every_answer_the_resolver_returns(
    monkeypatch,
) -> None:
    """A safe first address does not excuse a hostile second one.

    The pin picks one, but the policy has to see all of them: otherwise a name
    whose records mix a public address with the metadata one would be bound to
    whichever the resolver happened to list first.
    """
    observed: dict = {}
    guard, seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed),
    )
    _stub_resolver(monkeypatch, ["93.184.216.34", "169.254.169.254"])

    with pytest.raises(ValueError, match="forbidden address"):
        media_transport._upload_bytes_to_platform_media_sync(
            b"RIFF" + bytes(44),
            "probe.wav",
            api_key="sk-platform",
            base_url="https://gateway.example.test/v1",
            media_type="audio/wav",
            destination_guard=guard,
        )

    assert "url" not in observed
    assert len(seen) == 3


def test_guarded_upload_sends_to_the_validated_literal_under_the_configured_host(
    monkeypatch,
) -> None:
    """Binding the address may not change what the provider is told.

    The request goes to the literal the policy accepted, while the ``Host``
    header and the TLS server name stay the endpoint the user configured — a
    pinned request that looked like a request to an IP address would break the
    very proxy it is meant to reach.
    """
    observed: dict = {}
    guard, seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed, peer="93.184.216.34"),
    )
    _stub_resolver(monkeypatch, ["93.184.216.34"])

    url = media_transport._upload_bytes_to_platform_media_sync(
        b"RIFF" + bytes(44),
        "probe.wav",
        api_key="sk-platform",
        base_url="https://gateway.example.test/v1",
        media_type="audio/wav",
        destination_guard=guard,
    )

    assert url == "https://cdn.example.test/a.wav"
    assert observed["url"] == "https://93.184.216.34:443/v1/media/uploads"
    headers = observed["kwargs"]["headers"]
    assert headers["Host"] == "gateway.example.test"
    assert headers["Authorization"] == "Bearer sk-platform"
    assert observed["kwargs"]["extensions"] == {
        "sni_hostname": "gateway.example.test",
    }
    # The peer was read and judged by the same policy as the answers.
    assert seen[-1] == "https://93.184.216.34:443/v1/media/uploads"


def test_guarded_upload_wraps_an_ipv6_answer_in_brackets(monkeypatch) -> None:
    observed: dict = {}
    guard, _seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed, peer="2001:db8::1"),
    )
    _stub_resolver(monkeypatch, ["2001:db8::1"])

    media_transport._upload_bytes_to_platform_media_sync(
        b"RIFF" + bytes(44),
        "probe.wav",
        api_key="sk-platform",
        base_url="https://gateway.example.test/v1",
        media_type="audio/wav",
        destination_guard=guard,
    )

    assert observed["url"] == "https://[2001:db8::1]:443/v1/media/uploads"


def test_guarded_upload_refuses_a_peer_that_was_never_an_answer(
    monkeypatch,
) -> None:
    """A transport that ignored the pin, or a proxy in between, still gets caught."""
    observed: dict = {}
    guard, _seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed, peer="203.0.113.7"),
    )
    _stub_resolver(monkeypatch, ["93.184.216.34"])

    with pytest.raises(RuntimeError, match="was not resolved to"):
        media_transport._upload_bytes_to_platform_media_sync(
            b"RIFF" + bytes(44),
            "probe.wav",
            api_key="sk-platform",
            base_url="https://gateway.example.test/v1",
            media_type="audio/wav",
            destination_guard=guard,
        )

    assert "url" in observed  # the peer is read from the reply it answered


def test_guarded_upload_failure_names_the_configured_host(monkeypatch) -> None:
    """An operator reads their own endpoint in the message, not one address."""
    observed: dict = {}
    guard, _seen = _recording_guard()
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(
            observed,
            peer="93.184.216.34",
            status=500,
            text="backend exploded",
        ),
    )
    _stub_resolver(monkeypatch, ["93.184.216.34"])

    with pytest.raises(RuntimeError) as excinfo:
        media_transport._upload_bytes_to_platform_media_sync(
            b"RIFF" + bytes(44),
            "probe.wav",
            api_key="sk-platform",
            base_url="https://gateway.example.test/v1",
            media_type="audio/wav",
            destination_guard=guard,
        )

    assert "https://gateway.example.test/v1/media/uploads" in str(
        excinfo.value,
    )
    assert "93.184.216.34" not in str(excinfo.value)


def test_unguarded_production_upload_still_resolves_on_its_own(
    tmp_path,
    monkeypatch,
) -> None:
    """Nothing about the shared production path may change.

    The binding exists for the probe's policy. Ordinary uploads keep their
    own resolution, their cache and the URL the provider expects to see.
    """
    sample = tmp_path / "reference.wav"
    sample.write_bytes(b"RIFF" + bytes(44))
    observed: dict = {}
    monkeypatch.setattr(
        media_transport.httpx,
        "Client",
        _PinnedUploadClient(observed),
    )
    looked_up = _stub_resolver(monkeypatch, ["93.184.216.34"], explode=True)

    url = media_transport._upload_file_to_platform_media_sync(
        sample,
        api_key="sk-platform",
        base_url="https://gateway.example.test/v1",
        media_type="audio/wav",
    )

    assert not looked_up
    assert url == "https://cdn.example.test/a.wav"
    assert observed["url"] == "https://gateway.example.test/v1/media/uploads"
    assert "Host" not in observed["kwargs"]["headers"]
    # One client per upload, with redirects left on for the proxy's own path.
    assert observed["client_kwargs"]["follow_redirects"] is True


def test_oversized_local_video_is_refused_before_its_bytes_are_read(
    tmp_path,
    monkeypatch,
) -> None:
    """An over-budget non-image is refused on its declared type alone.

    Only an image can come back smaller from the downscaling ladder, and
    finding that out costs reading the whole file: measured against the merge
    base, a 128MiB clip peaked at ~134MB of Python allocations before raising
    the very same refusal, which defeats the budget that exists to bound
    inline memory. ``read_bytes`` is patched to record every call, so this
    fails again the moment the payload is pulled in first.
    """
    import os
    from pathlib import Path as _Path

    from utils.exceptions import ModelError

    video = tmp_path / "clip.mp4"
    video.touch()
    os.truncate(video, 64 * 1024 * 1024)  # sparse: size without the bytes
    monkeypatch.setattr(
        vlm_model.model_config,
        "get_vlm_max_inline_bytes",
        lambda: 1024,
    )
    reads: list[str] = []
    real_read_bytes = _Path.read_bytes

    def record_read(self, *args, **kwargs):
        reads.append(self.name)
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "read_bytes", record_read)

    with pytest.raises(ModelError, match="inline size limit exceeded"):
        asyncio.run(
            vlm_model.transport_local_media_url(
                video,
                api_key="vlm-key",
                model_name="qwen-vl-max",
                media_type="video/mp4",
                base_url="https://openai-compatible.example/v1",
                protocol="\u81ea\u5b9a\u4e49",
            ),
        )

    assert not reads
