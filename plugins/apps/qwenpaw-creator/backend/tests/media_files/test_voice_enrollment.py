# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=unused-argument
# pylint: disable=protected-access
"""Voice enrollment, TTS reuse, and native video reference voices."""

from __future__ import annotations

import asyncio
import io
import json
import math
import wave
from array import array
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from domain.errors import ConflictError
from models import config as model_config
from models import tts_model, video_model
from models.reference_audio import wan_voice_excerpt
from models.tts_capabilities import capability_for, supported_models
from models.video_capabilities import (
    REFERENCE_VOICE_PER_MEDIA,
    REFERENCE_VOICE_STANDALONE,
    video_reference_voice_support,
)
from services.media_files import audio_execution, r2v_execution
from services.media_files.audio_execution import (
    execute_file_voice_enrollment_command,
)
from services.project_files.models import (
    CharacterVoice,
    Project,
    VisualEntity,
    VisualVariant,
)
from services.runtime_files.runtime_dependencies import resolve_ffmpeg
from utils.exceptions import ModelError
from utils.paths import local_path_from_file_url

from .conftest import r2v_project_services


def _services_with_character(tmp_path, monkeypatch):
    services = r2v_project_services(
        tmp_path,
        monkeypatch,
        project_id="p-voice",
        name="voice project",
    )
    snapshot = services.projects.read("p-voice")
    candidate = snapshot.project.model_dump(mode="json")
    entity = VisualEntity(
        entity_id="hero",
        kind="character",
        name="旅人",
        description="低沉沙哑的中年旅人",
        required_variant_ids=[],
    )
    candidate["visual"]["entities"]["order"] = ["hero"]
    candidate["visual"]["entities"]["items"] = {
        "hero": entity.model_dump(mode="json"),
    }
    from services.runtime_files.models import ChangeOrigin, ReviewPolicy

    commit = services.commits.commit(
        base=snapshot,
        candidate=candidate,
        origin=ChangeOrigin.RUNTIME_TASK,
        review_policy=ReviewPolicy.AUTO_FIX,
        caused_by_request_id="seed-hero",
        round_id="round-seed-hero",
        transaction_id="txn-seed-hero",
        advance_accepted_baseline=True,
    )
    services.poller.note_commit(commit.snapshot)
    return services


def test_video_sample_version_yields_its_extracted_audio_track(
    tmp_path,
    monkeypatch,
):
    """用户把音色参考录在视频里时，样本取其音轨而不是被格式拒绝。

    2026-09-10 现场：一段黑屏 mp4 音色参考被 "must be audio media" 卡死，
    尽管素材理解已确认音轨可用。
    """

    import subprocess

    from api.file_asset_routes import _AssetInput, _ingest_many_sync
    from services.runtime_files.runtime_dependencies import resolve_ffmpeg

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    clip = tmp_path / "voice-ref.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            str(clip),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if generated.returncode != 0:
        pytest.skip(f"ffmpeg cannot synthesize fixture: {generated.stderr}")

    services = _services_with_character(tmp_path, monkeypatch)
    ingested, _ = _ingest_many_sync(
        services,
        project_id="p-voice",
        key="voice-ref-video",
        inputs=[
            _AssetInput(
                name="voice-ref.mp4",
                content=clip.read_bytes(),
                media_type="video/mp4",
            ),
        ],
        attach_source=False,
        scope="voice-ref-test",
    )
    version_id = ingested["items"][0]["assetVersionId"]

    (
        payload,
        media_type,
        resolved_id,
    ) = audio_execution._sample_bytes_for_version(
        services,
        project_id="p-voice",
        version_id=version_id,
        idempotency_key="voice-enroll-1",
    )

    assert media_type == "audio/wav"
    with wave.open(io.BytesIO(payload), "rb") as sample:
        assert sample.getnchannels() == 1
        assert sample.getframerate() == 24000
        seconds = sample.getnframes() / sample.getframerate()
    assert 0.8 <= seconds <= 1.3
    # The binding must reference a persisted audio version (downstream R2V
    # voice resolution rejects video-bound samples), provenance-linked to
    # the original video.
    assert resolved_id != version_id
    project = services.projects.read("p-voice").project
    resolved = project.assets.source_versions_by_id[resolved_id]
    assert resolved.media_kind == "audio"
    assert resolved.metadata.get("extractedFromVersionId") == version_id
    # Replays resolve to the same persisted version instead of minting one.
    _, _, replay_id = audio_execution._sample_bytes_for_version(
        services,
        project_id="p-voice",
        version_id=version_id,
        idempotency_key="voice-enroll-1",
    )
    assert replay_id == resolved_id


