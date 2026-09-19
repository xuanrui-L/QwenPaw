# -*- coding: utf-8 -*-
"""User-owned script/media boundary, independent of automation settings."""

from domain.errors import ValidationError
from .prompt_sync import digest


SCRIPT_ONLY_MESSAGE = "请先确认当前剧本并允许生成图片／视频；当前项目仅进行剧本创作。"
PRODUCTION_STAGE_POINTER = "/settings/production_stage"
SCRIPT_APPROVAL_POINTER = "/settings/script_approval_fingerprint"


def script_inputs(document):
    """Exclude generated media, prompts and provenance from approval."""
    if hasattr(document, "model_dump"):
        document = document.model_dump(mode="json")
    timelines = document.get("timelines", {})
    assets = document.get("assets", {})
    scripts = {}
    for slot in assets.get("artifact_slots_by_id", {}).values():
        if slot.get("kind") == "timeline_script":
            scripts[slot.get("owner_ref")] = slot.get("selected_version_id")
    return [
        {
            "id": timeline_id,
            "title": timeline.get("title"),
            "synopsis": timeline.get("synopsis"),
            "description": timeline.get("description"),
            "script": scripts.get(f"timeline:{timeline_id}"),
            "elements": {
                element_id: {
                    key: element.get("creation", {}).get(key)
                    for key in ("narrative", "intent", "continuity")
                }
                for element_id, element in timeline.get(
                    "elements_by_id",
                    {},
                ).items()
                if not element_id.startswith("snapshot:")
            },
        }
        for timeline_id in timelines.get("order", [])
        if not timeline_id.startswith("snapshot:")
        and (timeline := timelines.get("items", {}).get(timeline_id))
    ]


def script_fingerprint(document):
    return digest(script_inputs(document))


def derive_production_stage(before, after):
    """Derive approval under the commit lock from current script content."""
    old = before.get("settings", {})
    settings = after["settings"]
    stage = settings.get("production_stage", "media")
    old_stage = old.get("production_stage", "media")
    approved = old.get("script_approval_fingerprint")
    if stage != old_stage:
        if stage == "media":
            rows = script_inputs(after)
            if not any(
                row["script"]
                or str(row["description"] or "").strip()
                or any(
                    str(
                        values.get("narrative") or values.get("intent") or "",
                    ).strip()
                    for values in row["elements"].values()
                )
                for row in rows
            ):
                raise ValidationError("请先发布剧本内容，再确认生成图片／视频。")
            approved = script_fingerprint(after)
        else:
            approved = None
    elif approved and approved != script_fingerprint(after):
        settings["production_stage"] = "script"
        approved = None
    settings["script_approval_fingerprint"] = approved


def assert_media_production_allowed(project):
    if project.settings.production_stage == "script":
        raise ValidationError(SCRIPT_ONLY_MESSAGE)
