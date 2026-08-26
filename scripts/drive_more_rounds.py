#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trigger additional render rounds for an existing dogfood project."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

BASE_URL = "http://127.0.0.1:8088"
API_PREFIX = "/api/qwenpaw-creator"
API = BASE_URL + API_PREFIX

# Populated from CLI; kept as module globals for convenience.
PROJECT_ID: str = ""
ELEMENT_ID: str = "edit-scene-1"


def _json_hash(value) -> str:
    def _normalize_numbers(obj):
        if isinstance(obj, float) and obj.is_integer():
            return int(obj)
        if isinstance(obj, dict):
            return {k: _normalize_numbers(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_normalize_numbers(v) for v in obj]
        return obj

    encoded = json.dumps(
        _normalize_numbers(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def req(method: str, path: str, **kwargs):
    url = f"{API}{path}"
    kwargs.setdefault("timeout", 60)
    print(f"[{method}] {url}")
    resp = requests.request(method, url, **kwargs)
    print(f"  -> {resp.status_code}")
    if resp.status_code >= 400:
        print(f"  -> {resp.text[:500]}")
    return resp


def _current_timeline() -> tuple[int, str, dict]:
    """Return (generation, etag, timeline:main dict) from the current snapshot."""
    resp = req("GET", f"/projects/{PROJECT_ID}/project")
    resp.raise_for_status()
    snapshot = resp.json()
    return (
        snapshot["generation"],
        snapshot["etag"],
        snapshot["project"]["timelines"]["items"]["timeline:main"],
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive additional render-review rounds.",
    )
    parser.add_argument(
        "rounds",
        type=int,
        nargs="?",
        default=1,
        help="Number of rounds to drive",
    )
    parser.add_argument(
        "--project-id",
        default="project-12f0da6fbe2a584688fbd24470b935c4",
        help="Target project ID",
    )
    parser.add_argument(
        "--element-id",
        default="edit-scene-1",
        help="Element to mutate each round",
    )
    parser.add_argument(
        "--durations",
        type=lambda s: [int(x) for x in s.split(",")],
        default=None,
        help="Comma-separated durations in ms for each round",
    )
    parser.add_argument(
        "--wait-review",
        type=int,
        default=30,
        help="Seconds to wait after render for review to settle",
    )
    return parser.parse_args()


def _patch_with_compute(
    compute: Callable[[dict], tuple[str, Any, Any]],
    description: str,
) -> None:
    """Compute (path, before, value) from fresh snapshot and PATCH; retry on CAS."""
    deadline = time.time() + 60
    while time.time() < deadline:
        generation, etag, timeline = _current_timeline()
        snapshot = {
            "project": {"timelines": {"items": {"timeline:main": timeline}}},
        }
        path, before, value = compute(snapshot)
        command_id = f"patch-{uuid.uuid4().hex[:12]}"
        resp = req(
            "PATCH",
            f"/projects/{PROJECT_ID}/project",
            json={
                "clientCommandId": command_id,
                "editSessionId": f"edit-{uuid.uuid4().hex[:12]}",
                "baseGeneration": generation,
                "baseEtag": etag,
                "operations": [
                    {
                        "op": "replace",
                        "path": path,
                        "value": value,
                        "expectedValueHash": _json_hash(before),
                    },
                ],
            },
            headers={"Idempotency-Key": command_id},
        )
        if resp.status_code == 409:
            print(f"  CAS conflict on {description}, retrying...")
            time.sleep(0.5)
            continue
        resp.raise_for_status()
        print(f"Patched {description}")
        return
    raise TimeoutError(f"patch {description}")


def ensure_scene_gate_bypassed() -> None:
    """Set edit_plan.mechanical_exemption so master compose is not gated."""
    _generation, _etag, timeline = _current_timeline()
    plan = timeline.get("edit_plan") or {}
    if plan.get("mechanical_exemption"):
        print("Scene gate already bypassed")
        return

    def _compute(_snapshot: dict) -> tuple[str, Any, Any]:
        _timeline = _snapshot["project"]["timelines"]["items"]["timeline:main"]
        _plan = dict(_timeline.get("edit_plan") or {})
        before = _plan.copy()
        _plan["mechanical_exemption"] = True
        return "/timelines/items/timeline:main/edit_plan", before, _plan

    _patch_with_compute(_compute, "edit_plan mechanical_exemption")


def patch_element_for_new_round(new_intent: str, duration_ms: int) -> None:
    """Mutate the edit element enough to produce a distinct render output."""

    def _compute(snapshot: dict) -> tuple[str, Any, Any]:
        timeline = snapshot["project"]["timelines"]["items"]["timeline:main"]
        before = timeline["elements_by_id"]
        elements = dict(before)
        element = dict(elements[ELEMENT_ID])
        creation = dict(element["creation"])
        creation["intent"] = new_intent
        element["creation"] = creation
        span = dict(element["span"])
        span["duration_tick"] = duration_ms
        element["span"] = span
        render_source = dict(element["render_source"])
        render_source["source_out_tick"] = duration_ms
        element["render_source"] = render_source
        elements[ELEMENT_ID] = element
        return (
            "/timelines/items/timeline:main/elements_by_id",
            before,
            elements,
        )

    _patch_with_compute(_compute, "element intent + duration")
    print(f"Updated {ELEMENT_ID} intent={new_intent} duration={duration_ms}ms")


def render() -> str:
    resp = req(
        "POST",
        f"/projects/{PROJECT_ID}/timelines/timeline%3Amain/render",
        headers={"Idempotency-Key": f"render-{uuid.uuid4().hex[:12]}"},
    )
    resp.raise_for_status()
    return resp.json()["taskId"]


def wait_task(task_id: str) -> dict:
    time.sleep(2)
    deadline = time.time() + 300
    while time.time() < deadline:
        resp = req("GET", f"/projects/{PROJECT_ID}/tasks/{task_id}")
        if resp.status_code == 404:
            time.sleep(2)
            continue
        resp.raise_for_status()
        task = resp.json()
        print(f"  status: {task['status']}")
        if task["status"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "QUARANTINED",
        }:
            return task
        time.sleep(3)
    raise TimeoutError(task_id)


def show_state():
    root = (
        Path.home()
        / ".copaw"
        / "creator-runtime"
        / PROJECT_ID
        / "runtime"
        / "render-review"
    )
    for name in (
        "budget-timeline-timeline-main.json",
        "chain-timeline-timeline-main.json",
    ):
        path = root / name
        if path.exists():
            print(f"--- {name} ---")
            print(path.read_text())


def main():
    global PROJECT_ID, ELEMENT_ID
    args = _parse_args()
    PROJECT_ID = args.project_id
    ELEMENT_ID = args.element_id
    ensure_scene_gate_bypassed()
    for i in range(args.rounds):
        print(f"\n=== Driving round {i+1} ===")
        if args.durations:
            duration_ms = (
                args.durations[i]
                if i < len(args.durations)
                else args.durations[-1]
            )
        else:
            duration_ms = 2500 - i * 500
        patch_element_for_new_round(f"场景意图变动 {i+1}", duration_ms)
        task_id = render()
        task = wait_task(task_id)
        print(f"Render result: {task['status']}")
        # Wait for review to settle (complex projects need a little longer).
        time.sleep(args.wait_review)
        show_state()


if __name__ == "__main__":
    main()