@pytest.mark.parametrize("failure_stage", [None, "tts", "binding", "commit"])
def test_design_enrollment_persists_voice_and_recovers_audition(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    # pylint: disable=too-many-statements
    services = _services_with_character(tmp_path, monkeypatch)
    prompt = "低哑男声，语速缓慢"

    def enroll():
        return asyncio.run(
            execute_file_voice_enrollment_command(
                services,
                project_id="p-voice",
                target_ref="asset:hero",
                arguments={"voicePrompt": prompt},
                idempotency_key="voice-key-1",
            )
        )

    calls = {"design": 0, "synthesize": 0}
    expected_syntheses = 2 if failure_stage == "tts" else 1
    attach_sample = audio_execution._attach_voice_sample
    real_commit = services.commits.commit

    def commit(*args, **kwargs):
        if failure_stage == "commit":
            raise RuntimeError("temporary voice publication failure")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(services.commits, "commit", commit)

    def attach(*args, **kwargs):
        if failure_stage == "binding":
            raise RuntimeError("temporary sample binding failure")
        return attach_sample(*args, **kwargs)

    monkeypatch.setattr(audio_execution, "_attach_voice_sample", attach)

    async def fake_design_voice(*, voice_prompt, preview_text, preferred_name):
        calls["design"] += 1
        assert voice_prompt == prompt
        assert len(preview_text) >= tts_model.VOICE_PREVIEW_MIN_CHARS
        assert preferred_name == "旅人"
        return tts_model.VoiceEnrollment(
            voice_id="voice-designed-1",
            target_model="cosyvoice-v3.5-plus",
            origin="design",
        )

    monkeypatch.setattr(tts_model, "design_voice", fake_design_voice)
    monkeypatch.setattr(
        tts_model.config,
        "get_tts_model_name",
        lambda: "cosyvoice-v3.5-plus",
    )

    async def synthesize(text, **_kwargs):
        calls["synthesize"] += 1
        if failure_stage == "tts":
            raise RuntimeError("temporary audition failure")
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 4000)
        return tts_model.TTSSynthesis(
            audio_bytes=data.getvalue(),
            media_type="audio/wav",
            model="cosyvoice-v3.5-plus",
            voice="voice-designed-1",
            characters=len(text),
        )

    monkeypatch.setattr(tts_model, "synthesize", synthesize)
    if failure_stage == "commit":
        with pytest.raises(RuntimeError, match="publication failure"):
            enroll()
        failure_stage = None
    result = enroll()
    assert result.voice_id == "voice-designed-1"
    assert result.origin == "design"

    snapshot = services.projects.read("p-voice")
    voice = snapshot.project.visual.entities.items["hero"].voice
    assert voice is not None
    assert voice.voice_prompt == prompt
    assert bool(voice.sample_source_version_id) is (failure_stage is None)
    assert voice.enrollment_key == "voice-key-1"
    failure_stage = None

    replay = enroll()
    assert replay.replayed is True
    assert replay.voice_id == "voice-designed-1"
    assert replay.origin == "design"
    assert replay.sample_source_version_id is not None
    fresh = services.projects.read("p-voice").project
    assert (
        fresh.visual.entities.items["hero"].voice.sample_source_version_id
        == replay.sample_source_version_id
    )
    sample = fresh.assets.source_versions_by_id[
        replay.sample_source_version_id
    ]
    assert sample.metadata["voiceId"] == "voice-designed-1"
    assert sample.file_id in fresh.assets.files_by_id
    assert calls["design"] == 1
    assert calls["synthesize"] == expected_syntheses


