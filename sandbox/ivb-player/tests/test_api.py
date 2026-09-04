# -*- coding: utf-8 -*-
"""HTTP 层:内容端点 + 进度端点 + Range 流媒体。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ivb_player.format.reader import BundleError
from ivb_player.server.app import create_app
from ivb_player.testing import BundleSpec, write_bundle_dir, write_bundle_zip


@pytest.fixture
def client(tmp_path):
    path = write_bundle_zip(tmp_path / "api.zip", BundleSpec())
    app = create_app(path, db_path=tmp_path / "state.db")
    with TestClient(app) as instance:
        yield instance


def test_bundle_projection_is_prejoined(client):
    payload = client.get("/api/bundle").json()
    assert payload["entry_timeline_id"] == "timeline:open"
    assert payload["totals"] == {"nodes": 5, "endings": 2, "interactions": 1}
    option = payload["interactions"][0]["options"][0]
    # 前端不该再手写 edges[ref]:文案与色调都在服务端 join 好
    assert option == {
        "edge_ref": "edge:go_counter",
        "label": "走向柜台",
        "prompt": "你假装整理货架",
        "tone": "safe",
        "target_timeline_id": "timeline:counter",
        "hotspot": None,
    }
    assert payload["badge_labels"]["risky"] == "△ 冒险"
    assert payload["theme_css_vars"]["--ivb-accent"] == "#b8ff2e"


def test_presentation_theme_overrides_meta_accent(tmp_path):
    spec = BundleSpec(
        presentation={
            "schema_version": 1,
            "theme": {"accent": "#ff8ad8", "danger": "#ff0000"},
            "screens": {"title": {"cta_label": "开始观看"}},
        },
    )
    app = create_app(write_bundle_dir(tmp_path / "p", spec))
    with TestClient(app) as client:
        payload = client.get("/api/bundle").json()
    assert payload["theme_css_vars"]["--ivb-accent"] == "#ff8ad8"
    assert payload["theme_css_vars"]["--ivb-accent-rgb"] == "255, 138, 216"
    assert payload["meta"]["accent"] == "#ff8ad8"
    assert payload["screens"]["title"]["cta_label"] == "开始观看"


def test_presentation_issues_warnings_but_still_plays(tmp_path):
    spec = BundleSpec(
        presentation={
            "schema_version": 1,
            "theme": {"accent": "hotpink", "danger": "#ff0000"},
            "screens": {"nonsense": {}, "choice": {"layout": "wheel"}},
            "stylesheets": ["styles/ghost.css"],
        },
    )
    app = create_app(write_bundle_dir(tmp_path / "w", spec))
    with TestClient(app) as client:
        report = client.get("/api/validate").json()
        assert report["ok"] is True
    seen = {item["code"] for item in report["diagnostics"]}
    assert {
        "THEME_COLOR_MALFORMED",
        "SCREEN_FIELD_UNKNOWN",
        "STYLESHEET_MISSING",
        "SCREEN_LAYOUT_UNSUPPORTED",
    } <= seen
    assert report["summary"]["fatal"] == 0


def test_stylesheet_and_segment_round_trip(client):
    health = client.get("/api/health").json()
    assert health["ok"] and health["bundle_id"] == "project-smoke-0001"
    missing = client.get("/api/bundle/styles/ghost.css")
    assert missing.status_code == 404


def test_segment_full_read_and_range(client):
    whole = client.get("/api/bundle/segments/timeline_open.mp4")
    assert whole.status_code == 200
    assert whole.headers["accept-ranges"] == "bytes"
    body = whole.content

    partial = client.get(
        "/api/bundle/segments/timeline_open.mp4",
        headers={"Range": "bytes=10-29"},
    )
    assert partial.status_code == 206
    assert partial.content == body[10:30]
    assert partial.headers["content-range"] == f"bytes 10-29/{len(body)}"
    assert partial.headers["content-length"] == "20"

    suffix = client.get(
        "/api/bundle/segments/timeline_open.mp4",
        headers={"Range": f"bytes={len(body) - 8}-"},
    )
    assert suffix.status_code == 206
    assert suffix.content == body[-8:]


def test_segment_rejects_unsatisfiable_range(client):
    response = client.get(
        "/api/bundle/segments/timeline_open.mp4",
        headers={"Range": "bytes=999999-1000000"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"].startswith("bytes */")


def test_segment_blocks_path_traversal(client):
    for name in ("..%2Fmanifest.json", "%2e%2e/manifest.json", "/etc/passwd"):
        response = client.get(f"/api/bundle/segments/{name}")
        assert response.status_code in (400, 404), name
    unknown = client.get("/api/bundle/segments/timeline_ghost.mp4")
    assert unknown.status_code == 404


def test_progress_flow_records_edges_and_seconds(client):
    assert client.get("/api/state/progress").json()["visited"] == []

    client.post(
        "/api/state/visit", json={"timeline_id": "timeline:open"}
    ).raise_for_status()
    client.post(
        "/api/state/watch",
        json={
            "timeline_id": "timeline:open",
            "watched_seconds": 17.4,
        },
    ).raise_for_status()
    chosen = client.post(
        "/api/state/choice",
        json={
            "interaction_source": "timeline:open",
            "edge_ref": "edge:go_storage",
        },
    ).json()
    assert chosen["target_timeline_id"] == "timeline:storage"
    # 选择本身不记 visit:否则一次选择在 visits 里留两行。
    assert len(client.get("/api/state/progress").json()["path"]) == 1

    client.post(
        "/api/state/visit",
        json={
            "timeline_id": "timeline:storage",
            "choice_edge": "edge:go_storage",
        },
    ).raise_for_status()
    client.post(
        "/api/state/visit",
        json={
            "timeline_id": "timeline:bad_end",
        },
    ).raise_for_status()
    unlocked = client.post(
        "/api/state/ending",
        json={"timeline_id": "timeline:bad_end"},
    ).json()
    assert unlocked["first_time"] is True
    assert (
        client.post(
            "/api/state/ending",
            json={"timeline_id": "timeline:storage"},
        ).status_code
        == 422
    )  # storage 不是结局节点

    progress = client.get("/api/state/progress").json()
    assert progress["visited"] == [
        "timeline:open",
        "timeline:storage",
        "timeline:bad_end",
    ]
    assert progress["current_timeline"] == "timeline:bad_end"
    assert [row["choice_edge"] for row in progress["path"]] == [
        None,
        "edge:go_storage",
        None,
    ]
    assert progress["path"][0]["watched_seconds"] == pytest.approx(17.4)

    stats = client.get("/api/state/stats").json()
    assert stats["coverage"] == pytest.approx(3 / 5)
    assert stats["choices_made"] == 1
    assert stats["endings_unlocked"] == 1


def test_api_rejects_unknown_or_non_ending_ids(client):
    assert (
        client.post(
            "/api/state/visit",
            json={"timeline_id": "timeline:ghost"},
        ).status_code
        == 422
    )
    # counter 不是结局节点
    assert (
        client.post(
            "/api/state/ending",
            json={"timeline_id": "timeline:counter"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/state/choice",
            json={
                "interaction_source": "timeline:open",
                "edge_ref": "edge:ghost",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/state/watch",
            json={
                "timeline_id": "timeline:ghost",
                "watched_seconds": 1,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/state/choice",
            json={
                "interaction_source": "timeline:ghost",
                "edge_ref": "edge:go_counter",
            },
        ).status_code
        == 422
    )


def test_reset_really_clears(client):
    client.post("/api/state/visit", json={"timeline_id": "timeline:open"})
    client.post(
        "/api/state/choice",
        json={
            "interaction_source": "timeline:open",
            "edge_ref": "edge:go_counter",
        },
    )
    client.post("/api/state/visit", json={"timeline_id": "timeline:counter"})
    client.post("/api/state/ending", json={"timeline_id": "timeline:good_end"})
    deleted = client.post("/api/state/reset", json={}).json()["deleted"]
    assert deleted["visits"] == 2
    assert deleted["choice_stats"] == 1
    assert deleted["progress"] == 1
    assert client.get("/api/state/progress").json()["visited"] == []


def test_validate_endpoint_reports_a_fresh_read(client):
    report = client.get("/api/validate").json()
    assert report["ok"] is True
    assert report["summary"] == {"fatal": 0, "warning": 0}


def test_invalid_bundle_cannot_start_a_server(tmp_path):
    path = write_bundle_dir(tmp_path / "bad", BundleSpec(breaches=("cycle",)))
    with pytest.raises(BundleError) as exc:
        create_app(path)
    assert any(d.code == "CYCLE_DETECTED" for d in exc.value.diagnostics)


def test_index_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "IVB" in page.text
    assert client.get("/assets/app.js").status_code == 200
    assert "javascript" in client.get("/assets/app.js").headers["content-type"]
    css = client.get("/assets/styles/player.css")
    assert css.status_code == 200
    assert client.get("/assets/../manifest.json").status_code in (
        400,
        403,
        404,
    )
