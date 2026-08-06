# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,consider-using-from-import
"""External skill tools wired through the main Agent driver loop."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from models import config
import services.external_skills as external_skills
from services.file_agent_runtime import (
    AgentModelTurn,
    AgentRunStatus,
    AgentToolCall,
    CallbackAgentChatClient,
    FileCreatorAgentRuntime,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import (
    ExecutionAuthorizationStatus,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "project-1"
SESSION_ID = "session-1"
CONVERSATION_ID = "conversation-1"
GOAL_ID = "goal-1"

_SKILL_MD = """---
name: demo-skill
description: Use when the user asks for a demo artifact.
---

# Demo Skill
"""


@pytest.fixture(autouse=True)
def _reset_caches():
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()
    yield
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()


def _create_project(tmp_path, *, initial_goal: str | None):
    services = CreatorFileServices.create(tmp_path.resolve())

    def initialize(staged_root) -> None:
        services.sessions.initialize_staged_project(
            staged_root,
            PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            initial_goal=initial_goal,
            goal_id=GOAL_ID if initial_goal is not None else None,
            initial_message_id="message-initial"
            if initial_goal is not None
            else None,
            initial_client_message_id=(
                "client-initial" if initial_goal is not None else None
            ),
        )

    snapshot = services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Initial"),
        initialize_staged_project=initialize,
    )
    services.poller.note_commit(snapshot)
    return services, snapshot


async def _wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.02)


def _write_demo_skill(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "echo.py").write_text(
        "import sys\nprint('hello', *sys.argv[1:])\n",
        encoding="utf-8",
    )
    # Renders a real (tiny) H.264 clip plus a junk file wearing a media
    # extension, so import validation has one accept and one reject case.
    (scripts / "artifact.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('dist').mkdir(exist_ok=True)\n"
        "subprocess.run(\n"
        "    [\n"
        "        'ffmpeg', '-y', '-v', 'error',\n"
        "        '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:d=0.4:r=10',\n"
        "        '-pix_fmt', 'yuv420p', 'dist/output.mp4',\n"
        "    ],\n"
        "    check=True,\n"
        ")\n"
        "Path('dist/fake.mp4').write_bytes(b'fake-mp4-bytes')\n"
        "print('rendered')\n",
        encoding="utf-8",
    )
    return root


def _write_skills_config(data_root: Path, entries: list[dict]) -> None:
    (data_root / "config").mkdir(parents=True, exist_ok=True)
    (data_root / "config" / "skills_config.json").write_text(
        json.dumps({"skills": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()


def test_broken_skill_config_never_breaks_the_agent_run(
    tmp_path,
    monkeypatch,
) -> None:
    """Isolation: bad path + malformed entries keep the full loop green."""

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    _write_skills_config(
        tmp_path,
        [
            {
                "name": "ghost",
                "path": str(tmp_path / "missing"),
                "enabled": True,
            },
            {"name": "malformed entry ~~ not valid"},
        ],
    )
    turn = 0

    async def callback(messages, tools):
        nonlocal turn
        names = {item["function"]["name"] for item in tools}
        # Unavailable skills expose no skill tools and inject no context.
        assert "run_skill_script" not in names
        assert "ghost" not in messages[0]["content"]
        turn += 1
        if turn == 1:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="read-1",
                        name="read_project",
                        arguments={"projectId": PROJECT_ID},
                    ),
                ),
            )
        return AgentModelTurn(content="项目状态已读取。")

    async def scenario():
        services, _snapshot = _create_project(tmp_path, initial_goal="读取项目")
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        await driver.start()
        driver.notify(PROJECT_ID)
        await _wait_for(
            lambda: (
                services.sessions.get_project_session(
                    PROJECT_ID,
                ).last_consumed_message_seq
                == 1
            ),
        )
        await driver.wait_until_idle(PROJECT_ID)
        session = services.sessions.get_project_session(PROJECT_ID)
        runs = driver.runs.list(PROJECT_ID)
        await driver.stop()
        return session, runs

    session, runs = asyncio.run(scenario())
    assert session.status.value == "IDLE"
    assert session.error is None
    assert len(runs) == 1
    assert runs[0].status is AgentRunStatus.SUCCEEDED


def test_view_skill_returns_markdown_without_authorization(
    tmp_path,
    monkeypatch,
) -> None:
    """The viewer is the progressive-disclosure entry: read-only, no gate."""

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    skill_root = _write_demo_skill(tmp_path / "demo-src")
    _write_skills_config(
        tmp_path,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    turn = 0

    async def callback(messages, tools):
        nonlocal turn
        names = {item["function"]["name"] for item in tools}
        assert "view_skill" in names
        # Progressive disclosure: the system prompt carries the catalog
        # entry, never the SKILL.md body.
        assert "<name>demo-skill</name>" in messages[0]["content"]
        assert "scripts/echo.py" not in messages[0]["content"]
        turn += 1
        if turn == 1:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="view-1",
                        name="view_skill",
                        arguments={"skill": "demo-skill"},
                    ),
                ),
            )
        return AgentModelTurn(content="已阅读 skill 说明。")

    async def scenario():
        services, _snapshot = _create_project(
            tmp_path,
            initial_goal="查看 demo skill",
        )
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        await driver.start()
        driver.notify(PROJECT_ID)
        await _wait_for(
            lambda: (
                services.sessions.get_project_session(
                    PROJECT_ID,
                ).last_consumed_message_seq
                == 1
            ),
        )
        await driver.wait_until_idle(PROJECT_ID)
        messages = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
        runs = driver.runs.list(PROJECT_ID)
        authorizations = driver.executions.list_execution_authorizations(
            PROJECT_ID,
        )
        await driver.stop()
        return messages, runs, authorizations

    messages, runs, authorizations = asyncio.run(scenario())
    assert runs[0].status is AgentRunStatus.SUCCEEDED
    tool_results = [item for item in messages if item.role == "tool"]
    payload = json.loads(tool_results[0].content_parts[0].text or "{}")
    assert payload["ok"] is True
    assert payload["content"] == _SKILL_MD
    # Viewing a skill is read-only and never requests authorization.
    assert authorizations == []


def test_run_skill_script_requires_authorization_then_executes(
    tmp_path,
    monkeypatch,
) -> None:
    import services.file_agent_runtime.driver as driver_module

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        driver_module,
        "get_execution_authorization_mode",
        lambda: "required",
    )
    skill_root = _write_demo_skill(tmp_path / "demo-src")
    _write_skills_config(
        tmp_path,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    turn = 0

    async def callback(messages, tools):
        nonlocal turn
        names = {item["function"]["name"] for item in tools}
        assert "run_skill_script" in names
        assert "demo-skill" in messages[0]["content"]
        turn += 1
        if turn == 1:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="skill-1",
                        name="run_skill_script",
                        arguments={
                            # A wrong projectId echo must not kill the run:
                            # skill tools take their Project identity from
                            # the runtime, never from the model.
                            "projectId": "project-someone-else",
                            "skill": "demo-skill",
                            "script": "scripts/echo.py",
                            "args": ["from-driver"],
                        },
                    ),
                ),
            )
        return AgentModelTurn(content="脚本已执行。")

    async def scenario():
        services, _snapshot = _create_project(
            tmp_path,
            initial_goal="执行 demo skill",
        )
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        await driver.start()
        driver.notify(PROJECT_ID)
        await _wait_for(
            lambda: bool(
                driver.executions.list_execution_authorizations(PROJECT_ID),
            ),
        )
        authorization = driver.executions.list_execution_authorizations(
            PROJECT_ID,
        )[0]
        driver.executions.decide_execution_authorization(
            PROJECT_ID,
            authorization.authorization_id,
            authorization_token=authorization.authorization_token,
            status=ExecutionAuthorizationStatus.APPROVED,
            decision={
                "provider": authorization.requested_provider,
                "model": authorization.requested_model,
                "maxCost": 0,
                "maxCandidates": 1,
            },
        )
        await _wait_for(
            lambda: (
                services.sessions.get_project_session(
                    PROJECT_ID,
                ).last_consumed_message_seq
                == 1
            ),
        )
        await driver.wait_until_idle(PROJECT_ID)
        messages = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
        events = services.sessions.list_events(PROJECT_ID, SESSION_ID)
        runs = driver.runs.list(PROJECT_ID)
        await driver.stop()
        return authorization, messages, events, runs

    authorization, messages, events, runs = asyncio.run(scenario())
    assert authorization.operation == "run_skill_script"
    assert authorization.requested_provider == "external_skill"
    assert authorization.requested_model == "demo-skill"
    assert runs[0].status is AgentRunStatus.SUCCEEDED
    event_types = {item.event_type for item in events}
    assert "execution.authorization_required" in event_types
    assert "execution.authorization_decided" in event_types
    tool_results = [item for item in messages if item.role == "tool"]
    payloads = [
        json.loads(item.content_parts[0].text or "{}") for item in tool_results
    ]
    skill_payload = next(
        item for item in payloads if item.get("script") == "scripts/echo.py"
    )
    assert skill_payload["ok"] is True
    assert "hello from-driver" in skill_payload["stdout"]
    assert skill_payload["executionAuthorizationId"] == (
        authorization.authorization_id
    )


def test_rejected_skill_authorization_blocks_execution(
    tmp_path,
    monkeypatch,
) -> None:
    import services.file_agent_runtime.driver as driver_module

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        driver_module,
        "get_execution_authorization_mode",
        lambda: "required",
    )
    skill_root = _write_demo_skill(tmp_path / "demo-src")
    _write_skills_config(
        tmp_path,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    turn = 0

    async def callback(_messages, _tools):
        nonlocal turn
        turn += 1
        if turn == 1:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="skill-1",
                        name="run_skill_script",
                        arguments={
                            "projectId": PROJECT_ID,
                            "skill": "demo-skill",
                            "script": "scripts/echo.py",
                        },
                    ),
                ),
            )
        return AgentModelTurn(content="已按拒绝结果停止执行。")

    async def scenario():
        services, _snapshot = _create_project(
            tmp_path,
            initial_goal="执行 demo skill",
        )
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        await driver.start()
        driver.notify(PROJECT_ID)
        await _wait_for(
            lambda: bool(
                driver.executions.list_execution_authorizations(PROJECT_ID),
            ),
        )
        authorization = driver.executions.list_execution_authorizations(
            PROJECT_ID,
        )[0]
        driver.executions.decide_execution_authorization(
            PROJECT_ID,
            authorization.authorization_id,
            authorization_token=authorization.authorization_token,
            status=ExecutionAuthorizationStatus.REJECTED,
            decision={"reason": "not now"},
        )
        await driver.wait_until_idle(PROJECT_ID)
        messages = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
        await driver.stop()
        return messages

    messages = asyncio.run(scenario())
    tool_results = [item for item in messages if item.role == "tool"]
    payloads = [
        json.loads(item.content_parts[0].text or "{}") for item in tool_results
    ]
    failed = next(item for item in payloads if item.get("ok") is False)
    assert "authorization rejected" in failed["error"]["message"]
    # The sandbox never ran: no working copy was seeded for the skill.
    assert not (
        tmp_path / "skills-runtime" / PROJECT_ID / "demo-skill"
    ).exists()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required to render and probe the artifact",
)
def test_import_skill_artifacts_registers_project_source(
    tmp_path,
    monkeypatch,
) -> None:
    import services.file_agent_runtime.driver as driver_module

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        driver_module,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    skill_root = _write_demo_skill(tmp_path / "demo-src")
    _write_skills_config(
        tmp_path,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    turn = 0

    async def callback(_messages, _tools):
        nonlocal turn
        turn += 1
        if turn == 1:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="skill-1",
                        name="run_skill_script",
                        arguments={
                            "projectId": PROJECT_ID,
                            "skill": "demo-skill",
                            "script": "scripts/artifact.py",
                        },
                    ),
                ),
            )
        if turn == 2:
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="import-1",
                        name="import_skill_artifacts",
                        arguments={
                            "projectId": PROJECT_ID,
                            "skill": "demo-skill",
                            "paths": [
                                "dist/output.mp4",
                                "dist/fake.mp4",
                                "dist/missing.mp4",
                            ],
                        },
                    ),
                ),
            )
        return AgentModelTurn(content="产物已入库。")

    async def scenario():
        services, _snapshot = _create_project(tmp_path, initial_goal="生成并入库产物")
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        await driver.start()
        driver.notify(PROJECT_ID)
        # The scripted turn renders a real ffmpeg clip; allow extra time
        # on loaded machines.
        await _wait_for(
            lambda: (
                services.sessions.get_project_session(
                    PROJECT_ID,
                ).last_consumed_message_seq
                == 1
            ),
            timeout=60.0,
        )
        await driver.wait_until_idle(PROJECT_ID)
        project = services.projects.read(PROJECT_ID)
        messages = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
        runs = driver.runs.list(PROJECT_ID)
        project_root = services.projects.project_root(PROJECT_ID)
        await driver.stop()
        return project, messages, runs, project_root

    project, messages, runs, project_root = asyncio.run(scenario())
    assert runs[0].status is AgentRunStatus.SUCCEEDED
    tool_results = [item for item in messages if item.role == "tool"]
    payloads = [
        json.loads(item.content_parts[0].text or "{}") for item in tool_results
    ]
    import_payload = next(
        item for item in payloads if item.get("imported") is not None
    )
    assert import_payload["status"] == "success"
    assert import_payload["imported_count"] == 1
    assert any("missing.mp4" in issue for issue in import_payload["issues"])
    # Arbitrary bytes behind a media extension are rejected, not indexed.
    assert any(
        "fake.mp4" in issue and "validation" in issue
        for issue in import_payload["issues"]
    )
    entry = import_payload["imported"][0]
    versions = project.project.assets.source_versions_by_id
    version = versions[entry["source_asset_version_id"]]
    assert version.media_kind == "video"
    assert version.metadata["sourceKind"] == "external_skill_artifact"
    assert version.metadata["skill"] == "demo-skill"
    indexed = project.project.assets.files_by_id[entry["file_id"]]
    stored = project_root / indexed.relative_uri
    # The stored file is the real rendered clip from the per-Project sandbox.
    sandbox_artifact = (
        tmp_path
        / "skills-runtime"
        / PROJECT_ID
        / "demo-skill"
        / "dist"
        / "output.mp4"
    )
    assert stored.read_bytes() == sandbox_artifact.read_bytes()