@pytest.mark.parametrize("first_completed", ["old", "new"])
def test_later_voice_request_wins_regardless_of_provider_completion_order(
    tmp_path,
    monkeypatch,
    first_completed,
):
    services = _services_with_character(tmp_path, monkeypatch)

    async def scenario():
        started = {key: asyncio.Event() for key in ("old", "new")}
        releases = {key: asyncio.Event() for key in started}
        calls = []

        async def design(*, voice_prompt, **_kwargs):
            calls.append(voice_prompt)
            started[voice_prompt].set()
            await releases[voice_prompt].wait()
            return tts_model.VoiceEnrollment(
                voice_id=f"voice-{voice_prompt}",
                target_model="cosyvoice-v3.5-plus",
                origin="design",
            )

        async def audition(*_args, result, **_kwargs):
            return result

        async def delete(*_args, **_kwargs):
            raise AssertionError("no previously bound voice should be deleted")

        monkeypatch.setattr(tts_model, "design_voice", design)
        monkeypatch.setattr(tts_model, "delete_voice", delete)
        monkeypatch.setattr(audio_execution, "_ensure_voice_sample", audition)

        async def enroll(key):
            return await execute_file_voice_enrollment_command(
                services,
                project_id="p-voice",
                target_ref="asset:hero",
                arguments={"voicePrompt": key},
                idempotency_key=key,
            )

        tasks = {}
        for key in started:
            tasks[key] = asyncio.create_task(enroll(key))
            await asyncio.wait_for(started[key].wait(), timeout=3)
        results = {}
        for key in (
            first_completed,
            "new" if first_completed == "old" else "old",
        ):
            releases[key].set()
            results[key] = (
                await asyncio.gather(tasks[key], return_exceptions=True)
            )[0]
        assert isinstance(results["old"], ConflictError)
        assert results["new"].voice_id == "voice-new"
        assert (
            services.projects.read("p-voice")
            .project.visual.entities.items["hero"]
            .voice.voice_id
            == "voice-new"
        )
        # Retrying the obsolete key must not turn it into the newest intent
        # or pay the provider again.
        with pytest.raises(ConflictError):
            await enroll("old")
        assert calls == ["old", "new"]

    asyncio.run(scenario())


@pytest.fixture
def tts_defaults(monkeypatch):
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setenv("TTS_MODEL_NAME", "qwen-audio-3.1-tts-flash")
    monkeypatch.setenv("TTS_VOICE", "Cherry")


def _identity(*, voice="", voice_id=None):
    return audio_execution._requested_tts_identity(
        voice=voice,
        voice_id=voice_id,
        voice_model="",
    )


def test_reuse_key_matches_the_voice_the_runner_actually_speaks(
    monkeypatch,
    tts_defaults,
) -> None:
    spoken: dict = {}

    def fake_ws(*, model, voice, **_kwargs):
        spoken.update(model=model, voice=voice)
        return b"MP3xxxx", "audio/mpeg"

    monkeypatch.setattr(tts_model, "_synthesize_over_websocket", fake_ws)
    asyncio.run(tts_model.synthesize("同一句旁白"))
    assert spoken["voice"] != "Cherry"
    assert _identity() == (spoken["model"], spoken["voice"])


def test_reuse_key_names_only_voices_the_model_can_speak(
    monkeypatch,
    tts_defaults,
) -> None:
    for capability in supported_models():
        if capability.has_system_voices:
            monkeypatch.setenv("TTS_MODEL_NAME", capability.model)
            assert _identity()[1] in capability.system_voices, capability.model


def test_a_made_up_voice_is_keyed_as_requested(tts_defaults) -> None:
    assert _identity(voice="NotAVoice") == (
        "qwen-audio-3.1-tts-flash",
        "NotAVoice",
    )
    with pytest.raises(ValueError, match="unknown voice"):
        asyncio.run(tts_model.synthesize("旁白", voice="NotAVoice"))


