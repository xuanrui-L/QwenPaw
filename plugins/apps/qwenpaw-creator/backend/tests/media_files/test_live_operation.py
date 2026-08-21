# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,redefined-outer-name

"""Unit coverage for live operation: facts, recording bounds and publication."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.media_files.live_operation import (
    ActionFact,
    BoundingBox,
    TakeManifest,
    Viewport,
    build_image_records,
    build_take_records,
    facts_within,
    normalized_location,
)
from services.media_files.live_operation.bridge import (
    AgentRecorder,
    LiveOperationError,
    _ActivePage,
    _compile,
    run_browser_code,
)
from services.media_files.live_operation.recorder import (
    RecorderError,
    TakeRecorder,
    _viewport_from_metadata,
)
from services.media_files.live_operation.recording_link import (
    RecordingControlLink,
    _operation_name,
    _spec_description,
)

from services.media_files.live_operation.session import LiveSessionError

pytestmark = pytest.mark.unit


# ─── coordinate projection ──────────────────────────────────────────────


def test_bounding_box_rejects_degenerate_rectangles():
    assert (
        BoundingBox.from_raw({"x": 1, "y": 2, "width": 0, "height": 5}) is None
    )
    assert BoundingBox.from_raw({"x": 1, "y": 2, "width": 5}) is None
    assert BoundingBox.from_raw(None) is None
    box = BoundingBox.from_raw({"x": 1.5, "y": 2.5, "width": 4, "height": 6})
    assert (box.x, box.y, box.width, box.height) == (1.5, 2.5, 4.0, 6.0)


def test_normalized_location_centres_the_box_on_the_canvas():
    box = BoundingBox(x=256.0, y=108.0, width=768.0, height=28.0)
    location = normalized_location(box, Viewport(1280.0, 720.0))
    # x/y name the anchor point, which Creator places at the box centre.
    assert location == {
        "x": 0.5,
        "y": 0.16944,
        "width": 0.6,
        "height": 0.03889,
    }


def test_normalized_location_needs_a_usable_viewport():
    box = BoundingBox(x=0.0, y=0.0, width=10.0, height=10.0)
    assert normalized_location(box, Viewport(0.0, 0.0)) is None


def test_viewport_comes_from_screencast_metadata():
    viewport = _viewport_from_metadata(
        {"deviceWidth": 1280, "deviceHeight": 720, "pageScaleFactor": 1},
    )
    assert viewport == Viewport(1280.0, 720.0)
    assert (
        _viewport_from_metadata({"deviceWidth": 0, "deviceHeight": 0}) is None
    )
    assert _viewport_from_metadata(None) is None


# ─── manifest shape ─────────────────────────────────────────────────────


def _manifest_with_facts() -> TakeManifest:
    manifest = TakeManifest(take_id="take-001", label="搜索仓库")
    manifest.viewport = Viewport(1280.0, 720.0)
    manifest.video_width = 1280
    manifest.video_height = 720
    manifest.fps = 25
    manifest.duration_ms = 4200
    manifest.frame_count = 30
    manifest.record(
        ActionFact(
            op="click",
            t_start_ms=1000,
            t_end_ms=1200,
            target='get_by_role("button", name="Search")',
            bbox=BoundingBox(640.0, 360.0, 128.0, 72.0),
        ),
    )
    manifest.record(
        ActionFact(
            op="navigate",
            t_start_ms=2000,
            t_end_ms=2600,
            target="https://example.com",
        ),
    )
    return manifest


def test_manifest_serializes_facts_with_projected_locations():
    payload = json.loads(_manifest_with_facts().as_json_bytes())
    assert payload["schema"] == "creator.live_operation.take_manifest"
    assert payload["take_id"] == "take-001"
    assert payload["label"] == "搜索仓库"
    click, navigate = payload["facts"]
    assert click["op"] == "click"
    assert click["bbox"] == {
        "x": 640.0,
        "y": 360.0,
        "width": 128.0,
        "height": 72.0,
    }
    assert click["location"] == {
        "x": 0.55,
        "y": 0.55,
        "width": 0.1,
        "height": 0.1,
    }
    # A navigation has no rectangle, and must not invent one.
    assert "location" not in navigate


def test_manifest_summary_reports_what_the_model_needs():
    summary = _manifest_with_facts().summary()
    assert "4.2s" in summary
    assert "2 actions" in summary
    assert "1 with coordinates" in summary
    assert "1280x720" in summary


def test_facts_within_selects_and_rebases_the_clip_window():
    manifest = json.loads(_manifest_with_facts().as_json_bytes())
    selected = facts_within(manifest, start_ms=1500, end_ms=3000)
    assert [fact["op"] for fact in selected] == ["navigate"]
    assert selected[0]["clip_offset_ms"] == 500
    assert not facts_within(manifest, start_ms=9000, end_ms=9500)
    assert not facts_within({"facts": "broken"}, start_ms=0, end_ms=1)


def test_facts_within_keeps_actions_overlapping_the_cut_boundary():
    manifest = json.loads(_manifest_with_facts().as_json_bytes())
    selected = facts_within(manifest, start_ms=1100, end_ms=1150)
    assert [fact["op"] for fact in selected] == ["click"]
    assert selected[0]["clip_offset_ms"] == 0


# ─── recorded operation vocabulary ──────────────────────────────────────


def test_only_screen_changing_verbs_become_facts():
    assert _operation_name("locator_action", {"action": "click"}) == "click"
    assert _operation_name("navigate", {}) == "navigate"
    assert (
        _operation_name("input", {"kind": "mouse", "action": "click"})
        == "mouse_click"
    )
    # Perception must stay free: reading a page is not an action.
    assert _operation_name("capture_tree", {}) is None
    assert _operation_name("locator_read", {"prop": "inner_text"}) is None
    assert _operation_name("screenshot", {}) is None


def test_spec_description_renders_the_locator_call():
    spec = [
        {
            "method": "get_by_role",
            "args": ["button"],
            "kwargs": [["name", "Search"]],
        },
        {"method": "first", "args": [], "kwargs": []},
    ]
    assert (
        _spec_description(spec)
        == 'get_by_role("button", name="Search").first()'
    )


def test_spec_description_omits_unset_optional_arguments():
    spec = [
        {
            "method": "get_by_role",
            "args": ["heading"],
            "kwargs": [["name", None]],
        },
    ]
    assert _spec_description(spec) == 'get_by_role("heading")'


# ─── recording link behaviour ───────────────────────────────────────────


class _FakeLink:
    """A control link that answers the two verbs recording depends on."""

    variant = "playwright"

    def __init__(
        self,
        *,
        bbox: dict | None = None,
        fail_action: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._bbox = bbox
        self._fail_action = fail_action

    def is_available(self) -> bool:
        return True

    def on_event(self, sink):  # noqa: ARG002 - protocol shape only
        del sink
        return lambda: None

    async def request(
        self,
        method,
        params,
        *,
        timeout=None,
    ):
        del timeout  # accepted for protocol parity; unused by the fake
        self.calls.append((method, dict(params)))
        if method == "locator_bounding_box":
            return {"value": self._bbox}
        if method == "locator_action" and self._fail_action:
            raise RuntimeError("click failed")
        if method == "screenshot":
            return {"path": "/tmp/shot-1.png"}
        return {"evidence": "ok"}


def _link_with(
    manifest: TakeManifest | None,
    **kwargs,
) -> tuple[RecordingControlLink, _FakeLink]:
    inner = _FakeLink(**kwargs)
    link = RecordingControlLink(
        inner,
        manifest_source=lambda: manifest,
        elapsed_ms=lambda: 1234,
    )
    return link, inner


def test_actions_are_recorded_with_a_pre_action_rectangle():
    manifest = TakeManifest(take_id="take-001")
    link, inner = _link_with(
        manifest,
        bbox={"x": 10, "y": 20, "width": 30, "height": 40},
    )
    params = {
        "workspace_id": "w",
        "session_id": "s",
        "page_id": "p",
        "spec": [{"method": "get_by_text", "args": ["Save"], "kwargs": []}],
        "action": "click",
    }
    asyncio.run(link.request("locator_action", params))
    # The rectangle is read before the action, because a click that navigates
    # leaves nothing to measure afterwards.
    assert [method for method, _ in inner.calls] == [
        "locator_bounding_box",
        "locator_action",
    ]
    fact = manifest.facts[0]
    assert fact.op == "click"
    assert fact.target == 'get_by_text("Save")'
    assert fact.bbox == BoundingBox(10.0, 20.0, 30.0, 40.0)
    assert fact.failed is False


def test_a_failed_action_is_still_recorded_and_reraised():
    manifest = TakeManifest(take_id="take-001")
    link, _ = _link_with(manifest, fail_action=True)
    with pytest.raises(RuntimeError):
        asyncio.run(
            link.request(
                "locator_action",
                {
                    "workspace_id": "w",
                    "session_id": "s",
                    "spec": [],
                    "action": "click",
                },
            ),
        )
    assert manifest.facts[0].failed is True


def test_nothing_is_recorded_outside_a_take():
    link, inner = _link_with(None)
    asyncio.run(
        link.request(
            "locator_action",
            {
                "workspace_id": "w",
                "session_id": "s",
                "spec": [],
                "action": "click",
            },
        ),
    )
    # Without a running take there is no bbox probe either: perceiving and
    # acting off-camera must cost nothing.
    assert [method for method, _ in inner.calls] == ["locator_action"]


def test_screenshots_are_collected_for_publication():
    link, _ = _link_with(None)
    asyncio.run(
        link.request("screenshot", {"workspace_id": "w", "session_id": "s"}),
    )
    asyncio.run(
        link.request("screenshot", {"workspace_id": "w", "session_id": "s"}),
    )
    # The same image twice is one asset, not two.
    assert link.screenshots == ["/tmp/shot-1.png"]


def test_last_page_id_tracks_the_operated_page():
    link, _ = _link_with(None)
    asyncio.run(
        link.request(
            "navigate",
            {
                "workspace_id": "w",
                "session_id": "s",
                "page_id": "page-7",
                "url": "https://x",
            },
        ),
    )
    assert link.last_page_id == "page-7"


# ─── recorder lifecycle ─────────────────────────────────────────────────


class _FakeCdp:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._handlers: dict = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    async def send(self, method, params=None):
        del params  # the fake records only which methods were sent
        self.sent.append(method)
        return {}


def test_a_take_cannot_start_twice(tmp_path: Path):
    recorder = TakeRecorder(workspace=tmp_path)
    cdp = _FakeCdp()
    asyncio.run(recorder.start(cdp, label="first"))
    assert recorder.recording is True
    with pytest.raises(RecorderError):
        asyncio.run(recorder.start(cdp, label="second"))


def test_stopping_without_frames_reports_an_unchanged_screen(tmp_path: Path):
    recorder = TakeRecorder(workspace=tmp_path)

    async def scenario():
        await recorder.start(_FakeCdp(), label="nothing happened")
        return await recorder.stop()

    with pytest.raises(RecorderError, match="no frames"):
        asyncio.run(scenario())
    assert recorder.recording is False


def test_stop_if_recording_is_a_no_op_when_idle(tmp_path: Path):
    recorder = TakeRecorder(workspace=tmp_path)
    assert asyncio.run(recorder.stop_if_recording()) is None


def test_elapsed_is_zero_while_idle(tmp_path: Path):
    assert TakeRecorder(workspace=tmp_path).elapsed_ms() == 0


# ─── publication records ────────────────────────────────────────────────


def test_take_records_carry_the_manifest_pointer():
    manifest = _manifest_with_facts()
    video_file, manifest_file, version, logical_asset_id = build_take_records(
        project_id="proj-1",
        take_id="take-001",
        label="搜索仓库",
        video=b"video-bytes",
        manifest_payload=manifest.as_json_bytes(),
        duration_seconds=4.2,
        request_id="req-1",
    )
    assert video_file.media_type == "video/mp4"
    assert video_file.relative_uri.startswith("assets/sources/")
    assert manifest_file.schema_name == "creator.live_operation.take_manifest"
    assert version.media_kind == "video"
    assert version.duration_seconds == 4.2
    assert version.file_id == video_file.file_id
    # The sidecar pointer is what lets motion design find the recorded facts.
    assert version.metadata["manifestFileId"] == manifest_file.file_id
    assert version.metadata["sourceKind"] == "live_operation_take"
    assert logical_asset_id.startswith("asset-")


def test_identical_bytes_produce_identical_ids():
    first = build_take_records(
        project_id="proj-1",
        take_id="take-001",
        label="a",
        video=b"same",
        manifest_payload=b"{}",
        duration_seconds=1.0,
        request_id="req-1",
    )
    second = build_take_records(
        project_id="proj-1",
        take_id="take-002",
        label="b",
        video=b"same",
        manifest_payload=b"{}",
        duration_seconds=1.0,
        request_id="req-2",
    )
    # Re-publishing the same footage must not duplicate the asset.
    assert first[0].file_id == second[0].file_id
    assert first[2].version_id == second[2].version_id


def test_screenshot_records_are_image_sources():
    indexed, version, logical_asset_id = build_image_records(
        project_id="proj-1",
        name="Live operation screenshot 1",
        content=b"png-bytes",
        media_type="image/png",
        request_id="req-1",
    )
    assert indexed.relative_uri.endswith(".png")
    assert version.media_kind == "image"
    assert version.metadata["sourceKind"] == "live_operation_screenshot"
    assert logical_asset_id.startswith("asset-")


# ─── code execution surface ─────────────────────────────────────────────


def test_top_level_await_is_accepted():
    compiled = _compile("x = 1\nawait Browser.connect()")
    assert compiled is not None


def test_a_syntax_error_is_reported_as_such():
    with pytest.raises(LiveOperationError, match="syntax error"):
        _compile("await (")


def test_empty_code_is_rejected_before_a_browser_is_launched(tmp_path: Path):
    with pytest.raises(LiveOperationError, match="empty"):
        asyncio.run(run_browser_code("   ", run_root=tmp_path, run_id="run-1"))


def test_recording_defaults_to_the_page_just_opened():
    """start() must work right after open(), before any other operation.

    Regression guard: defaulting to the last page the control link happened
    to see made the first take depend on an incidental perceive/act call in
    between, which broke a second run in the same process.
    """

    class _StubRecorder:
        def __init__(self) -> None:
            self.started_with = None

        async def start(self, cdp, *, label=""):
            self.started_with = (cdp, label)
            return "take-001"

    class _StubSession:
        def __init__(self) -> None:
            self.requested = None

        async def cdp_session_for(self, page):
            self.requested = page
            return f"cdp-for-{page}"

    active = _ActivePage()
    recorder = _StubRecorder()
    agent_recorder = AgentRecorder(_StubSession(), recorder, active)

    # No page opened yet: starting must ask for a page, not film nothing.
    with pytest.raises(LiveSessionError, match="no page has been opened"):
        asyncio.run(agent_recorder.start())

    active.page = "page-object"
    take_id = asyncio.run(agent_recorder.start(label="first step"))
    assert take_id == "take-001"
    assert recorder.started_with == ("cdp-for-page-object", "first step")
