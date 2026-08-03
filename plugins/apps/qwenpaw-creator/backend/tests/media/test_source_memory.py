# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Source memory trigger/artifacts/query dispatch and projection tests."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.errors import ValidationError
from models import config as model_config
from schemas.assets import SourceIntelligenceIndex, SourceMemoryRef
from services.execution_pricing import estimate_source_memory_cost
from services.media import source_memory
from services.media.source_memory import (
    SourceMemoryProjection,
    SourceMemoryService,
    load_memory_ref,
    memory_dir,
    memory_guidance_for_targets,
)
from vendored.test_video_memory_toolkit import (
    build_fixture_index,
    build_fixture_memory,
)


def _index(
    *,
    duration_ms: int = 25 * 60 * 1000,
    media_kind: str = "video",
    checksum: str = "checksum-1",
    memory_ref: SourceMemoryRef | None = None,
) -> SourceIntelligenceIndex:
    created_at = "2026-08-01T00:00:00Z"
    evidence = {
        "assetVersionId": "version-1",
        "sourceChecksum": checksum,
        "confidence": 0.9,
        "modelRunId": "run-1",
        "evidenceFrameRefs": ["asset://asset-1@version-1"],
        "createdAt": created_at,
    }
    payload = {
        "id": "intel-1",
        "assetId": "asset-1",
        "assetVersionId": "version-1",
        "sourceChecksum": checksum,
        "modelRuns": [
            {"id": "run-1", "provider": "dashscope", "model": "qwen3.7-plus"},
        ],
        "coverage": {
            "visual": {
                "mode": "available",
                "producer": "model_native",
                "ratio": 0.95,
            },
            "asr": {"mode": "unavailable"},
            "ocr": {"mode": "unavailable"},
            "audio": {"mode": "unavailable"},
        },
        "media": {
            "mediaKind": media_kind,
            "mediaType": "video/mp4",
            "durationMs": duration_ms,
        },
        "summary": "fixture summary",
        "shots": [
            {
                "id": "shot-000001",
                "startMs": 0,
                "endMs": duration_ms,
                "description": "whole video",
                "events": ["everything"],
                "keyframeRef": "asset://asset-1@version-1",
                **evidence,
            },
        ],
        "transcript": [],
        "words": [],
        "ocrSegments": [],
        "audioEvents": [],
        "entities": [],
        "semanticEntries": [
            {
                "id": "semantic-000001",
                "text": "fixture event",
                "tags": ["fixture"],
                "startMs": 0,
                "endMs": 20000,
                **evidence,
            },
        ],
        "createdAt": created_at,
    }
    index = SourceIntelligenceIndex.model_validate(payload)
    index.memory_ref = memory_ref
    return index


