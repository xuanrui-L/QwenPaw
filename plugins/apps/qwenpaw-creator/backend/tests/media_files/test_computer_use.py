# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,redefined-outer-name

"""Unit coverage for the desktop (computer_use) live-operation path.

Actual desktop capture needs the Tauri host runtime, which is absent in CI, so
these tests cover everything host-independent: capability probing, graceful
degradation, the ffmpeg capture command, the recorder lifecycle, and the
approval coordinator that single-sources grants to the host access store.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from services.media_files.live_operation import (
    computer_use_status,
    run_computer_use_code,
)
from services.media_files.live_operation.approval import (
    ApprovalOutcome,
    DesktopApprovalCoordinator,
    DesktopApprovalRequest,
)
from services.media_files.live_operation.screen_recorder import (
    ScreenRecorder,
    _capture_command,
    _crop_filter,
    _viewport_from_bounds,
)

pytestmark = pytest.mark.unit


# ─── capability probing & degradation ───────────────────────────────────


def test_status_reports_each_precondition_separately():
    status = computer_use_status()
    for key in (
        "available",
        "screen_capture_supported",
        "native_helper_platform",
        "host_reachable",
        "ffmpeg",
    ):
        assert key in status
    # Availability is the conjunction of the parts, never more permissive.
    assert status["available"] == (
        status["screen_capture_supported"]
        and status["native_helper_platform"]
        and status["host_reachable"]
        and status["ffmpeg"]
    )


def test_run_degrades_clearly_without_a_desktop_host(tmp_path: Path):
    # No Tauri host in CI, so the run must explain that instead of failing.
    outcome = asyncio.run(
        run_computer_use_code(
            "await desktop.observe_window()",
            run_root=tmp_path,
            run_id="run-1",
            session_id="proj-1",
        ),
    )
    assert outcome.takes == []
    assert "unavailable" in outcome.output
    assert (
        "headless" in outcome.output
        or "Windows and macOS" in outcome.output
        or "ffmpeg" in outcome.output
    )


def test_empty_desktop_code_is_rejected(tmp_path: Path):
    from services.media_files.live_operation import LiveOperationError

    with pytest.raises(LiveOperationError, match="empty"):
        asyncio.run(
            run_computer_use_code(
                "   ",
                run_root=tmp_path,
                run_id="run-1",
                session_id="proj-1",
            ),
        )


# ─── ffmpeg capture command ─────────────────────────────────────────────


def test_crop_filter_from_window_bounds():
    assert (
        _crop_filter({"x": 40, "y": 60, "width": 800, "height": 600})
        == "crop=800:600:40:60"
    )
    assert _crop_filter({"width": 0, "height": 600}) is None
    assert _crop_filter(None) is None


def test_viewport_from_bounds_is_the_recorded_window_size():
    viewport = _viewport_from_bounds({"width": 1280, "height": 720})
    assert (viewport.width, viewport.height) == (1280.0, 720.0)
    assert _viewport_from_bounds({"width": 0, "height": 0}) is None


def test_capture_command_targets_the_platform_backend():
    command = _capture_command(
        ffmpeg="ffmpeg",
        fps=25,
        screen="1",
        crop="crop=800:600:0:0",
        output=Path("/tmp/take.mp4"),
    )
    if sys.platform == "darwin":
        assert "avfoundation" in command
        assert "1:none" in command
    elif sys.platform == "win32":
        assert "gdigrab" in command
        assert "desktop" in command
    else:
        # Linux has no supported desktop capture backend.
        assert command is None
        return
    joined = " ".join(command)
    assert "crop=800:600:0:0" in joined
    assert "libx264" in command
    assert command[-1] == "/tmp/take.mp4"


# ─── recorder lifecycle (no real capture) ───────────────────────────────


def test_recorder_reports_idle_state(tmp_path: Path):
    recorder = ScreenRecorder(workspace=tmp_path)
    assert recorder.recording is False
    assert recorder.elapsed_ms() == 0
    assert recorder.stop_if_recording() is None


def test_stop_without_start_is_an_error(tmp_path: Path):
    from services.media_files.live_operation import RecorderError

    recorder = ScreenRecorder(workspace=tmp_path)
    with pytest.raises(RecorderError, match="no take is recording"):
        recorder.stop()


# ─── approval coordinator (single-sourced to host store) ─────────────────


class _Decision:
    def __init__(self, allowed: bool, source: str) -> None:
        self.allowed = allowed
        self.source = source


class _FakeStore:
    """A stand-in for the host ComputerUseAccessStore."""

    def __init__(self, existing: _Decision | None = None) -> None:
        self._existing = existing
        self.session_records: list[tuple[object, bool]] = []
        self.persistent_records: list[object] = []

    def resolve(self, request):
        del request
        return self._existing

    def record_session(self, request, *, allowed):
        self.session_records.append((request, allowed))

    def record_persistent(self, request):
        self.persistent_records.append(request)


def _request() -> DesktopApprovalRequest:
    return DesktopApprovalRequest(
        session_id="s1",
        canonical_app_id="com.example.app",
        display_name="Example",
    )


def test_existing_host_grant_is_honored_without_prompting():
    store = _FakeStore(existing=_Decision(True, "persistent"))

    def never(_request):  # pragma: no cover - must not be called
        raise AssertionError("must not prompt when a host grant exists")

    coordinator = DesktopApprovalCoordinator(never, store=store)
    outcome = coordinator.decide(_request())
    assert isinstance(outcome, ApprovalOutcome)
    assert outcome.allowed is True
    assert outcome.source == "persistent"
    # Honoring an existing grant must not re-record it.
    assert not store.session_records


def test_decision_is_written_back_to_the_host_store():
    store = _FakeStore(existing=None)
    coordinator = DesktopApprovalCoordinator(
        lambda _r: (True, True),
        store=store,
    )
    outcome = coordinator.decide(_request())
    assert outcome.allowed is True
    assert outcome.source == "creator"
    assert store.session_records[0][1] is True
    # "Always allow" is the only persistent write.
    assert len(store.persistent_records) == 1


def test_session_only_grant_is_not_persisted():
    store = _FakeStore(existing=None)
    coordinator = DesktopApprovalCoordinator(
        lambda _r: (True, False),
        store=store,
    )
    coordinator.decide(_request())
    assert store.session_records[0][1] is True
    assert not store.persistent_records


def test_a_refusal_is_never_persisted():
    store = _FakeStore(existing=None)
    coordinator = DesktopApprovalCoordinator(
        lambda _r: (False, True),
        store=store,
    )
    outcome = coordinator.decide(_request())
    assert outcome.allowed is False
    # A refusal must not become a standing block that silently denies later.
    assert not store.persistent_records


def test_a_failed_prompt_denies_rather_than_crashes():
    store = _FakeStore(existing=None)

    def boom(_request):
        raise RuntimeError("prompt backend down")

    coordinator = DesktopApprovalCoordinator(boom, store=store)
    outcome = coordinator.decide(_request())
    assert outcome.allowed is False
    assert outcome.source == "prompt_error"


def test_without_a_store_it_still_prompts_and_decides():
    coordinator = DesktopApprovalCoordinator(
        lambda _r: (True, False),
        store=None,
    )
    outcome = coordinator.decide(_request())
    assert outcome.allowed is True
    assert outcome.source == "creator"
