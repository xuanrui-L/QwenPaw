# -*- coding: utf-8 -*-
"""Manual (real-key) acceptance for desktop live operation.

Actual desktop capture needs the Tauri host's native runtime, which is absent
in CI and on this environment, so this proves what is verifiable with a real
model here: that qwen3-max, given the computer-use guidance, reaches for the
computer_use tool and writes valid closed-vocabulary desktop code, and that
the tool degrades clearly instead of failing opaquely where no host exists.

    CREATOR_LIVE_OPERATION_REAL_TEST=1 \
    TEXT_API_KEY=<key> \
    python -m pytest -m manual_real \
        tests/manual/test_real_computer_use.py -s
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from models import config as model_config
from services.media_files.live_operation import (
    computer_use_status,
    run_computer_use_code,
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
            "desktop live-operation acceptance"
        ),
    ),
]

_GOAL = "我要做一个「如何在系统计算器里做一次加法」的桌面软件演示视频，" + "请你真实操作计算器应用，把值得放进演示的操作过程留成素材。"


def _require_text_model() -> None:
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


def test_model_reaches_for_computer_use_and_writes_desktop_code() -> None:
    """One real model turn must produce runnable desktop code on its own."""
    _require_text_model()
    monkey = os.environ.get("CREATOR_COMPUTER_USE_ENABLED")
    os.environ["CREATOR_COMPUTER_USE_ENABLED"] = "1"
    try:
        from services.file_agent_runtime.prompts import (
            live_operation_guidance as _guidance_module,
        )

        guidance = _guidance_module.live_operation_guidance()
        assert "computer_use" in guidance
        assert "desktop.observe_window" in guidance

        client = _model_client()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "computer_use",
                    "description": (
                        "用异步 Python 真实操作桌面应用；`desktop` 与 " "`recorder` 已在作用域内。"
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
    finally:
        if monkey is None:
            os.environ.pop("CREATOR_COMPUTER_USE_ENABLED", None)
        else:
            os.environ["CREATOR_COMPUTER_USE_ENABLED"] = monkey

    print("\n=== model turn ===")
    print("finish reason:", turn.finish_reason)
    print("tool calls:", [call.name for call in turn.tool_calls])
    assert turn.tool_calls, "the model did not reach for computer_use"
    call = turn.tool_calls[0]
    assert call.name == "computer_use"
    code = str(call.arguments.get("code") or "")
    print("--- model authored code ---")
    print(code)
    assert "desktop" in code
    # The desktop vocabulary is the host's closed set; a browser-ism here would
    # mean the reused manual failed to teach the real surface.
    assert "Browser.connect" not in code
    assert ".id" not in code
    assert ".name" not in code
    assert '"apps"' in code or "'apps'" in code
    assert '"id"' in code or "'id'" in code


def test_desktop_degrades_clearly_without_a_host(tmp_path: Path) -> None:
    """Where no desktop host exists, the run explains itself, not crashes."""
    status = computer_use_status()
    print("\ncapability:", status)
    outcome = asyncio.run(
        run_computer_use_code(
            "await desktop.observe_window()",
            run_root=tmp_path,
            run_id="acceptance",
            session_id="acceptance",
        ),
    )
    print("output:", outcome.output)
    if status["available"]:
        pytest.skip(
            "a real desktop host is present; degradation not exercised",
        )
    assert outcome.takes == []
    assert "unavailable" in outcome.output
