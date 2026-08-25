# -*- coding: utf-8 -*-
"""Shared deterministic normalization for authored prompt text."""

from __future__ import annotations

import re


def dialogue_match_key(text: str) -> str:
    """Normalize harmless whitespace differences before dialogue matching."""

    return "".join(text.split())


# Speaker prefixes ("老板娘：…" / "Regular: …") and stage directions
# ("（回头）") belong to the shot plan, not to the spoken line itself. Prompt
# prose may wrap the same spoken words in a sentence, so the contract compares
# only the dialogue that must reach the provider verbatim.
_DIALOGUE_SPEAKER_PREFIX = re.compile(r"^[^：:]{1,20}[：:]\s*")
_DIALOGUE_STAGE_DIRECTION = re.compile(r"[（(][^）)]*[）)]")


def dialogue_spoken_lines(dialogue: str) -> tuple[str, ...]:
    """Return the spoken sentences of a shot dialogue field, one per line."""

    lines: list[str] = []
    for raw in dialogue.splitlines():
        line = _DIALOGUE_SPEAKER_PREFIX.sub("", raw.strip())
        line = _DIALOGUE_STAGE_DIRECTION.sub("", line).strip()
        if line:
            lines.append(line)
    return tuple(lines)
