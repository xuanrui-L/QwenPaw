# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
# Vendored from Qwen-MM-Plugins (https://github.com/QwenLM/Qwen-MM-Plugins),
# commit 077aea63d9e7ad50d91bab6c8dff12183a24d48b, licensed under the
# Apache License, Version 2.0. See backend/vendor/NOTICE.md.
#
# Modifications for QwenPaw Creator:
# - Extracted only the resolution-budget math (`budget_to_pixels`,
#   `smart_resize`) from ``src/shared/image.py``; the two function bodies are
#   kept verbatim.
# - Inlined the budget constants (``TOKEN_SIZE``, ``DEFAULT_BUDGET``,
#   ``IMAGE_BUDGET_TOKENS``, ``VIDEO_BUDGET_TOKENS`` and the derived
#   ``*_MIN_PIXELS``) verbatim from ``src/shared/env.py`` so the module has no
#   runtime dependency on the upstream package.
"""Resolution-budget math shared with the upstream VLM tooling.

Maps a resolution preset to a pixel budget and resizes dimensions into that
budget, snapped to the visual-token patch grid expected by Qwen VLMs.
"""

from __future__ import annotations

import logging
import math

# Spatial patch size for one image token, just used to compute budgets.
TOKEN_SIZE = 32
# Default budget for image/video processing.
DEFAULT_BUDGET = "normal"

# Per-preset resolution budgets (visual-token counts) → pixels via budget_to_pixels(budget, MAP).
IMAGE_BUDGET_TOKENS = {"small": 256, "normal": 1024, "large": 2048}
IMAGE_MIN_PIXELS = min(IMAGE_BUDGET_TOKENS.values()) * TOKEN_SIZE * TOKEN_SIZE

VIDEO_BUDGET_TOKENS = {"small": 80, "normal": 256, "large": 1024}
VIDEO_MIN_PIXELS = min(VIDEO_BUDGET_TOKENS.values()) * TOKEN_SIZE * TOKEN_SIZE


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


__all__ = [
    "TOKEN_SIZE",
    "DEFAULT_BUDGET",
    "IMAGE_BUDGET_TOKENS",
    "IMAGE_MIN_PIXELS",
    "VIDEO_BUDGET_TOKENS",
    "VIDEO_MIN_PIXELS",
    "budget_to_pixels",
    "smart_resize",
]
