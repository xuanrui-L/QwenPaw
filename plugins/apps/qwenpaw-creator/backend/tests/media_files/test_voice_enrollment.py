# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Voice enrollment executor: prompt persistence and audition fallback."""
from __future__ import annotations

import asyncio
import io
import wave

import pytest

from models import tts_model
from services.media_files import audio_execution
from services.media_files.audio_execution import (
    execute_file_voice_enrollment_command,
)
from services.project_files.models import VisualEntity

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


@pytest.mark.parametrize("failure_stage", [None, "tts", "binding"])
def test_design_enrollment_persists_voice_and_recovers_audition(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    services = _services_with_character(tmp_path, monkeypatch)
    calls = {"design": 0, "synthesize": 0}
    expected_syntheses = 2 if failure_stage == "tts" else 1
    attach_sample = audio_execution._attach_voice_sample

    def attach(*args, **kwargs):
        if failure_stage == "binding":
            raise RuntimeError("temporary sample binding failure")
        return attach_sample(*args, **kwargs)

    monkeypatch.setattr(audio_execution, "_attach_voice_sample", attach)

    async def fake_design_voice(*, voice_prompt, preview_text, preferred_name):
        calls["design"] += 1
        assert voice_prompt == "低哑男声，语速缓慢"
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
    result = asyncio.run(
        execute_file_voice_enrollment_command(
            services,
            project_id="p-voice",
            target_ref="asset:hero",
            arguments={"voicePrompt": "低哑男声，语速缓慢"},
            idempotency_key="voice-key-1",
        ),
    )
    assert result.voice_id == "voice-designed-1"
    assert result.origin == "design"

    snapshot = services.projects.read("p-voice")
    voice = snapshot.project.visual.entities.items["hero"].voice
    assert voice is not None
    assert voice.voice_prompt == "低哑男声，语速缓慢"
    assert bool(voice.sample_source_version_id) is (failure_stage is None)
    assert voice.enrollment_key == "voice-key-1"
    failure_stage = None

    # Idempotent replay: same key returns the same binding, no provider call.
    async def boom(**_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("replay must not re-enroll")

    monkeypatch.setattr(tts_model, "design_voice", boom)
    replay = asyncio.run(
        execute_file_voice_enrollment_command(
            services,
            project_id="p-voice",
            target_ref="asset:hero",
            arguments={"voicePrompt": "低哑男声，语速缓慢"},
            idempotency_key="voice-key-1",
        ),
    )
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
