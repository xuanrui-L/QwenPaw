# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
# Vendored from Qwen-MM-Plugins (Apache-2.0), release commit 077aea6.
# Upstream path: src/shared/image.py (budget_to_pixels, smart_resize)
#   with resolution constants inlined from src/shared/env.py.
# Modified for QwenPaw Creator. See backend/vendor/NOTICE.md.
"""Resolution-budget math shared by document reading and render review.

This module is the canonical vendored copy shared across Creator worktrees;
keep its content byte-identical when re-vendoring on another branch.
"""

from __future__ import annotations

import logging
import math

# Patch-grid unit of Qwen-VL style models (pixels per token side).
TOKEN_SIZE = 32
DEFAULT_BUDGET = "normal"
IMAGE_BUDGET_TOKENS = {"small": 256, "normal": 1024, "large": 2048}
IMAGE_MIN_PIXELS = min(IMAGE_BUDGET_TOKENS.values()) * TOKEN_SIZE * TOKEN_SIZE


def budget_to_pixels(budget: str, tokens_map: dict[str, int]) -> int:
    """Map a resolution preset ('small'/'normal'/'large') to a pixel budget via its token count."""
    tokens = tokens_map.get(budget, tokens_map[DEFAULT_BUDGET])
    return tokens * TOKEN_SIZE * TOKEN_SIZE


def smart_resize(
    height: int,
    width: int,
    min_pixels: int,
    max_pixels: int,
    factor: int = TOKEN_SIZE,
) -> tuple[int, int]:
    """Resize (h, w) into [min_pixels, max_pixels], snapped to a multiple of `factor` (the patch grid)."""
    if min_pixels > max_pixels:
        logging.warning(
            "min_pixels (%d) > max_pixels (%d), clamping max_pixels to min_pixels",
            min_pixels,
            max_pixels,
        )
        max_pixels = min_pixels

    if height * width < min_pixels:
        scale = math.sqrt(min_pixels / (height * width))
        height = int(height * scale)
        width = int(width * scale)

    if height * width > max_pixels:
        scale = math.sqrt(max_pixels / (height * width))
        height = int(height * scale)
        width = int(width * scale)

    height = max(factor, round(height / factor) * factor)
    width = max(factor, round(width / factor) * factor)
    return height, width
