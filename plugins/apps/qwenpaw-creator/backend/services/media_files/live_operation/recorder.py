# -*- coding: utf-8 -*-
"""Take recording for live browser operation, driven by the agent.

The unified Browser SDK operates a page but never films it, so recording is
attached beside the very browser the agent is driving: a second CDP channel
subscribes to ``Page.screencastFrame`` and sends no operation command at all.
Frames arrive only when the page actually changes, so a take costs nothing
while the agent is thinking and contains no dead footage between steps.

Recording is explicitly started and stopped by the agent. Nothing is filmed
outside those bounds, which is what keeps takes short enough to stay useful
as editable source material.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from services.runtime_files.runtime_dependencies import resolve_ffmpeg

from .manifest import TakeManifest, Viewport

logger = logging.getLogger(__name__)

_ASSEMBLE_TIMEOUT_SECONDS = 180.0
# A screencast frame is only emitted on visual change, so the final frame has
# no successor to bound it. Hold it for one modest beat instead of dropping it,
# which would truncate the very state an operation just produced.
_TAIL_FRAME_SECONDS = 0.2
_MIN_FRAME_SECONDS = 0.02
_JPEG_QUALITY = 80


class RecorderError(RuntimeError):
    """A take could not be recorded or assembled."""


@dataclass(frozen=True, slots=True)
class RecordedTake:
    """One finished take: the video on disk plus its action facts."""

    take_id: str
    label: str
    video_path: Path
    manifest: TakeManifest

    @property
    def summary(self) -> str:
        return self.manifest.summary()


class TakeRecorder:
    """Own the screencast lifecycle for one live-operation session."""

    def __init__(
        self,
        *,
        workspace: Path,
        fps: int = 25,
        max_width: int = 1280,
        max_height: int = 720,
        max_duration_seconds: float = 300.0,
    ) -> None:
        self._workspace = workspace
        self._fps = max(1, int(fps))
        self._max_width = max(320, int(max_width))
        self._max_height = max(240, int(max_height))
        self._max_duration = max(5.0, float(max_duration_seconds))
        self._cdp: Any | None = None
        self._queue: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        self._frames: list[tuple[float, bytes]] = []
        self._viewport: Viewport | None = None
        self._manifest: TakeManifest | None = None
        self._started_at = 0.0
        self._take_index = 0
        self._takes: list[RecordedTake] = []

    @property
    def recording(self) -> bool:
        return self._manifest is not None

    @property
    def manifest(self) -> TakeManifest | None:
        """The in-flight manifest, so the bridge can record action facts."""
        return self._manifest

    @property
    def takes(self) -> list[RecordedTake]:
        return list(self._takes)

    def elapsed_ms(self) -> int:
        """Milliseconds since the current take started, or 0 when idle."""
        if not self.recording:
            return 0
        return int((time.monotonic() - self._started_at) * 1000)

    async def start(
        self,
        cdp_session: Any,
        *,
        label: str = "",
    ) -> str:
        """Begin filming the page ``cdp_session`` is attached to."""
        if self.recording:
            raise RecorderError(
                "a take is already recording; stop it before starting another",
            )
        self._take_index += 1
        take_id = f"take-{self._take_index:03d}"
        self._cdp = cdp_session
        self._frames = []
        self._viewport = None
        self._queue = asyncio.Queue()
        queue = self._queue
        cdp_session.on(
            "Page.screencastFrame",
            queue.put_nowait,
        )
        self._pump = asyncio.ensure_future(self._drain(cdp_session, queue))
        await cdp_session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": _JPEG_QUALITY,
                "maxWidth": self._max_width,
                "maxHeight": self._max_height,
                "everyNthFrame": 1,
            },
        )
        self._started_at = time.monotonic()
        self._manifest = TakeManifest(take_id=take_id, label=label)
        return take_id

    async def _drain(self, cdp_session: Any, queue: asyncio.Queue) -> None:
        """Collect frames and acknowledge each one so the next is sent."""
        while True:
            payload = await queue.get()
            try:
                data = base64.b64decode(str(payload.get("data") or ""))
            except (TypeError, ValueError):
                data = b""
            if data:
                self._frames.append((time.monotonic(), data))
                if self._viewport is None:
                    self._viewport = _viewport_from_metadata(
                        payload.get("metadata"),
                    )
            session_id = payload.get("sessionId")
            if session_id is None:
                continue
            try:
                await cdp_session.send(
                    "Page.screencastFrameAck",
                    {"sessionId": session_id},
                )
            except Exception:  # noqa: BLE001 - a lost ack only stalls frames
                logger.debug("screencast ack failed", exc_info=True)

    async def stop(self) -> RecordedTake:
        """Stop filming and assemble the collected frames into one mp4."""
        manifest = self._manifest
        if manifest is None:
            raise RecorderError("no take is recording")
        cdp_session = self._cdp
        self._manifest = None
        self._cdp = None
        if cdp_session is not None:
            try:
                await cdp_session.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001 - the take still holds frames
                logger.debug("stopScreencast failed", exc_info=True)
        # Frames already in flight belong to this take: the last operation's
        # result usually arrives a beat after the call that caused it.
        await asyncio.sleep(0.35)
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        frames = list(self._frames)
        self._frames = []
        if not frames:
            raise RecorderError(
                "the take captured no frames; the page did not change "
                "visually between start and stop",
            )
        manifest.viewport = self._viewport
        manifest.fps = self._fps
        manifest.frame_count = len(frames)
        video_path = await asyncio.to_thread(
            self._assemble,
            manifest.take_id,
            frames,
        )
        span_ms = int(
            (frames[-1][0] - frames[0][0] + _TAIL_FRAME_SECONDS) * 1000,
        )
        manifest.duration_ms = max(span_ms, 0)
        width, height = await asyncio.to_thread(_probe_size, video_path)
        manifest.video_width = width
        manifest.video_height = height
        take = RecordedTake(
            take_id=manifest.take_id,
            label=manifest.label,
            video_path=video_path,
            manifest=manifest,
        )
        self._takes.append(take)
        return take

    async def stop_if_recording(self) -> RecordedTake | None:
        """Close an unfinished take so a forgotten stop still yields video."""
        if not self.recording:
            return None
        try:
            return await self.stop()
        except RecorderError:
            return None

    def exceeded_budget(self) -> bool:
        """Whether the current take has outrun its configured duration."""
        return (
            self.recording
            and (time.monotonic() - self._started_at) > self._max_duration
        )

    def _assemble(
        self,
        take_id: str,
        frames: list[tuple[float, bytes]],
    ) -> Path:
        """Turn timestamped frames into a constant-rate mp4 with ffmpeg."""
        staging = (self._workspace / f"{take_id}-frames").resolve()
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        entries: list[str] = []
        for index, (captured_at, data) in enumerate(frames):
            name = f"f{index:05d}.jpg"
            (staging / name).write_bytes(data)
            following = (
                frames[index + 1][0]
                if index + 1 < len(frames)
                else captured_at + _TAIL_FRAME_SECONDS
            )
            hold = max(following - captured_at, _MIN_FRAME_SECONDS)
            entries.append(f"file '{name}'\nduration {hold:.3f}\n")
        # The concat demuxer ignores the last entry's duration, so the final
        # frame is repeated to give it a real on-screen presence.
        entries.append(f"file 'f{len(frames) - 1:05d}.jpg'\n")
        (staging / "frames.txt").write_text("".join(entries), encoding="utf-8")
        # ffmpeg runs inside the staging directory so the concat list can use
        # bare file names, which keeps the demuxer's path safety simple; the
        # output therefore has to be absolute.
        output = (self._workspace / f"{take_id}.mp4").resolve()
        command = [
            resolve_ffmpeg() or "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "frames.txt",
            "-vf",
            f"fps={self._fps},scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=staging,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_ASSEMBLE_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - report the real cause
            raise RecorderError(f"ffmpeg failed to assemble: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if result.returncode != 0 or not output.exists():
            raise RecorderError(
                "ffmpeg failed to assemble the take: "
                f"{(result.stderr or '')[-300:]}",
            )
        return output


def _viewport_from_metadata(raw: Any) -> Viewport | None:
    """Read the visible page size a frame was captured at.

    Screencast metadata reports the device size in CSS pixels, the same space
    locator bounding boxes use, so this is what makes action coordinates
    projectable onto the finished video.
    """
    if not isinstance(raw, dict):
        return None
    try:
        viewport = Viewport(
            float(raw.get("deviceWidth", 0)),
            float(raw.get("deviceHeight", 0)),
        )
    except (TypeError, ValueError):
        return None
    return viewport if viewport.usable else None


def _probe_size(path: Path) -> tuple[int, int]:
    """Return the assembled video's pixel size, or zeros when unknown."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return (0, 0)
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
        if streams:
            return (
                int(streams[0].get("width") or 0),
                int(streams[0].get("height") or 0),
            )
    except Exception:  # noqa: BLE001 - size is advisory metadata only
        logger.debug("ffprobe size failed", exc_info=True)
    return (0, 0)


__all__ = ["RecordedTake", "RecorderError", "TakeRecorder"]
