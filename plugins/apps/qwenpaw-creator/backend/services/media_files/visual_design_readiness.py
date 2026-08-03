# -*- coding: utf-8 -*-
"""Deterministic visual-design admission before storyboard generation."""

from __future__ import annotations

from dataclasses import dataclass

from domain.errors import ValidationError
from services.project_files.models import Project, R2VCreation, VisualEntity


@dataclass(frozen=True, slots=True)
class VisualDesignReadinessIssue:
    """One actionable reason why storyboarding cannot start yet."""

    code: str
    entity_id: str
    variant_id: str | None = None
    element_id: str | None = None

    def message(self) -> str:
        if self.code == "MISSING_REQUIRED_VARIANT":
            return f"{self.entity_id} 缺少必需 Variant {self.variant_id}"
        if self.code == "MISSING_VARIANT_BINDING":
            return f"{self.element_id} 未绑定 {self.entity_id} 的具体 Variant"
        if self.variant_id is not None:
            return f"{self.entity_id}/{self.variant_id} 尚无使用中视觉产物"
        return f"{self.entity_id} 尚无使用中视觉产物"


def _referenced_entities(
    project: Project,
) -> dict[str, list[tuple[str, R2VCreation]]]:
    references: dict[str, list[tuple[str, R2VCreation]]] = {}
    for timeline_id in project.timelines.order:
        timeline = project.timelines.items[timeline_id]
        for element_id, element in timeline.elements_by_id.items():
            if not element.enabled or not isinstance(
                element.creation,
                R2VCreation,
            ):
                continue
            entity_ids = dict.fromkeys(
                [
                    *element.creation.character_refs,
                    *(
                        [element.creation.scene_ref]
                        if element.creation.scene_ref is not None
                        else []
                    ),
                    *element.creation.prop_refs,
                ],
            )
            for entity_id in entity_ids:
                references.setdefault(entity_id, []).append(
                    (element_id, element.creation),
                )
    return references


def _entity_readiness_issues(
    entity: VisualEntity,
    element_references: list[tuple[str, R2VCreation]],
) -> list[VisualDesignReadinessIssue]:
    entity_id = entity.entity_id
    issues: list[VisualDesignReadinessIssue] = []
    defined = set(entity.variants.order)
    for variant_id in entity.required_variant_ids:
        if variant_id not in defined:
            issues.append(
                VisualDesignReadinessIssue(
                    code="MISSING_REQUIRED_VARIANT",
                    entity_id=entity_id,
                    variant_id=variant_id,
                ),
            )
            continue
        variant = entity.variants.items[variant_id]
        if variant.selected_artifact_version_id is None:
            issues.append(
                VisualDesignReadinessIssue(
                    code="MISSING_SELECTED_ARTIFACT",
                    entity_id=entity_id,
                    variant_id=variant_id,
                ),
            )

    if not entity.required_variant_ids:
        if entity.selected_artifact_version_id is None:
            issues.append(
                VisualDesignReadinessIssue(
                    code="MISSING_SELECTED_ARTIFACT",
                    entity_id=entity_id,
                ),
            )
        return issues

    if len(entity.required_variant_ids) <= 1:
        return issues
    for element_id, creation in element_references:
        if not creation.visual_variant_refs.get(entity_id):
            issues.append(
                VisualDesignReadinessIssue(
                    code="MISSING_VARIANT_BINDING",
                    entity_id=entity_id,
                    element_id=element_id,
                ),
            )
    return issues


def visual_design_readiness_issues(
    project: Project,
) -> tuple[VisualDesignReadinessIssue, ...]:
    """Return project-wide visual gaps relevant to enabled R2V Elements.

    The gate is intentionally project-wide: once storyboard production starts,
    every visual entity referenced by the enabled plan must have its declared
    Variant set materialized and selected. This keeps later Elements from
    discovering missing character states after storyboard spend has begun.
    """

    references = _referenced_entities(project)
    issues: list[VisualDesignReadinessIssue] = []
    for entity_id in project.visual.entities.order:
        element_references = references.get(entity_id)
        if not element_references:
            continue
        entity = project.visual.entities.items[entity_id]
        issues.extend(_entity_readiness_issues(entity, element_references))

    return tuple(issues)


def assert_visual_design_ready_for_storyboards(project: Project) -> None:
    """Block storyboard spend until the structured visual plan is complete."""

    issues = visual_design_readiness_issues(project)
    if not issues:
        return
    details = "；".join(issue.message() for issue in issues[:12])
    remaining = len(issues) - 12
    if remaining > 0:
        details += f"；另有 {remaining} 项"
    raise ValidationError(f"视觉设定尚未完成，分镜图未开始：{details}")


__all__ = [
    "VisualDesignReadinessIssue",
    "assert_visual_design_ready_for_storyboards",
    "visual_design_readiness_issues",
]
