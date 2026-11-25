# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=redefined-outer-name,unused-argument
# ``config_path`` is an environmental fixture: the test body never reads it,
# but without it these writes would land on the developer's real config.
"""Platform one-click configuration: the browser-fetched key and the preset
model land on the proxy-served sections only, and never on a section the
proxy cannot serve."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from api import model_routes
from api.model_routes import _probe_payload
from domain.errors import ValidationError
from models import media_transport
from test_model_connection_probe import (
    _ProbeConfig,
    _probe,
    _request,
)

_CHAT_URL = "https://platform-pre.agentscope.io/v1/chat/completions"


def _section(**overrides) -> dict:
    section = {
        "enabled": False,
        "model_name": "",
        "api_key": "",
        "base_url": "",
        "protocol": "OpenAI 协议",
        "custom_protocol": "",
    }
    section.update(overrides)
    return section


def _config() -> dict:
    """A fresh container: only the text lane has a model chosen."""
    return {
        "llm": _section(
            enabled=True,
            model_name="qwen3.8-flash",
            api_key="old",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        "vlm": _section(use_llm=True),
        "image": _section(),
        "video": _section(),
        "tts": _section(protocol="DashScope（百炼）"),
        "asr": _section(),
        "s2v": _section(
            protocol="DashScope（百炼）",
            base_url="https://dashscope.aliyuncs.com/api/v1",
            api_key="bailian-key",
        ),
        "embedding": _section(
            protocol="DashScope（百炼）",
            base_url="https://dashscope.aliyuncs.com/api/v1",
            api_key="bailian-key",
        ),
        "grounding": _section(reuse_llm=True, tavily_api_key="tvly"),
    }


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    path = (tmp_path / "config" / "model_config.json").resolve()
    monkeypatch.setenv("CREATOR_MODEL_CONFIG_PATH", str(path))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_config()), encoding="utf-8")
    return path


def _apply(api_key: str = "sk-as-issued", chat_url: str = _CHAT_URL) -> dict:
    return asyncio.run(
        model_routes.platform_autoconfigure(
            {"api_key": api_key, "chat_completions_url": chat_url},
        ),
    )


def test_autoconfigure_applies_the_key_and_presets_to_proxied_sections(
    config_path,
) -> None:
    result = _apply()

    loaded = model_routes.load_model_config(include_environment=False)
    for name, preset in (
        ("llm", "qwen3.8-flash"),
        ("vlm", "qwen3.8-flash"),
        ("image", "qwen-image-3.0"),
        ("video", "wan3.0-video"),
        ("tts", "qwen-audio-3.0-tts-flash"),
        ("asr", "qwen-audio-3.0-asr-flash"),
    ):
        item = getattr(loaded, name)
        assert item.protocol == "AgentScope Platform", name
        assert item.api_key == "sk-as-issued", name
        assert item.base_url == "https://platform-pre.agentscope.io/v1", name
        assert item.enabled is True, name
        assert item.model_name == preset, name

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["video"]["base_url"].endswith("/v1")

    for name in ("s2v", "embedding"):
        item = getattr(loaded, name)
        assert item.protocol == "DashScope（百炼）", name
        assert item.api_key == "bailian-key", name
    assert loaded.grounding.tavily_api_key == "tvly"

    by_section = {row["section"]: row for row in result["sections"]}
    assert by_section["llm"] == {
        "section": "llm",
        "model_name": "qwen3.8-flash",
        "ready": True,
        "replaced": False,
    }
    # Reusing the LLM does not count as replacing the VLM's model.
    assert by_section["vlm"]["replaced"] is False
    assert by_section["image"]["replaced"] is True
    assert by_section["tts"]["replaced"] is True
    assert by_section["tts"]["ready"] is True


def test_a_foreign_model_is_reset_while_a_valid_choice_survives(
    config_path,
) -> None:
    # ``fun-asr`` is a leftover from the Bailian lane and is not proxied, so
    # keeping it would fail on the first call. ``happyhorse-1.1`` is on the
    # platform, just not the default: that is an operator choice.
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored["asr"]["model_name"] = "fun-asr"
    stored["video"]["model_name"] = "happyhorse-1.1"
    config_path.write_text(json.dumps(stored), encoding="utf-8")

    result = _apply()

    loaded = model_routes.load_model_config(include_environment=False)
    assert loaded.asr.model_name == "qwen-audio-3.0-asr-flash"
    assert loaded.video.model_name == "happyhorse-1.1"

    by_section = {row["section"]: row for row in result["sections"]}
    assert by_section["asr"]["replaced"] is True
    assert by_section["video"]["replaced"] is False


def test_a_foreign_endpoint_is_refused_before_it_lands(config_path) -> None:
    # This value selects the media transport and the retry verdict of every
    # section it is written into, so a lookalike URL must not pass.
    with pytest.raises(ValidationError):
        _apply(chat_url="https://evil.test/v1/chat/completions")

    loaded = model_routes.load_model_config(include_environment=False)
    assert loaded.llm.api_key == "old"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"api_key": "", "chat_completions_url": _CHAT_URL}, "api_key"),
        ({"api_key": "sk-as-issued", "chat_completions_url": ""}, "chat"),
    ],
)
def test_an_incomplete_platform_response_writes_nothing(
    config_path,
    payload,
    expected,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(model_routes.platform_autoconfigure(payload))

    assert expected in str(excinfo.value)
    loaded = model_routes.load_model_config(include_environment=False)
    assert loaded.llm.api_key == "old"


@pytest.mark.parametrize(
    ("type_", "model_name"),
    [
        ("llm", "qwen3.8-flash"),
        ("vlm", "qwen3.8-max"),
        ("image", "qwen-image-3.0"),
        # ASR is absent on purpose: its probe is built by the async entry,
        # which uploads the sample and transcribes it.  Asserting a free
        # listing here measured a route the endpoint never takes.
        ("tts", "qwen-audio-3.0-tts-flash"),
    ],
)
def test_agentscope_gateway_probe_lists_models_and_never_completes(
    type_,
    model_name,
) -> None:
    """Every read-only section of the proxy probes for free.

    The chat route bills even at ``max_tokens=8``, and a Credits-exhausted
    account answers it with a 403 that looks exactly like a permission error -
    so a chat probe cannot tell a broken configuration from an empty balance.
    """
    url, headers, payload = _probe_payload(
        _request(
            type=type_,
            model_name=model_name,
            protocol="AgentScope Platform",
            base_url="https://platform-pre.agentscope.io/v1",
            provider=None,
        ),
    )

    assert url == "https://platform-pre.agentscope.io/v1/models"
    assert payload == {"_get_probe": True}
    assert "chat/completions" not in url
    assert headers["Authorization"] == "Bearer sk-test"


def test_agentscope_probe_is_keyed_on_the_host_not_the_label() -> None:
    # Recognition accepts the proxy host as well as the protocol label, so
    # keeping a custom label and pasting the proxy endpoint is still probed
    # for free instead of falling through to the billable chat completion.
    url, _headers, payload = _probe_payload(
        _request(
            type="llm",
            model_name="qwen3.8-flash",
            protocol="自定义",
            base_url="https://platform-pre.agentscope.io/v1",
            provider=None,
        ),
    )

    assert url == "https://platform-pre.agentscope.io/v1/models"
    assert payload == {"_get_probe": True}


def test_agentscope_video_probe_keeps_the_capability_guard() -> None:
    # Video runs the local capability guard first, then the free probe: the
    # Bailian fall-through would ping the temporary-upload policy API,
    # which answers 401 InvalidApiKey for a proxy key.
    url, _headers, payload = _probe_payload(
        _request(
            type="video",
            model_name="happyhorse-1.1",
            protocol="AgentScope Platform",
            base_url="https://platform-pre.agentscope.io/v1",
            provider=None,
        ),
    )

    assert url == "https://platform-pre.agentscope.io/v1/models"
    assert payload == {"_get_probe": True}
    assert "/uploads" not in url

    with pytest.raises(ValueError) as excinfo:
        _probe_payload(
            _request(
                type="video",
                model_name="not-a-real-model",
                protocol="AgentScope Platform",
                base_url="https://platform-pre.agentscope.io/v1",
                provider=None,
            ),
        )
    assert "VIDEO_MODEL_CAPABILITY_UNKNOWN" in str(excinfo.value)


def test_platform_asr_probe_stages_its_sample_the_way_production_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test button has to earn the URL it is about to send.

    A platform config cannot mint an ``oss://`` reference: the model proxy has
    no identity on the Bailian upload-policy API (measured ``401``), so going
    straight for the DashScope temporary OSS — as this probe did — failed the
    staging step before the request under test was ever put on the wire, and
    the route it then named is one the platform does not deploy. Everything
    here runs through the real endpoint, because the sync helper the earlier
    platform tests called never reached this code at all.
    """

    sent: dict[str, object] = {}
    staged: list[str] = []

    def refuse_dashscope(*_args, **_kwargs):
        raise AssertionError("a platform config must not mint an oss:// URL")

    async def platform_upload(path, **kwargs):
        del kwargs
        staged.append(path.name)
        return "https://platform-pre.agentscope.io/media/creator-probe.wav"

    async def fake_send(client, url, headers, payload):
        del client, headers
        sent["url"] = url
        sent["payload"] = payload
        return httpx.Response(200, json={"output": {"choices": []}})

    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_dashscope_temp",
        refuse_dashscope,
    )
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_platform_media",
        platform_upload,
    )
    monkeypatch.setattr(model_routes, "load_model_config", _ProbeConfig)
    monkeypatch.setattr(model_routes, "_send_probe", fake_send)

    result = asyncio.run(
        model_routes.test_model_connection(
            _request(
                type="asr",
                base_url="https://platform-pre.agentscope.io/v1",
                api_key="sk-as-test",
                model_name="qwen-audio-3.0-asr-flash",
                protocol="AgentScope Platform",
                provider="fun-asr",
            ),
        ),
    )

    assert staged == ["creator-probe.wav"]
    assert result.ok is True
    # The route the production classifier picks for this model name.
    assert sent["url"] == (
        "https://platform-pre.agentscope.io/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    assert sent["payload"]["model"] == "qwen-audio-3.0-asr-flash"
    assert sent["payload"]["_attests"] == "asr-qwen3"


@pytest.mark.parametrize(
    ("saved_protocol", "draft_protocol", "expected_path"),
    [
        # The dialog was switched to the platform and not saved yet: the
        # proxy serves this route under ``/v1``, while the saved token-portal
        # label points at an ``/api/v1`` path the platform never deploys.
        (
            "DashScope Fun-ASR",
            "AgentScope Platform",
            "/v1/services/aigc/multimodal-generation/generation",
        ),
        (
            "AgentScope Platform",
            "DashScope Fun-ASR",
            "/api/v1/services/aigc/multimodal-generation/generation",
        ),
    ],
)
def test_asr_probe_derives_its_route_from_the_draft_protocol(
    monkeypatch: pytest.MonkeyPatch,
    saved_protocol: str,
    draft_protocol: str,
    expected_path: str,
) -> None:
    """A probe tests the configuration in the dialog, not the saved one.

    ``_qwen3_endpoint`` used to read the persisted ASR protocol. On the
    official platform host the hostname identifies the gateway anyway, which
    kept the stale read invisible; behind a custom proxy domain the protocol
    label is the only thing saying which prefix the route lives under, and
    the probe posted to a path the configuration under test never serves.
    """

    async def fake_platform_upload(path, **kwargs):
        del path, kwargs
        return "https://gateway.invalid/media/creator-probe.wav"

    monkeypatch.setattr(
        model_routes.model_config,
        "get_asr_protocol",
        lambda: saved_protocol,
    )
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_platform_media",
        fake_platform_upload,
    )

    url, _headers, payload = _probe(
        _request(
            base_url="https://gateway.invalid/v1",
            model_name="qwen-audio-3.0-asr-flash",
            protocol=draft_protocol,
        ),
    )

    assert url.endswith(expected_path)
    assert payload["_attests"] == "asr-qwen3"


