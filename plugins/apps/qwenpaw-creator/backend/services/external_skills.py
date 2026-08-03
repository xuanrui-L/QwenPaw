# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=too-many-return-statements
"""Manually configured external skills for the Creator main Agent.

A skill is a local directory (SKILL.md + scripts/assets) declared in
``<CREATOR_DATA_ROOT>/config/skills_config.json``. Loading is fully
isolated: any parse failure, missing path or unmet requirement marks the
skill ``unavailable`` with a readable reason and never raises, so session
establishment and the existing Creator pipeline are unaffected.

Skills are exposed skill-style, not agent-style: a bounded context block is
injected into the main Agent system prompt and scripts run through the
``run_skill_script`` sandbox tool (workspace copy under
``<CREATOR_DATA_ROOT>/skills-runtime/<name>/``). No subagent role is added.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from models.config import load_skills_config
from schemas.skills import SkillEntry, SkillRequirement, SkillRequirementKind
from services.observability import trace_event
from services.storage_root import require_creator_data_root
from utils.logger import setup_logger

logger = setup_logger("creator.external_skills")

SKILL_CONTEXT_MAX_CHARS = 8000
SKILL_STREAM_TRUNCATE_BYTES = 64 * 1024
SKILL_SCRIPT_DEFAULT_TIMEOUT_SECONDS = 600
SKILL_SCRIPT_MAX_TIMEOUT_SECONDS = 1800
SKILL_FILE_READ_MAX_BYTES = 256 * 1024
SKILL_FILE_WRITE_MAX_BYTES = 2 * 1024 * 1024
SKILL_NEW_FILES_LIMIT = 100
# Turn budget for main-Agent runs that actually invoked a skill tool; a
# skill pipeline (read docs, author artifacts, run gates, render, import)
# is many single-tool turns by design.
EXTERNAL_SKILL_MAX_MODEL_TURNS = 96

RUN_SKILL_SCRIPT_TOOL_NAME = "run_skill_script"
READ_SKILL_FILE_TOOL_NAME = "read_skill_file"
WRITE_SKILL_FILE_TOOL_NAME = "write_skill_file"
IMPORT_SKILL_ARTIFACTS_TOOL_NAME = "import_skill_artifacts"

EXTERNAL_SKILL_TOOL_NAMES = frozenset(
    {
        RUN_SKILL_SCRIPT_TOOL_NAME,
        READ_SKILL_FILE_TOOL_NAME,
        WRITE_SKILL_FILE_TOOL_NAME,
        IMPORT_SKILL_ARTIFACTS_TOOL_NAME,
    },
)

_SANDBOX_IGNORE_NAMES = {".git", "node_modules", "__pycache__"}
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NODE_VERSION = re.compile(r"v?(\d+)")

# Minimal base env for skill subprocesses; entry.env names are forwarded on
# top as an explicit allowlist (controlled parameter passing to an external
# child process, not in-process env injection).
_BASE_ENV_NAMES = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TERM",
    "USER",
    "SHELL",
)


class SkillExecutionError(RuntimeError):
    """A run_skill_script/read/write request the sandbox must refuse."""


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    entry: SkillEntry
    status: str  # "available" | "unavailable"
    reason: str | None
    skill_md: str
    root: Path

    @property
    def available(self) -> bool:
        return self.status == "available"


# ── SKILL.md parsing ─────────────────────────────────────────────────────────


def parse_skill_md(text: str) -> dict[str, Any]:
    """Extract front matter (name/description) and body from SKILL.md."""

    match = _FRONT_MATTER.match(text)
    if match is None:
        raise ValueError("SKILL.md front matter block (--- ... ---) not found")
    import yaml

    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise ValueError("SKILL.md front matter must be a YAML mapping")
    body = text[match.end() :].strip()
    return {
        "name": str(meta.get("name") or "").strip(),
        "description": str(meta.get("description") or "").strip(),
        "body": body,
    }


# ── Requirement probing ──────────────────────────────────────────────────────


def _probe_requirement(requirement: SkillRequirement) -> str | None:
    """Return a readable failure reason, or None when satisfied."""

    if requirement.kind is SkillRequirementKind.BINARY:
        if shutil.which(requirement.value) is None:
            return f"required binary not found on PATH: {requirement.value}"
        return None
    if requirement.kind is SkillRequirementKind.ENV:
        if not os.environ.get(requirement.value, "").strip():
            return f"required env variable is not set: {requirement.value}"
        return None
    if requirement.kind is SkillRequirementKind.NODE_MIN:
        node = shutil.which("node")
        if node is None:
            return "required binary not found on PATH: node"
        try:
            probe = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            match = _NODE_VERSION.match(probe.stdout.strip())
            major = int(match.group(1)) if match else -1
        except Exception as exc:
            return f"node --version probe failed: {type(exc).__name__}"
        minimum = int(str(requirement.value).lstrip("v").split(".", 1)[0])
        if major < minimum:
            return f"node major version {major} is below required {minimum}"
        return None
    return f"unknown requirement kind: {requirement.kind}"


def _load_one(entry: SkillEntry) -> LoadedSkill:
    root = Path(entry.path).expanduser()

    def unavailable(reason: str) -> LoadedSkill:
        return LoadedSkill(
            entry=entry,
            status="unavailable",
            reason=reason,
            skill_md="",
            root=root,
        )

    try:
        root = root.resolve(strict=False)
        if not root.is_dir():
            return unavailable(f"skill path is not a directory: {root}")
        skill_md_path = root / "SKILL.md"
        if not skill_md_path.is_file():
            return unavailable(f"SKILL.md not found under {root}")
        text = skill_md_path.read_text(encoding="utf-8")
        parse_skill_md(text)
        for requirement in entry.requirements:
            reason = _probe_requirement(requirement)
            if reason is not None:
                return unavailable(reason)
        return LoadedSkill(
            entry=entry,
            status="available",
            reason=None,
            skill_md=text,
            root=root,
        )
    except Exception as exc:  # Isolation: a bad skill never raises.
        return unavailable(f"skill load failed: {type(exc).__name__}: {exc}")


_LOAD_CACHE: tuple[float, tuple[str, ...], list[LoadedSkill]] | None = None
_LOAD_CACHE_TTL_SECONDS = 30.0


def load_skills() -> list[LoadedSkill]:
    """Load every enabled configured skill; never raises."""

    global _LOAD_CACHE
    try:
        entries = [item for item in load_skills_config() if item.enabled]
        signature = tuple(entry.model_dump_json() for entry in entries)
        now = time.monotonic()
        if (
            _LOAD_CACHE is not None
            and _LOAD_CACHE[1] == signature
            and now - _LOAD_CACHE[0] < _LOAD_CACHE_TTL_SECONDS
        ):
            return list(_LOAD_CACHE[2])
        loaded = [_load_one(entry) for entry in entries]
        for skill in loaded:
            if not skill.available:
                logger.warning(
                    "external skill unavailable: name=%s reason=%s",
                    skill.entry.name,
                    skill.reason,
                )
        _LOAD_CACHE = (now, signature, loaded)
        return list(loaded)
    except Exception:
        logger.exception("external skill loading failed; continuing empty")
        return []


def _clear_load_cache() -> None:
    global _LOAD_CACHE
    _LOAD_CACHE = None


# ── System prompt context ────────────────────────────────────────────────────


def _skill_context_block(skill: LoadedSkill) -> str:
    parsed = parse_skill_md(skill.skill_md)
    description = (
        skill.entry.description or parsed["description"] or "（无描述）"
    ).strip()
    return (
        f"### 外置 Skill: {skill.entry.name}\n"
        f"- 触发时机: {description}\n"
        f"- 调用方式: 先用 `{READ_SKILL_FILE_TOOL_NAME}` 读取该 skill 的 "
        "SKILL.md 全文并严格按其流程执行；需要向沙箱写入你撰写的文件时用 "
        f"`{WRITE_SKILL_FILE_TOOL_NAME}`；执行 skill 内脚本用 "
        f"`{RUN_SKILL_SCRIPT_TOOL_NAME}`（script 为 skill 根目录内相对路径）；"
        f"产物媒体文件用 `{IMPORT_SKILL_ARTIFACTS_TOOL_NAME}` 导入 Project 资产。"
    )


_CONTEXT_HEADER = (
    "# 外置 Skill\n"
    "以下手动配置的外置 skill 已可用。当用户请求命中某个 skill 的触发时机时，"
    "优先使用该 skill 完成任务：所有 skill 文件读写与脚本执行都发生在其独立"
    "沙箱工作副本内，与 Project 工作区互不影响；最终产物必须用 "
    f"`{IMPORT_SKILL_ARTIFACTS_TOOL_NAME}` 导入后才能在 Project 中引用。"
)


def render_external_skills_context(
    skills: list[LoadedSkill] | None = None,
) -> str:
    """Build the bounded ``external_skills`` placeholder value.

    Available skills are appended in configuration order under a total
    budget of ``SKILL_CONTEXT_MAX_CHARS``; the block that overflows is
    truncated and later blocks are dropped, with a trace warning. No
    available skill renders an empty string (placeholder-compatible).
    """

    try:
        if skills is None:
            skills = load_skills()
        available = [skill for skill in skills if skill.available]
        if not available:
            return ""
        parts = [_CONTEXT_HEADER]
        used = len(_CONTEXT_HEADER)
        truncated = False
        for skill in available:
            block = "\n\n" + _skill_context_block(skill)
            if used + len(block) > SKILL_CONTEXT_MAX_CHARS:
                remaining = SKILL_CONTEXT_MAX_CHARS - used
                if remaining > 0:
                    parts.append(block[:remaining])
                truncated = True
                break
            parts.append(block)
            used += len(block)
        if truncated:
            trace_event(
                "creator.external_skills.context_truncated",
                component="creator.external_skills",
                status="warning",
                attributes={
                    "maxChars": SKILL_CONTEXT_MAX_CHARS,
                    "availableSkills": [
                        skill.entry.name for skill in available
                    ],
                },
            )
            logger.warning(
                "external skills context truncated at %s chars",
                SKILL_CONTEXT_MAX_CHARS,
            )
        return "".join(parts)
    except Exception:
        logger.exception(
            "external skills context build failed; injecting empty",
        )
        return ""


# ── Sandbox workspace ────────────────────────────────────────────────────────


def find_skill(name: str) -> LoadedSkill:
    for skill in load_skills():
        if skill.entry.name == name:
            return skill
    raise SkillExecutionError(f"skill is not configured or disabled: {name}")


def _require_available(name: str) -> LoadedSkill:
    skill = find_skill(name)
    if not skill.available:
        raise SkillExecutionError(
            f"skill {name} is unavailable: {skill.reason}",
        )
    return skill


def skill_runtime_root(skill: LoadedSkill, *, create: bool = True) -> Path:
    """Working copy under the Creator data root; seeded on first use."""

    runtime_root = (
        require_creator_data_root() / "skills-runtime" / skill.entry.name
    )
    if create and not runtime_root.exists():
        shutil.copytree(
            skill.root,
            runtime_root,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_SANDBOX_IGNORE_NAMES),
        )
    return runtime_root


def _resolve_inside(base: Path, relative: str, *, label: str) -> Path:
    candidate = (relative or "").strip()
    if not candidate:
        raise SkillExecutionError(f"{label} is required")
    raw = Path(candidate)
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise SkillExecutionError(
            f"{label} must be a relative path inside the skill root: {candidate}",
        )
    resolved = (base / raw).resolve(strict=False)
    try:
        resolved.relative_to(base.resolve(strict=False))
    except ValueError:
        raise SkillExecutionError(
            f"{label} escapes the skill sandbox: {candidate}",
        ) from None
    return resolved


def _subprocess_env(skill: LoadedSkill) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in _BASE_ENV_NAMES
        if os.environ.get(name)
    }
    for name in skill.entry.env:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _script_command(script_path: Path, args: list[str]) -> list[str]:
    if os.access(script_path, os.X_OK) and script_path.suffix not in {
        ".py",
        ".js",
        ".mjs",
        ".sh",
    }:
        return [str(script_path), *args]
    interpreter = {
        ".py": "python3",
        ".sh": "bash",
        ".js": "node",
        ".mjs": "node",
    }.get(script_path.suffix)
    if interpreter is None:
        if os.access(script_path, os.X_OK):
            return [str(script_path), *args]
        raise SkillExecutionError(
            f"cannot execute script without interpreter mapping: {script_path.name}",
        )
    return [interpreter, str(script_path), *args]


def _truncate_stream(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > SKILL_STREAM_TRUNCATE_BYTES
    view = data[:SKILL_STREAM_TRUNCATE_BYTES]
    return view.decode("utf-8", errors="replace"), truncated


def _snapshot_files(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if any(part in _SANDBOX_IGNORE_NAMES for part in path.parts):
            continue
        try:
            if path.is_file():
                stat = path.stat()
                snapshot[str(path.relative_to(root))] = (
                    stat.st_mtime_ns,
                    stat.st_size,
                )
        except OSError:
            continue
    return snapshot


async def execute_skill_script(
    *,
    skill_name: str,
    script: str,
    args: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one skill script inside its sandbox working copy."""

    skill = _require_available(skill_name)
    runtime_root = await asyncio.to_thread(skill_runtime_root, skill)
    script_path = _resolve_inside(runtime_root, script, label="script")
    if not script_path.is_file():
        raise SkillExecutionError(
            f"script not found in skill sandbox: {script}",
        )
    safe_args = [str(item) for item in (args or [])]
    timeout = (
        SKILL_SCRIPT_DEFAULT_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(
            1,
            min(int(timeout_seconds), SKILL_SCRIPT_MAX_TIMEOUT_SECONDS),
        )
    )
    command = _script_command(script_path, safe_args)
    before = await asyncio.to_thread(_snapshot_files, runtime_root)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(runtime_root),
        env=_subprocess_env(skill),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError):
        timed_out = True
        try:
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        stdout_data, stderr_data = await process.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text, stdout_truncated = _truncate_stream(stdout_data or b"")
    stderr_text, stderr_truncated = _truncate_stream(stderr_data or b"")
    after = await asyncio.to_thread(_snapshot_files, runtime_root)
    new_files = sorted(
        path for path, meta in after.items() if before.get(path) != meta
    )
    exit_code = process.returncode
    result: dict[str, Any] = {
        "ok": bool(not timed_out and exit_code == 0),
        "skill": skill.entry.name,
        "script": script,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "timeoutSeconds": timeout,
        "durationMs": duration_ms,
        "stdout": stdout_text,
        "stdoutTruncated": stdout_truncated,
        "stderr": stderr_text,
        "stderrTruncated": stderr_truncated,
        "workdir": str(runtime_root),
        "changedFiles": new_files[:SKILL_NEW_FILES_LIMIT],
        "changedFilesTruncated": len(new_files) > SKILL_NEW_FILES_LIMIT,
    }
    if timed_out:
        result["error"] = f"script timed out after {timeout}s and was killed"
    return result