def test_a_created_voice_keeps_its_own_identity(tts_defaults) -> None:
    assert _identity(voice_id="vc-abc") == (
        capability_for("qwen-audio-3.1-tts-flash").clone_model(),
        "vc-abc",
    )


def test_model_without_system_voices_keeps_an_empty_voice(
    monkeypatch, tts_defaults
) -> None:
    monkeypatch.setenv("TTS_MODEL_NAME", "cosyvoice-v3.5-plus")
    monkeypatch.setenv("TTS_VOICE", "")
    assert _identity() == ("cosyvoice-v3.5-plus", "")


def _project_with_voiced_character() -> Project:
    project = Project.new(project_id="p-voice", name="Voice")
    variant = VisualVariant(
        variant_id="var:base",
        selected_artifact_version_id="artifact:rusty-image",
    )
    entity = VisualEntity(
        entity_id="char:rusty",
        kind="character",
        name="锈锈",
        required_variant_ids=["var:base"],
        canonical_variant_id="var:base",
        variants={"items": {"var:base": variant}, "order": ["var:base"]},
        voice=CharacterVoice(
            voice_id="voice-1",
            target_model="cosyvoice-v3.5-plus",
            sample_source_version_id="source:rusty-sample",
            created_at=datetime.now(timezone.utc),
        ),
    )
    project.visual.entities.items["char:rusty"] = entity
    project.visual.entities.order.append("char:rusty")
    return project


def _gate(monkeypatch, supported: bool) -> None:
    monkeypatch.setattr(
        model_config,
        "get_video_model_name",
        lambda: "wan2.7-r2v" if supported else "happyhorse-1.1-r2v",
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "wan")


def _stub_sample_resolution(monkeypatch) -> None:
    def fake_resolve(
        *,
        project,
        project_root,
        version_id,
        media_prefix,
        label,
    ):
        # The fake mirrors the real keyword-only signature.
        del project, project_root
        assert media_prefix == "audio/"
        assert label == "characterVoiceSample"
        return (
            f"file:///samples/{version_id}.mp3",
            "sha-sample",
            f"source-version:{version_id}",
            {"ref": version_id},
        )

    monkeypatch.setattr(
        r2v_execution,
        "_resolve_single_media_version",
        fake_resolve,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supported", "enrolled", "versions", "present"),
    [
        (True, True, ("artifact:storyboard", "artifact:rusty-image"), True),
        (False, True, ("artifact:rusty-image",), False),
        (True, True, ("artifact:unrelated",), False),
        (True, False, ("artifact:rusty-image",), False),
    ],
    ids=["aligned", "unsupported-model", "unreferenced-character", "no-voice"],
)
def test_voice_rides_only_with_its_eligible_character(
    monkeypatch,
    supported,
    enrolled,
    versions,
    present,
) -> None:
    _gate(monkeypatch, supported=supported)
    _stub_sample_resolution(monkeypatch)
    project = _project_with_voiced_character()
    if not enrolled:
        project.visual.entities.items["char:rusty"].voice = None

    voice_urls, entries = r2v_execution._resolve_reference_voices(
        project=project,
        project_root=Path("/tmp"),
        creation=SimpleNamespace(character_refs=["char:rusty"]),
        version_ids=versions,
    )
    if present:
        assert voice_urls == ("", "file:///samples/source:rusty-sample.mp3")
        assert entries == [{"ref": "source:rusty-sample"}]
    else:
        assert not voice_urls
        assert not entries


_HTTP_CLIENT = httpx.AsyncClient


