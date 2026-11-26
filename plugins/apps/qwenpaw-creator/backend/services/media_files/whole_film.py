# -*- coding: utf-8 -*-
"""Assemble ordered episode masters into one immutable, downloadable MP4."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
import time

from domain.errors import ConflictError, NotFoundError, StorageIntegrityError
from services.media_files.interactive_bundle import selected_final_video
from services.media_files.keyframe_cache import verified_indexed_path
from services.media_files.local_execution import (
    FfmpegLocalMediaRunner,
    ensure_real_directory_chain,
)
from services.project_files.models import (
    IndexedFile,
    Project,
    narrative_timeline_ids,
)
from services.runtime_files.locking import CrossProcessFileLock
from services.runtime_files.path_safety import is_link_path, is_link_stat


@dataclass(frozen=True)
class WholeFilmPlan:
    key: str
    files: tuple[IndexedFile, ...]


def plan_whole_film(project: Project, version_ids: list[str]) -> WholeFilmPlan:
    timelines = [
        project.timelines.items[tid] for tid in narrative_timeline_ids(project)
    ]
    if project.narrative_edges or any(
        element.enabled and element.creation.type == "interaction"
        for timeline in timelines
        for element in timeline.elements_by_id.values()
    ):
        raise ConflictError("互动作品请使用互动包导出")
    if len(timelines) < 2:
        raise ConflictError("请直接下载该时间线的成片")
    current = [
        selected_final_video(project, tl.timeline_id, allow_legacy_slots=True)
        for tl in timelines
    ]
    if not all(current):
        raise ConflictError("请等待所有分集合成后重试")
    if current != version_ids:
        raise ConflictError("分集成片或顺序已变更，请重新选择整片")
    files = []
    for version_id in version_ids:
        version = project.assets.artifact_versions_by_id[version_id]
        indexed = project.assets.files_by_id.get(version.file_id)
        if not indexed or not indexed.media_type.startswith("video/"):
            raise ConflictError("找不到分集成片的视频文件")
        files.append(indexed)
    identity = list(zip(version_ids, (file.sha256 for file in files)))
    # Include the mux format so older timing or display transforms are rebuilt.
    key = sha256(json.dumps(["whole-film-v3", identity]).encode()).hexdigest()
    return WholeFilmPlan(key, tuple(files))


def cached_whole_film(project_root: Path, key: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise NotFoundError("找不到该整片")
    path = project_root / "runtime" / "whole-films" / key / "full-film.mp4"
    if path.resolve() != path:
        raise StorageIntegrityError("整片缓存路径无效")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        is_link_stat(info)
        or not stat.S_ISREG(info.st_mode)
        or not info.st_size
    ):
        raise StorageIntegrityError("整片缓存文件无效")
    return path


def _prune_old_films(directory: Path, current_key: str) -> None:
    # Leave ample time for a platform upload and simultaneous local downloads.
    cutoff = time.time() - 24 * 3600
    for entry in directory.iterdir():
        if entry.name == current_key or not re.fullmatch(
            r"[0-9a-f]{64}",
            entry.name,
        ):
            continue
        try:
            with CrossProcessFileLock(entry, timeout_seconds=0):
                output = entry / "full-film.mp4"
                if (
                    not is_link_path(entry)
                    and output.lstat().st_mtime < cutoff
                ):
                    output.unlink()
                    entry.rmdir()
        except OSError:
            continue


def materialize_whole_film(project_root: Path, plan: WholeFilmPlan) -> Path:
    cache = ensure_real_directory_chain(project_root, "runtime", "whole-films")
    with CrossProcessFileLock(cache / plan.key, timeout_seconds=None):
        cached = cached_whole_film(project_root, plan.key)
        if cached is not None:
            cached.touch()
            return cached
        inputs = [
            verified_indexed_path(project_root, file) for file in plan.files
        ]
        destination = (
            ensure_real_directory_chain(cache, plan.key) / "full-film.mp4"
        )
        with TemporaryDirectory(dir=cache, prefix=".joining-") as temporary:
            work = Path(temporary)
            output = work / "full-film.mp4"
            FfmpegLocalMediaRunner().join_final_cuts(
                inputs,
                output,
                work_dir=work,
            )
            if not output.is_file() or not output.stat().st_size:
                raise StorageIntegrityError("未生成有效的整片文件")
            output.replace(destination)
    _prune_old_films(cache, plan.key)
    return destination
