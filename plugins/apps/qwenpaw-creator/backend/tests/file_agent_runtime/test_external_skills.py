# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,consider-using-from-import
# pylint: disable=use-implicit-booleaness-not-comparison
"""External skills: config schema, isolated loading, injection and sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from models import config
from schemas.skills import SkillEntry, SkillRequirementKind
import services.external_skills as external_skills
from services.external_skills import (
    SKILL_CONTEXT_MAX_CHARS,
    SKILL_STREAM_TRUNCATE_BYTES,
    SkillExecutionError,
    execute_skill_script,
    external_skill_tool_manifests,
    load_skills,
    parse_skill_md,
    read_skill_file,
    render_external_skills_context,
    resolve_skill_artifact,
    write_skill_file,
)
from services.file_agent_runtime.prompts import render_creator_system_prompt

pytestmark = pytest.mark.unit

_SKILL_MD = """---
name: demo-skill
description: |
  Use when the user asks for a demo artifact.
---

# Demo Skill

Run scripts/echo.py.
"""


def _write_skill(root: Path, *, skill_md: str = _SKILL_MD) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(skill_md, encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "echo.py").write_text(
        "import os, sys\n"
        "print('hello', *sys.argv[1:])\n"
        "print('SECRET=' + os.environ.get('DEMO_SKILL_TOKEN', '<unset>'))\n"
        "print('LEAK=' + os.environ.get('DEMO_SKILL_PRIVATE', '<unset>'))\n",
        encoding="utf-8",
    )
    (scripts / "sleepy.py").write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    (scripts / "noisy.py").write_text(
        "import sys\nsys.stdout.write('x' * 200000)\n",
        encoding="utf-8",
    )
    (scripts / "artifact.py").write_text(
        "from pathlib import Path\n"
        "Path('dist').mkdir(exist_ok=True)\n"
        "Path('dist/output.mp4').write_bytes(b'fake-mp4-bytes')\n"
        "print('done')\n",
        encoding="utf-8",
    )
    return root


def _configure(
    tmp_path: Path,
    monkeypatch,
    entries: list[dict],
) -> Path:
    data_root = tmp_path / "creator-data"
    (data_root / "config").mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config" / "skills_config.json"
    config_path.write_text(
        json.dumps({"skills": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(data_root))
    monkeypatch.delenv("CREATOR_SKILLS_CONFIG_PATH", raising=False)
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()
    return data_root


@pytest.fixture(autouse=True)
def _reset_caches():
    yield
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()


# ── SkillEntry schema ────────────────────────────────────────────────────────


def test_skill_entry_schema_accepts_full_shape() -> None:
    entry = SkillEntry.model_validate(
        {
            "name": "edu-agent",
            "path": "/tmp/skill",
            "enabled": True,
            "description": "math videos",
            "env": ["DASHSCOPE_API_KEY"],
            "requirements": [
                {"kind": "binary", "value": "ffmpeg"},
                {"kind": "node_min", "value": "18"},
                {"kind": "env", "value": "DASHSCOPE_API_KEY"},
            ],
        },
    )
    assert entry.requirements[1].kind is SkillRequirementKind.NODE_MIN
    assert entry.env == ["DASHSCOPE_API_KEY"]


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "", "path": "/tmp/x"},
        {"name": "Bad Name!", "path": "/tmp/x"},
        {"name": "ok", "path": ""},
        {
            "name": "ok",
            "path": "/tmp/x",
            "requirements": [{"kind": "nope", "value": "x"}],
        },
        {"name": "ok", "path": "/tmp/x", "extra_field": 1},
    ],
)
def test_skill_entry_schema_rejects_invalid(raw: dict) -> None:
    with pytest.raises(Exception):
        SkillEntry.model_validate(raw)


# ── skills_config.json loading ───────────────────────────────────────────────


def test_load_skills_config_reads_and_caches(tmp_path, monkeypatch) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    data_root = _configure(
        tmp_path,
        monkeypatch,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    entries = config.load_skills_config()
    assert [item.name for item in entries] == ["demo-skill"]
    # Rewrite the file: fingerprint invalidation reloads the document.
    (data_root / "config" / "skills_config.json").write_text(
        json.dumps({"skills": []}),
        encoding="utf-8",
    )
    os.utime(
        data_root / "config" / "skills_config.json",
        ns=(1, 1),
    )
    assert config.load_skills_config() == []


def test_load_skills_config_isolates_broken_documents(
    tmp_path,
    monkeypatch,
) -> None:
    data_root = _configure(tmp_path, monkeypatch, [])
    path = data_root / "config" / "skills_config.json"
    path.write_text("{not json", encoding="utf-8")
    config._clear_skills_config_cache()
    assert config.load_skills_config() == []
    issues = config.load_skills_config_issues()
    assert len(issues) == 1
    assert "parse failed" in issues[0]["reason"]
    path.write_text(
        json.dumps(
            {
                "skills": [
                    {"name": "no-path-entry"},
                    {"name": "ok-entry", "path": str(tmp_path)},
                    {"name": "ok-entry", "path": str(tmp_path)},
                ],
            },
        ),
        encoding="utf-8",
    )
    config._clear_skills_config_cache()
    assert [item.name for item in config.load_skills_config()] == ["ok-entry"]
    issues = config.load_skills_config_issues()
    assert [item["name"] for item in issues] == ["no-path-entry", "ok-entry"]
    assert "schema validation failed" in issues[0]["reason"]
    assert "duplicate skill name" in issues[1]["reason"]


def test_invalid_config_entries_surface_as_unavailable_skills(
    tmp_path,
    monkeypatch,
) -> None:
    data_root = _configure(tmp_path, monkeypatch, [])
    (data_root / "config" / "skills_config.json").write_text(
        json.dumps(
            {
                "skills": [
                    {"name": "malformed entry ~~ not valid"},
                ],
            },
        ),
        encoding="utf-8",
    )
    config._clear_skills_config_cache()
    external_skills._clear_load_cache()
    loaded = load_skills()
    assert len(loaded) == 1
    assert loaded[0].status == "unavailable"
    assert "invalid configuration entry" in (loaded[0].reason or "")
    # Invalid placeholders never inject context nor expose tools.
    assert render_external_skills_context(loaded) == ""
    assert external_skill_tool_manifests(loaded) == []


def test_load_skills_config_missing_file_or_root(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CREATOR_DATA_ROOT", raising=False)
    monkeypatch.delenv("CREATOR_SKILLS_CONFIG_PATH", raising=False)
    config._clear_skills_config_cache()
    assert config.load_skills_config() == []
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path / "empty-root"))
    config._clear_skills_config_cache()
    assert config.load_skills_config() == []


# ── SKILL.md parsing ─────────────────────────────────────────────────────────


def test_parse_skill_md_extracts_front_matter_and_body() -> None:
    parsed = parse_skill_md(_SKILL_MD)
    assert parsed["name"] == "demo-skill"
    assert "demo artifact" in parsed["description"]
    assert parsed["body"].startswith("# Demo Skill")


@pytest.mark.parametrize(
    "text",
    ["no front matter at all", "---\n- just\n- a list\n---\nbody"],
)
def test_parse_skill_md_rejects_malformed(text: str) -> None:
    with pytest.raises(ValueError):
        parse_skill_md(text)


# ── Isolated loading ─────────────────────────────────────────────────────────


def test_load_skills_marks_missing_path_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(
        tmp_path,
        monkeypatch,
        [
            {
                "name": "ghost",
                "path": str(tmp_path / "does-not-exist"),
                "enabled": True,
            },
        ],
    )
    loaded = load_skills()
    assert len(loaded) == 1
    assert loaded[0].status == "unavailable"
    assert "not a directory" in (loaded[0].reason or "")


def test_load_skills_marks_bad_skill_md_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no front matter", encoding="utf-8")
    _configure(
        tmp_path,
        monkeypatch,
        [{"name": "broken", "path": str(broken), "enabled": True}],
    )
    loaded = load_skills()
    assert loaded[0].status == "unavailable"
    assert "front matter" in (loaded[0].reason or "")


def test_load_skills_marks_missing_requirements_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    monkeypatch.delenv("DEMO_SKILL_MISSING_ENV", raising=False)
    _configure(
        tmp_path,
        monkeypatch,
        [
            {
                "name": "demo-skill",
                "path": str(skill_root),
                "enabled": True,
                "requirements": [
                    {"kind": "binary", "value": "surely-not-a-binary-xyz"},
                ],
            },
            {
                "name": "demo-skill-env",
                "path": str(skill_root),
                "enabled": True,
                "requirements": [
                    {"kind": "env", "value": "DEMO_SKILL_MISSING_ENV"},
                ],
            },
        ],
    )
    loaded = load_skills()
    assert [item.status for item in loaded] == ["unavailable", "unavailable"]
    assert "surely-not-a-binary-xyz" in (loaded[0].reason or "")
    assert "DEMO_SKILL_MISSING_ENV" in (loaded[1].reason or "")
    # Unmet requirements must tell the operator how to fix them because
    # npx/uvx never install system binaries or env variables on their own.
    assert "install it manually" in (loaded[0].reason or "")
    assert "export it" in (loaded[1].reason or "")


def test_load_skills_skips_disabled_entries(tmp_path, monkeypatch) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    _configure(
        tmp_path,
        monkeypatch,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": False}],
    )
    assert load_skills() == []
    assert render_external_skills_context() == ""


# ── Context injection ────────────────────────────────────────────────────────


def test_context_injects_available_and_hides_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    _configure(
        tmp_path,
        monkeypatch,
        [
            {"name": "demo-skill", "path": str(skill_root), "enabled": True},
            {
                "name": "ghost",
                "path": str(tmp_path / "missing"),
                "enabled": True,
            },
        ],
    )
    context = render_external_skills_context()
    assert "demo-skill" in context
    assert "run_skill_script" in context
    assert "ghost" not in context
    assert len(context) <= SKILL_CONTEXT_MAX_CHARS


def test_context_truncates_over_budget(tmp_path, monkeypatch) -> None:
    entries = []
    for index in range(40):
        skill_md = _SKILL_MD.replace(
            "Use when the user asks for a demo artifact.",
            f"Use when scenario {index}: " + ("blah " * 120),
        )
        root = _write_skill(tmp_path / f"demo-{index}", skill_md=skill_md)
        entries.append(
            {"name": f"demo-{index}", "path": str(root), "enabled": True},
        )
    _configure(tmp_path, monkeypatch, entries)
    context = render_external_skills_context()
    assert len(context) <= SKILL_CONTEXT_MAX_CHARS
    assert "demo-0" in context
    assert "demo-39" not in context


def test_creator_prompt_renders_with_and_without_skills(
    tmp_path,
    monkeypatch,
) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    _configure(
        tmp_path,
        monkeypatch,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    rendered = render_creator_system_prompt(project_id="project-x")
    assert "demo-skill" in rendered
    # A broken configuration must not break session prompt assembly.
    _configure(tmp_path, monkeypatch, [])
    rendered = render_creator_system_prompt(project_id="project-x")
    assert "demo-skill" not in rendered
    assert rendered.startswith("# 定位")


def test_tool_manifests_only_when_available(tmp_path, monkeypatch) -> None:
    skill_root = _write_skill(tmp_path / "demo")
    _configure(
        tmp_path,
        monkeypatch,
        [
            {"name": "demo-skill", "path": str(skill_root), "enabled": True},
            {
                "name": "ghost",
                "path": str(tmp_path / "missing"),
                "enabled": True,
            },
        ],
    )
    manifests = external_skill_tool_manifests(load_skills())
    names = [item["function"]["name"] for item in manifests]
    assert names == [
        "read_skill_file",
        "write_skill_file",
        "run_skill_script",
        "import_skill_artifacts",
    ]
    enum = manifests[0]["function"]["parameters"]["properties"]["skill"][
        "enum"
    ]
    assert enum == ["demo-skill"]
    # The runtime injects the authoritative Project identity, so the
    # model is never required to echo projectId back on skill tools.
    for item in manifests:
        assert "projectId" not in item["function"]["parameters"]["required"]
    assert external_skill_tool_manifests([]) == []


# ── Sandbox execution ────────────────────────────────────────────────────────


_PROJECT = "project-test-1"


def _demo_config(tmp_path, monkeypatch, **entry_extra) -> Path:
    skill_root = _write_skill(tmp_path / "demo")
    _configure(
        tmp_path,
        monkeypatch,
        [
            {
                "name": "demo-skill",
                "path": str(skill_root),
                "enabled": True,
                **entry_extra,
            },
        ],
    )
    return skill_root


def test_execute_skill_script_runs_in_sandbox_copy(
    tmp_path,
    monkeypatch,
) -> None:
    skill_root = _demo_config(tmp_path, monkeypatch)
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/echo.py",
            args=["a1"],
        ),
    )
    assert result["ok"] is True
    assert result["exitCode"] == 0
    assert "hello a1" in result["stdout"]
    workdir = Path(result["workdir"])
    assert workdir != skill_root
    assert workdir.name == "demo-skill"
    # The working copy is scoped per Project.
    assert workdir.parent.name == _PROJECT
    assert (workdir / "SKILL.md").is_file()


def test_sandbox_is_isolated_per_project(tmp_path, monkeypatch) -> None:
    _demo_config(tmp_path, monkeypatch)

    async def scenario():
        first = await execute_skill_script(
            project_id="project-a",
            skill_name="demo-skill",
            script="scripts/artifact.py",
        )
        second = await execute_skill_script(
            project_id="project-b",
            skill_name="demo-skill",
            script="scripts/echo.py",
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert Path(first["workdir"]) != Path(second["workdir"])
    # project-a produced an artifact; project-b's sandbox has none.
    assert (Path(first["workdir"]) / "dist" / "output.mp4").is_file()
    assert not (Path(second["workdir"]) / "dist").exists()
    with pytest.raises(SkillExecutionError):
        resolve_skill_artifact(
            project_id="project-b",
            skill_name="demo-skill",
            path="dist/output.mp4",
        )


def test_sandbox_copy_carries_upstream_attribution(
    tmp_path,
    monkeypatch,
) -> None:
    # Upstream layout: LICENSE/NOTICE live above the skill directory.
    upstream = tmp_path / "upstream"
    skill_root = _write_skill(upstream / "src" / "capabilities" / "demo")
    (upstream / "LICENSE").write_text("Apache License 2.0", encoding="utf-8")
    (upstream / "NOTICE").write_text("Qwen-MM-Plugins", encoding="utf-8")
    _configure(
        tmp_path,
        monkeypatch,
        [{"name": "demo-skill", "path": str(skill_root), "enabled": True}],
    )
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/echo.py",
        ),
    )
    workdir = Path(result["workdir"])
    assert (workdir / "UPSTREAM_LICENSE").read_text() == "Apache License 2.0"
    assert (workdir / "UPSTREAM_NOTICE").read_text() == "Qwen-MM-Plugins"
    provenance = (workdir / external_skills.PROVENANCE_FILENAME).read_text()
    assert "Apache-2.0" in provenance
    assert str(skill_root) in provenance


def test_execute_skill_script_env_allowlist(tmp_path, monkeypatch) -> None:
    _demo_config(tmp_path, monkeypatch, env=["DEMO_SKILL_TOKEN"])
    monkeypatch.setenv("DEMO_SKILL_TOKEN", "tok-123")
    monkeypatch.setenv("DEMO_SKILL_PRIVATE", "must-not-leak")
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/echo.py",
        ),
    )
    assert "SECRET=tok-123" in result["stdout"]
    assert "LEAK=<unset>" in result["stdout"]


def test_execute_skill_script_rejects_path_escape(
    tmp_path,
    monkeypatch,
) -> None:
    _demo_config(tmp_path, monkeypatch)
    for candidate in ("../outside.py", "/etc/passwd", "scripts/../../x.py"):
        with pytest.raises(SkillExecutionError):
            asyncio.run(
                execute_skill_script(
                    project_id=_PROJECT,
                    skill_name="demo-skill",
                    script=candidate,
                ),
            )
    with pytest.raises(SkillExecutionError, match="invalid project id"):
        asyncio.run(
            execute_skill_script(
                project_id="../escape",
                skill_name="demo-skill",
                script="scripts/echo.py",
            ),
        )


def test_execute_skill_script_timeout_kills_process(
    tmp_path,
    monkeypatch,
) -> None:
    _demo_config(tmp_path, monkeypatch)
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/sleepy.py",
            timeout_seconds=1,
        ),
    )
    assert result["ok"] is False
    assert result["timedOut"] is True
    assert "timed out" in result["error"]


def test_execute_skill_script_cancellation_reaps_child(
    tmp_path,
    monkeypatch,
) -> None:
    """A cancelled Agent run must not leave the skill process behind."""

    skill_root = _demo_config(tmp_path, monkeypatch)
    (skill_root / "scripts" / "pidfile.py").write_text(
        "import os, pathlib, time\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    async def scenario() -> int:
        task = asyncio.ensure_future(
            execute_skill_script(
                project_id=_PROJECT,
                skill_name="demo-skill",
                script="scripts/pidfile.py",
            ),
        )
        pid_file = (
            Path(os.environ["CREATOR_DATA_ROOT"])
            / "skills-runtime"
            / _PROJECT
            / "demo-skill"
            / "child.pid"
        )
        for _ in range(200):
            if pid_file.is_file() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        child_pid = int(pid_file.read_text().strip())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return child_pid

    child_pid = asyncio.run(scenario())
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"skill child {child_pid} survived cancellation")


def test_execute_skill_script_bounds_output_while_reading(
    tmp_path,
    monkeypatch,
) -> None:
    """Multi-megabyte output is drained without being kept in memory."""

    skill_root = _demo_config(tmp_path, monkeypatch)
    (skill_root / "scripts" / "flood.py").write_text(
        "import sys\n"
        "chunk = 'y' * 65536\n"
        "for _ in range(160):\n"  # ~10MB in total
        "    sys.stdout.write(chunk)\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/flood.py",
        ),
    )
    assert result["ok"] is True
    assert result["stdoutTruncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= SKILL_STREAM_TRUNCATE_BYTES


def test_execute_skill_script_truncates_output(tmp_path, monkeypatch) -> None:
    _demo_config(tmp_path, monkeypatch)
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/noisy.py",
        ),
    )
    assert result["stdoutTruncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= SKILL_STREAM_TRUNCATE_BYTES


def test_execute_skill_script_refuses_unavailable_skill(
    tmp_path,
    monkeypatch,
) -> None:
    _configure(
        tmp_path,
        monkeypatch,
        [
            {
                "name": "ghost",
                "path": str(tmp_path / "missing"),
                "enabled": True,
            },
        ],
    )
    with pytest.raises(SkillExecutionError, match="unavailable"):
        asyncio.run(
            execute_skill_script(
                project_id=_PROJECT,
                skill_name="ghost",
                script="scripts/x.py",
            ),
        )
    with pytest.raises(SkillExecutionError, match="not configured"):
        asyncio.run(
            execute_skill_script(
                project_id=_PROJECT,
                skill_name="nope",
                script="scripts/x.py",
            ),
        )


def test_skill_file_read_write_and_artifact_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    _demo_config(tmp_path, monkeypatch)
    read = read_skill_file(
        project_id=_PROJECT,
        skill_name="demo-skill",
        path="SKILL.md",
    )
    assert read["ok"] is True
    assert "Demo Skill" in read["content"]
    written = write_skill_file(
        project_id=_PROJECT,
        skill_name="demo-skill",
        path="notes/PLAN.md",
        content="# plan",
    )
    assert written["ok"] is True
    with pytest.raises(SkillExecutionError):
        write_skill_file(
            project_id=_PROJECT,
            skill_name="demo-skill",
            path="../escape.md",
            content="x",
        )
    result = asyncio.run(
        execute_skill_script(
            project_id=_PROJECT,
            skill_name="demo-skill",
            script="scripts/artifact.py",
        ),
    )
    assert "dist/output.mp4" in result["changedFiles"]
    artifact = resolve_skill_artifact(
        project_id=_PROJECT,
        skill_name="demo-skill",
        path="dist/output.mp4",
    )
    assert artifact.read_bytes() == b"fake-mp4-bytes"
    with pytest.raises(SkillExecutionError):
        resolve_skill_artifact(
            project_id=_PROJECT,
            skill_name="demo-skill",
            path="dist/nope.mp4",
        )