def _bind(
    monkeypatch,
    model: str,
    captured: dict,
    *,
    backend: str = "wan",
) -> None:
    monkeypatch.setattr(model_config, "get_video_model_name", lambda: model)
    monkeypatch.setattr(model_config, "get_video_backend", lambda: backend)
    monkeypatch.setattr(model_config, "get_video_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        model_config,
        "get_video_submit_url",
        lambda: "https://provider.example/video-synthesis",
    )
    monkeypatch.setattr(model_config, "get_video_submit_timeout", lambda: 5)

    async def fake_resolve(url: str, _backend: str):
        name = url.rsplit("/", 1)[-1]
        if name.endswith((".mp3", ".wav")):
            kind = "audio"
        elif name.endswith((".mp4", ".mov")):
            kind = "video"
        else:
            kind = "image"
        return f"resolved://{name}", kind

    monkeypatch.setattr(
        video_model,
        "_resolve_reference_media_url",
        fake_resolve,
    )

    @asynccontextmanager
    async def fake_excerpt(url, *, voice_count):
        captured.setdefault("excerpt_counts", []).append(voice_count)
        yield url

    monkeypatch.setattr(video_model, "wan_voice_excerpt", fake_excerpt)

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"output": {"task_id": "task-voice-1"}}
        )

    transport = httpx.MockTransport(reply)
    monkeypatch.setattr(
        video_model.httpx,
        "AsyncClient",
        lambda **kwargs: _HTTP_CLIENT(transport=transport, **kwargs),
    )


# ── capability table ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "backend", "expected"),
    [
        ("wan2.7-r2v", "", (REFERENCE_VOICE_PER_MEDIA, 5)),
        ("wan2.7-r2v-2026-06-12", "", (REFERENCE_VOICE_PER_MEDIA, 5)),
        ("wan3.0-video", "", (REFERENCE_VOICE_STANDALONE, 5)),
        (
            "doubao-seedance-2-5-260628",
            "seedance2",
            (REFERENCE_VOICE_STANDALONE, 10),
        ),
        (
            "doubao-seedance-2-0-260128",
            "seedance2",
            (REFERENCE_VOICE_STANDALONE, 3),
        ),
        ("wan2.6-r2v", "", None),
        ("happyhorse-1.1-r2v", "", None),
        ("kling/kling-v3-omni-video-generation", "", None),
        ("", "", None),
    ],
)
def test_reference_voice_support_matches_the_provider_contract(
    model, backend, expected
):
    args = (backend,) if backend else ()
    assert video_reference_voice_support(model, *args) == expected


# ── request-body shapes ──────────────────────────────────────────────────────


def _submit(monkeypatch, model, *, backend="wan"):
    captured: dict = {}
    _bind(monkeypatch, model, captured, backend=backend)
    asyncio.run(
        video_model.submit_video_task(
            prompt="两个角色对话",
            reference_image_url_list=[
                "https://cdn.example/rusty.png",
                "https://cdn.example/scene.png",
            ],
            reference_voice_urls=["https://cdn.example/rusty-voice.mp3", ""],
            ratio="16:9",
            duration=5,
            resolution="720P",
        ),
    )
    return captured["body"]


def test_request_body_shape_per_family(monkeypatch) -> None:
    # wan2.7: voice rides on its subject media entry; voiceless refs carry
    # no reference_voice key at all.
    media = _submit(monkeypatch, "wan2.7-r2v")["input"]["media"]
    assert media[0] == {
        "type": "reference_image",
        "url": "resolved://rusty.png",
        "reference_voice": "resolved://rusty-voice.mp3",
    }
    assert media[1] == {
        "type": "reference_image",
        "url": "resolved://scene.png",
    }
    # wan3.0: standalone reference_audio entries, never per-media keys.
    body = _submit(monkeypatch, "wan3.0-video")
    media = body["input"]["media"]
    assert {
        "type": "reference_audio",
        "url": "resolved://rusty-voice.mp3",
    } in media
    assert all("reference_voice" not in item for item in media)
    assert "图1中的角色使用音频1的音色" in body["input"]["prompt"]
    assert "不复述试听样本" in body["input"]["prompt"]
    # seedance: audio becomes audio_url content items.
    body = _submit(
        monkeypatch,
        "doubao-seedance-2-0-260128",
        backend="seedance2",
    )
    audio_items = [
        item for item in body["content"] if item["type"] == "audio_url"
    ]
    assert audio_items == [
        {
            "type": "audio_url",
            "role": "reference_audio",
            "audio_url": {"url": "resolved://rusty-voice.mp3"},
        },
    ]
    # Families without documented audio input silently drop voices.
    media = _submit(monkeypatch, "happyhorse-1.1-r2v")["input"]["media"]
    assert all(item["type"] == "reference_image" for item in media)
    assert all("reference_voice" not in item for item in media)


