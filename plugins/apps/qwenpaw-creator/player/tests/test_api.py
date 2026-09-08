# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
"""HTTP 层:多包路由 + 内容端点 + 进度端点 + Range 流媒体。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ivb.format.reader import BundleError
from ivb.server.app import create_app
from ivb.state.store import ANONYMOUS_USER_ID
from ivb.testing import BundleSpec, write_bundle_dir

#: 默认 smoke 包的 project_id(= BundleSpec.bundle_id)。
SMOKE_PID = "project-smoke-0001"
#: 该包所有放映端点的前缀(多包路由后端点按 pid 收敛)。
P = f"/api/projects/{SMOKE_PID}"


@pytest.fixture
def client(library, bundle_zip):
    library.install(bundle_zip, owner_user_id=ANONYMOUS_USER_ID)
    app = create_app(library.data_dir)
    with TestClient(app) as instance:
        yield instance


def test_bundle_projection_is_prejoined(client):
    payload = client.get(f"{P}/bundle").json()
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


def test_presentation_theme_overrides_meta_accent(library, tmp_path):
    spec = BundleSpec(
        presentation={
            "schema_version": 1,
            "theme": {"accent": "#ff8ad8", "danger": "#ff0000"},
            "screens": {"title": {"cta_label": "开始观看"}},
        },
    )
    library.install(
        write_bundle_dir(tmp_path / "p", spec),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        payload = client.get(f"{P}/bundle").json()
    assert payload["theme_css_vars"]["--ivb-accent"] == "#ff8ad8"
    assert payload["theme_css_vars"]["--ivb-accent-rgb"] == "255, 138, 216"
    assert payload["meta"]["accent"] == "#ff8ad8"
    assert payload["screens"]["title"]["cta_label"] == "开始观看"


def test_presentation_issues_warnings_but_still_plays(library, tmp_path):
    spec = BundleSpec(
        presentation={
            "schema_version": 1,
            "theme": {"accent": "hotpink", "danger": "#ff0000"},
            "screens": {"nonsense": {}, "choice": {"layout": "wheel"}},
            "stylesheets": ["styles/ghost.css"],
        },
    )
    library.install(
        write_bundle_dir(tmp_path / "w", spec),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        report = client.get(f"{P}/validate").json()
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
    assert health["ok"] and health["projects"] == 1
    missing = client.get(f"{P}/styles/ghost.css")
    assert missing.status_code == 404


def test_segment_full_read_and_range(client):
    whole = client.get(f"{P}/segments/timeline_open.mp4")
    assert whole.status_code == 200
    assert whole.headers["accept-ranges"] == "bytes"
    body = whole.content

    partial = client.get(
        f"{P}/segments/timeline_open.mp4",
        headers={"Range": "bytes=10-29"},
    )
    assert partial.status_code == 206
    assert partial.content == body[10:30]
    assert partial.headers["content-range"] == f"bytes 10-29/{len(body)}"
    assert partial.headers["content-length"] == "20"

    suffix = client.get(
        f"{P}/segments/timeline_open.mp4",
        headers={"Range": f"bytes={len(body) - 8}-"},
    )
    assert suffix.status_code == 206
    assert suffix.content == body[-8:]


def test_segment_rejects_unsatisfiable_range(client):
    response = client.get(
        f"{P}/segments/timeline_open.mp4",
        headers={"Range": "bytes=999999-1000000"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"].startswith("bytes */")


def test_segment_blocks_path_traversal(client):
    for name in ("..%2Fmanifest.json", "%2e%2e/manifest.json", "/etc/passwd"):
        response = client.get(f"{P}/segments/{name}")
        assert response.status_code in (400, 404), name
    unknown = client.get(f"{P}/segments/timeline_ghost.mp4")
    assert unknown.status_code == 404


def test_progress_flow_records_edges_and_seconds(client):
    assert client.get(f"{P}/state/progress").json()["visited"] == []

    client.post(
        f"{P}/state/visit",
        json={"timeline_id": "timeline:open"},
    ).raise_for_status()
    client.post(
        f"{P}/state/watch",
        json={
            "timeline_id": "timeline:open",
            "watched_seconds": 17.4,
        },
    ).raise_for_status()
    chosen = client.post(
        f"{P}/state/choice",
        json={
            "interaction_source": "timeline:open",
            "edge_ref": "edge:go_storage",
        },
    ).json()
    assert chosen["target_timeline_id"] == "timeline:storage"
    # 选择本身不记 visit:否则一次选择在 visits 里留两行。
    assert len(client.get(f"{P}/state/progress").json()["path"]) == 1

    client.post(
        f"{P}/state/visit",
        json={
            "timeline_id": "timeline:storage",
            "choice_edge": "edge:go_storage",
        },
    ).raise_for_status()
    client.post(
        f"{P}/state/visit",
        json={
            "timeline_id": "timeline:bad_end",
        },
    ).raise_for_status()
    unlocked = client.post(
        f"{P}/state/ending",
        json={"timeline_id": "timeline:bad_end"},
    ).json()
    assert unlocked["first_time"] is True
    assert (
        client.post(
            f"{P}/state/ending",
            json={"timeline_id": "timeline:storage"},
        ).status_code
        == 422
    )  # storage 不是结局节点

    progress = client.get(f"{P}/state/progress").json()
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

    stats = client.get(f"{P}/state/stats").json()
    assert stats["coverage"] == pytest.approx(3 / 5)
    assert stats["choices_made"] == 1
    assert stats["endings_unlocked"] == 1


def test_api_rejects_unknown_or_non_ending_ids(client):
    assert (
        client.post(
            f"{P}/state/visit",
            json={"timeline_id": "timeline:ghost"},
        ).status_code
        == 422
    )
    # counter 不是结局节点
    assert (
        client.post(
            f"{P}/state/ending",
            json={"timeline_id": "timeline:counter"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{P}/state/choice",
            json={
                "interaction_source": "timeline:open",
                "edge_ref": "edge:ghost",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{P}/state/watch",
            json={
                "timeline_id": "timeline:ghost",
                "watched_seconds": 1,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{P}/state/choice",
            json={
                "interaction_source": "timeline:ghost",
                "edge_ref": "edge:go_counter",
            },
        ).status_code
        == 422
    )


def test_reset_really_clears(client):
    client.post(f"{P}/state/visit", json={"timeline_id": "timeline:open"})
    client.post(
        f"{P}/state/choice",
        json={
            "interaction_source": "timeline:open",
            "edge_ref": "edge:go_counter",
        },
    )
    client.post(f"{P}/state/visit", json={"timeline_id": "timeline:counter"})
    client.post(f"{P}/state/ending", json={"timeline_id": "timeline:good_end"})
    deleted = client.post(f"{P}/state/reset").json()["deleted"]
    assert deleted["visits"] == 2
    assert deleted["choice_stats"] == 1
    assert deleted["progress"] == 1
    assert client.get(f"{P}/state/progress").json()["visited"] == []


def test_validate_endpoint_reports_a_fresh_read(client):
    report = client.get(f"{P}/validate").json()
    assert report["ok"] is True
    assert report["summary"] == {"fatal": 0, "warning": 0}


def test_invalid_bundle_cannot_be_installed(library, tmp_path):
    bad = write_bundle_dir(tmp_path / "bad", BundleSpec(breaches=("cycle",)))
    with pytest.raises(BundleError) as exc:
        library.install(bad, owner_user_id=ANONYMOUS_USER_ID)
    assert any(d.code == "CYCLE_DETECTED" for d in exc.value.diagnostics)


def test_two_projects_route_by_pid(library, make_zip):
    """一个进程两个包:按 pid 各取各的内容与进度,互不串。"""

    library.install(
        make_zip("a", bundle_id="project-a"),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    library.install(
        make_zip("b", bundle_id="project-b", title="另一个故事"),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["projects"] == 2
        a = client.get("/api/projects/project-a/bundle").json()
        b = client.get("/api/projects/project-b/bundle").json()
        assert a["bundle_id"] == "project-a"
        assert b["bundle_id"] == "project-b"
        assert b["meta"]["title"] == "另一个故事"
        # 进度按 pid 隔离:a 看过的节点不出现在 b。
        client.post(
            "/api/projects/project-a/state/visit",
            json={"timeline_id": "timeline:open"},
        )
        seen_a = client.get("/api/projects/project-a/state/progress").json()
        seen_b = client.get("/api/projects/project-b/state/progress").json()
    assert seen_a["visited"] == ["timeline:open"]
    assert seen_b["visited"] == []


def test_unknown_project_is_404(client):
    response = client.get("/api/projects/project-ghost/bundle")
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_resolve_caches_until_reinstalled(library, make_dir):
    """缓存按 (pid, mtime) 命中;重新安装同 pid 立即失效并重读。"""

    library.install(
        make_dir("c", bundle_id="project-c"),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    first = library.resolve("project-c")
    assert library.resolve("project-c").inspection is first.inspection

    library.install(
        make_dir("c2", bundle_id="project-c", title="重传标题"),
        owner_user_id=ANONYMOUS_USER_ID,
    )
    second = library.resolve("project-c")
    assert second.inspection is not first.inspection
    assert second.bundle.meta.title == "重传标题"


def test_progress_is_isolated_per_user_header(client):
    """同一 pid,不同 X-User-Id 的进度互不可见;无头落 anonymous。"""

    alice = {"X-User-Id": "alice"}
    client.post(
        f"{P}/state/visit",
        json={"timeline_id": "timeline:open"},
        headers=alice,
    ).raise_for_status()
    assert client.get(f"{P}/state/progress", headers=alice).json()[
        "visited"
    ] == ["timeline:open"]
    # 不带头的请求是另一个用户(anonymous),看不到 alice 的进度。
    anon = client.get(f"{P}/state/progress").json()
    assert anon["visited"] == []
    assert client.get(f"{P}/state/stats", headers=alice).json()["visits"] == 1
    anon_stats = client.get(f"{P}/state/stats").json()
    assert anon_stats["visits"] == 0
    assert anon_stats["user_id"] == ANONYMOUS_USER_ID
    # reset 也只清自己那份:alice 清空后 anonymous 仍为空,互不影响。
    client.post(f"{P}/state/reset", headers=alice).raise_for_status()
    assert (
        client.get(f"{P}/state/progress", headers=alice).json()["visited"]
        == []
    )


def test_project_list_scopes_mine_versus_all(library, make_zip):
    """库列表:all 不过滤,mine 只看当前 X-User-Id 上传的。"""

    library.install(
        make_zip("a", bundle_id="project-a"),
        owner_user_id="alice",
    )
    library.install(make_zip("b", bundle_id="project-b"), owner_user_id="bob")
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        everything = client.get("/api/projects").json()
        assert everything["scope"] == "all"
        assert {p["project_id"] for p in everything["projects"]} == {
            "project-a",
            "project-b",
        }
        mine = client.get(
            "/api/projects",
            params={"scope": "mine"},
            headers={"X-User-Id": "alice"},
        ).json()
        assert mine["scope"] == "mine"
        assert mine["user_id"] == "alice"
        assert [p["project_id"] for p in mine["projects"]] == ["project-a"]


def test_project_detail_returns_catalog_row(client):
    detail = client.get(f"/api/projects/{SMOKE_PID}").json()
    assert detail["project_id"] == SMOKE_PID
    assert detail["node_count"] == 5
    assert detail["ending_count"] == 2
    assert detail["interaction_count"] == 1
    assert "storage_path" not in detail  # 内部路径不进公开视图
    assert client.get("/api/projects/project-ghost").status_code == 404


def _upload(client, path, *, user="alice", name="bundle.zip"):
    with Path(path).open("rb") as handle:
        return client.post(
            "/api/projects",
            files={"file": (name, handle, "application/zip")},
            headers={"X-User-Id": user} if user else {},
        )


def test_upload_registers_then_serves_the_project(library, make_zip):
    app = create_app(library.data_dir)
    package = make_zip("up", bundle_id="project-upload", title="上传的故事")
    with TestClient(app) as client:
        response = _upload(client, package)
        assert response.status_code == 201
        body = response.json()
        assert body["ok"] is True
        assert body["project_id"] == "project-upload"
        assert body["title"] == "上传的故事"
        assert body["node_count"] == 5
        # 上传后即可放映,且出现在 alice 的"我的"列表里。
        played = client.get("/api/projects/project-upload/bundle").json()
        assert played["bundle_id"] == "project-upload"
        mine = client.get(
            "/api/projects",
            params={"scope": "mine"},
            headers={"X-User-Id": "alice"},
        ).json()
        assert [p["project_id"] for p in mine["projects"]] == [
            "project-upload",
        ]


def test_upload_rejects_an_invalid_bundle(library, make_zip):
    app = create_app(library.data_dir)
    bad = make_zip("bad", bundle_id="project-bad", breaches=("cycle",))
    with TestClient(app) as client:
        response = _upload(client, bad)
        assert response.status_code == 422
        body = response.json()
        assert body["ok"] is False
        assert any(
            item["code"] == "CYCLE_DETECTED" for item in body["diagnostics"]
        )
        # 失败的包不进目录、不可放映。
        assert client.get("/api/projects/project-bad").status_code == 404


def test_upload_rejects_a_non_zip(library):
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        response = client.post(
            "/api/projects",
            files={"file": ("notes.txt", b"not a zip", "text/plain")},
            headers={"X-User-Id": "alice"},
        )
        assert response.status_code == 400


def test_upload_requires_a_user_header(library, make_zip):
    app = create_app(library.data_dir)
    package = make_zip("ok", bundle_id="project-ok")
    with TestClient(app) as client:
        assert _upload(client, package, user=None).status_code == 401


def test_upload_enforces_the_size_limit(library, make_zip):
    app = create_app(library.data_dir, max_upload_bytes=16)
    package = make_zip("big", bundle_id="project-big")
    with TestClient(app) as client:
        assert _upload(client, package).status_code == 413


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


def test_index_injects_base_href(client):
    """根挂载注入 <base href="/">,资源改相对路径以便挂子路径。"""

    page = client.get("/").text
    assert '<base href="/"/>' in page
    assert 'src="assets/app.js"' in page
    assert 'href="assets/styles/player.css"' in page


def test_base_href_follows_root_path(library, bundle_zip):
    library.install(bundle_zip, owner_user_id=ANONYMOUS_USER_ID)
    app = create_app(library.data_dir, root_path="/ivb")
    with TestClient(app) as client:
        page = client.get("/").text
    assert '<base href="/ivb/"/>' in page


def test_library_screen_is_wired_into_the_page(client):
    page = client.get("/").text
    for element_id in (
        "screen-library",
        "lib-grid",
        "lib-empty",
        "lib-scope-all",
        "lib-scope-mine",
        "btn-library",
    ):
        assert f'id="{element_id}"' in page


def test_frontend_is_project_id_aware(client):
    """静态锁:state.js 从 baseURI 反推 BASE 且端点带 pid;app.js 有库/放映分支。"""

    state_js = client.get("/assets/state.js").text
    app_js = client.get("/assets/app.js").text
    assert "document.baseURI" in state_js
    assert "/api/projects/" in state_js
    assert "segmentUrl" in state_js
    assert "currentPid" in app_js
    assert "bootLibrary" in app_js
    assert "bootPlayer" in app_js
    assert "global.Api.bundle(pid)" in app_js
    # 旧的写死单包端点不该再出现在前端。
    assert '"/api/bundle"' not in app_js
    assert "/api/state/" not in state_js


def test_create_app_from_env_reads_settings(
    tmp_path,
    monkeypatch,
    bundle_zip,
):
    """部署入口:env 驱动 data_dir 与 root_path(不依赖 cli.py)。"""

    from ivb.serve import create_app_from_env

    data = tmp_path / "envdata"
    monkeypatch.setenv("IVB_DATA_DIR", str(data))
    monkeypatch.setenv("IVB_ROOT_PATH", "/ivb")
    app = create_app_from_env()
    assert app.root_path == "/ivb"
    assert data.is_dir()  # ProjectLibrary 建好了持久卷根
    app.state.library.install(bundle_zip, owner_user_id=ANONYMOUS_USER_ID)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["projects"] == 1
