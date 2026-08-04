# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=too-many-lines
"""Long-source hierarchical graph memory inside Source Intelligence.

Write path: after ``commit_source_intelligence`` publishes a regular index,
sources longer than the threshold get a background Task (behind execution
authorization) that builds the vendored video-memory hierarchical graph:
P1 ffmpeg frame-diff scene segmentation → P2 one VLM subgraph extraction
per macro (bounded concurrency) in parallel with ASR transcription →
P3 text-only aggregation plus full-node embedding. Artifacts live in
``runtime/source-intelligence/<index-id>/memory/`` and are invalidated by
``sourceChecksum``.

Read path: ``query_source_memory`` dispatches nine query types over the
vendored :class:`MemoryToolkit` with an in-process graph cache; semantic
lookups embed the query on the fly through the Creator embedding client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - fixed ffmpeg argv, no shell
import tempfile
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from pydantic import Field

from domain.enums import TaskKind, TaskStatus
from domain.errors import ValidationError
from models import asr_model, embedding_model, vlm_model
from models import config as model_config
from schemas.assets import (
    SemanticIndexEntry,
    SourceIntelligenceIndex,
    SourceMemoryRef,
    SourceModelRunRef,
)
from schemas.common import StrictModel
from services.execution_pricing import (
    CostEstimate,
    estimate_source_memory_cost,
)
from services.runtime_files.errors import RecordNotFoundError
from services.runtime_files.execution_models import (
    ExecutionAuthorizationRecord,
    ExecutionAuthorizationStatus,
    TaskAttemptStatus,
    TaskRecord,
)
from services.runtime_files.execution_store import (
    ExecutionStateConflict,
    ProjectExecutionStore,
)
from services.runtime_files.runtime_dependencies import resolve_ffmpeg
from vendor.mm_plugins.video_memory.aggregation import aggregate_hierarchy
from vendor.mm_plugins.video_memory.embeddings import EmbeddingIndex
from vendor.mm_plugins.video_memory.json_utils import extract_json
from vendor.mm_plugins.video_memory.prompts import (
    SUBGRAPH_CONSTRUCTION_PROMPT,
)
from vendor.mm_plugins.video_memory.schema import (
    HierarchicalGraphMemory,
    MacroEvent,
    Subgraph,
)
from vendor.mm_plugins.video_memory.segmentation import (
    compute_cut_scores,
    decode_jpeg_to_hls,
    plan_segments,
)
from vendor.mm_plugins.video_memory.subgraph import (
    apply_subgraph_payload,
    build_segment_context,
)
from vendor.mm_plugins.video_memory.toolkit import MemoryToolkit

logger = logging.getLogger("creator.source_memory")

# Sources longer than this get a memory build (plan: 20 minutes).
MEMORY_BUILD_THRESHOLD_MS = 20 * 60 * 1000
# P2 subgraph extraction concurrency (plan window: 4-8).
SUBGRAPH_CONCURRENCY = 6
# P1 detection sampling (matches the upstream pipeline defaults).
DETECT_FPS = 0.25
FRAME_WORKERS = 10
MIN_SCENE_SEC = 30.0
MAX_SCENE_SEC = 300.0
# P2 clip encoding for the VLM segment observation. Non-DashScope
# OpenAI-compatible gateways receive the clip as an inline base64 data
# URL, so each macro clip must fit the configured inline transport
# limit after Base64 expansion (~4/3); encoding steps down this
# (max_dim, crf, fps) ladder until the segment fits.
CLIP_ENCODE_LADDER = (
    (720, 28, 8),
    (512, 30, 8),
    (384, 32, 6),
    (320, 35, 4),
    (256, 38, 3),
)
CLIP_SIZE_BUDGET_CAP_BYTES = 6 * 1024 * 1024
_BASE64_EXPANSION = 4.0 / 3.0
_CLIP_BUDGET_HEADROOM_BYTES = 64 * 1024
_CLIP_MIN_WORKABLE_BUDGET_BYTES = 256 * 1024


def _clip_size_budget_bytes() -> int:
    """Raw clip budget derived from the active transport limit.

    The inline pre-flight check in the VLM backend enforces
    ``get_vlm_max_inline_bytes`` on the raw file, while the gateway sees
    the Base64-expanded request body; budget against both, capped at a
    conservative default. Configurations too small for any workable
    segment clip are rejected up front instead of producing a budget
    that transport would later refuse.
    """
    inline_limit = model_config.get_vlm_max_inline_bytes()
    base64_safe = int(inline_limit / _BASE64_EXPANSION)
    budget = (
        min(
            CLIP_SIZE_BUDGET_CAP_BYTES,
            inline_limit,
            base64_safe,
        )
        - _CLIP_BUDGET_HEADROOM_BYTES
    )
    if budget < _CLIP_MIN_WORKABLE_BUDGET_BYTES:
        raise ValidationError(
            "VLM max_inline_bytes is too small for source-memory clips: "
            f"derived budget {budget} bytes is below the workable minimum "
            f"{_CLIP_MIN_WORKABLE_BUDGET_BYTES} bytes",
        )
    return budget


SUBGRAPH_MAX_TOKENS = 16384
AGGREGATION_MAX_TOKENS = 8192
SUBGRAPH_RETRIES = 2
PROJECTION_REVIEW_MAX_TOKENS = 4096

# Outer-VLM review of the P3 projection drafts. Not an agent prompt
# (no placeholder whitelist involvement) — a Creator-side constant like
# the vendored pipeline prompts.
PROJECTION_REVIEW_PROMPT = """You are the Source Intelligence reviewer.
Below are draft catalog entries projected from a hierarchical memory of
a long video: one overall summary plus per-super-event semantic entries
with millisecond time windows. Each entry carries an immutable
"entryId".

Review the drafts: fix wording, drop entries that are vague, redundant
or internally inconsistent. You may only edit text, tags and
confidence; keep each kept entry's entryId exactly as given, never
invent new entries and never change startMs/endMs. Do not invent new
facts. Return ONLY a JSON object:
{"summary": str, "semanticEntries": [{"entryId": str, "text": str,
"tags": [str], "startMs": int, "endMs": int, "confidence": float}]}

