# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Media reviews use the actual owning unit, not old authoring rows."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.project_files.models import (
    ElementLocation,
    Project,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
)
from services.run_review import media_review
from services.run_review.faithfulness import build_faithfulness_elements

pytestmark = pytest.mark.unit


def test_review_context_uses_exact_unit_narrative_and_span():
    project = Project.new(project_id="review-context", name="Review")
    for element_id, text in (("e1", "纸船先旋转，再停靠岸边。"), ("e10", "不属于本次的内容")):
        project.timelines.items["timeline:main"].elements_by_id[
            element_id
        ] = TimelineElement(
            element_id=element_id,
            label=element_id,
            location=ElementLocation(),
            span=TimelineSpan(start_tick=0, duration_tick=6000),
            creation=R2VCreation(narrative=text, video_prompt=text),
        )
    services = SimpleNamespace(
        projects=SimpleNamespace(
            read=lambda _: SimpleNamespace(project=project),
        ),
    )
    context = media_review._derive_plan_context(
        services,
        project.project_id,
        {"targetRef": "element:e10"},
    )
    assert context["narrative"] == "不属于本次的内容"
    assert context["expected_duration_seconds"] == 6
    assert "planned_shots" not in context


def test_retired_shots_cannot_become_faithfulness_expectations():
    assert not build_faithfulness_elements(
        {"planned_shots": [{"description": "旧内容"}]},
    )
    elements = build_faithfulness_elements({"narrative": "纸船先旋转再停靠岸边。"})
    sequence = next(row for row in elements if row["key"] == "faith_sequence")
    assert "纸船先旋转再停靠岸边" in sequence["question"]
    assert "不要把叙述段落数或分镜面板数当成切镜数量" in sequence["question"]


def test_video_facts_use_span_and_detected_cuts_not_planned_rows(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        media_review,
        "is_operator_enabled",
        lambda _name: True,
    )
    monkeypatch.setattr(
        media_review,
        "_gray_samples_and_has_cuts",
        lambda _path: ([], False),
    )

    def collect(_path, **kwargs):
        recorded.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(media_review, "collect_video_facts", collect)
    result = asyncio.run(
        media_review._video_objective_facts(
            Path("unused.mp4"),
            {
                "expected_duration_seconds": 6,
                "aspect_ratio": "9:16",
                "planned_shots": [{"duration_seconds": 999}, {}],
            },
        ),
    )
    assert result == {"ok": True}
    assert recorded["expected_duration_seconds"] == 6
    assert "planned_shot_count" not in recorded
    assert recorded["transcript_sentences"] is None
