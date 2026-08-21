# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,redefined-outer-name

"""Unit coverage for live operation: facts, recording bounds and publication."""

from __future__ import annotations

import asyncio
import base64
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
    assert (
        BoundingBox.from_raw(
            {"x": float("nan"), "y": 2, "width": 5, "height": 5},
        )
        is None
    )
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


def test_normalized_location_intersects_partially_visible_targets():
    viewport = Viewport(100.0, 100.0)
    assert normalized_location(
        BoundingBox(x=-20.0, y=80.0, width=40.0, height=40.0),
        viewport,
    ) == {"x": 0.1, "y": 0.9, "width": 0.2, "height": 0.2}
    assert (
        normalized_location(
            BoundingBox(x=150.0, y=150.0, width=10.0, height=10.0),
            viewport,
        )
        is None
    )


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


def test_facts_within_scales_offsets_for_source_playback_rate():
    manifest = json.loads(_manifest_with_facts().as_json_bytes())
    selected = facts_within(
        manifest,
        start_ms=500,
        end_ms=3000,
        playback_rate=2.0,
    )
    assert [fact["clip_offset_ms"] for fact in selected] == [250, 750]
    assert not facts_within(
        manifest,
        start_ms=0,
        end_ms=3000,
        playback_rate=float("nan"),
    )


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


def test_action_finishing_at_duration_watchdog_is_clipped_to_video():
    manifest = TakeManifest(take_id="take-001")
    active = manifest

    class SlowLink(_FakeLink):
        async def request(self, method, params, *, timeout=None):
            nonlocal active
            if method == "locator_action":
                manifest.duration_ms = 1300
                active = None
            return await super().request(method, params, timeout=timeout)

    link = RecordingControlLink(
        SlowLink(),
        manifest_source=lambda: active,
        elapsed_ms=lambda: 1200 if active is not None else 0,
    )
    asyncio.run(
        link.request(
            "locator_action",
            {
                "workspace_id": "w",
                "session_id": "s",
                "page_id": "p",
                "spec": [],
                "action": "click",
            },
        ),
    )
    assert manifest.facts[0].t_start_ms == 1200
    assert manifest.facts[0].t_end_ms == 1300


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


def test_scroll_records_timing_without_a_misleading_document_box():
    manifest = TakeManifest(take_id="take-001")
    link, inner = _link_with(
        manifest,
        bbox={"x": 0, "y": 0, "width": 1280, "height": 18000},
    )
    asyncio.run(
        link.request(
            "locator_action",
            {
                "workspace_id": "w",
                "session_id": "s",
                "page_id": "p",
                "spec": [
                    {"method": "locator", "args": ["body"], "kwargs": []},
                ],
                "action": "scroll",
            },
        ),
    )

    assert [method for method, _ in inner.calls] == ["locator_action"]
    assert manifest.facts[0].op == "scroll"
    assert manifest.facts[0].bbox is None


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

    def remove_listener(self, event, handler):
        if self._handlers.get(event) is handler:
            self._handlers.pop(event)

    async def send(self, method, params=None):
        del params  # the fake records only which methods were sent
        self.sent.append(method)
        return {}


class _FailingStartCdp(_FakeCdp):
    async def send(self, method, params=None):
        await super().send(method, params)
        if method == "Page.startScreencast":
            raise RuntimeError("start failed")
        return {}


def test_a_take_cannot_start_twice(tmp_path: Path):
    recorder = TakeRecorder(workspace=tmp_path)
    cdp = _FakeCdp()
    asyncio.run(recorder.start(cdp, label="first"))
    assert recorder.recording is True
    with pytest.raises(RecorderError):
        asyncio.run(recorder.start(cdp, label="second"))


def test_failed_start_removes_listener_and_allows_a_clean_retry(
    tmp_path: Path,
):
    recorder = TakeRecorder(workspace=tmp_path)
    cdp = _FailingStartCdp()

    async def scenario():
        with pytest.raises(RuntimeError, match="start failed"):
            await recorder.start(cdp)
        assert recorder.recording is False
        assert not cdp._handlers
        healthy = _FakeCdp()
        await recorder.start(healthy)
        assert recorder.recording is True

    asyncio.run(scenario())


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


class _EventCdp:
    def __init__(self) -> None:
        self.handlers: list = []
        self.sent: list[str] = []

    def on(self, event, handler):
        assert event == "Page.screencastFrame"
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        assert event == "Page.screencastFrame"
        self.handlers.remove(handler)

    async def send(self, method, params=None):
        del params
        self.sent.append(method)
        return {}

    def emit_frame(self):
        payload = {
            "data": base64.b64encode(b"jpeg-frame").decode("ascii"),
            "sessionId": 1,
            "metadata": {"deviceWidth": 800, "deviceHeight": 600},
        }
        for handler in list(self.handlers):
            handler(payload)


def _fake_assembler(tmp_path: Path):
    def assemble(take_id, frames, stopped_at):
        del frames, stopped_at
        output = tmp_path / f"{take_id}.mp4"
        output.write_bytes(b"fake-mp4")
        return output

    return assemble