Drafts:
"""

MEMORY_DIR_NAME = "memory"
GRAPH_FILENAME = "graph_memory.json"
EMBEDDINGS_FILENAME = "embeddings.npz"
META_FILENAME = "memory_meta.json"
PROJECTION_FILENAME = "projection.json"

SOURCE_MEMORY_OPERATION = "build_source_memory"

QUERY_TYPES = (
    "summary",
    "super_events",
    "macro_events",
    "subgraph",
    "search_nodes",
    "search_ocr",
    "search_asr",
    "by_time",
    "enumerate",
)


def memory_build_threshold_ms() -> int:
    """Threshold in ms; env override supports isolated-stack testing."""
    raw = os.environ.get("CREATOR_MEMORY_BUILD_THRESHOLD_MS", "")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return MEMORY_BUILD_THRESHOLD_MS
    return parsed if parsed > 0 else MEMORY_BUILD_THRESHOLD_MS


class SourceMemorySemanticDraft(StrictModel):
    """One projected semantic entry draft (producer: source_memory)."""

    text: str = Field(min_length=1)
    tags: list[str]
    start_ms: int = Field(alias="startMs", ge=0)
    end_ms: int = Field(alias="endMs", gt=0)
    confidence: float = Field(ge=0.0, le=1.0)


class ProjectionReview(StrictModel):
    """Outer-VLM review verdict attached to a projection."""

    status: Literal["approved"] = "approved"
    model: str = Field(min_length=1)
    reviewed_at: str = Field(alias="reviewedAt", min_length=1)


class SourceMemoryProjection(StrictModel):
    """Summary/semantics projected from the P3 hierarchy.

    Drafts are reviewed by the outer Source Intelligence VLM during the
    build; only reviewed projections are folded into the standard index
    surfaces. The immutable index file is never rewritten.
    """

    producer: Literal["source_memory"] = "source_memory"
    index_id: str = Field(alias="indexId", min_length=1)
    summary: str = Field(min_length=1)
    semantic_entries: list[SourceMemorySemanticDraft] = Field(
        alias="semanticEntries",
    )
    review: ProjectionReview | None = None


# ── Artifact locations & hydration ──────────────────────────────────────────


def memory_dir(project_root: Path, index_id: str) -> Path:
    return (
        project_root
        / "runtime"
        / "source-intelligence"
        / index_id
        / MEMORY_DIR_NAME
    )


def load_memory_ref(
    project_root: Path,
    index_id: str,
    source_checksum: str,
) -> SourceMemoryRef | None:
    """Load the built-memory pointer; stale checksums invalidate it."""
    directory = memory_dir(project_root, index_id)
    meta_path = directory / META_FILENAME
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, Mapping):
        return None
    if meta.get("sourceChecksum") != source_checksum:
        return None
    if (
        not (directory / GRAPH_FILENAME).is_file()
        or not (directory / EMBEDDINGS_FILENAME).is_file()
    ):
        return None
    built_at = str(meta.get("builtAt") or "")
    macro_count = meta.get("macroCount")
    if not built_at or not isinstance(macro_count, int):
        return None
    relative = directory.relative_to(project_root).as_posix()
    return SourceMemoryRef(
        graphPath=f"{relative}/{GRAPH_FILENAME}",
        embeddingsPath=f"{relative}/{EMBEDDINGS_FILENAME}",
        builtAt=built_at,
        macroCount=macro_count,
    )


SOURCE_MEMORY_RUN_ID = "source_memory"


def merge_projection_semantics(project_root: Path, index: Any) -> None:
    """Fold the reviewed P3 projection into a loaded index, in memory.

    Only projections carrying an approved outer-VLM review are merged
    (fail-close for unreviewed drafts). The Root digest is appended to
    ``index.summary`` and the SuperEvent entries join ``semanticEntries``
    with ``modelRunId=source_memory``; the immutable index file on disk
    stays untouched (same hydrated-only contract as ``memoryRef``).
    """
    if index.memory_ref is None:
        return
    directory = memory_dir(project_root, index.id)
    try:
        meta = json.loads(
            (directory / META_FILENAME).read_text(encoding="utf-8"),
        )
        raw = json.loads(
            (directory / PROJECTION_FILENAME).read_text(encoding="utf-8"),
        )
        projection = SourceMemoryProjection.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if projection.review is None or projection.review.status != "approved":
        return
    if not isinstance(meta, Mapping):
        return
    if meta.get("sourceChecksum") != index.source_checksum:
        return
    built_at = str(meta.get("builtAt") or "")
    if not built_at:
        return
    evidence = [f"memory://{index.id}/{GRAPH_FILENAME}"]
    common: dict[str, Any] = {
        "assetVersionId": index.asset_version_id,
        "sourceChecksum": index.source_checksum,
        "modelRunId": SOURCE_MEMORY_RUN_ID,
        "evidenceFrameRefs": evidence,
        "createdAt": built_at,
    }
    duration_ms = index.media.duration_ms
    # Reviewed Root digest enters the standard summary surface with a
    # clear origin marker (and also as an anchor semantic entry).
    marker = "[长素材记忆摘要 · 已审校]"
    if marker not in index.summary:
        index.summary = f"{index.summary}\n\n{marker} {projection.summary}"
    drafts: list[SemanticIndexEntry] = [
        SemanticIndexEntry(
            id="sem-mem-summary",
            text=projection.summary,
            tags=["memory", "summary"],
            confidence=0.6,
            **common,
        ),
    ]
    for n, draft in enumerate(projection.semantic_entries):
        end_ms = draft.end_ms
        if duration_ms is not None:
            end_ms = min(end_ms, duration_ms)
        if end_ms <= draft.start_ms:
            continue
        drafts.append(
            SemanticIndexEntry(
                id=f"sem-mem-{n:03d}",
                text=draft.text,
                tags=draft.tags,
                startMs=draft.start_ms,
                endMs=end_ms,
                confidence=draft.confidence,
                **common,
            ),
        )
    existing_ids = {entry.id for entry in index.semantic_entries}
    index.semantic_entries.extend(
        entry for entry in drafts if entry.id not in existing_ids
    )
    if SOURCE_MEMORY_RUN_ID not in {run.id for run in index.model_runs}:
        index.model_runs.append(
            SourceModelRunRef(
                id=SOURCE_MEMORY_RUN_ID,
                provider="creator",
                model="source-memory-p3",
            ),
        )


def has_built_memory(
    project_root: Path,
    project: Any,
    logical_asset_id: str,
) -> bool:
    """True when the asset's current intelligence has a valid memory."""
    for source in project.sources.sources.items.values():
        if source.logical_asset_id != logical_asset_id:
            continue
        selected = source.current_intelligence_version_id
        if not selected:
            return False
        record = project.assets.intelligence_versions_by_id.get(selected)
        if record is None:
            return False
        return (
            load_memory_ref(
                project_root,
                record.intelligence_version_id,
                record.source_checksum,
            )
            is not None
        )
    return False


# ── ffmpeg helpers (Creator-owned IO around the vendored planning) ─────────


def _require_ffmpeg() -> str:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required for source memory builds; set "
            "CREATOR_FFMPEG_PATH, install ffmpeg, or install imageio-ffmpeg",
        )
    return ffmpeg