def test_audio_urls_are_rejected_as_plain_references(monkeypatch) -> None:
    # An audio URL counts as neither image nor video, so the pre-upload
    # budget check already fails the request before any transport happens;
    # the in-loop guard stays as defence in depth.
    captured: dict = {}
    _bind(monkeypatch, "wan2.7-r2v", captured)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            video_model.submit_video_task(
                prompt="x",
                reference_image_url_list=["https://cdn.example/voice.mp3"],
                ratio="16:9",
                duration=5,
                resolution="720P",
            ),
        )
    assert "VIDEO_REFERENCE_BUDGET_EXCEEDED" in str(excinfo.value)
    assert captured.get("body") is None


def test_voice_mapping_and_budget_follow_deduplicated_wire_order(monkeypatch):
    captured = {}
    _bind(monkeypatch, "wan3.0-video", captured)
    asyncio.run(
        video_model.submit_video_task(
            prompt="两个角色轮流说话",
            reference_image_url_list=[
                "https://cdn.example/storyboard.png",
                "https://cdn.example/rusty.png",
                "https://cdn.example/rusty.png",
                "https://cdn.example/friend.png",
                "https://cdn.example/rusty-closeup.png",
            ],
            reference_voice_urls=[
                "",
                "https://cdn.example/rusty.mp3",
                "https://cdn.example/rusty.mp3",
                "https://cdn.example/friend.wav",
                "https://cdn.example/rusty.mp3",
            ],
            ratio="16:9",
            duration=5,
            resolution="720P",
        ),
    )
    body = captured["body"]["input"]
    assert [
        m["url"] for m in body["media"] if m["type"] == "reference_audio"
    ] == [
        "resolved://rusty.mp3",
        "resolved://friend.wav",
    ]
    assert captured["excerpt_counts"] == [2, 2]
    assert "图2中的角色使用音频1的音色" in body["prompt"]
    assert "图3中的角色使用音频2的音色" in body["prompt"]
    assert "图4中的角色使用音频1的音色" in body["prompt"]
    assert "图1中的角色使用" not in body["prompt"]

    captured.clear()
    with pytest.raises(ModelError, match="at most 5"):
        asyncio.run(
            video_model.submit_video_task(
                prompt="六个说话者",
                reference_image_url_list=[
                    f"https://cdn.example/{i}.png" for i in range(6)
                ],
                reference_voice_urls=[
                    f"https://cdn.example/{i}.wav" for i in range(6)
                ],
                ratio="16:9",
                duration=5,
                resolution="720P",
            ),
        )
    assert "body" not in captured


@pytest.mark.parametrize("voice_count", [1, 2, 5])
def test_voice_excerpt_decodes_and_fits_total_budget(tmp_path, voice_count):
    if not resolve_ffmpeg():
        pytest.skip("FFmpeg is required for the real media boundary probe")
    source = tmp_path / "voice.wav"
    samples = array("h", [0] * 16000)
    samples.extend(
        int(10000 * math.sin(i * math.tau * 220 / 8000)) for i in range(160000)
    )
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    original = source.read_bytes()

    async def inspect():
        async with wan_voice_excerpt(
            source.as_uri(),
            voice_count=voice_count,
        ) as url:
            excerpt = local_path_from_file_url(url)
            with wave.open(str(excerpt), "rb") as audio:
                seconds = audio.getnframes() / audio.getframerate()
                assert audio.getframerate() == 24000
                assert audio.getnchannels() == 1
                assert 1 <= seconds <= 15 / voice_count
                assert seconds == pytest.approx(14.5 / voice_count, abs=0.03)
                assert any(audio.readframes(2400))  # leading silence removed
        assert not excerpt.exists()

    asyncio.run(inspect())
    assert source.read_bytes() == original

    # A silent audition must not become a nominally valid empty sample.
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(32000))
    with pytest.raises(ModelError, match="at least 1 second"):
        asyncio.run(inspect())
