# -*- coding: utf-8 -*-
"""Shared request fingerprint for interaction motion drafts.

The drafting pipeline stamps the fingerprint into ``motion.design_notes``;
the work graph compares it against the CURRENT question/options/edges so an
edited choice point goes back to READY instead of staying DONE forever.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

from services.project_files.models import (
    InteractionCreation,
    MotionGraphic,
    NarrativeEdge,
    Project,
)

FINGERPRINT_MARKER = "input_fingerprint="


def interaction_request_fingerprint(
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
    project: Project | None = None,
) -> str:
    parts = [
        creation.question,
        creation.design_prompt,
        creation.base_frame_ref or "",
        str(creation.countdown_seconds or ""),
        creation.default_edge_ref or "",
    ]
    for option in creation.options:
        if option.design_prompt:
            parts.append("option_design:" + option.design_prompt)
        edge = edges_by_id.get(option.edge_ref)
        parts.extend(
            [
                option.edge_ref,
                json.dumps(
                    (
                        option.hotspot.model_dump(mode="json")
                        if option.hotspot
                        else None
                    ),
                    sort_keys=True,
                ),
                edge.label if edge is not None else "",
                edge.prompt if edge is not None else "",
                edge.target_timeline_id if edge is not None else "",
            ],
        )
    if project is not None:
        parts.extend(
            [
                project.name,
                project.description,
                project.settings.aspect_ratio,
                json.dumps(project.visual.style, sort_keys=True),
                str(project.visual.visual_bible),
                project.interactive_presentation.design_prompt,
            ],
        )
        parts.extend(
            project.timelines.items[e.target_timeline_id].title
            for o in creation.options
            if (e := edges_by_id.get(o.edge_ref)) is not None
        )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def motion_matches_request(
    motion: MotionGraphic | None,
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
    project: Project | None = None,
) -> bool:
    """True when the drafted motion still covers the current request.

    Hand-authored motions (no fingerprint marker) are never auto-invalidated.
    """

    if motion is None or not (motion.html or motion.html_file_id):
        return False
    notes = motion.design_notes or ""
    if FINGERPRINT_MARKER not in notes:
        return True
    expected = interaction_request_fingerprint(creation, edges_by_id, project)
    return f"{FINGERPRINT_MARKER}{expected}" in notes


__all__ = [
    "FINGERPRINT_MARKER",
    "interaction_request_fingerprint",
    "motion_matches_request",
]
