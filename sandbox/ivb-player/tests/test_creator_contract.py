# -*- coding: utf-8 -*-
"""跨端契约:Creator 导出器产出的 zip 必须被放映端原样读通。

本文件不复制 Creator 的逻辑,而是 **真的调用它的导出器**,再把字节流交给
放映端 Reader / Server。任何一侧改字段名、改语义、改默认值,这里都会红 ——
这正是把两份实现钉在一份规范上的机制。

Creator backend 不在本项目的依赖里(它是插件),所以导入失败就整模块跳过,
不让放映端的独立测试挂在环境上。
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parents[1] / "plugins" / "apps" / "qwenpaw-creator" / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    bundle_mod = importlib.import_module(
        "services.media_files.interactive_bundle",
    )
    models = importlib.import_module("services.project_files.models")
except Exception as exc:  # pragma: no cover - 环境缺失时整模块跳过
    pytest.skip(
        f"creator backend unavailable ({type(exc).__name__}: {exc})",
        allow_module_level=True,
    )

from ivb_player.format.reader import inspect_bundle  # noqa: E402
from ivb_player.format.model import (  # noqa: E402
    BUILTIN_THEME,
    DEFAULT_BADGE_LABELS,
)
from ivb_player.server.app import create_app  # noqa: E402
from ivb_player.testing import fake_mp4  # noqa: E402

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attach_final_cut(project, timeline_id: str, payload: bytes) -> str:
    """把一段成片挂进 `timeline:{id}:render` 槽位 —— 导出器的唯一素材入口。"""

    file_id = f"file:{timeline_id}:final"
    version_id = f"{timeline_id}:final:v1"
    slot_id = f"timeline:{timeline_id}:render"
    project.assets.files_by_id[file_id] = models.IndexedFile(
        file_id=file_id,
        kind="artifact_payload",
        relative_uri=f"assets/final/{timeline_id.replace(':', '_')}.mp4",
        sha256=_sha(payload),
        size_bytes=len(payload),
        media_type="video/mp4",
        created_at=NOW,
    )
    project.assets.artifact_slots_by_id[slot_id] = models.ArtifactSlot(
        slot_id=slot_id,
        kind="final_video",
        owner_ref=f"timeline:{timeline_id}",
        version_ids=[version_id],
        selected_version_id=version_id,
    )
    project.assets.artifact_versions_by_id[version_id] = (
        models.ArtifactVersion(
            version_id=version_id,
            slot_id=slot_id,
            kind="final_video",
            owner_ref=f"timeline:{timeline_id}",
            name=f"{timeline_id} final",
            file_id=file_id,
            checksum=_sha(payload),
            based_on_generation=0,
            created_at=NOW,
        )
    )
    return file_id


def _export_creator_bundle(tmp_path: Path) -> Path:
    """一个真实的分岔项目:2 条分支、1 个抉择点、带三档 tone。

    `at_seconds=88` 刻意沿用 Creator 侧的真实值,分段则给到 95 秒 —— 契约测试
    要跑的是"合规包",不是"我们互相迁就的包"。
    """

    choice = models.TimelineElement(
        element_id="el:choice",
        label="观众抉择",
        span=models.TimelineSpan(start_tick=88_000, duration_tick=4_000),
        creation=models.InteractionCreation(
            type="interaction",
            question="是否当众揭发沈修？",
            options=[
                models.InteractionOption(edge_ref="edge:a"),
                models.InteractionOption(edge_ref="edge:b"),
            ],
            countdown_seconds=10,
            default_edge_ref="edge:a",
        ),
    )
    timelines = {
        "tl:ep3": models.Timeline(
            timeline_id="tl:ep3",
            title="第3集 · 双重身份",
            synopsis="沈修的双重身份被当众戳穿。",
            elements_by_id={"el:choice": choice},
        ),
        "tl:ep4a": models.Timeline(
            timeline_id="tl:ep4a",
            title="第4集A · 真相大白",
            synopsis="真相大白，正义得到伸张。",
        ),
        "tl:ep4b": models.Timeline(
            timeline_id="tl:ep4b", title="第4集B · 沉默代价"
        ),
    }
    project = models.Project(
        project_id="project-contract",
        created_at=NOW,
        updated_at=NOW,
        name="雾山谜案",
        description="雾山深处的双重身份悬疑剧。",
        timelines={
            "items": timelines,
            "order": ["tl:ep3", "tl:ep4a", "tl:ep4b"],
        },
        narrative_edges=[
            models.NarrativeEdge(
                edge_id="edge:a",
                source_timeline_id="tl:ep3",
                target_timeline_id="tl:ep4a",
                label="选择A · 揭发真相",
                tone="safe",
            ),
            models.NarrativeEdge(
                edge_id="edge:b",
                source_timeline_id="tl:ep3",
                target_timeline_id="tl:ep4b",
                label="选择B · 保持沉默",
                tone="danger",
            ),
        ],
    )
    payloads: dict[str, bytes] = {}
    for timeline_id in timelines:
        payload = fake_mp4(95.0)
        payloads[_attach_final_cut(project, timeline_id, payload)] = payload
    archive = bundle_mod.assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )
    target = tmp_path / "creator-export.ivb.zip"
    target.write_bytes(archive)
    return target


@pytest.fixture
def exported(tmp_path) -> Path:
    return _export_creator_bundle(tmp_path)


def test_creator_export_loads_with_zero_diagnostics(exported):
    inspection = inspect_bundle(exported)

    assert [str(item) for item in inspection.fatal] == []
    assert [str(item) for item in inspection.warnings] == []
    assert inspection.bundle is not None


def test_creator_export_shape(exported):
    bundle = inspect_bundle(exported).bundle

    assert bundle is not None
    assert bundle.meta.bundle_id == "project-contract"
    assert bundle.entry_timeline_id == "tl:ep3"
    assert bundle.timeline_ids == ("tl:ep3", "tl:ep4a", "tl:ep4b")
    assert bundle.endings == ("tl:ep4a", "tl:ep4b")
    assert bundle.nodes["tl:ep3"].children == ("tl:ep4a", "tl:ep4b")
    assert bundle.edges["edge:a"].tone == "safe"
    assert bundle.edges["edge:b"].tone == "danger"
    (point,) = bundle.interactions
    assert point.at_seconds == pytest.approx(88.0)
    assert point.option_edges() == ("edge:a", "edge:b")
    assert point.default_edge_ref == "edge:a"


def test_creator_export_segments_are_streamable(exported):
    """分段路径必须能按 manifest 里的相对路径直接读:Range 流媒体就靠这个。"""

    inspection = inspect_bundle(exported)
    bundle = inspection.bundle

    assert bundle is not None
    assert bundle.segments["tl:ep4a"] == "segments/tl_ep4a.mp4"
    assert inspection.durations["tl:ep4a"] == pytest.approx(95.0)


def test_creator_presentation_matches_player_defaults(exported):
    """Creator 写出的表现层必须与放映端内置主题同构:字段名一致、未覆盖项
    落到同一批默认值,否则"可选表现层"就变成第二套真相。"""

    presentation = inspect_bundle(exported).bundle.presentation

    assert presentation.present is True
    assert set(presentation.theme.as_css_vars()) == set(
        BUILTIN_THEME.as_css_vars(),
    )
    assert (
        presentation.screens["choice"]["badge_labels"] == DEFAULT_BADGE_LABELS
    )
    assert presentation.screens["choice"]["layout"] == "list"
    assert presentation.screens["map"]["reveal_depth"] == 1
    assert presentation.stylesheets == ()


def test_badge_labels_are_one_source_across_both_players():
    """两套播放器共用同一批文案:选项卡上写什么,不该由两侧各自决定。"""

    assert bundle_mod.TONE_BADGES == DEFAULT_BADGE_LABELS


def test_player_server_boots_on_the_creator_export(exported):
    """放映端服务能直接吃下 Creator 的 zip,并且选项文案已 join 到位。"""

    client = create_app(exported)
    http = pytest.importorskip("fastapi.testclient").TestClient(client)

    payload = http.get("/api/bundle").json()
    assert payload["bundle_id"] == "project-contract"
    assert (
        payload["theme_css_vars"]["--ivb-accent"] == bundle_mod.DEFAULT_ACCENT
    )
    (interaction,) = payload["interactions"]
    assert {option["label"] for option in interaction["options"]} == {
        "选择A · 揭发真相",
        "选择B · 保持沉默",
    }
    assert [option["tone"] for option in interaction["options"]] == [
        "safe",
        "danger",
    ]

    segment = http.get(
        "/api/bundle/segments/tl_ep3.mp4", headers={"Range": "bytes=0-99"}
    )
    assert segment.status_code == 206
    assert len(segment.content) == 100
