# -*- coding: utf-8 -*-
"""Shared request fingerprint for interaction motion drafts.

The drafting pipeline stamps the fingerprint into ``motion.design_notes``;
the work graph compares it against the CURRENT question/options/edges so an
edited choice point goes back to READY instead of staying DONE forever.
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from services.project_files.models import (
    InteractionCreation,
    MotionGraphic,
    NarrativeEdge,
)

FINGERPRINT_MARKER = "input_fingerprint="


def interaction_request_fingerprint(
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
) -> str:
    parts = [
        creation.question,
        str(creation.countdown_seconds or ""),
        creation.default_edge_ref or "",
    ]
    for option in creation.options:
        edge = edges_by_id.get(option.edge_ref)
        parts.extend(
            [
                option.edge_ref,
                edge.label if edge is not None else "",
                edge.prompt if edge is not None else "",
                edge.target_timeline_id if edge is not None else "",
            ],
        )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def motion_matches_request(
    motion: MotionGraphic | None,
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
) -> bool:
    """True when the drafted motion still covers the current request.

    Hand-authored motions (no fingerprint marker) are never auto-invalidated.
    """

    if motion is None or not (motion.html or motion.html_file_id):
        return False
    notes = motion.design_notes or ""
    if FINGERPRINT_MARKER not in notes:
        return True
    expected = interaction_request_fingerprint(creation, edges_by_id)
    return f"{FINGERPRINT_MARKER}{expected}" in notes


__all__ = [
    "FINGERPRINT_MARKER",
    "interaction_request_fingerprint",
    "motion_matches_request",
]