def test_each_take_removes_its_cdp_listener(tmp_path: Path, monkeypatch):
    recorder = TakeRecorder(workspace=tmp_path)
    monkeypatch.setattr(recorder, "_assemble", _fake_assembler(tmp_path))
    monkeypatch.setattr(
        "services.media_files.live_operation.recorder._probe_output",
        lambda _path: (1280, 720, 7120),
    )
    cdp = _EventCdp()

    async def scenario():
        for label in ("first", "second"):
            await recorder.start(cdp, label=label)
            assert len(cdp.handlers) == 1
            cdp.emit_frame()
            await asyncio.sleep(0)
            await recorder.stop()
            assert not cdp.handlers

    asyncio.run(scenario())
    assert [take.label for take in recorder.takes] == ["first", "second"]
    assert [take.manifest.duration_ms for take in recorder.takes] == [
        7120,
        7120,
    ]
    assert [take.manifest.frame_count for take in recorder.takes] == [178, 178]


def test_take_duration_ceiling_auto_stops_and_remains_collectable(
    tmp_path: Path,
    monkeypatch,
):
    recorder = TakeRecorder(workspace=tmp_path)
    recorder._max_duration = 0.01
    monkeypatch.setattr(recorder, "_assemble", _fake_assembler(tmp_path))
    monkeypatch.setattr(
        "services.media_files.live_operation.recorder._probe_output",
        lambda _path: (800, 600, 500),
    )
    cdp = _EventCdp()

    async def scenario():
        await recorder.start(cdp, label="bounded")
        cdp.emit_frame()
        await asyncio.sleep(0.5)
        assert recorder.recording is False
        # Agent code that calls stop just after the ceiling receives the take
        # that was safely auto-stopped instead of failing and losing it.
        return await recorder.stop()

    take = asyncio.run(scenario())
    assert take.label == "bounded"
    assert len(recorder.takes) == 1


def test_scoped_browser_links_are_isolated_between_concurrent_tasks():
    from qwenpaw.browser.runtime import links as runtime_links

    class Link:
        variant = "test"

        def __init__(self, name):
            self.name = name

    global_link = Link("global")
    one = Link("one")
    two = Link("two")
    runtime_links.register_local(global_link)

    async def see_scoped(link):
        with runtime_links.scoped_links([link]):
            await asyncio.sleep(0.02)
            return runtime_links.link_for("test")

    async def scenario():
        return await asyncio.gather(see_scoped(one), see_scoped(two))

    try:
        observed = asyncio.run(scenario())
        assert observed == [one, two]
        assert runtime_links.link_for("test") is global_link
    finally:
        runtime_links.unregister_local(global_link)


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


def test_screenshot_extension_matches_declared_media_type():
    indexed, version, _ = build_image_records(
        project_id="proj-1",
        name="WebP screenshot",
        content=b"webp-bytes",
        media_type="image/webp",
        request_id="req-1",
    )
    assert indexed.relative_uri.endswith(".webp")
    assert indexed.media_type == "image/webp"
    assert version.media_type == "image/webp"


# ─── code execution surface ─────────────────────────────────────────────


def test_top_level_await_is_accepted():
    compiled = _compile("x = 1\nawait Browser.connect()")
    assert compiled is not None


def test_a_syntax_error_is_reported_as_such():
    with pytest.raises(LiveOperationError, match="syntax error"):
        _compile("await (")


@pytest.mark.parametrize(
    "code",
    (
        "import os",
        "from os import environ",
        "print(Browser.__dict__)",
        "print(__builtins__)",
    ),
)
def test_model_code_cannot_escape_into_the_creator_backend(code: str):
    with pytest.raises(LiveOperationError, match="unavailable"):
        _compile(code)


def test_model_code_rejects_non_cooperative_while_loops():
    with pytest.raises(
        LiveOperationError,
        match="while loops are unavailable",
    ):
        _compile("while True:\n    pass")


def test_model_code_bounds_range_before_it_can_block_the_event_loop():
    from services.media_files.live_operation.bridge import _execute

    with pytest.raises(LiveOperationError, match="limited to 1000 items"):
        asyncio.run(_execute(_compile("list(range(1001))"), {}))

    assert (
        asyncio.run(_execute(_compile("result = list(range(1000))"), {}))
        is None
    )


def test_model_print_is_captured_without_process_global_stdout():
    import io

    from services.media_files.live_operation.bridge import _execute

    output = io.StringIO()
    asyncio.run(
        _execute(
            _compile('print("isolated", 7, sep=":")'),
            {},
            output=output,
        ),
    )
    assert output.getvalue() == "isolated:7\n"


def test_empty_code_is_rejected_before_a_browser_is_launched(tmp_path: Path):
    with pytest.raises(LiveOperationError, match="empty"):
        asyncio.run(run_browser_code("   ", run_root=tmp_path, run_id="run-1"))


def test_desktop_guidance_does_not_depend_on_browser_switch(monkeypatch):
    from models import config
    from services.file_agent_runtime.prompts import (
        live_operation_guidance as guidance_module,
    )

    monkeypatch.setattr(config, "get_live_operation_enabled", lambda: False)
    monkeypatch.setattr(config, "get_computer_use_enabled", lambda: True)
    guidance = guidance_module.live_operation_guidance()
    assert "# 真实桌面操作" in guidance
    assert "# 真实网站操作" not in guidance


def test_browser_guidance_stops_acquisition_once_acceptance_is_covered(
    monkeypatch,
):
    from models import config
    from services.file_agent_runtime.prompts import (
        live_operation_guidance as guidance_module,
    )

    monkeypatch.setattr(config, "get_live_operation_enabled", lambda: True)
    monkeypatch.setattr(config, "get_computer_use_enabled", lambda: False)
    monkeypatch.setattr(
        guidance_module,
        "load_host_browser_manual",
        lambda: "",
    )

    guidance = guidance_module.live_operation_guidance()

    assert "素材达到验收标准后立即停止采集" in guidance
    assert "批量 `patch_project`" in guidance


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
