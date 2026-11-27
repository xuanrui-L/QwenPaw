# -*- coding: utf-8 -*-
"""Trim the material prefix off a Wan3.0 video-extension response.

Measured against the live API (2026-11-19): submitting an 8s material
with ``duration=13`` returns a 13s MP4 whose FIRST 8 seconds are the
material itself (output head frame SSIM 0.81 vs the material's first
frame, 0.40 vs its last). The segment artifact we publish must be the
newly generated tail only, so the material duration is cut before
Asset staging. Re-encode (not stream-copy) because ``-c copy`` can only
seek on keyframes and would leave up to a GOP of the material behind.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile

from domain.errors import ValidationError
from services.media_files import media_gate
from services.runtime_files.media_probe import probe_media
from services.runtime_files.runtime_dependencies import resolve_ffmpeg

from .secure_video_stream import MaterializedVideo

_TRIM_TIMEOUT_SECONDS = 120


def trim_extension_prefix(
    video: MaterializedVideo,
    offset_seconds: float,
) -> MaterializedVideo:
    """Return a verified task-local copy with the head cut off.

    Raises ``ValidationError`` on any provider-toolkit or verification
    failure; the caller decides the degradation (keep the untrimmed
    response rather than losing an already-paid render). Never touches
    the input file.
    """

    if offset_seconds <= 0:
        raise ValidationError("续写裁剪偏移必须为正数")
    executable = resolve_ffmpeg() or "ffmpeg"
    try:
        total = probe_media(
            video.path,
            ffmpeg_path=executable,
        ).duration_seconds
    except Exception as error:  # noqa: BLE001 - probe failures are the
        # documented MediaProbeError surface of probe_media; the caller
        # only catches ValidationError, so converge here.
        from services.runtime_files.media_probe import MediaProbeError

        if not isinstance(error, MediaProbeError):
            raise
        raise ValidationError(
            f"续写产物探测失败，无法安全裁剪: {error}",
        ) from error
    if total is None or total <= offset_seconds:
        raise ValidationError(
            f"续写产物时长 {total} 不足以裁掉 {offset_seconds}s 素材前缀",
        )
    descriptor, name = tempfile.mkstemp(
        prefix="exttrim-",
        suffix=video.path.suffix,
        dir=video.path.parent,
    )
    os.close(descriptor)
    output = Path(name)
    arguments = [
        executable,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{offset_seconds:.6f}",
        "-i",
        os.fspath(video.path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
    ]
    if video.container in {"mp4", "quicktime"}:
        arguments.extend(["-movflags", "+faststart"])
    try:
        media_gate.run(
            [*arguments, os.fspath(output)],
            check=True,
            capture_output=True,
            timeout=_TRIM_TIMEOUT_SECONDS,
        )
        remaining = probe_media(
            output,
            ffmpeg_path=executable,
        ).duration_seconds
        if not remaining or remaining <= 0:
            raise ValidationError("续写裁剪产物时长非法")
        digest = hashlib.sha256()
        with output.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return replace(
            video,
            path=output,
            sha256=digest.hexdigest(),
            size_bytes=output.stat().st_size,
        )
    except ValidationError:
        output.unlink(missing_ok=True)
        raise
    except Exception as error:
        output.unlink(missing_ok=True)
        raise ValidationError(f"续写产物裁剪失败: {error}") from error


__all__ = ["trim_extension_prefix"]
