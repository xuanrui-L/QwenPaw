# -*- coding: utf-8 -*-

from uuid import uuid4

import pytest
from playwright.sync_api import expect

from pages.home_page import HomePage


pytestmark = pytest.mark.shell


def test_home_and_composer_use_current_project_contract(page):
    home = HomePage(page).open()
    expect(page.get_by_role("heading", name="我的项目")).to_be_visible()
    composer = home.open_composer()
    expect(
        page.get_by_text("开始创作吧！请将目标、素材和限制交给 Agent"),
    ).to_be_visible()
    composer.select_scenario("通用")
    composer.fill_name("E2E 冒烟项目").fill_goal("只验证当前 Composer")
    composer.add_url("https://example.com/ref.mp4")
    expect(
        composer.attachment_chip("https://example.com/ref.mp4"),
    ).to_be_visible()
    expect(
        composer.root.get_by_role("button", name="启动 Agent", exact=True),
    ).to_be_enabled()


def test_model_config_values_are_visible_without_legacy_surfaces(page, api):
    config = api.models_config()
    assert set(config) == {
        "llm",
        "vlm",
        "grounding",
        "asr",
        "tts",
        "image",
        "video",
        "oss",
        "creationCheckpoints",
        "executionAuthorization",
    }
    HomePage(page).open().open_model_config()
    modal = page.locator(".model-config-modal")
    expect(modal.locator("button.segmented-tab")).to_have_count(7)


def test_project_export_progress_stays_centered_on_resize(page, project):
    project_id = project["projectId"]
    export_routes = []
    # Hold packaging open long enough to check both layouts, then download
    # the real archive through the same request.
    page.route(
        f"**/projects/{project_id}/export",
        lambda route: export_routes.append(route),
    )
    page.goto(f"/#/project/{project_id}")
    page.get_by_role("button", name="下载 / 导出").click()
    page.get_by_role("menuitem", name="导出项目", exact=True).click()
    card = page.locator("[data-export-progress]")
    expect(card).to_contain_text("正在打包项目")
    for width, height in [(1280, 820), (390, 740)]:
        page.set_viewport_size({"width": width, "height": height})
        box = card.bounding_box()
        assert box is not None
        assert box["x"] + box["width"] / 2 == pytest.approx(width / 2, abs=1)
        assert box["y"] + box["height"] / 2 == pytest.approx(height / 2, abs=1)

    with page.expect_download() as archive:
        export_routes[0].continue_()
    assert archive.value.failure() is None
    expect(card).to_contain_text("已交给浏览器保存")


def test_feedback_identifies_partial_motion_screenshot(page, project):
    page.goto(f"/#/project/{project['projectId']}")
    trigger = page.locator("[data-agent-feedback]")
    expect(trigger).to_be_visible()
    page.evaluate(
        """() => {
        const frame = document.createElement('iframe');
        frame.setAttribute('sandbox', 'allow-scripts');
        frame.dataset.liveMotionPlayer = '';
        frame.srcdoc = '<body style="background: green">Motion</body>';
        frame.style.cssText = 'position:fixed;top:150px;left:20px;width:80px;height:60px';
        document.body.append(frame);
    }""",
    )
    trigger.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("img", name="截图 1", exact=True)).to_be_visible()
    note = dialog.get_by_text("动效的当前帧", exact=False)
    expect(note).to_be_visible()
    dialog.locator("input[type=file]").set_input_files(
        {
            "name": "invalid.svg",
            "mimeType": "image/svg+xml",
            "buffer": b"<svg/>",
        },
    )
    expect(dialog.get_by_role("alert")).to_be_visible()
    expect(note).to_be_visible()
    dialog.get_by_role("button", name="移除截图 1").click()
    expect(note).to_have_count(0)


def test_create_snapshot_and_delete_project(api):
    created = api.create_project(f"E2E contract {uuid4().hex[:8]}")
    project_id = created["projectId"]
    assert created["projectSnapshotId"]
    try:
        snapshot = api.project_snapshot(project_id)
        assert snapshot["projectId"] == project_id
        assert snapshot["project"]["schema_version"] == 4
        assert snapshot["project"]["timelines"]["order"] == ["timeline:main"]
        session = api.get(f"/projects/{project_id}/session")
        assert session.status_code == 200
        assert session.json()["session"]["status"] == "IDLE"
    finally:
        api.delete_project(project_id)
    assert api.get(f"/projects/{project_id}/project").status_code == 404
