# -*- coding: utf-8 -*-
"""Manual (real-key) acceptance for live website operation.

Billed DashScope calls plus a real browser session, so this is skipped unless
CREATOR_LIVE_OPERATION_REAL_TEST is set. The point of the run is that the
model itself decides how to work: it is given a goal, the browser_use tool and
the same guidance a production run gets, and nothing about the order of steps
is prescribed here.

    CREATOR_LIVE_OPERATION_REAL_TEST=1 \
    DASHSCOPE_API_KEY=<key> \
    python -m pytest -m manual_real \
        tests/manual/test_real_live_operation.py -s

Assertions guard structural invariants only — that the model reached the tool,
that whatever it recorded became Project source material, and that recorded
action coordinates survive into the take manifest. Whether the footage is
good is judged by watching the printed mp4 path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest

from models import config as model_config
from services.media_files.live_operation import (
    build_take_records,
    facts_within,
    run_browser_code,
)

_ENABLED = os.environ.get(
    "CREATOR_LIVE_OPERATION_REAL_TEST",
    "",
).strip().lower() in {"1", "true", "yes", "on"}

pytestmark = [
    pytest.mark.manual_real,
    pytest.mark.skipif(
        not _ENABLED,
        reason=(
            "set CREATOR_LIVE_OPERATION_REAL_TEST=1 to run the billed "
            "live-operation acceptance"
        ),
    ),
]


@pytest.fixture(autouse=True)
def _reap_browsers():
    """Give each case a clean browser plane.

    Two real sessions back to back can otherwise contend for the shared
    Playwright control link; each case owns its own run in production, so the
    reset only restores that isolation for the test.
    """
    yield
    from qwenpaw.browser.control_link.playwright import adapter as pw_adapter

    for link in list(getattr(pw_adapter, "_LIVE", [])):
        with contextlib.suppress(Exception):
            asyncio.run(link.close_all())


_GOAL = "我要做一个「如何在 example.com 上查看页面内容」的教程视频，" + "请你真实操作这个网站，把值得放进教程的操作过程留成素材。"


def _require_text_model() -> None:
    """Fail before spending anything when the text model is not configured."""
    missing = [
        name
        for name, value in (
            ("api_key", model_config.get_text_api_key().strip()),
            ("base_url", model_config.get_text_base_url().strip()),
            ("model", model_config.get_text_model_name().strip()),
        )
        if not value
    ]
    if missing:
        pytest.skip(f"Creator text model is not configured: {missing}")


def _model_client():
    from services.file_agent_runtime.model_client import (
        AgentScopeAgentChatClient,
    )

    return AgentScopeAgentChatClient(timeout_seconds=300.0)


def test_model_chooses_how_to_operate_and_record(tmp_path: Path) -> None:
    """One real model turn must produce runnable browser code on its own."""
    _require_text_model()
    from services.file_agent_runtime.prompts.live_operation_guidance import (
        live_operation_guidance,
    )

    guidance = live_operation_guidance()
    assert "recorder.start" in guidance
    assert "await Browser.connect" in guidance

    client = _model_client()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "browser_use",
                "description": (
                    "用异步 Python 操作真实浏览器；`Browser` 与 `recorder` " "已在作用域内。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    messages = [
        {"role": "system", "content": guidance},
        {"role": "user", "content": _GOAL},
    ]

    async def one_turn():
        return await client.complete(
            messages=messages,
            tools=tools,
            on_text_delta=None,
            on_thinking_delta=None,
            on_tool_call_delta=None,
        )

    turn = asyncio.run(one_turn())
    print("\n=== model turn ===")
    print("finish reason:", turn.finish_reason)
    print("tool calls:", [call.name for call in turn.tool_calls])
    assert turn.tool_calls, "the model did not reach for browser_use"
    call = turn.tool_calls[0]
    assert call.name == "browser_use"
    code = str(call.arguments.get("code") or "")
    print("--- model authored code ---")
    print(code)
    # The closed SDK is what the model must write against; a Playwright-ism
    # here would mean the reused manual failed to teach the real surface.
    assert "Browser.connect" in code
    assert "page.fill(" not in code

    outcome = asyncio.run(
        run_browser_code(code, run_root=tmp_path, run_id="acceptance"),
    )
    print("--- tool output ---")
    print(outcome.output)
    print(
        "takes:",
        len(outcome.takes),
        "screenshots:",
        len(outcome.screenshots),
    )
    for take in outcome.takes:
        print(
            f"take {take.take_id}: {take.summary} -> {take.video_path}",
        )
        assert take.video_path.is_file()
        assert take.video_path.stat().st_size > 0
        payload = json.loads(take.manifest.as_json_bytes())
        print("manifest:", json.dumps(payload, ensure_ascii=False)[:800])
        assert payload["video"]["frame_count"] > 0
        assert payload["facts"], "a recorded take must carry action facts"
        # Every take must be publishable as ordinary source material.
        video_file, manifest_file, version, _ = build_take_records(
            project_id="acceptance",
            take_id=take.take_id,
            label=take.label,
            video=take.video_path.read_bytes(),
            manifest_payload=take.manifest.as_json_bytes(),
            duration_seconds=take.manifest.duration_ms / 1000 or None,
            request_id="req-acceptance",
        )
        assert version.metadata["manifestFileId"] == manifest_file.file_id
        assert video_file.media_type == "video/mp4"


def test_recorded_coordinates_reach_motion_design(tmp_path: Path) -> None:
    """Facts recorded during a take must survive into clip-window queries."""
    _require_text_model()
    code = (
        "browser = await Browser.connect()\n"
        'page = await browser.open("https://example.com")\n'
        "obs = await page.snapshot()\n"
        'print("perceived", len(obs.text or ""))\n'
        'await recorder.start(label="查看页面标题并翻页")\n'
        "heading = page.get_by_role('heading').first\n"
        "await heading.scroll()\n"
        "await page.wait_for_timeout(700)\n"
        'await page.goto("https://example.com/?tutorial=2")\n'
        "await page.wait_for_timeout(900)\n"
        "info = await recorder.stop()\n"
        'print("take", info["take_id"], info["summary"])\n'
    )
    outcome = asyncio.run(
        run_browser_code(code, run_root=tmp_path, run_id="facts"),
    )
    assert outcome.takes, "the deterministic script must produce one take"
    manifest = json.loads(outcome.takes[0].manifest.as_json_bytes())
    duration_ms = manifest["video"]["duration_ms"]
    covering = facts_within(manifest, start_ms=0, end_ms=duration_ms)
    print("facts in clip window:", json.dumps(covering, ensure_ascii=False))
    assert covering, "the whole take window must cover its own facts"
    positioned = [fact for fact in covering if fact.get("location")]
    assert positioned, "at least one action must carry canvas coordinates"
    location = positioned[0]["location"]
    # Normalized canvas coordinates are what an Overlay location consumes.
    for axis in ("x", "y", "width", "height"):
        assert 0.0 <= float(location[axis]) <= 1.5
