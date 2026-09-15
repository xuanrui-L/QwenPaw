# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-position,redefined-outer-name
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

# 放映端已搬进 creator,player/ 与 backend/ 是同胞目录 -> 直接取父目录。
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "backend"

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

from ivb.format.reader import inspect_bundle  # noqa: E402
from ivb.server.app import create_app  # noqa: E402
from ivb.state.store import ANONYMOUS_USER_ID  # noqa: E402
from ivb.testing import fake_mp4  # noqa: E402

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
    project.assets.artifact_versions_by_id[
        version_id
    ] = models.ArtifactVersion(
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
            motion=models.MotionGraphic(
                html=(
                    "<html><body><span data-interaction-countdown></span>"
                    '<button data-edge-ref="edge:a">'
                    '选择A</button><button data-edge-ref="edge:b">'
                    "选择B</button></body></html>"
                ),
            ),
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
            timeline_id="tl:ep4b",
            title="第4集B · 沉默代价",
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
    authoring = importlib.import_module(
        "services.media_files.presentation_authoring",
    )
    html = (
        (ROOT / "tests/fixtures/authored-presentation.html")
        .read_text()
        .replace(
            "__NODES__",
            "".join(
                '<button data-action="jump" '
                f'data-node-ref="{tid}">{tid}</button>'
                for tid in timelines
            ),
        )
    )
    project.interactive_presentation.motion = models.MotionGraphic(
        html=html,
        design_notes="input_fingerprint="
        + authoring.presentation_fingerprint(project),
    )
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
    assert bundle.segments["tl:ep4a"] == "segments/746c3a65703461.mp4"
    assert inspection.durations["tl:ep4a"] == pytest.approx(95.0)


def test_creator_presentation_is_authored_html(exported):
    import zipfile

    presentation = inspect_bundle(exported).bundle.presentation
    with zipfile.ZipFile(exported) as archive:
        assert (
            presentation.authored_html
            == archive.read("presentation.html").decode()
        )
    assert presentation.screens == {}
    assert presentation.stylesheets == ()


def test_runtime_shell_contains_no_work_page_template():
    """作品页面必须来自模型文档，而不是导出器的布局代码。"""

    assert "data-screen" not in bundle_mod.PLAYER_HTML


@pytest.mark.parametrize(
    "breach",
    ["missing", "script", "mismatch", "foreign_node"],
)
def test_authored_document_failures_are_fatal(exported, tmp_path, breach):
    import json
    import zipfile

    with zipfile.ZipFile(exported) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if breach == "missing":
        members.pop("presentation.html")
    elif breach == "script":
        members["presentation.html"] = members["presentation.html"].replace(
            b"<head>",
            b"<head><script>alert(1)</script>",
        )
    elif breach == "foreign_node":
        members["presentation.html"] = members["presentation.html"].replace(
            b"tl:ep4a",
            b"unknown",
        )
    else:
        manifest = json.loads(members["manifest.json"])
        manifest["authored_html"] = {"invalid": "not the reviewed document"}
        members["manifest.json"] = json.dumps(manifest).encode()
    bad = tmp_path / "bad-authored.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    assert any(
        d.code == "PRESENTATION_CONTRACT" for d in inspect_bundle(bad).fatal
    )


def test_player_server_boots_on_the_creator_export(exported, library):
    """放映端服务能直接吃下 Creator 的 zip,并且选项文案已 join 到位。"""

    library.install(exported, owner_user_id=ANONYMOUS_USER_ID)
    app = create_app(library.data_dir)
    http = pytest.importorskip("fastapi.testclient").TestClient(app)

    payload = http.get("/api/projects/project-contract/bundle").json()
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
        "/api/projects/project-contract/segments/746c3a657033.mp4",
        headers={"Range": "bytes=0-99"},
    )
    assert segment.status_code == 206
    assert len(segment.content) == 100