def _seek_jpeg(
    ffmpeg: str,
    video_path: str,
    ts: float,
    scale: str,
    quality: int,
    timeout: int,
) -> bytes | None:
    command = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-ss",
        str(ts),
        "-i",
        video_path,
        "-an",
        "-frames:v",
        "1",
        "-vf",
        f"scale={scale}",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        str(quality),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _detect_segments_sync(
    local_path: Path,
    duration_sec: float,
) -> list[tuple[float, float]]:
    """Phase 1: parallel frame-diff scene detection (no API calls)."""
    ffmpeg = _require_ffmpeg()
    n_frames = max(4, int(duration_sec * DETECT_FPS))
    timestamps = [i / DETECT_FPS for i in range(n_frames)]

    def _extract(ts: float) -> tuple[float, bytes | None]:
        return ts, _seek_jpeg(
            ffmpeg,
            str(local_path),
            ts,
            "360:-2",
            quality=10,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=FRAME_WORKERS) as pool:
        results = list(pool.map(_extract, timestamps))

    hls_frames = []
    for ts, data in results:
        if data is None:
            continue
        hls = decode_jpeg_to_hls(data)
        if hls is not None:
            hls_frames.append((ts, hls))
    logger.info(
        "source memory P1: %d/%d detection frames decoded",
        len(hls_frames),
        n_frames,
    )
    cut_times, cut_scores = compute_cut_scores(hls_frames)
    return plan_segments(
        cut_times,
        cut_scores,
        start_sec=0.0,
        end_sec=duration_sec,
        min_scene_sec=MIN_SCENE_SEC,
        max_scene_sec=MAX_SCENE_SEC,
    )


