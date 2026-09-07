# -*- coding: utf-8 -*-
"""
Narrow classification of system history records, never creative decisions.
"""

from __future__ import annotations

from typing import Any, Mapping

from .prompt_sync import is_prompt_sync_pointer


def is_version_bookkeeping(change: Any) -> bool:
    """Recognize automatic timeline copies and history-only ordering changes.

    Artifact publication/selection and live timeline order remain reviewable.
    A snapshot-looking path alone cannot hide edits or deletion of content.
    """
    pointer = change.json_pointer or ""
    before, after = change.before, change.after
    if pointer == "/timelines/order":
        if not all(
            isinstance(value, list)
            and all(isinstance(item, str) for item in value)
            for value in (before, after)
        ):
            return False
        return before != after and [
            item for item in before if not item.startswith("snapshot:")
        ] == [item for item in after if not item.startswith("snapshot:")]
    tokens = pointer.split("/")
    if len(tokens) != 4 or tokens[1:3] != ["timelines", "items"]:
        return False
    timeline_id = tokens[3].replace("~1", "/").replace("~0", "~")
    return bool(
        timeline_id.startswith("snapshot:")
        and change.kind == "create"
        and before is None
        and isinstance(after, Mapping)
        and after.get("timeline_id") == timeline_id
        and after.get("description") == "自动快照：修改前的时间轴副本",
    )


def is_human_review_change(change: Any) -> bool:
    return not (
        is_prompt_sync_pointer(change.json_pointer or "")
        or is_version_bookkeeping(change)
    )
