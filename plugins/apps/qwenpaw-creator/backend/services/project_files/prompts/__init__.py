# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Hash-verified Project Workspace prompt templates."""

from __future__ import annotations

import hashlib
from pathlib import Path


_PROMPT_PATH = Path(__file__).resolve().parent / "workspace_schema.system.txt"
_PROMPT_SHA256 = (
    "4bef3ad121a399506abffda4181e7ddb910ab2e34dba20b3a3ed03979fa3f385"
)
_SCHEMA_PLACEHOLDER = "{{project_json_schema}}"


def render_workspace_schema_prompt(*, project_json_schema: str) -> str:
    data = _PROMPT_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != _PROMPT_SHA256:
        raise RuntimeError("Prompt hash mismatch: workspace_schema.system")
    text = data.decode("utf-8").strip()
    if text.count(_SCHEMA_PLACEHOLDER) != 1:
        raise RuntimeError(
            "Prompt placeholder mismatch: workspace_schema.system requires project_json_schema",
        )
    return text.replace(_SCHEMA_PLACEHOLDER, project_json_schema)


__all__ = ["render_workspace_schema_prompt"]