def _clip_segment_sync(
    local_path: Path,
    out_path: Path,
    start_sec: float,
    end_sec: float,
    max_dim: int,
    crf: int,
    fps: int,
) -> Path:
    """Encode one macro segment for VLM observation."""
    ffmpeg = _require_ffmpeg()
    duration = max(0.5, end_sec - start_sec)
    scale = (
        f"scale='min({max_dim},iw)':'min({max_dim},ih)':"
        "force_original_aspect_ratio=decrease,"
        "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    )
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-ss",
        str(start_sec),
        "-t",
        str(duration),
        "-i",
        str(local_path),
        "-vf",
        scale,
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        str(crf),
        "-an",
        "-threads",
        "4",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(  # nosec B603
        command,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            "ffmpeg segment clip failed: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}",
        )
    return out_path


def _clip_segment_within_budget_sync(
    local_path: Path,
    out_path: Path,
    start_sec: float,
    end_sec: float,
) -> Path:
    """Encode a segment, stepping down the ladder until it fits the
    inline transport budget of OpenAI-compatible gateways."""
    budget = _clip_size_budget_bytes()
    last_size = 0
    for max_dim, crf, fps in CLIP_ENCODE_LADDER:
        _clip_segment_sync(
            local_path,
            out_path,
            start_sec,
            end_sec,
            max_dim,
            crf,
            fps,
        )
        last_size = out_path.stat().st_size
        if last_size <= budget:
            return out_path
        logger.info(
            "segment clip %s too large at %dpx (%d bytes > %d), "
            "stepping down",
            out_path.name,
            max_dim,
            last_size,
            budget,
        )
    raise RuntimeError(
        f"segment clip stays above transport budget: {last_size} bytes",
    )


def _segment_fps(duration_sec: float) -> float:
    """Match the Source Intelligence native sampling tiers."""
    if duration_sec <= 120:
        return 2.0
    if duration_sec <= 600:
        return 1.0
    return 0.5


def _merge_asr_into_macros(
    macros: list[MacroEvent],
    transcript: list[dict[str, float | str]],
) -> None:
    """Merge ASR transcript into macro events by time-range overlap."""
    if not transcript:
        return
    for macro in macros:
        ms, me = macro.time_range
        texts = [
            str(segment["text"])
            for segment in transcript
            if float(segment["start_sec"]) < me
            and float(segment["end_sec"]) > ms
        ]
        if texts:
            macro.asr_text = " ".join(texts)


# ── Build job ───────────────────────────────────────────────────────────────


def _stable_id(prefix: str, project_id: str, key: str) -> str:
    return (
        f"{prefix}-"
        + uuid5(
            NAMESPACE_URL,
            f"qwenpaw-creator:source-memory:{prefix}:{project_id}:{key}",
        ).hex
    )


class SourceMemoryBuildJob(StrictModel):
    project_id: str
    task_id: str
    authorization_id: str | None
    index_id: str
    asset_id: str
    asset_version_id: str
    source_checksum: str
    duration_ms: int
    local_path: str


class SourceMemoryService:
    """Owns memory build scheduling and memory queries for one root."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.executions = ProjectExecutionStore(services.root)
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._toolkits: dict[str, tuple[float, MemoryToolkit]] = {}
        self._toolkits_lock = asyncio.Lock()

    # -- trigger -----------------------------------------------------------

    def should_build(
        self,
        index: SourceIntelligenceIndex,
        project_root: Path,
    ) -> bool:
        if index.media.media_kind != "video":
            return False
        duration_ms = index.media.duration_ms or 0
        if duration_ms <= memory_build_threshold_ms():
            return False
        if not model_config.is_embedding_configured():
            return False
        return (
            load_memory_ref(
                project_root,
                index.id,
                index.source_checksum,
            )
            is None
        )

    async def maybe_schedule_build(
        self,
        *,
        project_id: str,
        index: SourceIntelligenceIndex,
        local_path: Path | None,
        run_id: str,
        round_id: str,
        caused_by_request_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Schedule the background build after an index publication."""
        project_root = self.services.projects.project_root(project_id)
        if not self.should_build(index, project_root):
            return None
        if local_path is None or not local_path.is_file():
            logger.info(
                "source memory build skipped: no local media for %s",
                index.id,
            )
            return None
        duration_ms = int(index.media.duration_ms or 0)
        estimate = estimate_source_memory_cost(
            duration_ms=duration_ms,
            vlm_model=model_config.get_vlm_model_name(),
            embedding_model=model_config.get_embedding_model_name(),
        )
        task = await asyncio.to_thread(
            self._admit_sync,
            project_id,
            index,
            local_path,
            duration_ms,
            estimate,
            run_id,
            round_id,
            caused_by_request_id,
        )
        if task is None:
            return None
        job = SourceMemoryBuildJob(
            project_id=project_id,
            task_id=task.task_id,
            authorization_id=task.metadata.get("authorizationId"),
            index_id=index.id,
            asset_id=index.asset_id,
            asset_version_id=index.asset_version_id,
            source_checksum=index.source_checksum,
            duration_ms=duration_ms,
            local_path=str(local_path),
        )
        self._spawn(job)
        return {
            "taskId": task.task_id,
            "authorizationId": job.authorization_id,
            "estimatedCost": estimate.estimated_cost,
        }

    def _admit_sync(
        self,
        project_id: str,
        index: SourceIntelligenceIndex,
        local_path: Path,
        duration_ms: int,
        estimate: CostEstimate,
        run_id: str,
        round_id: str,
        caused_by_request_id: str | None,
    ) -> TaskRecord | None:
        task_id = _stable_id("memtask", project_id, index.id)
        try:
            existing = self.executions.get_task(project_id, task_id)
        except RecordNotFoundError:
            existing = None
        if existing is not None:
            if existing.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return None
            if existing.status is TaskStatus.SUCCEEDED:
                return None
            # FAILED/CANCELLED builds are not auto-retried; a fresh index
            # version (new task id) restarts the flow.
            return None
        authorization_id: str | None = None
        requires_authorization = (
            model_config.get_execution_authorization_mode()
            != model_config.EXECUTION_AUTHORIZATION_ALLOW_ALL
        )
        target_ref = f"asset:{index.asset_id}"
        if requires_authorization:
            authorization_id = _stable_id("memauth", project_id, index.id)
            minutes = max(1, round(duration_ms / 60_000))
            record = ExecutionAuthorizationRecord(
                authorization_id=authorization_id,
                project_id=project_id,
                round_id=round_id,
                run_id=run_id,
                task_id=task_id,
                execution_request_id=_stable_id(
                    "memreq",
                    project_id,
                    index.id,
                ),
                operation=SOURCE_MEMORY_OPERATION,
                target_scope=[target_ref],
                authorization_token=secrets.token_urlsafe(32),
                summary=(
                    f"为长素材（约 {minutes} 分钟）构建层次图记忆，"
                    f"用于台词/语义/时间检索 · 预计 {estimate.display_text}"
                ),
                scope={
                    "operation": SOURCE_MEMORY_OPERATION,
                    "targetRefs": [target_ref],
                    "parameters": {
                        "analysisVersionId": index.id,
                        "durationMs": duration_ms,
                    },
                    "billing": estimate.as_payload(),
                },
                requested_provider="dashscope",
                requested_model=model_config.get_vlm_model_name(),
                requested_candidates=1,
                estimated_cost=estimate.estimated_cost,
                caused_by_request_id=caused_by_request_id,
            )
            self.executions.create_execution_authorization(record)
        candidate = TaskRecord(
            task_id=task_id,
            project_id=project_id,
            kind=TaskKind.SOURCE_MEMORY_BUILD,
            request_fingerprint=uuid5(
                NAMESPACE_URL,
                f"source-memory:{index.id}:{index.source_checksum}",
            ).hex,
            idempotency_key=task_id,
            input_refs=[target_ref],
            caused_by_request_id=caused_by_request_id,
            metadata={
                "targetRef": target_ref,
                "analysisVersionId": index.id,
                "assetVersionId": index.asset_version_id,
                "sourceChecksum": index.source_checksum,
                "durationMs": duration_ms,
                "localPath": str(local_path),
                "authorizationId": authorization_id,
                "estimatedCost": estimate.estimated_cost,
            },
        )
        task = self.executions.create_task(candidate)
        logger.info(
            "source memory build admitted: project=%s task=%s index=%s "
            "duration=%dms authorization=%s",
            project_id,
            task_id,
            index.id,
            duration_ms,
            authorization_id,
        )
        return task

    def _spawn(self, job: SourceMemoryBuildJob) -> asyncio.Task[None] | None:
        current = self._jobs.get(job.task_id)
        if current is not None and not current.done():
            return current
        worker = asyncio.create_task(
            self._drive(job),
            name=f"source-memory:{job.task_id}",
        )
        self._jobs[job.task_id] = worker

        def discard(done: asyncio.Task[None]) -> None:
            if self._jobs.get(job.task_id) is done:
                self._jobs.pop(job.task_id, None)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException:  # pylint: disable=broad-except
                    pass

        worker.add_done_callback(discard)
        return worker

    async def _drive(self, job: SourceMemoryBuildJob) -> None:
        try:
            approved = await self._await_authorization(job)
            if not approved:
                return
            await self._execute(job)
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except Exception as error:  # pylint: disable=broad-except
            logger.exception(
                "source memory build failed: project=%s task=%s",
                job.project_id,
                job.task_id,
            )
            await asyncio.to_thread(self._fail_sync, job, error)

    async def _await_authorization(self, job: SourceMemoryBuildJob) -> bool:
        if not job.authorization_id:
            return True
        while True:
            record = await asyncio.to_thread(
                self.executions.get_execution_authorization,
                job.project_id,
                job.authorization_id,
            )
            if record.status is ExecutionAuthorizationStatus.APPROVED:
                return True
            if record.status in {
                ExecutionAuthorizationStatus.REJECTED,
                ExecutionAuthorizationStatus.EXPIRED,
            }:
                await asyncio.to_thread(
                    self._cancel_sync,
                    job,
                    f"execution authorization {record.status.value.lower()}",
                )
                return False
            task = await asyncio.to_thread(
                self.executions.get_task,
                job.project_id,
                job.task_id,
            )
            if task.status is not TaskStatus.QUEUED:
                return False
            await asyncio.sleep(2.0)

    def _cancel_sync(self, job: SourceMemoryBuildJob, reason: str) -> None:
        try:
            self.executions.transition_task(
                job.project_id,
                job.task_id,
                expected_status=TaskStatus.QUEUED,
                status=TaskStatus.CANCELLED,
                updates={
                    "error": {
                        "code": "MEMORY_BUILD_DECLINED",
                        "message": reason,
                    },
                },
            )
        except ExecutionStateConflict:
            pass

    def _fail_sync(self, job: SourceMemoryBuildJob, error: Exception) -> None:
        try:
            task = self.executions.get_task(job.project_id, job.task_id)
            if task.status is TaskStatus.RUNNING:
                attempt_id = self._attempt_id(job, task.last_attempt_seq)
                self.executions.append_attempt(
                    job.project_id,
                    job.task_id,
                    event_id=f"{attempt_id}-failed",
                    attempt_id=attempt_id,
                    status=TaskAttemptStatus.FAILED,
                    error={
                        "code": "MEMORY_BUILD_FAILED",
                        "message": str(error)[:2000],
                    },
                )
            elif task.status is TaskStatus.QUEUED:
                self.executions.transition_task(
                    job.project_id,
                    job.task_id,
                    expected_status=TaskStatus.QUEUED,
                    status=TaskStatus.FAILED,
                    updates={
                        "error": {
                            "code": "MEMORY_BUILD_FAILED",
                            "message": str(error)[:2000],
                        },
                    },
                )
        except (ExecutionStateConflict, RecordNotFoundError):
            pass

    @staticmethod
    def _attempt_id(job: SourceMemoryBuildJob, attempt_seq: int) -> str:
        """Attempt ids must be fresh per retry round; the open RUNNING
        attempt keeps the id derived from the seq it was admitted at."""
        base = _stable_id("memattempt", job.project_id, job.index_id)
        return f"{base}-r{attempt_seq}"

    # -- build pipeline ------------------------------------------------------

    async def _execute(  # pylint: disable=too-many-statements
        self,
        job: SourceMemoryBuildJob,
    ) -> None:
        task = await asyncio.to_thread(
            self.executions.get_task,
            job.project_id,
            job.task_id,
        )
        if task.status is not TaskStatus.QUEUED:
            return
        attempt_id = self._attempt_id(job, task.last_attempt_seq + 1)
        # Converge on durable artifacts before spending anything: a
        # restart between persistence and the SUCCEEDED event must not
        # replay billed model calls under the same authorization.
        existing = await asyncio.to_thread(self._existing_ref, job)
        if existing is not None:
            await asyncio.to_thread(
                self.executions.append_attempt,
                job.project_id,
                job.task_id,
                event_id=f"{attempt_id}-running",
                attempt_id=attempt_id,
                status=TaskAttemptStatus.RUNNING,
                input={
                    "analysisVersionId": job.index_id,
                    "converged": True,
                },
            )
            await asyncio.to_thread(
                self.executions.append_attempt,
                job.project_id,
                job.task_id,
                event_id=f"{attempt_id}-succeeded",
                attempt_id=attempt_id,
                status=TaskAttemptStatus.SUCCEEDED,
                output={
                    "converged": True,
                    "graphPath": existing.graph_path,
                    "embeddingsPath": existing.embeddings_path,
                    "macroCount": existing.macro_count,
                },
                output_refs=[existing.graph_path],
            )
            logger.info(
                "source memory build converged on existing artifacts: "
                "task=%s",
                job.task_id,
            )
            return
        await asyncio.to_thread(
            self.executions.append_attempt,
            job.project_id,
            job.task_id,
            event_id=f"{attempt_id}-running",
            attempt_id=attempt_id,
            status=TaskAttemptStatus.RUNNING,
            input={
                "analysisVersionId": job.index_id,
                "durationMs": job.duration_ms,
            },
        )
        local_path = Path(job.local_path)
        if not local_path.is_file():
            raise RuntimeError(
                f"source media is no longer available: {local_path}",
            )
        duration_sec = job.duration_ms / 1000.0

        # Phase 1: local frame-diff segmentation.
        segments = await asyncio.to_thread(
            _detect_segments_sync,
            local_path,
            duration_sec,
        )
        macros = [
            MacroEvent(
                macro_id=f"macro_{i:04d}",
                label=f"scene_{i:04d}",
                time_range=[s, e],
            )
            for i, (s, e) in enumerate(segments)
        ]
        logger.info(
            "source memory P1 done: task=%s macros=%d",
            job.task_id,
            len(macros),
        )

        # ASR: reuse the transcript the published index already carries
        # (single billing, consistent text); transcribe only when the
        # index never produced the ASR modality. Available-but-empty
        # coverage (a silent source) is a legitimate final state and
        # must not be billed again.
        transcript: list[dict[str, float | str]] = []
        asr_available, index_transcript = await self._index_transcript(job)
        if asr_available:
            transcript = index_transcript
        elif model_config.get_asr_api_key():
            try:
                result = await asr_model.transcribe(local_path.as_uri())
                transcript = [
                    {
                        "start_sec": segment.start_ms / 1000.0,
                        "end_sec": segment.end_ms / 1000.0,
                        "text": segment.text,
                    }
                    for segment in result.segments
                ]
            except Exception as error:  # pylint: disable=broad-except
                logger.warning(
                    "source memory ASR failed (non-fatal): %s",
                    error,
                )
        _merge_asr_into_macros(macros, transcript)

        semaphore = asyncio.Semaphore(SUBGRAPH_CONCURRENCY)
        work_root = Path(
            tempfile.mkdtemp(prefix=f"source-memory-{job.task_id[:24]}-"),
        )
        try:
            await asyncio.gather(
                *(
                    self._extract_subgraph(
                        macro,
                        local_path,
                        work_root,
                        semaphore,
                    )
                    for macro in macros
                ),
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, work_root, True)
        extracted = sum(
            1
            for macro in macros
            if macro.subgraph
            and (macro.subgraph.entities or macro.subgraph.micro_events)
        )
        logger.info(
            "source memory P2 done: task=%s subgraphs=%d/%d",
            job.task_id,
            extracted,
            len(macros),
        )
        # Quality gate: a memory whose subgraphs all failed would only
        # serve fallback aggregations; fail the build instead of
        # persisting an unusable graph.
        if macros and extracted == 0:
            raise RuntimeError(
                "subgraph extraction failed for every macro segment",
            )

        # Phase 3: text-only aggregation via the configured VLM backend.
        async def call_llm(prompt: str) -> str:
            return await vlm_model.chat_completion(
                [{"type": "text", "text": prompt}],
                temperature=0.3,
                max_tokens=AGGREGATION_MAX_TOKENS,
            )

        root, supers, macro_rels, super_rels = await aggregate_hierarchy(
            macros,
            call_llm,
        )
        memory = HierarchicalGraphMemory(
            video_key=job.index_id,
            video_path=str(local_path),
            root=root,
            super_events=supers,
            macro_events=macros,
            macro_relations=macro_rels,
            super_relations=super_rels,
        )

        nodes = memory.get_all_nodes()
        vectors: np.ndarray | None = None
        if nodes:
            embedded = await embedding_model.embed(
                [str(node["text"]) for node in nodes],
            )
            vectors = np.asarray(embedded, dtype=np.float32)
        index_obj = EmbeddingIndex()
        index_obj.build(nodes, vectors)

        # Outer-VLM review of the projection drafts (the WT6 contract:
        # drafts enter the index surfaces only after review).
        draft_projection = SourceMemoryProjection(
            indexId=job.index_id,
            summary=self._projection_summary(memory),
            semanticEntries=self._projection_entries(memory),
        )
        projection = await self._review_projection(draft_projection)

        output = await asyncio.to_thread(
            self._persist_artifacts_sync,
            job,
            memory,
            index_obj,
            len(nodes),
            projection,
        )
        await asyncio.to_thread(
            self.executions.append_attempt,
            job.project_id,
            job.task_id,
            event_id=f"{attempt_id}-succeeded",
            attempt_id=attempt_id,
            status=TaskAttemptStatus.SUCCEEDED,
            output=output,
            output_refs=[str(output["graphPath"])],
        )
        logger.info(
            "source memory build succeeded: task=%s macros=%d supers=%d "
            "nodes=%d",
            job.task_id,
            len(macros),
            len(supers),
            len(nodes),
        )

    def _existing_ref(
        self,
        job: SourceMemoryBuildJob,
    ) -> SourceMemoryRef | None:
        """Valid persisted artifacts for this build job, if any."""
        try:
            project_root = self.services.projects.project_root(
                job.project_id,
            )
        except Exception:  # pylint: disable=broad-except
            return None
        return load_memory_ref(
            project_root,
            job.index_id,
            job.source_checksum,
        )

    async def _index_transcript(
        self,
        job: SourceMemoryBuildJob,
    ) -> tuple[bool, list[dict[str, float | str]]]:
        """ASR availability and transcript records (seconds) from the
        published index."""
        from services.source_analysis import source_analysis_service

        try:
            index = await asyncio.to_thread(
                source_analysis_service(self.services).load,
                job.project_id,
                job.asset_id,
                job.index_id,
            )
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(
                "source memory could not reload index transcript: %s",
                error,
            )
            return False, []
        asr_coverage = index.coverage.get("asr")
        available = (
            asr_coverage is not None and asr_coverage.mode == "available"
        )
        return available, [
            {
                "start_sec": segment.start_ms / 1000.0,
                "end_sec": segment.end_ms / 1000.0,
                "text": segment.text,
            }
            for segment in index.transcript
        ]

    async def _extract_subgraph(
        self,
        macro: MacroEvent,
        local_path: Path,
        work_root: Path,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            start, end = macro.time_range
            clip_path = work_root / f"{macro.macro_id}.mp4"
            try:
                await asyncio.to_thread(
                    _clip_segment_within_budget_sync,
                    local_path,
                    clip_path,
                    start,
                    end,
                )
            except Exception as error:  # pylint: disable=broad-except
                logger.warning(
                    "source memory clip failed for %s: %s",
                    macro.macro_id,
                    error,
                )
                macro.subgraph = Subgraph(macro_id=macro.macro_id)
                return
            prompt = (
                build_segment_context(macro) + SUBGRAPH_CONSTRUCTION_PROMPT
            )
            content = [
                vlm_model.multimodal_media_part(
                    clip_path.as_uri(),
                    "video",
                    fps=_segment_fps(end - start),
                ),
                {"type": "text", "text": prompt},
            ]
            payload: dict[str, Any] | None = None
            for attempt in range(SUBGRAPH_RETRIES + 1):
                try:
                    response = await vlm_model.chat_completion(
                        content,
                        temperature=0.7,
                        max_tokens=SUBGRAPH_MAX_TOKENS,
                    )
                    candidate = extract_json(response)
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
                except Exception as error:  # pylint: disable=broad-except
                    logger.warning(
                        "source memory subgraph %s attempt %d failed: %s",
                        macro.macro_id,
                        attempt + 1,
                        error,
                    )
            try:
                clip_path.unlink(missing_ok=True)
            except OSError:
                pass
            if payload is None:
                macro.subgraph = Subgraph(macro_id=macro.macro_id)
                return
            apply_subgraph_payload(macro, payload)

    async def _review_projection(
        self,
        draft: SourceMemoryProjection,
    ) -> SourceMemoryProjection:
        """Outer-VLM review of the projection drafts.

        Returns the reviewed projection (``review`` set). On any failure
        the drafts are kept without a review verdict and are therefore
        never merged into the index surfaces (fail-close).
        """
        # Stable per-draft IDs let the server, not the model, own the
        # authoritative time windows: the reviewer may only edit text,
        # tags and confidence or drop entries. Unknown/duplicated IDs or
        # any startMs/endMs drift fail the review closed.
        drafts_by_id = {
            f"entry-{position}": entry
            for position, entry in enumerate(draft.semantic_entries)
        }
        payload = {
            "summary": draft.summary,
            "semanticEntries": [
                {
                    "entryId": entry_id,
                    **entry.model_dump(mode="json", by_alias=True),
                }
                for entry_id, entry in drafts_by_id.items()
            ],
        }
        prompt = PROJECTION_REVIEW_PROMPT + json.dumps(
            payload,
            ensure_ascii=False,
        )
        try:
            response = await vlm_model.chat_completion(
                [{"type": "text", "text": prompt}],
                temperature=0.2,
                max_tokens=PROJECTION_REVIEW_MAX_TOKENS,
            )
            candidate = extract_json(response)
            reviewed_entries: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for raw in candidate["semanticEntries"]:
                entry_id = str(raw.get("entryId") or "")
                original = drafts_by_id.get(entry_id)
                if original is None:
                    raise ValueError(
                        f"review invented an unknown entry: {entry_id!r}",
                    )
                if entry_id in seen_ids:
                    raise ValueError(
                        f"review duplicated entry {entry_id!r}",
                    )
                seen_ids.add(entry_id)
                if (
                    int(raw.get("startMs", -1)) != original.start_ms
                    or int(raw.get("endMs", -1)) != original.end_ms
                ):
                    raise ValueError(
                        f"review changed the time window of {entry_id!r}",
                    )
                reviewed_entries.append(
                    {
                        "text": raw["text"],
                        "tags": raw["tags"],
                        # The draft owns the authoritative window.
                        "startMs": original.start_ms,
                        "endMs": original.end_ms,
                        "confidence": raw["confidence"],
                    },
                )
            reviewed = SourceMemoryProjection.model_validate(
                {
                    "indexId": draft.index_id,
                    "summary": candidate["summary"],
                    "semanticEntries": reviewed_entries,
                    "review": {
                        "status": "approved",
                        "model": model_config.get_vlm_model_name(),
                        "reviewedAt": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                },
            )
            return reviewed
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(
                "projection review failed (drafts stay unreviewed): %s",
                error,
            )
            return draft

    def _persist_artifacts_sync(
        self,
        job: SourceMemoryBuildJob,
        memory: HierarchicalGraphMemory,
        index_obj: EmbeddingIndex,
        node_count: int,
        projection: SourceMemoryProjection,
    ) -> dict[str, Any]:
        project_root = self.services.projects.project_root(job.project_id)
        directory = memory_dir(project_root, job.index_id)
        directory.mkdir(parents=True, exist_ok=True)
        graph_path = directory / GRAPH_FILENAME
        embeddings_path = directory / EMBEDDINGS_FILENAME
        memory.save(str(graph_path))
        index_obj.save(str(embeddings_path))
        # np.savez appends .npz when missing; normalize the artifact name.
        appended = directory / f"{EMBEDDINGS_FILENAME}.npz"
        if appended.exists():
            os.replace(appended, embeddings_path)
        built_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        projection_path = directory / PROJECTION_FILENAME
        tmp = projection_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                projection.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, projection_path)
        meta = {
            "indexId": job.index_id,
            "assetId": job.asset_id,
            "assetVersionId": job.asset_version_id,
            "sourceChecksum": job.source_checksum,
            "builtAt": built_at,
            "macroCount": len(memory.macro_events),
            "superCount": len(memory.super_events),
            "nodeCount": node_count,
            "graphPath": GRAPH_FILENAME,
            "embeddingsPath": EMBEDDINGS_FILENAME,
        }
        meta_path = directory / META_FILENAME
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, meta_path)
        relative = directory.relative_to(project_root).as_posix()
        return {
            "analysisVersionId": job.index_id,
            "macroCount": len(memory.macro_events),
            "superCount": len(memory.super_events),
            "nodeCount": node_count,
            "graphPath": f"{relative}/{GRAPH_FILENAME}",
            "embeddingsPath": f"{relative}/{EMBEDDINGS_FILENAME}",
            "builtAt": built_at,
        }

    @staticmethod
    def _projection_summary(memory: HierarchicalGraphMemory) -> str:
        root = memory.root
        parts = [part for part in (root.title, root.description) if part]
        return "\n\n".join(parts) or "(memory summary unavailable)"

    @staticmethod
    def _projection_entries(
        memory: HierarchicalGraphMemory,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for super_event in memory.super_events:
            if len(super_event.time_range) < 2:
                continue
            start_ms = max(0, int(super_event.time_range[0] * 1000))
            end_ms = int(super_event.time_range[1] * 1000)
            if end_ms <= start_ms:
                continue
            text = super_event.label
            if super_event.description:
                text = f"{super_event.label}: {super_event.description}"
            tags = [
                str(item.get("name", ""))
                if isinstance(item, dict)
                else str(item)
                for item in super_event.key_entities
            ]
            tags = [tag for tag in tags if tag] or ["memory"]
            entries.append(
                {
                    "text": text,
                    "tags": tags[:8],
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "confidence": 0.6,
                },
            )
        return entries

    # -- recovery ------------------------------------------------------------

    def recover_interrupted(self) -> None:
        """Converge or fail interrupted builds; never replay billed work.

        A build cut while RUNNING is closed as FAILED. It is re-queued
        only when complete artifacts are already durable (the follow-up
        attempt converges without new model calls); otherwise it stays
        FAILED and a rebuild requires a fresh commit/authorization.
        QUEUED tasks resume their authorization wait as before.
        """
        for project_id in self._list_project_ids():
            try:
                tasks = self.executions.list_tasks(project_id)
            except Exception:  # pylint: disable=broad-except
                continue
            for task in tasks:
                if task.kind is not TaskKind.SOURCE_MEMORY_BUILD:
                    continue
                if task.status is TaskStatus.RUNNING:
                    task = self._close_interrupted(task)
                    if task.status is not TaskStatus.FAILED:
                        continue
                    job = self._job_from_task(task)
                    if job is None or self._existing_ref(job) is None:
                        logger.warning(
                            "source memory build %s interrupted without "
                            "durable artifacts; not retried automatically",
                            task.task_id,
                        )
                        continue
                    try:
                        task = self.executions.transition_task(
                            task.project_id,
                            task.task_id,
                            expected_status=TaskStatus.FAILED,
                            status=TaskStatus.QUEUED,
                        )
                    except (ExecutionStateConflict, RecordNotFoundError):
                        continue
                if task.status is TaskStatus.QUEUED:
                    job = self._job_from_task(task)
                    if job is not None:
                        self._spawn(job)

    def _list_project_ids(self) -> list[str]:
        try:
            return [
                summary.project_id for summary in self.services.projects.list()
            ]
        except Exception:  # pylint: disable=broad-except
            return []

    def _close_interrupted(self, task: TaskRecord) -> TaskRecord:
        """Close the attempt left RUNNING by a restart as FAILED."""
        try:
            attempts = self.executions.list_attempts(
                task.project_id,
                task.task_id,
            )
            if not attempts:
                return task
            open_attempt = attempts[-1]
            self.executions.append_attempt(
                task.project_id,
                task.task_id,
                event_id=f"{open_attempt.attempt_id}-interrupted",
                attempt_id=open_attempt.attempt_id,
                status=TaskAttemptStatus.FAILED,
                error={
                    "code": "MEMORY_BUILD_INTERRUPTED",
                    "message": "runtime restarted during the memory build",
                },
            )
            return self.executions.get_task(
                task.project_id,
                task.task_id,
            )
        except (ExecutionStateConflict, RecordNotFoundError):
            return task

    @staticmethod
    def _job_from_task(task: TaskRecord) -> SourceMemoryBuildJob | None:
        metadata = task.metadata
        index_id = str(metadata.get("analysisVersionId") or "")
        local_path = str(metadata.get("localPath") or "")
        checksum = str(metadata.get("sourceChecksum") or "")
        version_id = str(metadata.get("assetVersionId") or "")
        target_ref = str(metadata.get("targetRef") or "")
        duration = metadata.get("durationMs")
        required = (index_id, local_path, checksum, version_id)
        if not all(required) or not target_ref.startswith("asset:"):
            return None
        if not isinstance(duration, int):
            return None
        authorization_id = metadata.get("authorizationId")
        return SourceMemoryBuildJob(
            project_id=task.project_id,
            task_id=task.task_id,
            authorization_id=(
                str(authorization_id) if authorization_id else None
            ),
            index_id=index_id,
            asset_id=target_ref.partition(":")[2],
            asset_version_id=version_id,
            source_checksum=checksum,
            duration_ms=duration,
            local_path=local_path,
        )

    # -- query path ----------------------------------------------------------

    async def query_memory(  # pylint: disable=too-many-branches
        self,
        *,
        project_id: str,
        logical_asset_id: str,
        query_type: str,
        query: str | None = None,
        node_types: list[str] | None = None,
        macro_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        if query_type not in QUERY_TYPES:
            raise ValidationError(
                f"unknown query_type: {query_type}; expected one of "
                f"{', '.join(QUERY_TYPES)}",
            )
        from services.source_analysis import source_analysis_service

        index = await asyncio.to_thread(
            source_analysis_service(self.services).load,
            project_id,
            logical_asset_id,
        )
        if index.memory_ref is None:
            return {
                "ok": True,
                "available": False,
                "assetId": logical_asset_id,
                "reason": ("该素材尚未构建长素材记忆；构建在素材理解完成后自动" "排队，需要执行授权通过后才会生成。"),
            }
        project_root = self.services.projects.project_root(project_id)
        graph_path = project_root / index.memory_ref.graph_path
        embeddings_path = project_root / index.memory_ref.embeddings_path
        toolkit = await self._toolkit_for(graph_path, embeddings_path)
        top = max(1, min(int(top_k or 10), 50))
        query_text = (query or "").strip()
        if query_type in {
            "search_nodes",
            "search_ocr",
            "search_asr",
            "enumerate",
        }:
            if not query_text:
                raise ValidationError(f"{query_type} 查询需要 query 参数")
        query_embedding = None
        if query_type in {
            "search_nodes",
            "search_ocr",
            "search_asr",
            "enumerate",
        }:
            query_embedding = await self._embed_query(query_text)

        if query_type == "summary":
            result: Any = toolkit.get_summary()
        elif query_type == "super_events":
            result = toolkit.get_super_events()
        elif query_type == "macro_events":
            filter_id = (macro_id or "").strip()
            if filter_id.startswith("super_"):
                result = toolkit.get_macro_events(super_id=filter_id)
            else:
                result = toolkit.get_macro_events()
        elif query_type == "subgraph":
            if not macro_id:
                raise ValidationError("subgraph 查询需要 macro_id 参数")
            result = toolkit.get_subgraph(macro_id)
        elif query_type == "search_nodes":
            result = toolkit.search_nodes(
                query_text,
                top_k=top,
                node_types=node_types,
                query_embedding=query_embedding,
            )
        elif query_type == "search_ocr":
            result = toolkit.search_ocr_text(
                query_text,
                top_k=top,
                query_embedding=query_embedding,
            )
        elif query_type == "search_asr":
            result = toolkit.search_asr_text(
                query_text,
                top_k=top,
                query_embedding=query_embedding,
            )
        elif query_type == "enumerate":
            result = toolkit.enumerate_events(
                query_text,
                node_types=node_types,
                query_embedding=query_embedding,
            )
        else:  # by_time
            if start_ms is None or end_ms is None or end_ms <= start_ms:
                raise ValidationError(
                    "by_time 查询需要有效的 start_ms/end_ms 半开区间",
                )
            result = toolkit.search_by_time(
                start_sec=start_ms / 1000.0,
                end_sec=end_ms / 1000.0,
            )
        return {
            "ok": True,
            "available": True,
            "assetId": logical_asset_id,
            "analysisVersionId": index.id,
            "queryType": query_type,
            "result": result,
            "hitWindowsMs": _collect_hit_windows(toolkit, result),
        }

    @staticmethod
    async def _embed_query(query_text: str) -> np.ndarray | None:
        try:
            vectors = await embedding_model.embed([query_text])
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(
                "query embedding unavailable, BM25-only search: %s",
                error,
            )
            return None
        return np.asarray(vectors[0], dtype=np.float32)

    async def _toolkit_for(
        self,
        graph_path: Path,
        embeddings_path: Path,
    ) -> MemoryToolkit:
        key = str(graph_path)
        try:
            mtime = graph_path.stat().st_mtime
        except OSError as error:
            raise ValidationError(
                f"graph memory 文件不可用: {graph_path.name}",
            ) from error
        cached = self._toolkits.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        async with self._toolkits_lock:
            cached = self._toolkits.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            toolkit = await asyncio.to_thread(
                _load_toolkit_sync,
                graph_path,
                embeddings_path,
            )
            self._toolkits[key] = (mtime, toolkit)
            return toolkit


def _load_toolkit_sync(
    graph_path: Path,
    embeddings_path: Path,
) -> MemoryToolkit:
    memory = HierarchicalGraphMemory.load(str(graph_path))
    index = None
    if embeddings_path.is_file():
        index = EmbeddingIndex()
        index.load(str(embeddings_path))
    return MemoryToolkit(memory, index)


def _collect_hit_windows(
    toolkit: MemoryToolkit,
    result: Any,
) -> list[dict[str, Any]]:
    """Macro time windows (ms) for every macro hit inside a query result."""
    macro_ids: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            candidate = value.get("macro_id")
            if isinstance(candidate, str) and candidate:
                macro_ids.append(candidate)
            parent = value.get("parent_macro")
            if isinstance(parent, Mapping):
                nested = parent.get("macro_id")
                if isinstance(nested, str) and nested:
                    macro_ids.append(nested)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    windows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in macro_ids:
        if candidate in seen:
            continue
        seen.add(candidate)
        # pylint: disable-next=protected-access
        macro = toolkit._macro_map.get(candidate)
        if macro is None or len(macro.time_range) < 2:
            continue
        windows.append(
            {
                "macroId": candidate,
                "startMs": int(macro.time_range[0] * 1000),
                "endMs": int(macro.time_range[1] * 1000),
            },
        )
    return windows


# ── prompt guidance ─────────────────────────────────────────────────────────

_MEMORY_GUIDANCE_AVAILABLE = """\
## 长素材记忆（query_source_memory）

本次委派的素材已构建层次图记忆（Root → SuperEvent → MacroEvent → 子图节点），\
可用 `query_source_memory` 工具按台词、语义或时间精确定位片段：

- 先用 `summary` / `super_events` 建立全局认知，再用 `macro_events`（可传 \
super_id 过滤）缩小范围；
- 台词线索用 `search_asr`，屏幕文字用 `search_ocr`，事件/实体语义用 \
`search_nodes`，计数/枚举类问题用 `enumerate`，已知时间段用 `by_time`；
- 命中后用 `subgraph` 下钻目标 macro 查看事件、实体与关系细节；
- 返回的 `hitWindowsMs` 是候选时间窗；结论必须回到原片窄窗核验：按该窗口重新\
观察原生视频帧确认内容一致后才可写入素材理解或回复。"""

_MEMORY_GUIDANCE_UNAVAILABLE = """\
## 长素材记忆

本次委派的素材尚无层次图记忆（未达到时长阈值、embedding 未配置或构建未完成）。\
`query_source_memory` 会返回 available=false；此时按常规流程直接观察原生媒体。"""


def memory_guidance_for_targets(
    project_root: Path | None,
    project: Any,
    target_refs: list[str] | tuple[str, ...] | None,
) -> str:
    """Render the memory_guidance prompt block for a delegation."""
    if project_root is None or project is None:
        return _MEMORY_GUIDANCE_UNAVAILABLE
    for target_ref in target_refs or ():
        kind, _, identifier = str(target_ref).partition(":")
        if kind != "asset" or not identifier:
            continue
        try:
            if has_built_memory(project_root, project, identifier):
                return _MEMORY_GUIDANCE_AVAILABLE
        except Exception:  # pylint: disable=broad-except
            continue
    return _MEMORY_GUIDANCE_UNAVAILABLE


# ── service registry ────────────────────────────────────────────────────────

_SERVICES: dict[str, SourceMemoryService] = {}


def source_memory_service(services: Any) -> SourceMemoryService:
    key = str(services.root)
    instance = _SERVICES.get(key)
    if instance is None or instance.services is not services:
        instance = SourceMemoryService(services)
        _SERVICES[key] = instance
    return instance


def clear_source_memory_service_registry() -> None:
    _SERVICES.clear()


def recover_interrupted_source_memory(services: Any) -> None:
    """Startup hook: converge builds interrupted by a restart."""
    source_memory_service(services).recover_interrupted()


__all__ = [
    "MEMORY_BUILD_THRESHOLD_MS",
    "QUERY_TYPES",
    "SOURCE_MEMORY_OPERATION",
    "SourceMemoryBuildJob",
    "SourceMemoryProjection",
    "SourceMemorySemanticDraft",
    "SourceMemoryService",
    "clear_source_memory_service_registry",
    "has_built_memory",
    "load_memory_ref",
    "memory_build_threshold_ms",
    "memory_dir",
    "memory_guidance_for_targets",
    "recover_interrupted_source_memory",
    "source_memory_service",
]
