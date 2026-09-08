# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
"""HTTP 层:内容端点 + 进度端点 + Range 流媒体。"""

from __future__ import annotations

import re

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
    # 前端不该再手写 edges[ref]:文案、色调与来源都在服务端 join 好
    assert option == {
        "edge_ref": "edge:go_counter",
        "label": "走向柜台",
        "prompt": "你假装整理货架",
        "tone": "safe",
        "target_timeline_id": "timeline:counter",
        "source_timeline_id": "timeline:open",
        "hotspot": None,
    }
    # 边在 IVB v1 里没有来源字段,只能从抉择点反推;地图靠它区分汇流点。
    assert payload["edges"]["edge:go_storage"]["source_timeline_id"] == (
        "timeline:open"
    )
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
        "/api/state/visit",
        json={"timeline_id": "timeline:open"},
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


def _between(text: str, start: str, stop: str) -> str:
    """取 [start, stop) 之间的源码片段 —— 把静态断言锁在单个函数体内。"""

    begin = text.index(start)
    end = text.index(stop)
    return text[begin:end]


def test_tone_is_posthoc_never_on_the_choice_card(client):
    """tone 是**事后标注**:抉择卡不给风险色与文案,只有事后两处消费它。

    理由:红色 = "别选"是替观众做判断,会把分支内容压到没人玩。数据链路
    一个字没改(/api/bundle 照旧下发 tone,跨端契约测试锁着),这里锁的是
    放映端前端的**消费点** —— 正向反向都钉,以免后人把 tone 整个删掉。
    """

    script = client.get("/assets/app.js").text
    stylesheet = client.get("/assets/styles/player.css").text

    card = _between(script, "function makeCard", "function startCountdown")
    assert "tone" not in card
    assert "badge" not in card
    assert ".choice-card.tone" not in stylesheet
    assert ".badge" not in stylesheet

    review = _between(script, "function renderReview", "function layout")
    assert "tone-" in review and "badge_labels" in review
    # 回顾只讲本局 —— 直接吃全历史 path 会把上一局的风险档标到这一局身上。
    assert "S.progress.path" not in review
    assert "currentRunPath" in review
    drawing = _between(script, "function renderMap", "function wire")
    assert "walked" in drawing and "tone-" in drawing
    # 未走过的分支保持中性,否则地图就成了风险预告图。
    assert "walked.has(" in drawing

    for selector in (".review-list .tone-danger", ".map-link.tone-danger"):
        assert selector in stylesheet


def test_map_separates_walked_edges_from_reachable_ones(client):
    """地图必须分得清"走过的路"与"只是可去",且零进度时得看得见起点。

    方案 B:地图只长在观众踩过的地方。revealed = {entry} ∪ visited,不再按
    reveal_depth 向下游展开。fog 节点不渲染,入口没看过时显示"起点"。
    """

    script = client.get("/assets/app.js").text
    stylesheet = client.get("/assets/styles/player.css").text

    drawing = _between(script, "function renderMap", "function wire")
    assert "walkedEdge" in drawing
    assert "reachable" in drawing
    # 风险色仍然只跟着走过的边,不能被 reachable 带回去。
    assert 'linkClass += " seen"' in drawing
    # 汇流点必须按边的来源精确匹配,否则一个父节点走过会把另一个也染绿。
    assert "item.source_timeline_id === id" in drawing
    assert "isChildOf" not in script
    # 方案 B:两端都没揭示的边不画。
    assert "if (!shown) return" in drawing
    # 方案 B:fog 节点不画。
    assert "if (!open) return" in drawing
    # 入口没看过时显示"起点"而非"可去"。
    assert '"起点"' in drawing

    reveal = _between(script, "function revealSet", "function renderMap")
    assert "entry_timeline_id" in reveal
    # 方案 B:不再按 reveal_depth 展开下游,revealed 只有入口+已看。
    assert "reveal_depth" not in reveal
    assert "frontier" not in reveal

    assert ".map-link.reachable" in stylesheet
    # 拿 --ivb-fog 当文字色等于隐身(它与页面底色几乎同色),中过三招。
    assert "color:var(--ivb-fog)" not in stylesheet


def test_every_dom_id_used_by_app_js_exists_in_the_page(client):
    """app.js 按名字批量抓 DOM,页面上少一个 id 就是运行时 undefined。

    放映端没有 JS 测试运行器,这类“两端名字对得上”的守卫只能放在 Python 里。
    """

    script = client.get("/assets/app.js").text
    page = client.get("/").text
    block = _between(script, "const dom = {}", "].forEach")
    used = set(re.findall(r'"([A-Za-z0-9_-]+)"', block))
    declared = set(re.findall(r'id="([^"]+)"', page))
    assert used, "没抓到 id 列表,下面的断言会是空真"
    assert used - declared == set()


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
