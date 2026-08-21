# -*- coding: utf-8 -*-
"""System screen recording for desktop live operation.

Computer Use drives a native desktop app but, like the browser SDK, never
films it. Where the browser attaches a CDP screencast to the very page it
drives, the desktop has only one real screen, so recording is a system
capture (ffmpeg avfoundation on macOS, gdigrab on Windows) cropped to the
window the agent is operating. Start and stop are explicit, so only the
segment a step needs is filmed.

The captured mp4 and its action manifest are the same shape a browser take
produces, so they publish and drive motion design through exactly the same
path — a desktop take is just source footage cropped to a window.
"""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping

from services.runtime_files.runtime_dependencies import resolve_ffmpeg

from .manifest import TakeManifest, Viewport
from .recorder import RecorderError

logger = logging.getLogger(__name__)

_STOP_GRACE_SECONDS = 8.0
_PROBE_TIMEOUT_SECONDS = 30.0


class ScreenRecorder:
    """Own one ffmpeg system-capture process for a desktop take."""

    def __init__(
        self,
        *,
        workspace: Path,
        fps: int = 25,
        max_duration_seconds: float = 300.0,
    ) -> None:
        self._workspace = workspace
        self._fps = max(1, int(fps))
        self._max_duration = max(5.0, float(max_duration_seconds))
        self._process: subprocess.Popen | None = None
        self._manifest: TakeManifest | None = None
        self._output: Path | None = None
        self._started_at = 0.0
        self._take_index = 0

    @property
    def recording(self) -> bool:
        return self._manifest is not None

    @property
    def manifest(self) -> TakeManifest | None:
        return self._manifest

    def elapsed_ms(self) -> int:
        if not self.recording:
            return 0
        return int((time.monotonic() - self._started_at) * 1000)

    def exceeded_budget(self) -> bool:
        return (
            self.recording
            and (time.monotonic() - self._started_at) > self._max_duration
        )

    def start(
        self,
        *,
        label: str = "",
        window_bounds: Mapping[str, Any] | None = None,
        screen: str = "1",
    ) -> str:
        """Begin capturing the screen, cropped to a window when known."""
        if self.recording:
            raise RecorderError(
                "a take is already recording; stop it before starting another",
            )
        crop = _crop_filter(window_bounds)
        viewport = _viewport_from_bounds(window_bounds)
        self._take_index += 1
        take_id = f"desktop-take-{self._take_index:03d}"
        output = (self._workspace / f"{take_id}.mp4").resolve()
        command = _capture_command(
            ffmpeg=resolve_ffmpeg() or "ffmpeg",
            fps=self._fps,
            screen=screen,
            crop=crop,
            output=output,
        )
        if command is None:
            raise RecorderError(
                "desktop screen recording is only supported on macOS and "
                "Windows",
            )
        try:
            # A long-lived capture process that outlives this call, so it is
            # deliberately not a context-managed 'with'.
            # pylint: disable-next=consider-using-with
            process = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001 - surface a clear cause
            raise RecorderError(
                f"could not start screen recording: {exc}",
            ) from exc
        self._process = process
        self._output = output
        self._started_at = time.monotonic()
        self._manifest = TakeManifest(take_id=take_id, label=label)
        self._manifest.viewport = viewport
        self._manifest.fps = self._fps
        return take_id

    def stop(self) -> tuple[Path, TakeManifest]:
        """Stop capturing and return the finished mp4 with its manifest."""
        manifest = self._manifest
        process = self._process
        output = self._output
        if manifest is None or process is None or output is None:
            raise RecorderError("no take is recording")
        self._manifest = None
        self._process = None
        self._output = None
        duration_ms = int((time.monotonic() - self._started_at) * 1000)
        self._terminate(process)
        if not output.exists() or output.stat().st_size == 0:
            raise RecorderError(
                "screen recording produced no output; the capture device may "
                "be unavailable or screen-recording permission was denied",
            )
        manifest.duration_ms = max(duration_ms, 0)
        if manifest.viewport is not None:
            # The capture is cropped to the window, so the recorded frame size
            # is the window size actions are already projected against.
            manifest.video_width = int(manifest.viewport.width)
            manifest.video_height = int(manifest.viewport.height)
        return output, manifest

    def stop_if_recording(self) -> tuple[Path, TakeManifest] | None:
        if not self.recording:
            return None
        try:
            return self.stop()
        except RecorderError:
            return None

    def _terminate(self, process: subprocess.Popen) -> None:
        """Ask ffmpeg to finish the file, then insist if it will not."""
        # ffmpeg finalizes the mp4 moov atom on a graceful 'q'; killing it
        # outright can leave an unplayable file, so try that route first.
        try:
            if process.stdin is not None:
                process.stdin.write(b"q")
                process.stdin.flush()
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=_STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            logger.warning("screen recording did not stop gracefully; killing")
        process.terminate()
        try:
            process.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()


def _capture_command(
    *,
    ffmpeg: str,
    fps: int,
    screen: str,
    crop: str | None,
    output: Path,
) -> list[str] | None:
    """Build the platform screen-capture argv, or None where unsupported."""
    if sys.platform == "darwin":
        source = ("-f", "avfoundation", "-i", f"{screen}:none")
    elif sys.platform == "win32":
        source = ("-f", "gdigrab", "-i", "desktop")
    else:
        return None
    command = [
        ffmpeg,
        "-y",
        "-framerate",
        str(fps),
        *source,
    ]
    filters = [f"fps={fps}", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if crop:
        filters.insert(0, crop)
    command += [
        "-vf",
        ",".join(filters),
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        str(output),
    ]
    return command


def _crop_filter(bounds: Mapping[str, Any] | None) -> str | None:
    """Render an ffmpeg crop filter from window bounds, when they are sane."""
    if not isinstance(bounds, Mapping):
        return None
    try:
        width = int(bounds["width"])
        height = int(bounds["height"])
        left = int(bounds.get("x", bounds.get("left", 0)))
        top = int(bounds.get("y", bounds.get("top", 0)))
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return f"crop={width}:{height}:{max(left, 0)}:{max(top, 0)}"


def _viewport_from_bounds(bounds: Mapping[str, Any] | None) -> Viewport | None:
    """The recorded window size actions are projected against."""
    if not isinstance(bounds, Mapping):
        return None
    try:
        viewport = Viewport(float(bounds["width"]), float(bounds["height"]))
    except (KeyError, TypeError, ValueError):
        return None
    return viewport if viewport.usable else None


def screen_capture_supported() -> bool:
    """Whether this platform has a screen-capture backend at all."""
    return sys.platform in ("darwin", "win32")


def ffmpeg_available() -> bool:
    return bool(resolve_ffmpeg() or shutil.which("ffmpeg"))


__all__ = [
    "ScreenRecorder",
    "ffmpeg_available",
    "screen_capture_supported",
]