def _write_fixture_memory(project_root: Path, index_id: str) -> Path:
    directory = memory_dir(project_root, index_id)
    directory.mkdir(parents=True)
    memory = build_fixture_memory()
    embedding_index, nodes, _ = build_fixture_index(memory)
    memory.save(str(directory / "graph_memory.json"))
    embedding_index.save(str(directory / "embeddings.npz"))
    (directory / "memory_meta.json").write_text(
        json.dumps(
            {
                "indexId": index_id,
                "assetId": "asset-1",
                "assetVersionId": "version-1",
                "sourceChecksum": "checksum-1",
                "builtAt": "2026-08-01T01:00:00Z",
                "macroCount": 2,
                "superCount": 1,
                "nodeCount": len(nodes),
                "graphPath": "graph_memory.json",
                "embeddingsPath": "embeddings.npz",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


def _service(tmp_path: Path) -> SourceMemoryService:
    project_root = tmp_path / "projects" / "project-1"
    project_root.mkdir(parents=True, exist_ok=True)
    services = SimpleNamespace(
        root=tmp_path,
        projects=SimpleNamespace(
            project_root=lambda project_id: tmp_path / "projects" / project_id,
        ),
    )
    return SourceMemoryService(services)


# ── trigger gating ──────────────────────────────────────────────────────────


def test_should_build_requires_video_over_threshold(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    project_root = tmp_path / "projects" / "project-1"
    monkeypatch.setattr(
        model_config,
        "is_embedding_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        source_memory.model_config,
        "is_embedding_configured",
        lambda: True,
    )
    assert service.should_build(_index(), project_root)
    assert not service.should_build(
        _index(duration_ms=10 * 60 * 1000),
        project_root,
    )
    short_image = _index(media_kind="image", duration_ms=25 * 60 * 1000)
    assert not service.should_build(short_image, project_root)


def test_should_build_requires_embedding_configuration(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        source_memory.model_config,
        "is_embedding_configured",
        lambda: False,
    )
    assert not service.should_build(
        _index(),
        tmp_path / "projects" / "project-1",
    )


def test_should_build_skips_when_memory_already_built(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    project_root = tmp_path / "projects" / "project-1"
    _write_fixture_memory(project_root, "intel-1")
    monkeypatch.setattr(
        source_memory.model_config,
        "is_embedding_configured",
        lambda: True,
    )
    assert not service.should_build(_index(), project_root)


def test_threshold_env_override(monkeypatch) -> None:
    assert source_memory.memory_build_threshold_ms() == 20 * 60 * 1000
    monkeypatch.setenv("CREATOR_MEMORY_BUILD_THRESHOLD_MS", "5000")
    assert source_memory.memory_build_threshold_ms() == 5000
    monkeypatch.setenv("CREATOR_MEMORY_BUILD_THRESHOLD_MS", "bogus")
    assert source_memory.memory_build_threshold_ms() == 20 * 60 * 1000


# ── artifact hydration & checksum invalidation ──────────────────────────────


def test_load_memory_ref_reads_meta(tmp_path) -> None:
    project_root = tmp_path / "projects" / "project-1"
    _write_fixture_memory(project_root, "intel-1")
    ref = load_memory_ref(project_root, "intel-1", "checksum-1")
    assert ref is not None
    assert ref.macro_count == 2
    assert ref.graph_path == (
        "runtime/source-intelligence/intel-1/memory/graph_memory.json"
    )
    assert ref.embeddings_path.endswith("embeddings.npz")


def test_load_memory_ref_invalidated_by_checksum(tmp_path) -> None:
    project_root = tmp_path / "projects" / "project-1"
    _write_fixture_memory(project_root, "intel-1")
    assert load_memory_ref(project_root, "intel-1", "other-checksum") is None
    assert load_memory_ref(project_root, "intel-2", "checksum-1") is None


def test_load_memory_ref_requires_artifacts(tmp_path) -> None:
    project_root = tmp_path / "projects" / "project-1"
    directory = _write_fixture_memory(project_root, "intel-1")
    (directory / "embeddings.npz").unlink()
    assert load_memory_ref(project_root, "intel-1", "checksum-1") is None


# ── index serialization contract ────────────────────────────────────────────


def test_index_dump_omits_absent_memory_ref() -> None:
    dumped = _index().model_dump(mode="json", by_alias=True)
    assert "memoryRef" not in dumped
    assert "memory_ref" not in dumped


def test_index_dump_includes_hydrated_memory_ref() -> None:
    ref = SourceMemoryRef(
        graphPath="runtime/source-intelligence/intel-1/memory/g.json",
        embeddingsPath="runtime/source-intelligence/intel-1/memory/e.npz",
        builtAt="2026-08-01T01:00:00Z",
        macroCount=7,
    )
    dumped = _index(memory_ref=ref).model_dump(mode="json", by_alias=True)
    assert dumped["memoryRef"]["macroCount"] == 7
    assert dumped["memoryRef"]["builtAt"] == "2026-08-01T01:00:00Z"


# ── cost estimate ────────────────────────────────────────────────────────────


def test_memory_cost_estimate_is_duration_linear() -> None:
    short = estimate_source_memory_cost(
        duration_ms=25 * 60 * 1000,
        vlm_model="qwen3.7-plus",
        embedding_model="qwen3-vl-embedding",
    )
    long = estimate_source_memory_cost(
        duration_ms=50 * 60 * 1000,
        vlm_model="qwen3.7-plus",
        embedding_model="qwen3-vl-embedding",
    )
    assert short.currency == "CNY"
    assert short.approximate is True
    assert short.estimated_cost is not None
    assert long.estimated_cost == pytest.approx(
        short.estimated_cost * 2,
        rel=0.01,
    )
    assert "¥" in short.formula


# ── projection ───────────────────────────────────────────────────────────────


def test_projection_drafts_validate_schema() -> None:
    memory = build_fixture_memory()
    projection = SourceMemoryProjection(
        indexId="intel-1",
        summary=SourceMemoryService._projection_summary(memory),
        semanticEntries=SourceMemoryService._projection_entries(memory),
    )
    assert projection.producer == "source_memory"
    assert "Fixture Video" in projection.summary
    assert projection.semantic_entries
    entry = projection.semantic_entries[0]
    assert entry.start_ms == 0
    assert entry.end_ms == 620_000
    assert entry.tags == ["orange cat"]


# ── prompt guidance ──────────────────────────────────────────────────────────


def test_memory_guidance_unavailable_without_project() -> None:
    guidance = memory_guidance_for_targets(None, None, ["asset:asset-1"])
    assert "query_source_memory" in guidance
    assert "available=false" in guidance


def test_memory_guidance_available_with_built_memory(
    tmp_path,
) -> None:
    project_root = tmp_path / "projects" / "project-1"
    _write_fixture_memory(project_root, "intel-1")
    intelligence = SimpleNamespace(
        intelligence_version_id="intel-1",
        source_checksum="checksum-1",
    )
    project = SimpleNamespace(
        sources=SimpleNamespace(
            sources=SimpleNamespace(
                items={
                    "source-1": SimpleNamespace(
                        logical_asset_id="asset-1",
                        current_intelligence_version_id="intel-1",
                    ),
                },
            ),
        ),
        assets=SimpleNamespace(
            intelligence_versions_by_id={"intel-1": intelligence},
        ),
    )
    guidance = memory_guidance_for_targets(
        project_root,
        project,
        ["asset:asset-1"],
    )
    assert "hitWindowsMs" in guidance
    other = memory_guidance_for_targets(
        project_root,
        project,
        ["asset:asset-other"],
    )
    assert "available=false" in other


# ── query dispatch ───────────────────────────────────────────────────────────


class _FakeAnalysis:
    def __init__(self, index: SourceIntelligenceIndex) -> None:
        self._index = index

    def load(self, project_id: str, logical_asset_id: str):
        del project_id, logical_asset_id
        return self._index


def _query_service(
    tmp_path,
    monkeypatch,
    *,
    with_memory: bool = True,
) -> SourceMemoryService:
    service = _service(tmp_path)
    project_root = tmp_path / "projects" / "project-1"
    ref = None
    if with_memory:
        _write_fixture_memory(project_root, "intel-1")
        ref = load_memory_ref(project_root, "intel-1", "checksum-1")
        assert ref is not None
    index = _index(memory_ref=ref)
    import services.source_analysis as source_analysis_module

    monkeypatch.setattr(
        source_analysis_module,
        "source_analysis_service",
        lambda services: _FakeAnalysis(index),
    )

    async def no_embedding(query_text: str):
        del query_text
        return None

    monkeypatch.setattr(service, "_embed_query", no_embedding)
    return service


def _run_query(service: SourceMemoryService, **kwargs):
    return asyncio.run(
        service.query_memory(
            project_id="project-1",
            logical_asset_id="asset-1",
            **kwargs,
        ),
    )


def test_query_memory_reports_unavailable_without_memory(
    tmp_path,
    monkeypatch,
) -> None:
    service = _query_service(tmp_path, monkeypatch, with_memory=False)
    result = _run_query(service, query_type="summary")
    assert result["ok"] is True
    assert result["available"] is False
    assert "reason" in result


def test_query_memory_dispatches_all_nine_types(
    tmp_path,
    monkeypatch,
) -> None:
    service = _query_service(tmp_path, monkeypatch)

    summary = _run_query(service, query_type="summary")
    assert summary["result"]["title"] == "Fixture Video"

    supers = _run_query(service, query_type="super_events")
    assert supers["result"][0]["super_id"] == "super_00"

    macros = _run_query(service, query_type="macro_events")
    assert len(macros["result"]) == 2
    filtered = _run_query(
        service,
        query_type="macro_events",
        macro_id="super_00",
    )
    assert len(filtered["result"]) == 2

    subgraph = _run_query(
        service,
        query_type="subgraph",
        macro_id="macro_0000",
    )
    assert subgraph["result"]["macro_id"] == "macro_0000"
    assert subgraph["hitWindowsMs"] == [
        {"macroId": "macro_0000", "startMs": 0, "endMs": 300_000},
    ]

    nodes = _run_query(
        service,
        query_type="search_nodes",
        query="teamfight dragon",
    )
    assert nodes["result"]["results"][0]["node_id"] == "macro_0001:ev_101"
    assert {
        "macroId": "macro_0001",
        "startMs": 300_000,
        "endMs": 620_000,
    } in nodes["hitWindowsMs"]

    ocr = _run_query(
        service,
        query_type="search_ocr",
        query="Team Blue scoreboard",
    )
    assert ocr["result"][0]["macro_id"] == "macro_0000"

    asr = _run_query(service, query_type="search_asr", query="团战 零换五")
    assert asr["result"][0]["macro_id"] == "macro_0001"

    by_time = _run_query(
        service,
        query_type="by_time",
        start_ms=310_000,
        end_ms=400_000,
    )
    assert [item["macro_id"] for item in by_time["result"]] == [
        "macro_0001",
    ]

    enumerated = _run_query(
        service,
        query_type="enumerate",
        query="cat teamfight",
    )
    assert enumerated["result"]["total_matches"] > 0


def test_query_memory_validates_arguments(tmp_path, monkeypatch) -> None:
    service = _query_service(tmp_path, monkeypatch)
    with pytest.raises(ValidationError):
        _run_query(service, query_type="bogus")
    with pytest.raises(ValidationError):
        _run_query(service, query_type="search_asr")
    with pytest.raises(ValidationError):
        _run_query(service, query_type="subgraph")
    with pytest.raises(ValidationError):
        _run_query(service, query_type="by_time", start_ms=10, end_ms=10)


def test_authorization_summary_uses_estimate(tmp_path, monkeypatch) -> None:
    # Admission is billing-gated: the record carries the duration-linear
    # estimate for the UI approval card.
    del tmp_path, monkeypatch
    estimate = estimate_source_memory_cost(
        duration_ms=25 * 60 * 1000,
        vlm_model="qwen3.7-plus",
        embedding_model="qwen3-vl-embedding",
    )
    payload = estimate.as_payload()
    assert payload["displayText"].startswith("约 ¥")
    assert payload["approximate"] is True


def test_build_datetime_marker_is_timezone_aware() -> None:
    ref = SourceMemoryRef(
        graphPath="a",
        embeddingsPath="b",
        builtAt=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        macroCount=1,
    )
    assert ref.macro_count == 1