def test_platform_probe_upload_refuses_a_redirect_off_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sample upload obeys the redirect policy the probe request uses.

    Production follows redirects because the proxy is the operator's own. A
    probe carrying the configured bearer may not be moved to a host nobody
    named, so it asks for the strict client and reports the move instead of
    quietly testing somewhere else.
    """

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("host", "")))
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(
            *args,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    async def refuse_send(client, url, headers, payload):
        del client, url, headers, payload
        raise AssertionError("the model request ran after a refused redirect")

    # A guarded upload binds an address before the redirect policy can be
    # trusted, so the configured name is resolved exactly as it would be on a
    # socket — here to an answer the destination policy accepts.
    def fake_getaddrinfo(host, port, **kwargs):
        del host, kwargs
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(media_transport.httpx, "Client", client_factory)
    monkeypatch.setattr(model_routes, "load_model_config", _ProbeConfig)
    monkeypatch.setattr(model_routes, "_send_probe", refuse_send)

    result = asyncio.run(
        model_routes.test_model_connection(
            _request(
                base_url="https://gateway.invalid/v1",
                model_name="qwen-audio-3.0-asr-flash",
                protocol="AgentScope Platform",
            ),
        ),
    )

    assert result.ok is False
    # One request only: the 302 was never followed to the metadata service.
    # It went to the address the policy validated, still named by the host the
    # operator configured.
    assert len(seen) == 1
    assert "93.184.216.34" in seen[0][0]
    assert seen[0][0].endswith("/v1/media/uploads")
    assert seen[0][1] == "gateway.invalid"
    assert "redirected" in (result.error or "")


def test_platform_probe_upload_carries_the_destination_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata address is refused before the upload, not after it.

    The upload target is derived from the same base URL as the model request,
    so it used to reach the address the policy forbids with the bearer
    attached and still report a refusal at the end. Nothing may leave.
    """

    def forbid(*args, **kwargs):
        del args, kwargs
        raise AssertionError("a probe upload client was opened")

    async def refuse_send(client, url, headers, payload):
        del client, url, headers, payload
        raise AssertionError("the model request ran after a refused target")

    monkeypatch.setattr(media_transport.httpx, "Client", forbid)
    monkeypatch.setattr(model_routes, "load_model_config", _ProbeConfig)
    monkeypatch.setattr(model_routes, "_send_probe", refuse_send)

    result = asyncio.run(
        model_routes.test_model_connection(
            _request(
                base_url="http://169.254.169.254/v1",
                model_name="qwen-audio-3.0-asr-flash",
                protocol="AgentScope Platform",
            ),
        ),
    )

    assert result.ok is False
    assert "元数据" in (result.error or "")


def test_a_platform_probe_refusal_never_stages_the_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No network side effect may run ahead of the destination policy.

    The platform upload carries a guard of its own, which is what kept the
    route-level ordering untested: swapping the two statements in the probe
    still refused, just one request later. The first line of defence has to be
    the probe itself, so this stub replaces the upload entirely and any call to
    it is a failure regardless of what the real client would have checked.
    """

    async def refuse_upload(path, **kwargs):
        del path, kwargs
        raise AssertionError("the probe uploaded before checking its target")

    monkeypatch.setattr(model_routes, "load_model_config", _ProbeConfig)
    monkeypatch.setattr(
        media_transport,
        "upload_local_file_to_platform_media",
        refuse_upload,
    )

    result = asyncio.run(
        model_routes.test_model_connection(
            _request(
                base_url="http://169.254.169.254/v1",
                model_name="qwen-audio-3.0-asr-flash",
                protocol="AgentScope Platform",
            ),
        ),
    )

    assert result.ok is False
    assert "元数据" in (result.error or "")
