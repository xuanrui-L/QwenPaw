# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Benign trailing closers execute the lossless prefix instead of failing.

Reproduces the 2026-08 production case: a 4.4KB streamed tool-call
argument ended with exactly one surplus ``}`` after a complete JSON
object, forcing a repair-and-retry turn even though dropping the tail
was provably lossless.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services.file_agent_runtime.model_client import _parse_tool_arguments
from services.file_agent_runtime.driver import _unfinished_video_element_ids
from services.file_agent_runtime import driver as driver_module
from services.file_agent_runtime import model_context
from services.project_files.models import (
    ArtifactSlot,
    ElementLocation,
    Project,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
)


pytestmark = pytest.mark.unit


def test_single_surplus_closing_brace_is_accepted_as_strict():
    payload = {"projectId": "p-1", "program": ".", "jsonArgs": {"a": 1}}
    raw = json.dumps(payload) + "}"

    arguments, parse_error, repaired, strict_error = _parse_tool_arguments(
        raw,
    )

    assert arguments == payload
    assert parse_error is None
    assert repaired is False
    assert strict_error is None


def test_trailing_real_content_still_goes_through_repair():
    # The tail carries information (a truncated sibling key): accepting the
    # prefix would silently drop it, so the repair path must stay in charge.
    payload = {"projectId": "p-1", "jsonArgs": {"a": 1}}
    raw = json.dumps(payload) + ', "program": "."}'

    arguments, parse_error, repaired, strict_error = _parse_tool_arguments(
        raw,
    )

    assert strict_error is not None
    assert repaired or parse_error is not None
    assert arguments != payload or repaired


# ---------------------------------------------------------------------------
# YOLO completion loop: unfinished video detection
# ---------------------------------------------------------------------------


def _element(element_id: str, start_tick: int = 0) -> TimelineElement:
    return TimelineElement(
        element_id=element_id,
        label=element_id,
        span=TimelineSpan(start_tick=start_tick, duration_tick=4_000),
        location=ElementLocation(),
        creation=R2VCreation(
            narrative="测试叙事",
            storyboard_prompt="测试分镜",
        ),
    )


def _project_with_elements(*element_ids: str) -> Project:
    project = Project.new(project_id="p-yolo", name="YOLO Loop")
    timeline = project.timelines.items["timeline:main"]
    for index, element_id in enumerate(element_ids):
        timeline.elements_by_id[element_id] = _element(
            element_id,
            start_tick=index * 4_000,
        )
    return project


def _finish_video(project: Project, element_id: str) -> None:
    slot_id = f"element:{element_id}:main"
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        kind="element_video",
        owner_ref=f"element:{element_id}",
        version_ids=["artifact-version-x"],
        selected_version_id="artifact-version-x",
    )


def test_elements_without_main_video_are_unfinished():
    project = _project_with_elements("elem:a", "elem:b", "elem:c")
    _finish_video(project, "elem:b")

    assert _unfinished_video_element_ids(project) == ["elem:a", "elem:c"]


def test_unselected_video_slot_is_still_unfinished():
    project = _project_with_elements("elem:a")
    slot_id = "element:elem:a:main"
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        kind="element_video",
        owner_ref="element:elem:a",
        version_ids=[],
        selected_version_id=None,
    )

    assert _unfinished_video_element_ids(project) == ["elem:a"]


# ---- artifact 选区解析注入（方案 3.5b）----------------------------------


def _script_selection_project(tmp_path):
    """带一个 timeline_script 版本（含落盘文件）的 Project。"""

    import hashlib
    from datetime import UTC, datetime

    from services.project_files.script_artifacts import (
        add_script_version,
        script_file_relative_uri,
    )

    project = Project.new(project_id="p-selection", name="Selection")
    text = (
        "## 场 1 · 内景 · 旧宅大厅 · 夜\n\n"
        + "铺垫。" * 200
        + "\n\n**林晚**（低声）：这里……和二十年前一模一样。\n"
    )
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    version = add_script_version(
        project,
        "timeline:main",
        text,
        file_id="file-script-sel",
        checksum=checksum,
        name="剧本 v1",
        now=datetime(2026, 8, 15, tzinfo=UTC),
        based_on_generation=0,
    )
    payload_path = tmp_path / script_file_relative_uri("file-script-sel")
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(text, encoding="utf-8")
    return project, version, text