def read_skill_file(*, skill_name: str, path: str) -> dict[str, Any]:
    """Read one UTF-8 text file from the sandbox (or pristine skill root)."""

    skill = _require_available(skill_name)
    runtime_root = skill_runtime_root(skill, create=False)
    base = runtime_root if runtime_root.exists() else skill.root
    target = _resolve_inside(base, path, label="path")
    if not target.is_file():
        raise SkillExecutionError(f"file not found in skill: {path}")
    data = target.read_bytes()
    truncated = len(data) > SKILL_FILE_READ_MAX_BYTES
    text = data[:SKILL_FILE_READ_MAX_BYTES].decode("utf-8", errors="replace")
    return {
        "ok": True,
        "skill": skill.entry.name,
        "path": path,
        "content": text,
        "truncated": truncated,
        "sizeBytes": len(data),
    }


def write_skill_file(
    *,
    skill_name: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Write one UTF-8 text file into the sandbox working copy."""

    skill = _require_available(skill_name)
    runtime_root = skill_runtime_root(skill)
    target = _resolve_inside(runtime_root, path, label="path")
    payload = content.encode("utf-8")
    if len(payload) > SKILL_FILE_WRITE_MAX_BYTES:
        raise SkillExecutionError(
            f"file content exceeds {SKILL_FILE_WRITE_MAX_BYTES} bytes",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {
        "ok": True,
        "skill": skill.entry.name,
        "path": path,
        "sizeBytes": len(payload),
    }


def resolve_skill_artifact(*, skill_name: str, path: str) -> Path:
    """Resolve one sandbox-relative artifact path for asset import."""

    skill = _require_available(skill_name)
    runtime_root = skill_runtime_root(skill, create=False)
    if not runtime_root.exists():
        raise SkillExecutionError(
            f"skill sandbox has no working copy yet: {skill_name}",
        )
    target = _resolve_inside(runtime_root, path, label="path")
    if not target.is_file():
        raise SkillExecutionError(
            f"artifact not found in skill sandbox: {path}",
        )
    return target


# ── Main-Agent tool manifests ────────────────────────────────────────────────


def _flat_schema(
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "projectId": {"type": "string", "minLength": 1},
            **properties,
        },
        "required": ["projectId", *required],
        "additionalProperties": False,
    }


def external_skill_tool_manifests(
    skills: list[LoadedSkill],
) -> list[dict[str, Any]]:
    """Tool manifests for the main Agent when any skill is available."""

    names = sorted(skill.entry.name for skill in skills if skill.available)
    if not names:
        return []
    skill_property = {
        "type": "string",
        "enum": names,
        "description": "已配置且可用的外置 skill 名称。",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": READ_SKILL_FILE_TOOL_NAME,
                "description": (
                    "读取外置 skill 沙箱内的 UTF-8 文本文件（SKILL.md、"
                    "references、生成的中间文件等）。只读，不修改任何内容。"
                ),
                "parameters": _flat_schema(
                    {
                        "skill": skill_property,
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "skill 根目录内的相对路径。",
                        },
                    },
                    ["skill", "path"],
                ),
            },
        },
        {
            "type": "function",
            "function": {
                "name": WRITE_SKILL_FILE_TOOL_NAME,
                "description": (
                    "把你撰写的文本内容写入外置 skill 的沙箱工作副本"
                    "（例如 SKILL.md 流程要求你产出的 markdown/HTML/JSON 文件）。"
                    "只能写沙箱内相对路径，不影响 Project 工作区。"
                ),
                "parameters": _flat_schema(
                    {
                        "skill": skill_property,
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "skill 根目录内的相对路径。",
                        },
                        "content": {"type": "string"},
                    },
                    ["skill", "path", "content"],
                ),
            },
        },
        {
            "type": "function",
            "function": {
                "name": RUN_SKILL_SCRIPT_TOOL_NAME,
                "description": (
                    "在外置 skill 的沙箱工作副本内执行 skill 自带脚本。"
                    "script 必须是 skill 根目录内相对路径；子进程使用最小"
                    "基础环境加 skill 声明的 env 白名单；stdout/stderr 各"
                    "截断 64KB 返回。执行需要用户授权。"
                ),
                "parameters": _flat_schema(
                    {
                        "skill": skill_property,
                        "script": {
                            "type": "string",
                            "minLength": 1,
                            "description": "skill 根目录内的脚本相对路径。",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                        },
                        "timeoutSeconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": SKILL_SCRIPT_MAX_TIMEOUT_SECONDS,
                        },
                    },
                    ["skill", "script"],
                ),
            },
        },
        {
            "type": "function",
            "function": {
                "name": IMPORT_SKILL_ARTIFACTS_TOOL_NAME,
                "description": (
                    "把外置 skill 沙箱内已生成的媒体产物（视频/音频/图片）"
                    "经现有资产导入通路写入 Project 资产库，返回可引用的 "
                    "workspace refs。"
                ),
                "parameters": _flat_schema(
                    {
                        "skill": skill_property,
                        "paths": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 16,
                            "uniqueItems": True,
                            "description": "沙箱内产物文件的相对路径列表。",
                        },
                    },
                    ["skill", "paths"],
                ),
            },
        },
    ]


__all__ = [
    "EXTERNAL_SKILL_MAX_MODEL_TURNS",
    "EXTERNAL_SKILL_TOOL_NAMES",
    "IMPORT_SKILL_ARTIFACTS_TOOL_NAME",
    "LoadedSkill",
    "READ_SKILL_FILE_TOOL_NAME",
    "RUN_SKILL_SCRIPT_TOOL_NAME",
    "SKILL_CONTEXT_MAX_CHARS",
    "SKILL_SCRIPT_MAX_TIMEOUT_SECONDS",
    "SKILL_STREAM_TRUNCATE_BYTES",
    "SkillExecutionError",
    "WRITE_SKILL_FILE_TOOL_NAME",
    "execute_skill_script",
    "external_skill_tool_manifests",
    "find_skill",
    "load_skills",
    "parse_skill_md",
    "read_skill_file",
    "render_external_skills_context",
    "resolve_skill_artifact",
    "skill_runtime_root",
    "write_skill_file",
]
