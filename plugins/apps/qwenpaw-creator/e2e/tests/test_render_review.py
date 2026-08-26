# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=redefined-outer-name
"""Render review end-to-end smoke tests.

These tests exercise the Creator API surfaces that the render review loop
relies on. They do not wait for a real VLM/composition cycle; that is covered
by unit tests with stubbed VLM responses. The goal here is to detect API-level
regressions such as the self-review config disappearing or project creation
breaking the review runtime layout.
"""

from __future__ import annotations

import pytest

from utils.api_client import CreatorApiClient

pytestmark = [pytest.mark.render_review]


def test_model_config_exposes_self_review(api: CreatorApiClient) -> None:
    """The self-review toggle must be visible to the console."""
    config = api.models_config()
    assert "selfReview" in config
    self_review = config["selfReview"]
    assert self_review.get("render_enabled") is True
    assert "media_enabled" in self_review
    assert "sync_enabled" in self_review


def test_active_reviews_endpoint_exists_for_new_project(
    api: CreatorApiClient,
    project: dict,
) -> None:
    """A new project has no active reviews and the endpoint is reachable."""
    project_id = project["projectId"]
    response = api.get(f"/projects/{project_id}/runtime/reviews/active")
    # Empty list or 204 both mean "no active reviews".
    assert response.status_code in (200, 204)


def test_health_endpoint_reports_creator_runtime(
    api: CreatorApiClient,
) -> None:
    """Smoke test that the Creator plugin health check is alive."""
    response = api.get("/health")
    response.raise_for_status()
    payload = response.json()
    assert payload.get("status") == "ok"
    assert payload.get("runtime") == "creator-filesystem"