def test_parse_artifact_selection_path():
    from services.file_agent_runtime.driver import (
        _parse_artifact_selection_path,
    )

    assert _parse_artifact_selection_path(
        "artifact:script:timeline:main@artifact-1",
    ) == ("script:timeline:main", "artifact-1")
    # 非 artifact path（RFC 6901 指针）与残缺形式一律不解析。
    assert _parse_artifact_selection_path("/strategy/creative_brief") is None
    assert _parse_artifact_selection_path("artifact:slot-only") is None
    assert _parse_artifact_selection_path("artifact:@v") is None
    assert _parse_artifact_selection_path(None) is None


def test_artifact_selection_injects_window_around_offsets(tmp_path):
    from services.file_agent_runtime.driver import _artifact_selection_note

    project, version, text = _script_selection_project(tmp_path)
    target = "这里……和二十年前一模一样。"
    start = text.index(target)
    end = start + len(target)

    note = _artifact_selection_note(
        project,
        tmp_path,
        {
            "path": f"artifact:script:timeline:main@{version.version_id}",
            "start": start,
            "end": end,
            "label": "剧本正文",
        },
    )

    assert note is not None
    assert f"«{target}»" in note
    assert f"字符 {start}-{end}" in note
    # 窗口截断：正文远超 ±300 字符，注入必须带省略号且不含全文开头。
    assert note.count("…") >= 1
    assert "## 场 1" not in note


def test_invalid_selection_reports_fallback_note(tmp_path):
    from services.file_agent_runtime.driver import _artifact_selection_note

    project, version, _ = _script_selection_project(tmp_path)
    # 过期版本：报告选区版本已过期并指出当前版本。
    stale = _artifact_selection_note(
        project,
        tmp_path,
        {
            "path": "artifact:script:timeline:main@artifact-older",
            "start": 0,
            "end": 5,
        },
    )
    assert stale is not None
    assert "选区所在版本已过期" in stale
    assert version.version_id in stale
    # 未知槽位：报告选区目标已不存在。
    missing = _artifact_selection_note(
        project,
        tmp_path,
        {"path": "artifact:script:timeline:ghost@v1"},
    )
    assert missing is not None
    assert "已不存在" in missing


def test_message_text_appends_selection_notes(tmp_path):
    from types import SimpleNamespace

    from services.file_agent_runtime.driver import _message_text

    project, version, text = _script_selection_project(tmp_path)
    message = SimpleNamespace(
        content_parts=[],
        metadata={
            "context": {
                "selection": {
                    "path": (
                        "artifact:script:timeline:main"
                        f"@{version.version_id}"
                    ),
                    "start": 0,
                    "end": 4,
                    "text": text[:4],
                },
            },
        },
    )
    rendered = _message_text(message, project=project, project_root=tmp_path)
    assert "[选区上下文" in rendered
    # 不带 project 上下文时保持旧行为：只透传结构化 JSON。
    rendered_plain = _message_text(message)
    assert "[选区上下文" not in rendered_plain


# The local names the driver hands inline media to the provider on.
_MEDIA_ROW_VARIABLES = frozenset(
    {"user_content", "frame_content", "page_content"},
)


def _media_user_rows() -> list[tuple[str, bool]]:
    """Every user row literal in the driver that carries media parts."""
    tree = ast.parse(
        Path(driver_module.__file__).read_text(encoding="utf-8"),
    )
    rows: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields: dict[str, ast.expr] = {
            key.value: value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        role = fields.get("role")
        content = fields.get("content")
        if not isinstance(role, ast.Constant) or role.value != "user":
            continue
        if (
            isinstance(content, ast.Name)
            and content.id in _MEDIA_ROW_VARIABLES
        ):
            rows.append((content.id, "source" in fields))
    return rows


def test_media_bearing_user_rows_say_the_runtime_observed_them():
    """Inline media rides the user role, so the stamp is the only tell.

    The provider's protocol refuses media on a tool row, which leaves the
    demotion ladder no way to separate a frame a reader tool sampled from a
    picture a person uploaded except by the source the driver wrote on the
    row. An unstamped row is traded as an attachment, and the record then
    reports a loss the runtime never had to take.
    """
    rows = _media_user_rows()

    assert {name for name, _stamped in rows} == set(_MEDIA_ROW_VARIABLES)
    assert [name for name, stamped in rows if not stamped] == []
    assert model_context.MEDIA_OBSERVATION_SOURCE in (
        model_context._AUTOMATED_HISTORY_SOURCES
    )
