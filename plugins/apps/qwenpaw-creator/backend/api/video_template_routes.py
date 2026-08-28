# -*- coding: utf-8 -*-
"""Built-in video template listing endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from services.media_files.video_templates import list_video_templates

router = APIRouter(prefix="/video-templates", tags=["video-templates"])


@router.get("")
async def list_templates() -> dict[str, Any]:
    templates = list_video_templates()
    return {
        "items": [
            {
                "templateId": t.template_id,
                "name": t.name,
                "description": t.description,
                "contentType": t.content_type,
                "scenario": t.scenario,
                "colorGrade": t.color_grade,
                "defaultTransitionKind": t.default_transition_kind,
                "previewDescription": t.preview_description,
                "iconEmoji": t.icon_emoji,
                "captionBlueprints": list(t.caption_blueprint_order),
                "energy": t.energy,
                "density": t.density,
                "decoration": t.decoration,
            }
            for t in templates
        ],
    }
