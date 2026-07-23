# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=consider-using-with,raise-missing-from,too-many-branches
# pylint: disable=too-many-statements,unused-argument
"""File-native local video execution for edit and composition commands.

The service freezes one ``project.json`` snapshot, resolves every input through
its ``IndexedFile`` and immutable version record, persists Run/Task/Attempt
facts, and executes an injectable runner in
``<project>/runtime/task-work/<task-id>`` without holding Project or Runtime
locks.  A successful output is published through :class:`AssetFileStore` and
then indexed and selected by one Project commit.  Late cancelled or stale
outputs remain immutable but unindexed and are recorded in Runtime quarantine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
from typing import Any, Protocol, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from domain.enums import (
    CreatorCommandType,
    SpecialistRole,
    SpecialistRunStatus,
    TaskKind,
    TaskStatus,
)
from domain.errors import (
    ConflictError,
    NotFoundError,
    StorageIntegrityError,
    ValidationError,
)
from services.media_files.overlay import (
    PET_OS_VIBES,
    render_interview_summary_overlay,
    render_pet_os_overlay,
)
from services.media_files.motion_overlay import render_motion_overlay
from services.project_files.assets import (
    AssetAlreadyExists,
    AssetFileError,
    AssetFileStore,
)
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    ArtifactVersionRenderSource,
    EditCreation,
    ElementOutputRenderSource,
    IndexedFile,
    OverlayCreation,
    Project,
    R2VCreation,
    SourceVersionRenderSource,
    Timeline,
    TimelineElement,
    TransitionCreation,
)
from services.media_files.element_adapter import (
    find_timeline_element,
    selected_element_output,
)
from services.project_files.remote_cache import (
    public_source_url,
    resolve_remote_cache,
)
from services.project_files.store import ProjectSnapshot
from services.runtime_files.atomic_store import (
    AtomicJsonRecordStore,
    canonical_json_bytes,
)
from services.runtime_files.errors import RecordNotFoundError
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskAttemptStatus,
    TaskRecord,
)
from services.runtime_files.execution_store import (
    ExecutionPayloadConflict,
    ExecutionStateConflict,
    ProjectExecutionStore,
)
from services.runtime_files.models import ChangeOrigin, ReviewPolicy
from services.runtime_files.media_probe import MediaProbeError, probe_media
from services.runtime_files.runtime_dependencies import resolve_ffmpeg

# pylint: disable=no-name-in-module
from utils.logger import setup_logger
from utils.remote_download import download_remote_file

# pylint: enable=no-name-in-module

if TYPE_CHECKING:
    from services.project_files.facade import CreatorFileServices


logger = setup_logger("services.media_files.local_execution")

_RETIRED_MOTION_MOTIFS = (
    "speed_lines",
    "side_eye",
    "sassy_cat",
    "surprised_cat",
    "happy_cat",
    "confused_cat",
)

_LOCAL_MEDIA_COMMANDS = frozenset(
    {
        CreatorCommandType.EXECUTE_EDIT,
        CreatorCommandType.COMPOSE_FINAL_VIDEO,
    },
)
_MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
_DEFAULT_FFMPEG_TIMEOUT_SECONDS = 15 * 60.0
_DEFAULT_FFMPEG_TERMINATION_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class LocalMediaInput:
    version_id: str
    file_id: str | None
    checksum: str
    media_type: str
    path: Path
    source_ref: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    duration_seconds: float | None = None
    original_sound: str = "preserve"
    location: Mapping[str, Any] | None = None
    overlay: Mapping[str, Any] | None = None
    motions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LocalMediaExecutionSpec:
    command: CreatorCommandType
    target_ref: str
    task_id: str
    work_dir: Path
    output_path: Path
    inputs: tuple[LocalMediaInput, ...]
    transitions: tuple[dict[str, Any], ...]
    audio_plan: Mapping[str, Any] | str
    expected_duration_seconds: float | None
    canvas_size: tuple[int, int]
    on_element_done: Callable[[int, int], None] | None = None


class LocalMediaRunner(Protocol):
    """Runner boundary.  Implementations write exactly ``spec.output_path``."""

    async def render(self, spec: LocalMediaExecutionSpec) -> Mapping[str, Any]:
        ...


class FfmpegLocalMediaRunner:
    """Default local ffmpeg runner with no global ``/generated`` authority."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = _DEFAULT_FFMPEG_TIMEOUT_SECONDS,
        termination_grace_seconds: float = (
            _DEFAULT_FFMPEG_TERMINATION_GRACE_SECONDS
        ),
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            not math.isfinite(termination_grace_seconds)
            or termination_grace_seconds <= 0
        ):
            raise ValueError(
                "termination_grace_seconds must be finite and positive",
            )
        self.executable = executable or resolve_ffmpeg() or "ffmpeg"
        self.timeout_seconds = float(timeout_seconds)
        self.termination_grace_seconds = float(termination_grace_seconds)

    async def render(self, spec: LocalMediaExecutionSpec) -> Mapping[str, Any]:
        self._validate_supported_directives(spec)
        overlay_warnings = await asyncio.to_thread(self._render_sync, spec)
        metadata: dict[str, Any] = {"runner": "ffmpeg"}
        if overlay_warnings:
            metadata["overlayWarnings"] = overlay_warnings
        return {
            "media_type": "video/mp4",
            "duration_seconds": spec.expected_duration_seconds,
            "metadata": metadata,
        }

    def _render_sync(self, spec: LocalMediaExecutionSpec) -> list[str]:
        if not spec.inputs:
            raise ValidationError("本地媒体执行至少需要一个输入")
        concat_inputs: list[Path] = []
        overlay_warnings: list[str] = []
        if spec.command in (
            CreatorCommandType.EXECUTE_EDIT,
            CreatorCommandType.COMPOSE_FINAL_VIDEO,
        ) or any(
            item.start_seconds is not None
            or item.location is not None
            or item.overlay is not None
            or item.motions
            for item in spec.inputs
        ):
            segment_dir = _ensure_real_directory_chain(
                spec.work_dir,
                "segments",
            )
            for index, item in enumerate(spec.inputs):
                start_seconds = item.start_seconds or 0.0
                end_seconds = item.end_seconds
                if end_seconds is None:
                    if item.duration_seconds is None:
                        raise ValidationError(
                            "Edit Element 缺少可执行 source range",
                        )
                    end_seconds = start_seconds + item.duration_seconds
                segment_duration = end_seconds - start_seconds
                segment = segment_dir / f"{index:06d}.mp4"
                placement_filter = self._placement_filter(
                    item.location,
                    canvas_size=spec.canvas_size,
                    duration_seconds=segment_duration,
                )
                self._run(
                    [
                        "-y",
                        "-ss",
                        f"{start_seconds:.6f}",
                        "-t",
                        f"{segment_duration:.6f}",
                        "-i",
                        os.fspath(item.path),
                        "-filter_complex",
                        placement_filter,
                        "-map",
                        "[outv]",
                        "-map",
                        "0:a?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        os.fspath(segment),
                    ],
                    cwd=spec.work_dir,
                )
                warning = self._apply_overlay(item, segment)
                if warning is not None:
                    overlay_warnings.append(
                        f"{item.source_ref or item.version_id}: {warning}",
                    )
                for warning in self._apply_motion_overlays(item, segment):
                    overlay_warnings.append(
                        f"{item.source_ref or item.version_id}: {warning}",
                    )
                concat_inputs.append(segment)
                if spec.on_element_done is not None:
                    spec.on_element_done(index + 1, len(spec.inputs))
        else:
            concat_inputs.extend(item.path for item in spec.inputs)
        self._concat(concat_inputs, spec.output_path, work_dir=spec.work_dir)
        return overlay_warnings

    @staticmethod
    def _normalized_location(
        raw: Mapping[str, Any] | None,
    ) -> dict[str, float]:
        defaults = {
            "x": 0.5,
            "y": 0.5,
            "width": 1.0,
            "height": 1.0,
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "rotation_degrees": 0.0,
            "opacity": 1.0,
        }
        if not isinstance(raw, Mapping):
            return defaults
        normalized = dict(defaults)
        for key, fallback in defaults.items():
            try:
                value = float(raw.get(key, fallback))
            except (TypeError, ValueError):
                value = fallback
            normalized[key] = value if math.isfinite(value) else fallback
        normalized["width"] = max(1e-6, normalized["width"])
        normalized["height"] = max(1e-6, normalized["height"])
        normalized["anchor_x"] = min(1.0, max(0.0, normalized["anchor_x"]))
        normalized["anchor_y"] = min(1.0, max(0.0, normalized["anchor_y"]))
        normalized["opacity"] = min(1.0, max(0.0, normalized["opacity"]))
        return normalized

    @classmethod
    def _placement_filter(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        canvas_size: tuple[int, int],
        duration_seconds: float,
    ) -> str:
        """Build one anchor-based placement graph shared with the UI preview."""

        location = cls._normalized_location(raw)
        canvas_width, canvas_height = canvas_size
        box_width = max(2, round(canvas_width * location["width"]) // 2 * 2)
        box_height = max(2, round(canvas_height * location["height"]) // 2 * 2)
        anchor_x = round(box_width * location["anchor_x"])
        anchor_y = round(box_height * location["anchor_y"])
        padded_width = max(2, 2 * max(anchor_x, box_width - anchor_x))
        padded_height = max(2, 2 * max(anchor_y, box_height - anchor_y))
        pad_x = padded_width // 2 - anchor_x
        pad_y = padded_height // 2 - anchor_y
        radians = math.radians(location["rotation_degrees"])
        rotated_width = max(
            2,
            (
                math.ceil(
                    abs(padded_width * math.cos(radians))
                    + abs(padded_height * math.sin(radians)),
                )
                + 1
            )
            // 2
            * 2,
        )
        rotated_height = max(
            2,
            (
                math.ceil(
                    abs(padded_width * math.sin(radians))
                    + abs(padded_height * math.cos(radians)),
                )
                + 1
            )
            // 2
            * 2,
        )
        overlay_x = round(location["x"] * canvas_width - rotated_width / 2)
        overlay_y = round(location["y"] * canvas_height - rotated_height / 2)
        return (
            f"[0:v]scale={box_width}:{box_height}:force_original_aspect_ratio=increase,"
            f"crop={box_width}:{box_height},setsar=1,format=rgba,"
            f"colorchannelmixer=aa={location['opacity']:.6f},"
            f"pad={padded_width}:{padded_height}:{pad_x}:{pad_y}:color=0x00000000,"
            f"rotate={location['rotation_degrees']:.6f}*PI/180:"
            f"ow={rotated_width}:oh={rotated_height}:c=none[fg];"
            f"color=c=black:s={canvas_width}x{canvas_height}:r=30:"
            f"d={duration_seconds:.6f}[bg];"
            f"[bg][fg]overlay={overlay_x}:{overlay_y}:shortest=1,format=yuv420p[outv]"
        )

    def _apply_overlay(
        self,
        item: LocalMediaInput,
        segment: Path,
    ) -> str | None:
        overlay = self._normalized_overlay(item)
        if overlay is None:
            return None
        video_size = self._probe_video_size(segment)
        rendered = segment.with_name(f"{segment.stem}-overlay.mp4")
        styled_error: str | None = None
        motion = overlay.get("motion")
        if (
            isinstance(motion, Mapping)
            and str(motion.get("html") or "").strip()
        ):
            # A text overlay carrying a generated motion document renders
            # through the deterministic motion pipeline; the procedural
            # template below stays as the fallback so the required text
            # bubble never silently disappears.
            result = render_motion_overlay(
                ffmpeg_path=self.executable,
                input_path=segment,
                output_path=rendered,
                html=str(motion["html"]),
                fps=min(60, max(8, int(motion.get("fps") or 24))),
                loop=bool(motion.get("loop", True)),
                video_size=video_size,
                appear_at=overlay["appear_at"],
                duration=overlay["duration"],
                location=overlay.get("location"),
                viewport_inset=0.05,
            )
            if result.success:
                os.replace(rendered, segment)
                return None
            rendered.unlink(missing_ok=True)
            styled_error = (
                f"{overlay['kind']} 生成样式渲染失败，已回退固定样式: "
                f"{result.error or '未知错误'}"
            )
        if overlay["kind"] == "pet_os":
            result = render_pet_os_overlay(
                ffmpeg_path=self.executable,
                input_path=segment,
                output_path=rendered,
                text=overlay["text"],
                vibe=overlay["vibe"],
                video_size=video_size,
                appear_at=overlay["appear_at"],
                duration=overlay["duration"],
                location=overlay.get("location"),
            )
        else:
            result = render_interview_summary_overlay(
                ffmpeg_path=self.executable,
                input_path=segment,
                output_path=rendered,
                text=overlay["text"],
                video_size=video_size,
                appear_at=overlay["appear_at"],
                duration=overlay["duration"],
                location=overlay.get("location"),
            )
        if not result.success:
            rendered.unlink(missing_ok=True)
            return result.error or f"{overlay['kind']} overlay 渲染失败"
        os.replace(rendered, segment)
        return styled_error

    def _apply_motion_overlays(
        self,
        item: LocalMediaInput,
        segment: Path,
    ) -> list[str]:
        """Burn every generated motion overlay into one prepared segment.

        Motion overlays are additive polish: a failed document only records a
        warning so the edit itself still completes.
        """

        warnings: list[str] = []
        video_size: tuple[int, int] | None = None
        for index, motion in enumerate(item.motions):
            normalized = self._normalized_motion(item, motion)
            if normalized is None:
                continue
            if video_size is None:
                video_size = self._probe_video_size(segment)
            rendered = segment.with_name(
                f"{segment.stem}-motion-{index:02d}.mp4",
            )
            result = render_motion_overlay(
                ffmpeg_path=self.executable,
                input_path=segment,
                output_path=rendered,
                html=normalized["html"],
                fps=normalized["fps"],
                loop=normalized["loop"],
                video_size=video_size,
                appear_at=normalized["appear_at"],
                duration=normalized["duration"],
                location=normalized.get("location"),
                viewport_inset=0.05,
            )
            if not result.success:
                rendered.unlink(missing_ok=True)
                warnings.append(
                    f"motion overlay {normalized['element_id']} 渲染失败: "
                    f"{result.error or '未知错误'}",
                )
                continue
            os.replace(rendered, segment)
        return warnings

    @staticmethod
    def _normalized_motion(
        item: LocalMediaInput,
        raw: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        html = str(raw.get("html") or "")
        if not html.strip():
            return None
        if any(
            f'data-motion-motif="{motif}"' in html
            or f"data-motion-motif='{motif}'" in html
            for motif in _RETIRED_MOTION_MOTIFS
        ):
            return None
        segment_duration = (
            item.end_seconds - item.start_seconds
            if item.start_seconds is not None and item.end_seconds is not None
            else item.duration_seconds
        )
        if segment_duration is None or not math.isfinite(segment_duration):
            return None
        try:
            appear_at = float(raw.get("appear_at", 0.0))
        except (TypeError, ValueError):
            appear_at = 0.0
        if not math.isfinite(appear_at):
            appear_at = 0.0
        appear_at = max(0.0, appear_at)
        if appear_at >= segment_duration:
            return None
        try:
            duration = float(raw.get("duration", segment_duration - appear_at))
        except (TypeError, ValueError):
            duration = segment_duration - appear_at
        if not math.isfinite(duration) or duration <= 0:
            duration = segment_duration - appear_at
        duration = min(duration, segment_duration - appear_at)
        try:
            fps = int(raw.get("fps", 24))
        except (TypeError, ValueError):
            fps = 24
        return {
            "element_id": str(raw.get("element_id") or ""),
            "html": html,
            "fps": min(60, max(8, fps)),
            "loop": bool(raw.get("loop", True)),
            "appear_at": appear_at,
            "duration": duration,
            "location": (
                dict(raw["location"])
                if isinstance(raw.get("location"), Mapping)
                else None
            ),
        }

    @staticmethod
    def _normalized_overlay(item: LocalMediaInput) -> dict[str, Any] | None:
        raw = item.overlay
        if not isinstance(raw, Mapping):
            return None
        kind = str(raw.get("kind") or "").strip().casefold()
        text = str(raw.get("text") or "").strip()
        if kind not in {"pet_os", "interview_summary"} or not text:
            return None
        segment_duration = (
            item.end_seconds - item.start_seconds
            if item.start_seconds is not None and item.end_seconds is not None
            else item.duration_seconds
        )
        if segment_duration is None or not math.isfinite(segment_duration):
            return None
        try:
            appear_at = float(raw.get("appear_at", 0.0))
        except (TypeError, ValueError):
            appear_at = 0.0
        if not math.isfinite(appear_at):
            appear_at = 0.0
        appear_at = max(0.0, appear_at)
        if appear_at >= segment_duration:
            return None
        try:
            duration = float(raw.get("duration", segment_duration - appear_at))
        except (TypeError, ValueError):
            duration = segment_duration - appear_at
        if not math.isfinite(duration) or duration <= 0:
            duration = segment_duration - appear_at
        duration = min(duration, segment_duration - appear_at)
        vibe = str(raw.get("vibe") or "chill").strip().casefold()
        return {
            "kind": kind,
            "text": text,
            "vibe": vibe if vibe in PET_OS_VIBES else "chill",
            "appear_at": appear_at,
            "duration": duration,
            "location": (
                dict(raw["location"])
                if isinstance(raw.get("location"), Mapping)
                else None
            ),
            "motion": (
                dict(raw["motion"])
                if isinstance(raw.get("motion"), Mapping)
                else None
            ),
        }

    def _probe_video_size(self, path: Path) -> tuple[int, int]:
        try:
            metadata = probe_media(
                os.fspath(path),
                ffmpeg_path=self.executable,
                timeout=min(self.timeout_seconds, 30.0),
            )
            if metadata.width and metadata.height:
                return metadata.width, metadata.height
        except MediaProbeError:
            pass
        return 1280, 720

    @staticmethod
    def _validate_supported_directives(spec: LocalMediaExecutionSpec) -> None:
        unsupported_transitions: list[str] = []
        for transition in spec.transitions:
            kind = str(transition.get("kind") or "cut").strip().casefold()
            raw_duration = transition.get("duration_ms", 0)
            duration_ms = (
                int(raw_duration)
                if isinstance(raw_duration, (int, float))
                and not isinstance(raw_duration, bool)
                else 0
            )
            if kind != "cut" or duration_ms != 0:
                unsupported_transitions.append(
                    f"{kind or '<empty>'}:{duration_ms}ms",
                )
        if unsupported_transitions:
            raise ValidationError(
                "默认 ffmpeg runner 尚不支持非硬切 transition: "
                + ", ".join(unsupported_transitions),
            )

        unsupported_original_sound = sorted(
            {
                item.original_sound.strip()
                for item in spec.inputs
                if item.original_sound.strip().casefold()
                not in {"", "preserve"}
            },
        )
        if unsupported_original_sound:
            raise ValidationError(
                "默认 ffmpeg runner 尚不支持 original_sound 指令: "
                + ", ".join(unsupported_original_sound),
            )

        if isinstance(spec.audio_plan, Mapping):
            preserve_original = spec.audio_plan.get("preserve_original", True)
            instructions = [
                str(spec.audio_plan.get(key) or "").strip()
                for key in ("music_prompt", "voiceover", "notes")
            ]
            if preserve_original is not True or any(instructions):
                raise ValidationError(
                    "默认 ffmpeg runner 仅支持保留原声且无配乐、旁白或备注的 audio_plan",
                )
        elif str(spec.audio_plan or "").strip():
            # Planning agents record free-text audio ideas on the
            # composition (e.g. "轻快节奏背景音乐").  The string form carries no
            # structured directive the local pipeline could execute, and
            # neither the UI nor any command lets users clear the field, so
            # failing here would dead-end COMPOSE_FINAL_VIDEO for every
            # agent-driven project.  Preserve the original audio and surface
            # the note in the log instead.
            logger.warning(
                "默认 ffmpeg runner 忽略自由文本 audio_plan（保留原声）: %s",
                str(spec.audio_plan).strip(),
            )

    def _concat(
        self,
        inputs: Sequence[Path],
        output: Path,
        *,
        work_dir: Path,
    ) -> None:
        manifest = work_dir / "concat.txt"
        manifest.write_text(
            "".join(f"file '{_ffconcat_path(path)}'\n" for path in inputs),
            encoding="utf-8",
        )
        self._run(
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                os.fspath(manifest),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                os.fspath(output),
            ],
            cwd=work_dir,
        )

    def _run(self, arguments: Sequence[str], *, cwd: Path) -> None:
        try:
            process = subprocess.Popen(
                [self.executable, *arguments],
                cwd=cwd,
                # ffmpeg 默认读 stdin；继承服务进程的 tty 会在后台运行时
                # 触发 SIGTTIN 把进程组挂起，必须显式断开。
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ValidationError("本机未安装 ffmpeg") from exc
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.communicate(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.communicate(timeout=self.termination_grace_seconds)
                except subprocess.TimeoutExpired:
                    # The child has already received the strongest portable
                    # signal.  Never turn cleanup into another unbounded wait.
                    pass
            raise ConflictError(
                f"ffmpeg 执行超时（{self.timeout_seconds:g} 秒）",
            ) from exc
        if process.returncode != 0:
            detail = (stderr or stdout or "ffmpeg failed")[-4000:]
            raise ConflictError(f"ffmpeg 执行失败: {detail}")


@dataclass(frozen=True, slots=True)
class FileLocalMediaExecutionResult:
    task_id: str
    run_id: str
    transaction_id: str
    artifact_version_id: str
    project_etag: str
    project_generation: int
    replayed: bool

    def command_response(self, command_id: str) -> dict[str, Any]:
        return {
            "commandId": command_id,
            "status": "APPLIED",
            "eventSeq": 0,
            "transactionId": self.transaction_id,
            "workingHead": self.project_etag,
        }


@dataclass(frozen=True, slots=True)
class _FrozenInput:
    version_id: str
    indexed: IndexedFile | None
    checksum: str
    source_ref: str
    media_type: str
    source_url: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    duration_seconds: float | None = None
    original_sound: str = "preserve"
    location: Mapping[str, Any] | None = None
    overlay: Mapping[str, Any] | None = None
    motions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class _ResolvedExecution:
    command: CreatorCommandType
    target_ref: str
    task_kind: TaskKind
    inputs: tuple[_FrozenInput, ...]
    transitions: tuple[dict[str, Any], ...]
    audio_plan: Mapping[str, Any] | str
    expected_duration_seconds: float | None
    canvas_size: tuple[int, int]
    slot_id: str
    slot_kind: str
    owner_ref: str
    artifact_name: str
    read_set: tuple[dict[str, Any], ...]
    source_selections: tuple[dict[str, Any], ...]


def _stable_id(prefix: str, project_id: str, key: str) -> str:
    digest = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:file-local-media:{prefix}:{project_id}:{key}",
    ).hex
    return f"{prefix}-{digest}"


def _fingerprint(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _target_id(target_ref: str, prefix: str) -> str:
    expected = f"{prefix}:"
    if not target_ref.startswith(expected) or not target_ref[len(expected) :]:
        raise ValidationError(f"targetRef 必须是 {expected}<id>")
    return target_ref[len(expected) :]


def _target_timeline(project: Project, target_ref: str) -> Timeline:
    """Resolve a timeline targetRef.

    Canonical Timeline IDs already carry the ``timeline:`` prefix (for
    example ``timeline:main``), so both ``timeline:timeline:main`` and the
    bare ID ``timeline:main`` must resolve to the same Timeline.
    """

    timeline_id = _target_id(target_ref, "timeline")
    timeline = project.timelines.items.get(timeline_id)
    if timeline is None:
        timeline = project.timelines.items.get(target_ref)
    if timeline is None:
        raise NotFoundError("Timeline 不存在")
    return timeline


def _canvas_size(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    """Resolve Project display settings to one even-pixel render canvas."""

    try:
        width_part, height_part = aspect_ratio.split(":", 1)
        ratio = float(width_part) / float(height_part)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        ratio = 16 / 9
    base = 1080 if "1080" in str(resolution).casefold() else 720
    if ratio >= 1:
        height = base
        width = round(height * ratio)
    else:
        width = base
        height = round(width / ratio)
    return (max(2, width // 2 * 2), max(2, height // 2 * 2))


def _ffconcat_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")


def _require_real_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise StorageIntegrityError(
            f"Runtime task-work 父目录不存在: {path}",
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise StorageIntegrityError(f"Runtime task-work 路径不安全: {path}")


def _ensure_real_directory_chain(root: Path, *segments: str) -> Path:
    """Create one descendant chain without ever following a symlink parent."""

    _require_real_directory(root)
    current = root
    for segment in segments:
        if (
            not segment
            or segment in {".", ".."}
            or "/" in segment
            or "\\" in segment
        ):
            raise StorageIntegrityError(
                f"Runtime task-work 路径段不安全: {segment!r}",
            )
        child = current / segment
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _require_real_directory(child)
        current = child
    return current


def _indexed_version(
    project: Project,
    version_id: str,
    *,
    require_artifact: bool,
) -> tuple[IndexedFile | None, Any]:
    version = (
        project.assets.artifact_versions_by_id.get(version_id)
        if require_artifact
        else project.assets.source_versions_by_id.get(version_id)
    )
    if version is None:
        label = "ArtifactVersion" if require_artifact else "AssetVersion"
        raise NotFoundError(f"{label} 不存在: {version_id}")
    indexed = (
        project.assets.files_by_id.get(version.file_id)
        if version.file_id is not None
        else None
    )
    if indexed is None and not (
        not require_artifact and public_source_url(version) is not None
    ):
        raise StorageIntegrityError(f"版本缺少 IndexedFile: {version_id}")
    media_type = (
        indexed.media_type if indexed is not None else version.media_type
    )
    if not media_type.casefold().startswith("video/"):
        raise ValidationError(f"媒体输入不是视频: {version_id}")
    return indexed, version


def _read_set_item(
    kind: str,
    version_id: str,
    indexed: IndexedFile | None,
    checksum: str,
) -> dict[str, Any]:
    return {
        "ref": f"{kind}:{version_id}",
        "versionId": version_id,
        "fileId": indexed.file_id if indexed is not None else None,
        "checksum": checksum,
    }


def _resolved_element_input(
    project: Project,
    element: TimelineElement,
) -> tuple[IndexedFile | None, Any, str]:
    render_source = element.render_source
    if isinstance(render_source, ElementOutputRenderSource):
        selected = selected_element_output(
            project,
            find_timeline_element(project, render_source.element_id)[1],
            render_source.output_name,
        )
        if selected is None:
            raise ConflictError(
                f"Element {element.element_id} 的 render_source 尚无选中版本",
            )
        version_id = selected[1]
        indexed, version = _indexed_version(
            project,
            version_id,
            require_artifact=True,
        )
        return indexed, version, "artifact-version"
    if isinstance(render_source, ArtifactVersionRenderSource):
        indexed, version = _indexed_version(
            project,
            render_source.version_id,
            require_artifact=True,
        )
        return indexed, version, "artifact-version"
    if isinstance(render_source, SourceVersionRenderSource):
        indexed, version = _indexed_version(
            project,
            render_source.version_id,
            require_artifact=False,
        )
        return indexed, version, "asset-version"
    raise ConflictError(f"Element {element.element_id} 尚无可渲染来源")


def _edit_overlay(
    timeline: Timeline,
    element: TimelineElement,
) -> Mapping[str, Any] | None:
    """Project one independently persisted Overlay onto one edit input.

    The domain permits any number of overlapping Elements.  The current local
    runner can burn in one text overlay per source segment, so it fails clearly
    when the Timeline asks it to render more than that.
    """

    overlays = sorted(
        (
            candidate
            for candidate in timeline.elements_by_id.values()
            if candidate.enabled
            and isinstance(candidate.creation, OverlayCreation)
            and candidate.creation.overlay_kind
            in {"pet_os", "interview_summary"}
            and candidate.span.overlaps(element.span)
        ),
        key=lambda candidate: (candidate.z_index, candidate.element_id),
    )
    if not overlays:
        return None
    if len(overlays) > 1:
        raise ValidationError(
            f"本地 runner 暂不支持在 Edit Element {element.element_id} 上叠加多个 Overlay Element",
        )
    overlay = overlays[0]
    intersection_start = max(element.span.start_tick, overlay.span.start_tick)
    intersection_end = min(element.span.end_tick, overlay.span.end_tick)
    motion = overlay.creation.motion
    return {
        "kind": overlay.creation.overlay_kind,
        "text": overlay.creation.text,
        "vibe": overlay.creation.vibe,
        "location": (
            overlay.location.model_dump(mode="json")
            if overlay.location is not None
            else None
        ),
        "motion": (
            {"html": motion.html, "fps": motion.fps, "loop": motion.loop}
            if motion is not None
            else None
        ),
        "appear_at": (intersection_start - element.span.start_tick)
        / timeline.ticks_per_second,
        "duration": (intersection_end - intersection_start)
        / timeline.ticks_per_second,
        "element_id": overlay.element_id,
    }


def _validate_contiguous_edit_elements(
    elements: Sequence[TimelineElement],
) -> None:
    """Match Timeline edit positions to the concat-based local runner.

    The runner emits selected source ranges back-to-back; it does not render
    leading or interstitial blank media. Reject a Timeline that claims gaps or
    overlaps instead of publishing an artifact whose duration disagrees with
    the Timeline and preview playhead.
    """

    expected_start_tick = 0
    for element in elements:
        if element.span.start_tick != expected_start_tick:
            raise ValidationError(
                "Edit Timeline 必须从 0 开始并连续排列；"
                f"{element.element_id} 的 span.start_tick="
                f"{element.span.start_tick}，期望 {expected_start_tick}。"
                "span 表示片段在成片中的位置；源素材时间只写在 "
                "render_source.source_in_tick/source_out_tick。",
            )
        expected_start_tick = element.span.end_tick


def _edit_motion_overlays(
    timeline: Timeline,
    element: TimelineElement,
) -> tuple[Mapping[str, Any], ...]:
    """Project every generated motion Overlay onto one edit input.

    Unlike the single burned-in text overlay, any number of motion documents
    may stack on one segment; they composite in ``z_index`` order.
    """

    overlays = sorted(
        (
            candidate
            for candidate in timeline.elements_by_id.values()
            if candidate.enabled
            and isinstance(candidate.creation, OverlayCreation)
            and candidate.creation.overlay_kind == "motion"
            and candidate.creation.motion is not None
            and candidate.span.overlaps(element.span)
        ),
        key=lambda candidate: (candidate.z_index, candidate.element_id),
    )
    motions: list[Mapping[str, Any]] = []
    for overlay in overlays:
        motion = overlay.creation.motion
        intersection_start = max(
            element.span.start_tick,
            overlay.span.start_tick,
        )
        intersection_end = min(element.span.end_tick, overlay.span.end_tick)
        motions.append(
            {
                "element_id": overlay.element_id,
                "html": motion.html,
                "fps": motion.fps,
                "loop": motion.loop,
                "location": (
                    overlay.location.model_dump(mode="json")
                    if overlay.location is not None
                    else None
                ),
                "appear_at": (intersection_start - element.span.start_tick)
                / timeline.ticks_per_second,
                "duration": (intersection_end - intersection_start)
                / timeline.ticks_per_second,
            },
        )
    return tuple(motions)


def _timeline_execution(
    *,
    project: Project,
    timeline: Timeline,
    target_ref: str,
    command: CreatorCommandType,
) -> _ResolvedExecution:
    creation_types = (
        (EditCreation,)
        if command is CreatorCommandType.EXECUTE_EDIT
        else (R2VCreation, EditCreation)
    )
    visual_elements = sorted(
        (
            element
            for element in timeline.elements_by_id.values()
            if element.enabled and isinstance(element.creation, creation_types)
        ),
        key=lambda element: (element.span.start_tick, element.element_id),
    )
    if not visual_elements:
        label = (
            "Edit"
            if command is CreatorCommandType.EXECUTE_EDIT
            else "R2V/Edit"
        )
        raise ConflictError(f"Timeline 没有可执行的 {label} Element")
    if command is CreatorCommandType.EXECUTE_EDIT:
        _validate_contiguous_edit_elements(visual_elements)
    inputs: list[_FrozenInput] = []
    read_set: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    durations: list[float | None] = []
    for order, element in enumerate(visual_elements, 1):
        indexed, version, version_kind = _resolved_element_input(
            project,
            element,
        )
        render_source = element.render_source
        start_seconds: float | None = None
        end_seconds: float | None = None
        if isinstance(element.creation, EditCreation):
            assert isinstance(render_source, SourceVersionRenderSource)
            assert not isinstance(render_source, ArtifactVersionRenderSource)
            assert render_source.source_out_tick is not None
            start_seconds = (
                render_source.source_in_tick / timeline.ticks_per_second
            )
            end_seconds = (
                render_source.source_out_tick / timeline.ticks_per_second
            )
        inputs.append(
            _FrozenInput(
                version_id=version.version_id,
                indexed=indexed,
                checksum=version.checksum,
                source_ref=f"element:{element.element_id}",
                media_type=(
                    indexed.media_type
                    if indexed is not None
                    else version.media_type
                ),
                source_url=(
                    public_source_url(version)
                    if indexed is None and version_kind == "asset-version"
                    else None
                ),
                duration_seconds=version.duration_seconds,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                original_sound=(
                    element.creation.original_sound
                    if isinstance(element.creation, EditCreation)
                    else "preserve"
                ),
                location=(
                    element.location.model_dump(mode="json")
                    if element.location is not None
                    else None
                ),
                overlay=(
                    _edit_overlay(timeline, element)
                    if isinstance(element.creation, EditCreation)
                    else None
                ),
                motions=(
                    _edit_motion_overlays(timeline, element)
                    if isinstance(element.creation, EditCreation)
                    else ()
                ),
            ),
        )
        read_set.append(
            _read_set_item(
                version_kind,
                version.version_id,
                indexed,
                version.checksum,
            ),
        )
        selections.append(
            {
                "sourceRef": f"element:{element.element_id}",
                "versionId": version.version_id,
                "order": order,
                "startTick": element.span.start_tick,
                "durationTick": element.span.duration_tick,
                "sourceInTick": (
                    render_source.source_in_tick
                    if isinstance(element.creation, EditCreation)
                    else None
                ),
                "sourceOutTick": (
                    render_source.source_out_tick
                    if isinstance(element.creation, EditCreation)
                    else None
                ),
            },
        )
        durations.append(
            element.span.duration_tick / timeline.ticks_per_second,
        )
    transitions = tuple(
        {
            "elementId": element.element_id,
            "fromElementId": element.creation.from_element_id,
            "toElementId": element.creation.to_element_id,
            "kind": element.creation.transition_kind,
            "duration_ms": round(
                element.span.duration_tick * 1000 / timeline.ticks_per_second,
            ),
        }
        for element in sorted(
            timeline.elements_by_id.values(),
            key=lambda item: (item.span.start_tick, item.element_id),
        )
        if element.enabled and isinstance(element.creation, TransitionCreation)
    )
    duration = (
        sum(item for item in durations if item is not None)
        if all(item is not None for item in durations)
        else None
    )
    return _ResolvedExecution(
        command=command,
        target_ref=target_ref,
        task_kind=(
            TaskKind.AI_EDIT_EXECUTE
            if command is CreatorCommandType.EXECUTE_EDIT
            else TaskKind.COMPOSE
        ),
        inputs=tuple(inputs),
        transitions=transitions,
        audio_plan="",
        expected_duration_seconds=duration,
        canvas_size=_canvas_size(
            project.settings.aspect_ratio,
            project.settings.resolution,
        ),
        slot_id=f"timeline:{timeline.timeline_id}:render",
        slot_kind="final_video",
        owner_ref=f"timeline:{timeline.timeline_id}",
        artifact_name=(
            f"{project.name} AI 剪辑成片"
            if command is CreatorCommandType.EXECUTE_EDIT
            else f"{project.name} 最终成片"
        ),
        read_set=tuple(read_set),
        source_selections=tuple(selections),
    )


def _resolve_execution(
    *,
    snapshot: ProjectSnapshot,
    command: CreatorCommandType,
    target_ref: str,
    arguments: Mapping[str, Any],
) -> _ResolvedExecution:
    project = snapshot.project
    if command is CreatorCommandType.EXECUTE_EDIT:
        timeline = _target_timeline(project, target_ref)
        return _timeline_execution(
            project=project,
            timeline=timeline,
            target_ref=target_ref,
            command=command,
        )
    if command is CreatorCommandType.COMPOSE_FINAL_VIDEO:
        timeline = _target_timeline(project, target_ref)
        return _timeline_execution(
            project=project,
            timeline=timeline,
            target_ref=target_ref,
            command=command,
        )
    raise ValidationError(f"不支持的本地媒体命令: {command.value}")


def _resolved_fingerprint(
    resolved: _ResolvedExecution,
    *,
    generation: int,
    etag: str,
) -> str:
    return _fingerprint(
        {
            "command": resolved.command.value,
            "targetRef": resolved.target_ref,
            "inputs": [
                {
                    "versionId": item.version_id,
                    "fileId": (
                        item.indexed.file_id
                        if item.indexed is not None
                        else None
                    ),
                    "checksum": item.checksum,
                    "sourceUrl": item.source_url,
                    "sourceRef": item.source_ref,
                    "start": item.start_seconds,
                    "end": item.end_seconds,
                    "originalSound": item.original_sound,
                    "location": item.location,
                    "overlay": item.overlay,
                    "motions": [dict(motion) for motion in item.motions],
                }
                for item in resolved.inputs
            ],
            "transitions": list(resolved.transitions),
            "audioPlan": resolved.audio_plan,
            "canvasSize": list(resolved.canvas_size),
            "inputGeneration": generation,
            "inputEtag": etag,
        },
    )


def _json_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _terminal_task_message(task: TaskRecord) -> str:
    message = f"本地媒体 Task 已终止: {task.status.value}"
    if isinstance(task.error, Mapping):
        detail = str(task.error.get("message") or "").strip()
        if detail and detail != message:
            message = f"{message}: {detail}"
    return message


class FileLocalMediaExecutionService:
    """Convergent Run/Task worker for local edit and compose execution."""

    def __init__(
        self,
        services: CreatorFileServices,
        *,
        runner: LocalMediaRunner | None = None,
        max_output_bytes: int = _MAX_VIDEO_BYTES,
    ) -> None:
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.services = services
        self.runner = runner or FfmpegLocalMediaRunner()
        self.executions = ProjectExecutionStore(services.root)
        self.max_output_bytes = max_output_bytes
        self._closed = False

    async def execute(
        self,
        *,
        project_id: str,
        command: CreatorCommandType | str,
        target_ref: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
        expected_object_versions: Sequence[str] = (),
    ) -> FileLocalMediaExecutionResult:
        if self._closed:
            raise ConflictError("本地媒体执行服务正在关闭")
        command_value = CreatorCommandType(command)
        if command_value not in _LOCAL_MEDIA_COMMANDS:
            raise ValidationError(f"不支持的文件本地媒体命令: {command_value.value}")
        ids = self._ids(project_id, idempotency_key)
        command_request_hash = _fingerprint(
            {
                "command": command_value.value,
                "targetRef": target_ref,
                "arguments": dict(arguments),
            },
        )
        try:
            existing_task = await asyncio.to_thread(
                self.executions.get_task,
                project_id,
                ids["task_id"],
            )
        except RecordNotFoundError:
            existing_task = None
        if existing_task is not None:
            self._assert_command_replay(
                existing_task,
                command=command_value,
                target_ref=target_ref,
                command_request_hash=command_request_hash,
            )
            if existing_task.status is TaskStatus.SUCCEEDED:
                return self._result_from_task(existing_task, replayed=True)
            if existing_task.status in {
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.QUARANTINED,
            }:
                raise ConflictError(_terminal_task_message(existing_task))
            if existing_task.status is TaskStatus.RUNNING:
                if existing_task.result is None:
                    raise ConflictError("本地媒体 Task 已由另一个执行者领取")
                return await self._converge(
                    task=existing_task,
                    ids=ids,
                    replayed=True,
                )

        base = await asyncio.to_thread(self.services.projects.read, project_id)
        if any(
            f"project:{base.etag}:" not in value
            for value in expected_object_versions
        ):
            raise ConflictError("本地媒体命令目标已被其他写者修改")
        resolved = await asyncio.to_thread(
            _resolve_execution,
            snapshot=base,
            command=command_value,
            target_ref=target_ref,
            arguments=dict(arguments),
        )
        request_fingerprint = _resolved_fingerprint(
            resolved,
            generation=base.generation,
            etag=base.etag,
        )
        run, task = await self._admit(
            base=base,
            resolved=resolved,
            request_fingerprint=request_fingerprint,
            command_request_hash=command_request_hash,
            idempotency_key=idempotency_key,
            ids=ids,
        )
        if task.status is TaskStatus.SUCCEEDED:
            return self._result_from_task(task, replayed=True)
        if task.status in {
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.QUARANTINED,
        }:
            raise ConflictError(_terminal_task_message(task))
        if task.status is TaskStatus.RUNNING and task.result is not None:
            return await self._converge(task=task, ids=ids, replayed=True)

        task = await self._start(
            run=run,
            task=task,
            resolved=resolved,
            ids=ids,
        )
        if not await self._claim_runner(task):
            raise ConflictError("本地媒体 Task 已由另一个执行者领取")
        try:
            spec = await asyncio.to_thread(
                self._prepare_spec,
                base,
                resolved,
                task,
            )
            runner_output = await self.runner.render(spec)
            published_result = await asyncio.to_thread(
                self._materialize_and_publish,
                base,
                resolved,
                task,
                ids,
                spec,
                runner_output,
            )
            latest = await asyncio.to_thread(
                self.executions.get_task,
                project_id,
                task.task_id,
            )
            if latest.status is TaskStatus.CANCELLED:
                await self._quarantine(
                    task=latest,
                    ids=ids,
                    reason="TASK_CANCELLED_BEFORE_IMPORT",
                    result=published_result,
                    run_status=SpecialistRunStatus.CANCELLED,
                )
                raise ConflictError("本地媒体 Task 已取消，迟到结果已隔离")
            try:
                task = await asyncio.to_thread(
                    self.executions.transition_task,
                    project_id,
                    task.task_id,
                    expected_status=TaskStatus.RUNNING,
                    status=TaskStatus.RUNNING,
                    updates={
                        "progress": max(0.9, latest.progress or 0.0),
                        "result": published_result,
                        "output_refs": [
                            f"artifact-version:{ids['artifact_version_id']}",
                        ],
                    },
                )
            except ExecutionStateConflict:
                latest = await asyncio.to_thread(
                    self.executions.get_task,
                    project_id,
                    task.task_id,
                )
                if latest.status is not TaskStatus.CANCELLED:
                    raise
                await self._quarantine(
                    task=latest,
                    ids=ids,
                    reason="TASK_CANCELLED_BEFORE_IMPORT",
                    result=published_result,
                    run_status=SpecialistRunStatus.CANCELLED,
                )
                raise ConflictError("本地媒体 Task 已取消，迟到结果已隔离")
            return await self._converge(task=task, ids=ids, replayed=False)
        except (ConflictError, ValidationError, StorageIntegrityError) as exc:
            await self._fail_if_running(
                project_id,
                ids,
                "LOCAL_MEDIA_EXECUTION_FAILED",
                message=str(exc),
            )
            raise
        except AssetFileError as exc:
            await self._fail_if_running(
                project_id,
                ids,
                "LOCAL_MEDIA_EXECUTION_FAILED",
                message=str(exc),
            )
            raise StorageIntegrityError(str(exc)) from exc
        except Exception as exc:
            await self._fail_if_running(
                project_id,
                ids,
                "LOCAL_MEDIA_EXECUTION_FAILED",
                message=str(exc),
            )
            raise StorageIntegrityError(f"本地媒体执行失败: {exc}") from exc

    @staticmethod
    def _ids(project_id: str, key: str) -> dict[str, str]:
        return {
            "run_id": _stable_id("run", project_id, key),
            "round_id": _stable_id("round", project_id, key),
            "task_id": _stable_id("task", project_id, key),
            "attempt_id": _stable_id("attempt", project_id, key),
            "attempt_started_event_id": _stable_id(
                "attempt-started",
                project_id,
                key,
            ),
            "attempt_succeeded_event_id": _stable_id(
                "attempt-succeeded",
                project_id,
                key,
            ),
            "attempt_failed_event_id": _stable_id(
                "attempt-failed",
                project_id,
                key,
            ),
            "attempt_quarantined_event_id": _stable_id(
                "attempt-quarantined",
                project_id,
                key,
            ),
            "file_id": _stable_id("file", project_id, key),
            "artifact_version_id": _stable_id(
                "artifact-version",
                project_id,
                key,
            ),
            "transaction_id": _stable_id("media-transaction", project_id, key),
            "quarantine_id": _stable_id("quarantine", project_id, key),
        }

    async def _admit(
        self,
        *,
        base: ProjectSnapshot,
        resolved: _ResolvedExecution,
        request_fingerprint: str,
        command_request_hash: str,
        idempotency_key: str,
        ids: Mapping[str, str],
    ) -> tuple[SpecialistRunRecord, TaskRecord]:
        metadata = {
            "commandType": resolved.command.value,
            "targetRef": resolved.target_ref,
            "commandRequestHash": command_request_hash,
            "slotId": resolved.slot_id,
            "artifactVersionId": ids["artifact_version_id"],
            "fileId": ids["file_id"],
        }
        run_candidate = SpecialistRunRecord(
            run_id=ids["run_id"],
            project_id=base.project.project_id,
            round_id=ids["round_id"],
            role=SpecialistRole.AI_EDITING_DIRECTOR,
            target_refs=[resolved.target_ref],
            input_generation=base.generation,
            input_etag=base.etag,
            request_fingerprint=request_fingerprint,
            read_set=list(resolved.read_set),
            caused_by_request_id=idempotency_key,
            review_policy=ReviewPolicy.AUTO_FIX,
            metadata=metadata,
        )
        try:
            run = await asyncio.to_thread(
                self.executions.get_run,
                base.project.project_id,
                ids["run_id"],
            )
        except RecordNotFoundError:
            run = await asyncio.to_thread(
                self.executions.create_run,
                run_candidate,
            )
        if (
            run.request_fingerprint != request_fingerprint
            or run.input_etag != base.etag
            or run.target_refs != [resolved.target_ref]
        ):
            raise ConflictError("Idempotency-Key 已用于不同的本地媒体 Run")
        task_candidate = TaskRecord(
            task_id=ids["task_id"],
            project_id=base.project.project_id,
            round_id=ids["round_id"],
            run_id=ids["run_id"],
            kind=resolved.task_kind,
            request_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
            input_generation=base.generation,
            input_etag=base.etag,
            expected_target_version=base.etag,
            input_refs=[
                resolved.target_ref,
                *(item.version_id for item in resolved.inputs),
            ],
            read_set=list(resolved.read_set),
            caused_by_request_id=idempotency_key,
            review_policy=ReviewPolicy.AUTO_FIX,
            metadata=metadata,
        )
        try:
            task = await asyncio.to_thread(
                self.executions.get_task,
                base.project.project_id,
                ids["task_id"],
            )
        except RecordNotFoundError:
            try:
                task = await asyncio.to_thread(
                    self.executions.create_task,
                    task_candidate,
                )
            except (ExecutionPayloadConflict, ExecutionStateConflict) as exc:
                raise ConflictError(str(exc)) from exc
        if (
            task.request_fingerprint != request_fingerprint
            or task.input_etag != base.etag
            or task.input_refs != task_candidate.input_refs
        ):
            raise ConflictError("Idempotency-Key 已用于不同的本地媒体 Task")
        return run, task

    @staticmethod
    def _assert_command_replay(
        task: TaskRecord,
        *,
        command: CreatorCommandType,
        target_ref: str,
        command_request_hash: str,
    ) -> None:
        if (
            task.metadata.get("commandType") != command.value
            or task.metadata.get("targetRef") != target_ref
            or task.metadata.get("commandRequestHash") != command_request_hash
        ):
            raise ConflictError("Idempotency-Key 已用于不同的本地媒体命令")

    async def _claim_runner(self, task: TaskRecord) -> bool:
        claim = {
            "taskId": task.task_id,
            "requestFingerprint": task.request_fingerprint,
            "claimedAt": datetime.now(UTC).isoformat(),
        }

        def claim_sync():
            with self.services.projects.lifecycle_lock(task.project_id):
                self.services.projects.read(task.project_id)
                project_root = self.services.projects.project_root(
                    task.project_id,
                )
                task_root = _ensure_real_directory_chain(
                    project_root,
                    "runtime",
                    "tasks",
                    task.task_id,
                )
                claim_store = AtomicJsonRecordStore(
                    task_root / "runner-claim.json",
                )
                created = claim_store.try_create(claim)
                existing = None if created is not None else claim_store.read()
                return created, existing

        created, existing = await asyncio.to_thread(claim_sync)
        if created is not None:
            return True
        assert isinstance(existing, dict)
        if (
            existing.get("taskId") != task.task_id
            or existing.get("requestFingerprint") != task.request_fingerprint
        ):
            raise StorageIntegrityError("本地媒体 runner claim 内容损坏")
        return False

    async def _start(
        self,
        *,
        run: SpecialistRunRecord,
        task: TaskRecord,
        resolved: _ResolvedExecution,
        ids: Mapping[str, str],
    ) -> TaskRecord:
        project_id = task.project_id
        if run.status in {
            SpecialistRunStatus.QUEUED,
            SpecialistRunStatus.QUEUED_CAPACITY,
        }:
            run = await asyncio.to_thread(
                self.executions.transition_run,
                project_id,
                run.run_id,
                expected_status=run.status,
                status=SpecialistRunStatus.RUNNING_MODEL,
            )
        if task.status is TaskStatus.QUEUED:
            await asyncio.to_thread(
                self.executions.append_attempt,
                project_id,
                task.task_id,
                event_id=ids["attempt_started_event_id"],
                attempt_id=ids["attempt_id"],
                status=TaskAttemptStatus.RUNNING,
                input={
                    "command": resolved.command.value,
                    "targetRef": resolved.target_ref,
                    "inputs": [
                        {
                            "versionId": item.version_id,
                            "fileId": (
                                item.indexed.file_id
                                if item.indexed is not None
                                else None
                            ),
                            "checksum": item.checksum,
                            "sourceUrl": item.source_url,
                            "start": item.start_seconds,
                            "end": item.end_seconds,
                        }
                        for item in resolved.inputs
                    ],
                    "artifactVersionId": ids["artifact_version_id"],
                },
            )
            task = await asyncio.to_thread(
                self.executions.get_task,
                project_id,
                task.task_id,
            )
        if run.status is SpecialistRunStatus.RUNNING_MODEL:
            await asyncio.to_thread(
                self.executions.transition_run,
                project_id,
                run.run_id,
                expected_status=SpecialistRunStatus.RUNNING_MODEL,
                status=SpecialistRunStatus.WAITING_RUNTIME,
            )
        return task

    def _prepare_spec(
        self,
        base: ProjectSnapshot,
        resolved: _ResolvedExecution,
        task: TaskRecord,
    ) -> LocalMediaExecutionSpec:
        project_root = self.services.projects.project_root(
            base.project.project_id,
        )
        work_dir = _ensure_real_directory_chain(
            project_root,
            "runtime",
            "task-work",
            task.task_id,
        )
        input_dir = _ensure_real_directory_chain(work_dir, "inputs")
        file_store = AssetFileStore(project_root)
        local_inputs: list[LocalMediaInput] = []
        prepared_paths: dict[tuple[str, str, str], Path] = {}
        runtime_tasks = (
            self.executions.list_tasks(base.project.project_id)
            if any(item.source_url is not None for item in resolved.inputs)
            else []
        )
        for frozen in resolved.inputs:
            location_identity = (
                frozen.indexed.file_id
                if frozen.indexed is not None
                else str(frozen.source_url or "")
            )
            input_identity = (
                frozen.version_id,
                frozen.checksum,
                location_identity,
            )
            destination = prepared_paths.get(input_identity)
            suffix = mimetypes.guess_extension(frozen.media_type) or ".mp4"
            if not suffix.startswith(".") or len(suffix) > 12:
                suffix = ".mp4"
            if destination is None:
                destination = input_dir / (
                    f"{len(prepared_paths):06d}-{frozen.version_id}{suffix}"
                )
                if destination.exists():
                    raise StorageIntegrityError("Task input scratch 已存在")
                if frozen.indexed is not None:
                    with (
                        file_store.open_verified(frozen.indexed) as source,
                        destination.open("xb") as target,
                    ):
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                elif frozen.source_url is not None:
                    source_version = (
                        base.project.assets.source_versions_by_id.get(
                            frozen.version_id,
                        )
                    )
                    cached = (
                        resolve_remote_cache(
                            project_root,
                            source_version,
                            runtime_tasks,
                        )
                        if source_version is not None
                        else None
                    )
                    if cached is not None:
                        destination = cached.path
                    else:
                        download_remote_file(
                            frozen.source_url,
                            str(destination),
                        )
                else:
                    raise StorageIntegrityError(
                        f"媒体输入缺少本地文件与公网 URL: {frozen.version_id}",
                    )
                prepared_paths[input_identity] = destination
            local_inputs.append(
                LocalMediaInput(
                    version_id=frozen.version_id,
                    file_id=(
                        frozen.indexed.file_id
                        if frozen.indexed is not None
                        else None
                    ),
                    checksum=frozen.checksum,
                    media_type=frozen.media_type,
                    path=destination,
                    source_ref=frozen.source_ref,
                    start_seconds=frozen.start_seconds,
                    end_seconds=frozen.end_seconds,
                    duration_seconds=frozen.duration_seconds,
                    original_sound=frozen.original_sound,
                    location=frozen.location,
                    overlay=frozen.overlay,
                    motions=frozen.motions,
                ),
            )
        output_path = work_dir / "output.mp4"
        if output_path.exists():
            raise StorageIntegrityError("Task output scratch 已存在")

        def on_element_done(done: int, total: int) -> None:
            if total <= 0:
                return
            try:
                self.executions.transition_task(
                    base.project.project_id,
                    task.task_id,
                    expected_status=TaskStatus.RUNNING,
                    status=TaskStatus.RUNNING,
                    updates={"progress": done / total},
                )
            except ExecutionStateConflict:
                # Cancellation or another terminal transition won the Runtime
                # lock; the renderer will observe that state before import.
                return

        spec = LocalMediaExecutionSpec(
            command=resolved.command,
            target_ref=resolved.target_ref,
            task_id=task.task_id,
            work_dir=work_dir,
            output_path=output_path,
            inputs=tuple(local_inputs),
            transitions=resolved.transitions,
            audio_plan=resolved.audio_plan,
            expected_duration_seconds=resolved.expected_duration_seconds,
            canvas_size=resolved.canvas_size,
            on_element_done=(
                on_element_done
                if resolved.command is CreatorCommandType.EXECUTE_EDIT
                else None
            ),
        )
        AtomicJsonRecordStore(work_dir / "spec.json").write(
            {
                "taskId": task.task_id,
                "command": resolved.command.value,
                "targetRef": resolved.target_ref,
                "inputGeneration": base.generation,
                "inputEtag": base.etag,
                "inputs": [
                    {
                        "versionId": item.version_id,
                        "fileId": item.file_id,
                        "checksum": item.checksum,
                        "path": item.path.relative_to(project_root).as_posix(),
                        "start": item.start_seconds,
                        "end": item.end_seconds,
                        "originalSound": item.original_sound,
                        "location": item.location,
                        "overlay": item.overlay,
                        "motions": [dict(motion) for motion in item.motions],
                    }
                    for item in local_inputs
                ],
                "outputPath": output_path.relative_to(project_root).as_posix(),
                "canvasSize": list(resolved.canvas_size),
            },
        )
        return spec

    def _materialize_and_publish(
        self,
        base: ProjectSnapshot,
        resolved: _ResolvedExecution,
        task: TaskRecord,
        ids: Mapping[str, str],
        spec: LocalMediaExecutionSpec,
        runner_output: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            output_stat = spec.output_path.lstat()
        except FileNotFoundError as exc:
            raise ValidationError("本地媒体 runner 未生成 output.mp4") from exc
        if spec.output_path.is_symlink() or not stat.S_ISREG(
            output_stat.st_mode,
        ):
            raise StorageIntegrityError("本地媒体输出不是普通文件")
        resolved_output = spec.output_path.resolve(strict=True)
        if not resolved_output.is_relative_to(
            spec.work_dir.resolve(strict=True),
        ):
            raise StorageIntegrityError("本地媒体输出逃逸 task-work")
        if (
            output_stat.st_size <= 0
            or output_stat.st_size > self.max_output_bytes
        ):
            raise ValidationError("本地媒体输出为空或超过大小限制")
        file_store = AssetFileStore(
            self.services.projects.project_root(base.project.project_id),
        )
        with spec.output_path.open("rb") as stream:
            staged = file_store.stage_stream(
                stream,
                staging_id=task.task_id[:80],
            )
        relative_uri = PurePosixPath(
            "assets",
            "artifacts",
            f"{ids['file_id']}.mp4",
        ).as_posix()
        indexed = IndexedFile(
            file_id=ids["file_id"],
            kind="artifact_payload",
            relative_uri=relative_uri,
            sha256=staged.sha256,
            size_bytes=staged.size_bytes,
            media_type="video/mp4",
            created_at=datetime.now(UTC),
        )
        try:
            file_store.publish(
                staged,
                relative_uri,
                expected_sha256=staged.sha256,
                expected_size_bytes=staged.size_bytes,
            )
        except AssetAlreadyExists:
            file_store.abandon(staged)
            if not file_store.inspect(indexed).available:
                raise StorageIntegrityError("稳定本地媒体输出路径已存在但内容不同")
        metadata = {
            "taskId": task.task_id,
            "runId": task.run_id,
            "commandType": resolved.command.value,
            "sourceSelections": list(resolved.source_selections),
            "runner": _json_mapping(runner_output.get("metadata")),
        }
        reported_duration = runner_output.get("duration_seconds")
        duration = (
            float(reported_duration)
            if isinstance(reported_duration, (int, float))
            and not isinstance(reported_duration, bool)
            and float(reported_duration) >= 0
            else resolved.expected_duration_seconds
        )
        artifact = ArtifactVersion(
            version_id=ids["artifact_version_id"],
            slot_id=resolved.slot_id,
            kind=resolved.slot_kind,
            owner_ref=resolved.owner_ref,
            name=resolved.artifact_name,
            file_id=indexed.file_id,
            checksum=indexed.sha256,
            based_on_generation=task.input_generation or base.generation,
            provenance_refs=[str(item["ref"]) for item in resolved.read_set],
            duration_seconds=duration,
            input_fingerprint=task.request_fingerprint,
            created_at=indexed.created_at,
            metadata=metadata,
        )
        return {
            "taskId": task.task_id,
            "runId": task.run_id,
            "transactionId": ids["transaction_id"],
            "commandType": resolved.command.value,
            "targetRef": resolved.target_ref,
            "indexedFile": indexed.model_dump(mode="json"),
            "artifactVersion": artifact.model_dump(mode="json"),
            "outputRef": f"artifact-version:{artifact.version_id}",
        }

    async def _converge(
        self,
        *,
        task: TaskRecord,
        ids: Mapping[str, str],
        replayed: bool,
    ) -> FileLocalMediaExecutionResult:
        if not isinstance(task.result, dict):
            raise StorageIntegrityError("RUNNING 本地媒体 Task 缺少可重放 result")
        result = dict(task.result)

        def commit_if_live() -> tuple[str, TaskRecord, ProjectSnapshot | None]:
            # Cancellation and Project import share this lifecycle boundary.
            # A cancel that wins the lock leaves Project untouched; once this
            # finalizer wins, Project commit and Task success become one
            # indivisible decision from every other Project-scoped writer's
            # point of view.
            with self.services.projects.lifecycle_lock(task.project_id):
                latest = self.executions.get_task(
                    task.project_id,
                    task.task_id,
                    _lifecycle_lock_held=True,
                )
                if latest.status is TaskStatus.CANCELLED:
                    return "CANCELLED", latest, None
                if latest.status in {
                    TaskStatus.FAILED,
                    TaskStatus.QUARANTINED,
                }:
                    return latest.status.value, latest, None
                current = self.services.projects.read(task.project_id)
                if self._result_is_converged(current.project, result):
                    snapshot = current
                elif (
                    current.etag != latest.input_etag
                    or current.generation != latest.input_generation
                ):
                    return "STALE", latest, current
                else:
                    candidate = current.project.model_dump(mode="json")
                    self._apply_result(candidate, result)
                    commit = self.services.commits.commit(
                        base=current,
                        candidate=candidate,
                        origin=ChangeOrigin.RUNTIME_TASK,
                        review_policy=ReviewPolicy.AUTO_FIX,
                        caused_by_request_id=latest.caused_by_request_id,
                        round_id=ids["round_id"],
                        transaction_id=ids["transaction_id"],
                        advance_accepted_baseline=True,
                        _lifecycle_lock_held=True,
                    )
                    snapshot = commit.snapshot
                success = {
                    **result,
                    "projectEtag": snapshot.etag,
                    "projectGeneration": snapshot.generation,
                }
                if latest.status is TaskStatus.RUNNING:
                    self.executions.append_attempt(
                        task.project_id,
                        task.task_id,
                        event_id=ids["attempt_succeeded_event_id"],
                        attempt_id=ids["attempt_id"],
                        status=TaskAttemptStatus.SUCCEEDED,
                        output=success,
                        output_refs=[str(success["outputRef"])],
                        _lifecycle_lock_held=True,
                    )
                    latest = self.executions.get_task(
                        task.project_id,
                        task.task_id,
                        _lifecycle_lock_held=True,
                    )
                return "SUCCEEDED", latest, snapshot

        outcome, current_task, snapshot = await asyncio.to_thread(
            commit_if_live,
        )
        if outcome == "CANCELLED":
            await self._quarantine(
                task=current_task,
                ids=ids,
                reason="TASK_CANCELLED_BEFORE_IMPORT",
                result=result,
                run_status=SpecialistRunStatus.CANCELLED,
            )
            raise ConflictError("本地媒体 Task 已取消，迟到结果已隔离")
        if outcome == "STALE":
            await self._quarantine(
                task=current_task,
                ids=ids,
                reason="PROJECT_INPUT_SNAPSHOT_STALE",
                result=result,
                run_status=SpecialistRunStatus.STALE,
            )
            raise ConflictError("本地媒体执行期间 Project 已变化，结果已隔离")
        if outcome != "SUCCEEDED" or snapshot is None:
            raise ConflictError(f"本地媒体 Task 已终止: {outcome}")
        await asyncio.to_thread(self.services.poller.note_commit, snapshot)
        success = (
            current_task.result
            if isinstance(current_task.result, dict)
            else {
                **result,
                "projectEtag": snapshot.etag,
                "projectGeneration": snapshot.generation,
            }
        )
        artifact = ArtifactVersion.model_validate(success["artifactVersion"])
        await self._finish_run(
            task.project_id,
            ids["run_id"],
            SpecialistRunStatus.SUCCEEDED,
            summary=f"已生成并选择 {artifact.name}",
        )
        return self._result_from_task(current_task, replayed=replayed)

    @staticmethod
    def _result_is_converged(
        project: Project,
        result: Mapping[str, Any],
    ) -> bool:
        try:
            indexed = IndexedFile.model_validate(result["indexedFile"])
            artifact = ArtifactVersion.model_validate(
                result["artifactVersion"],
            )
            command = CreatorCommandType(str(result["commandType"]))
        except (KeyError, TypeError, ValueError):
            return False
        slot = project.assets.artifact_slots_by_id.get(artifact.slot_id)
        if (
            project.assets.files_by_id.get(indexed.file_id) != indexed
            or project.assets.artifact_versions_by_id.get(artifact.version_id)
            != artifact
            or slot is None
            or slot.selected_version_id != artifact.version_id
        ):
            return False
        return command in {
            CreatorCommandType.EXECUTE_EDIT,
            CreatorCommandType.COMPOSE_FINAL_VIDEO,
        }

    @staticmethod
    def _apply_result(
        candidate: dict[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        indexed = IndexedFile.model_validate(result["indexedFile"])
        artifact = ArtifactVersion.model_validate(result["artifactVersion"])
        assets = candidate["assets"]
        indexed_json = indexed.model_dump(mode="json")
        artifact_json = artifact.model_dump(mode="json")
        existing_file = assets["files_by_id"].get(indexed.file_id)
        if existing_file is not None and existing_file != indexed_json:
            raise ConflictError("本地媒体 IndexedFile 稳定 ID 内容冲突")
        existing_version = assets["artifact_versions_by_id"].get(
            artifact.version_id,
        )
        if existing_version is not None and existing_version != artifact_json:
            raise ConflictError("本地媒体 ArtifactVersion 稳定 ID 内容冲突")
        assets["files_by_id"][indexed.file_id] = indexed_json
        assets["artifact_versions_by_id"][artifact.version_id] = artifact_json
        raw_slot = assets["artifact_slots_by_id"].get(artifact.slot_id)
        if raw_slot is None:
            raw_slot = ArtifactSlot(
                slot_id=artifact.slot_id,
                kind=artifact.kind,
                owner_ref=artifact.owner_ref,
                version_ids=[artifact.version_id],
                selected_version_id=artifact.version_id,
            ).model_dump(mode="json")
            assets["artifact_slots_by_id"][artifact.slot_id] = raw_slot
        else:
            if (
                raw_slot["kind"] != artifact.kind
                or raw_slot["owner_ref"] != artifact.owner_ref
            ):
                raise ConflictError("本地媒体 ArtifactSlot 归属冲突")
            if artifact.version_id not in raw_slot["version_ids"]:
                raw_slot["version_ids"].append(artifact.version_id)
            raw_slot["selected_version_id"] = artifact.version_id
        command = CreatorCommandType(str(result["commandType"]))
        if command not in {
            CreatorCommandType.EXECUTE_EDIT,
            CreatorCommandType.COMPOSE_FINAL_VIDEO,
        }:
            raise ConflictError("Timeline 不支持该本地媒体命令")

    async def _quarantine(
        self,
        *,
        task: TaskRecord,
        ids: Mapping[str, str],
        reason: str,
        result: Mapping[str, Any],
        run_status: SpecialistRunStatus,
    ) -> None:
        latest = await asyncio.to_thread(
            self.executions.get_task,
            task.project_id,
            task.task_id,
        )
        if latest.status is TaskStatus.RUNNING:
            try:
                await asyncio.to_thread(
                    self.executions.append_attempt,
                    task.project_id,
                    task.task_id,
                    event_id=ids["attempt_quarantined_event_id"],
                    attempt_id=ids["attempt_id"],
                    status=TaskAttemptStatus.QUARANTINED,
                    output=dict(result),
                    output_refs=[str(result.get("outputRef") or "")],
                    error={"code": reason, "message": reason},
                )
            except ExecutionStateConflict:
                pass
        await asyncio.to_thread(
            self.executions.quarantine_task_result,
            task.project_id,
            task.task_id,
            reason=reason,
            result=dict(result),
            quarantine_id=ids["quarantine_id"],
            transition_task=False,
        )
        await self._finish_run(task.project_id, ids["run_id"], run_status)

    async def _fail_if_running(
        self,
        project_id: str,
        ids: Mapping[str, str],
        code: str,
        *,
        message: str | None = None,
    ) -> None:
        try:
            task = await asyncio.to_thread(
                self.executions.get_task,
                project_id,
                ids["task_id"],
            )
        except RecordNotFoundError:
            return
        if task.status is TaskStatus.RUNNING:
            try:
                await asyncio.to_thread(
                    self.executions.append_attempt,
                    project_id,
                    task.task_id,
                    event_id=ids["attempt_failed_event_id"],
                    attempt_id=ids["attempt_id"],
                    status=TaskAttemptStatus.FAILED,
                    error={"code": code, "message": message or code},
                )
            except ExecutionStateConflict:
                pass
        await self._finish_run(
            project_id,
            ids["run_id"],
            SpecialistRunStatus.FAILED,
        )

    async def _finish_run(
        self,
        project_id: str,
        run_id: str,
        status: SpecialistRunStatus,
        *,
        summary: str | None = None,
    ) -> None:
        try:
            run = await asyncio.to_thread(
                self.executions.get_run,
                project_id,
                run_id,
            )
        except RecordNotFoundError:
            return
        if run.status in {
            SpecialistRunStatus.SUCCEEDED,
            SpecialistRunStatus.BLOCKED,
            SpecialistRunStatus.FAILED,
            SpecialistRunStatus.STALE,
            SpecialistRunStatus.CANCELLED,
        }:
            return
        try:
            await asyncio.to_thread(
                self.executions.transition_run,
                project_id,
                run_id,
                expected_status=run.status,
                status=status,
                updates={"final_summary_text": summary} if summary else None,
            )
        except ExecutionStateConflict:
            return

    async def recover_project(self, project_id: str) -> list[str]:
        """Recover local-media Tasks after process restart.

        A RUNNING Task with a durable published result converges normally (or
        quarantines if its frozen Project input is stale).  A RUNNING Task
        without a result, and any never-started QUEUED Task, is failed closed:
        restarting ffmpeg implicitly could duplicate side effects or consume a
        partial scratch output.  A new command/idempotency key is then required.
        """

        recovered: list[str] = []
        for task in await asyncio.to_thread(
            self.executions.list_tasks,
            project_id,
        ):
            command_name = str(task.metadata.get("commandType") or "")
            try:
                command = CreatorCommandType(command_name)
            except ValueError:
                continue
            if command not in _LOCAL_MEDIA_COMMANDS or task.status not in {
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
            }:
                continue
            key = str(task.idempotency_key or "")
            if not key:
                continue
            ids = self._ids(project_id, key)
            if ids["task_id"] != task.task_id:
                raise StorageIntegrityError("本地媒体 Task 稳定 ID 与 key 不一致")
            if task.status is TaskStatus.RUNNING and task.result is not None:
                try:
                    await self._converge(task=task, ids=ids, replayed=True)
                except ConflictError:
                    # Stale/cancel convergence already persisted quarantine.
                    pass
                recovered.append(task.task_id)
                continue
            error = {
                "code": "LOCAL_MEDIA_PROCESS_RESTARTED",
                "message": "进程重启前本地媒体执行未形成可收敛结果",
            }
            if task.status is TaskStatus.RUNNING:
                try:
                    await asyncio.to_thread(
                        self.executions.append_attempt,
                        project_id,
                        task.task_id,
                        event_id=ids["attempt_failed_event_id"],
                        attempt_id=ids["attempt_id"],
                        status=TaskAttemptStatus.FAILED,
                        error=error,
                    )
                except ExecutionStateConflict:
                    pass
            else:
                await asyncio.to_thread(
                    self.executions.transition_task,
                    project_id,
                    task.task_id,
                    expected_status=TaskStatus.QUEUED,
                    status=TaskStatus.FAILED,
                    updates={"error": error},
                )
            await self._finish_run(
                project_id,
                ids["run_id"],
                SpecialistRunStatus.FAILED,
            )
            recovered.append(task.task_id)
        return recovered

    async def shutdown(self) -> None:
        """Stop admitting new work; in-flight request ownership stays explicit."""

        self._closed = True

    @staticmethod
    def _result_from_task(
        task: TaskRecord,
        *,
        replayed: bool,
    ) -> FileLocalMediaExecutionResult:
        result = task.result if isinstance(task.result, dict) else {}
        try:
            artifact = ArtifactVersion.model_validate(
                result["artifactVersion"],
            )
            transaction_id = str(result["transactionId"])
            project_etag = str(result["projectEtag"])
            project_generation = int(result["projectGeneration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                "SUCCEEDED 本地媒体 Task 缺少可重放结果",
            ) from exc
        return FileLocalMediaExecutionResult(
            task_id=task.task_id,
            run_id=str(task.run_id or ""),
            transaction_id=transaction_id,
            artifact_version_id=artifact.version_id,
            project_etag=project_etag,
            project_generation=project_generation,
            replayed=replayed,
        )


async def execute_file_local_media_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    command: CreatorCommandType | str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
    expected_object_versions: Sequence[str] = (),
    runner: LocalMediaRunner | None = None,
) -> FileLocalMediaExecutionResult:
    return await FileLocalMediaExecutionService(
        services,
        runner=runner,
    ).execute(
        project_id=project_id,
        command=command,
        target_ref=target_ref,
        arguments=arguments,
        idempotency_key=idempotency_key,
        expected_object_versions=expected_object_versions,
    )


async def recover_file_local_media_project(
    services: CreatorFileServices,
    project_id: str,
) -> list[str]:
    """Lifecycle hook for startup recovery of one Project's local tasks."""

    return await FileLocalMediaExecutionService(services).recover_project(
        project_id,
    )


__all__ = [
    "FileLocalMediaExecutionResult",
    "FileLocalMediaExecutionService",
    "FfmpegLocalMediaRunner",
    "LocalMediaExecutionSpec",
    "LocalMediaInput",
    "LocalMediaRunner",
    "execute_file_local_media_command",
    "recover_file_local_media_project",
]
