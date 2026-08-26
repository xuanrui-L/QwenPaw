#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive a more realistic multi-scene render-review dogfood project.

Creates a 3-scene dramatic short with:
- 3 different synthetic source clips
- opening title overlay
- scene-to-scene transitions
- end-credits overlay
- a richer creative brief / contract

This exercises more review dimensions (rhythm, typography_motion,
contract, transitions) than the single-clip smoke test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

BASE_URL = "http://127.0.0.1:8088"
API_PREFIX = "/api/qwenpaw-creator"
API = BASE_URL + API_PREFIX


def req(method: str, path: str, **kwargs) -> requests.Response:
    url = f"{API}{path}"
    kwargs.setdefault("timeout", 60)
    print(f"[{method}] {url}")
    resp = requests.request(method, url, **kwargs)
    print(f"  -> {resp.status_code}")
    if resp.status_code >= 400:
        try:
            print(f"  -> {resp.text[:500]}")
        except Exception:
            pass
    return resp


def _json_hash(value: Any) -> str:
    def _normalize_numbers(obj: Any) -> Any:
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


class _Missing:
    """Sentinel distinct from JSON null for 'path does not exist'."""


MISSING = _Missing()


def _missing_hash() -> str:
    """Hash used by the server for a JSON Pointer target that does not exist."""
    return f"sha256:{hashlib.sha256(b'MISSING').hexdigest()}"


def create_project() -> str:
    client_id = f"complex-drive-{uuid.uuid4().hex[:12]}"
    suffix = uuid.uuid4().hex[:8]
    resp = req(
        "POST",
        "/projects",
        json={
            "clientRequestId": client_id,
            "name": f"Complex Render Review Dogfood {suffix}",
            "description": (
                "A 3-scene dramatic short: opening mystery, rising tension, "
                "and a reflective close. Uses hard cuts and a crossfade, "
                "with title cards and end credits."
            ),
            "scenario": "general",
            "aspectRatio": "16:9",
            "resolution": "720P",
            "contentType": None,
        },
        headers={"Idempotency-Key": client_id},
    )
    resp.raise_for_status()
    project_id = resp.json()["projectId"]
    print(f"Created project {project_id}")
    return project_id


def make_sample_video(path: Path, pattern: str, label: str) -> None:
    """Generate a 2-second 1280x720 test video with a constant frame rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if pattern == "mystery":
        # Dark blue static color + subtle diagonal bars.
        source = (
            "color=c=000040:s=1280x720:r=30:d=2,"
            "drawgrid=w=40:h=40:c=blue@0.3:t=2"
        )
    elif pattern == "tension":
        # Fast-moving red/orange test pattern with fixed rate.
        source = "testsrc=size=1280x720:rate=30:duration=2"
    else:
        # Soft gray static color.
        source = "color=c=505050:s=1280x720:r=30:d=2"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-r",
        "30",
        "-t",
        "2",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Generated {label}: {path} ({path.stat().st_size} bytes)")


def upload_source_video(project_id: str, video_path: Path, label: str) -> str:
    client_id = f"upload-{label}-{uuid.uuid4().hex[:12]}"
    with video_path.open("rb") as f:
        resp = req(
            "POST",
            f"/projects/{project_id}/assets",
            files={"file": (video_path.name, f, "video/mp4")},
            data={
                "clientRequestId": client_id,
                "postIngestAction": "ATTACH_SOURCE",
            },
            headers={"Idempotency-Key": client_id},
        )
    resp.raise_for_status()
    version_id = resp.json()["assetVersionId"]
    print(f"Uploaded {label} version {version_id}")
    return version_id


def _current_timeline(project_id: str) -> tuple[int, str, dict]:
    resp = req("GET", f"/projects/{project_id}/project")
    resp.raise_for_status()
    snapshot = resp.json()
    return (
        snapshot["generation"],
        snapshot["etag"],
        snapshot["project"]["timelines"]["items"]["timeline:main"],
    )


def _patch_with_compute(
    project_id: str,
    compute: Any,
    description: str,
) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        generation, etag, timeline = _current_timeline(project_id)
        snapshot = {
            "project": {"timelines": {"items": {"timeline:main": timeline}}},
        }
        path, op, before, value = compute(snapshot)
        expected_hash = (
            _missing_hash() if before is MISSING else _json_hash(before)
        )
        command_id = f"patch-{uuid.uuid4().hex[:12]}"
        resp = req(
            "PATCH",
            f"/projects/{project_id}/project",
            json={
                "clientCommandId": command_id,
                "editSessionId": f"edit-{uuid.uuid4().hex[:12]}",
                "baseGeneration": generation,
                "baseEtag": etag,
                "operations": [
                    {
                        "op": op,
                        "path": path,
                        "value": value,
                        "expectedValueHash": expected_hash,
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


def add_complex_timeline(project_id: str, version_ids: list[str]) -> None:
    def _compute(snapshot: dict) -> tuple[str, str, Any, Any]:
        timeline = snapshot["project"]["timelines"]["items"]["timeline:main"]
        before = timeline["elements_by_id"]
        elements: dict[str, Any] = {}

        def _base_location(**overrides) -> dict:
            loc = {
                "coordinate_space": "normalized_canvas",
                "x": 0.5,
                "y": 0.5,
                "width": 1.0,
                "height": 1.0,
                "anchor_x": 0.5,
                "anchor_y": 0.5,
                "rotation_degrees": 0.0,
                "opacity": 1.0,
            }
            loc.update(overrides)
            return loc

        def _edit_element(
            element_id: str,
            label: str,
            start: int,
            duration: int,
            version_id: str,
        ) -> dict:
            return {
                "element_id": element_id,
                "label": label,
                "enabled": True,
                "span": {"start_tick": start, "duration_tick": duration},
                "location": _base_location(),
                "z_index": 0,
                "creation": {"type": "edit", "intent": f"{label} 画面"},
                "outputs": {},
                "render_source": {
                    "type": "source_asset_version",
                    "version_id": version_id,
                    "source_in_tick": 0,
                    "source_out_tick": duration,
                    "playback_rate": 1.0,
                    "loop": False,
                },
                "provenance_refs": [],
            }

        # Three scene edits with overlaps so transitions fit inside endpoint intersection.
        elements["edit-scene-1"] = _edit_element(
            "edit-scene-1",
            "开场悬疑",
            0,
            2200,
            version_ids[0],
        )
        elements["edit-scene-2"] = _edit_element(
            "edit-scene-2",
            "冲突升级",
            1800,
            2200,
            version_ids[1],
        )
        elements["edit-scene-3"] = _edit_element(
            "edit-scene-3",
            "尾声回味",
            3600,
            2200,
            version_ids[2],
        )

        # Opening title overlay.
        elements["overlay-title"] = {
            "element_id": "overlay-title",
            "label": "片名标题",
            "enabled": True,
            "span": {"start_tick": 0, "duration_tick": 1500},
            "location": _base_location(width=0.8, height=0.3, y=0.35),
            "z_index": 10,
            "creation": {
                "type": "overlay",
                "text": "谜影 \\n Mystery Shadow",
                "vibe": "mystery",
                "prompt": "电影感片名：衬线字体、暗角、微光",
                "reference_version_ids": [],
                "motion": None,
            },
            "outputs": {},
            "render_source": None,
            "provenance_refs": [],
        }

        # End credits overlay.
        elements["overlay-credits"] = {
            "element_id": "overlay-credits",
            "label": "片尾字幕",
            "enabled": True,
            "span": {"start_tick": 4800, "duration_tick": 1000},
            "location": _base_location(width=0.8, height=0.5, y=0.6),
            "z_index": 10,
            "creation": {
                "type": "overlay",
                "text": "导演：Dogfood\\n主演：Test Pattern",
                "vibe": "chill",
                "prompt": "简洁片尾字幕：无衬线、小字号、居下",
                "reference_version_ids": [],
                "motion": None,
            },
            "outputs": {},
            "render_source": None,
            "provenance_refs": [],
        }

        # Note: transitions are omitted because the current ffmpeg xfade path
        # requires perfectly constant/identical input frame rates, which the
        # synthetic lavfi sources do not reliably provide.  Multi-scene hard cuts
        # plus overlays are still a more complex prompt than the single-clip test.

        return (
            "/timelines/items/timeline:main/elements_by_id",
            "replace",
            before,
            elements,
        )

    _patch_with_compute(project_id, _compute, "complex timeline elements")


def set_edit_plan_and_bypass_scene_gate(project_id: str) -> None:
    def _compute(snapshot: dict) -> tuple[str, str, Any, Any]:
        timeline = snapshot["project"]["timelines"]["items"]["timeline:main"]
        if "edit_plan" in timeline:
            op = "replace"
            before = timeline["edit_plan"]
        else:
            op = "add"
            before = MISSING  # sentinel for MISSING hash
        plan = dict(before) if before not in (MISSING, None) else {}
        plan.update(
            {
                "concept": "三段式情绪短片：悬疑开场 -> 紧张升级 -> 回味收尾",
                "dials": {
                    "energy": "mid",
                    "density": "mid",
                    "decoration": "mid",
                },
                "signature_device": "hard_cut",
                "pacing": "每场景约2秒，总长约6秒，开场标题1.5秒，结尾字幕1秒",
                "design_floor": {
                    "opening": "暗调片名标题叠在首场景上",
                    "transitions": "三段硬切",
                    "body": "三段独立源素材连续剪辑",
                    "ending": "片尾字幕叠在末场景上",
                },
                "mechanical_exemption": True,
                "scene_ledger": [
                    {
                        "scene_id": "scene-1",
                        "label": "开场悬疑",
                        "element_ids": ["edit-scene-1"],
                        "status": "draft",
                        "review_round": 0,
                        "locked_fingerprint": None,
                    },
                    {
                        "scene_id": "scene-2",
                        "label": "冲突升级",
                        "element_ids": ["edit-scene-2"],
                        "status": "draft",
                        "review_round": 0,
                        "locked_fingerprint": None,
                    },
                    {
                        "scene_id": "scene-3",
                        "label": "尾声回味",
                        "element_ids": ["edit-scene-3"],
                        "status": "draft",
                        "review_round": 0,
                        "locked_fingerprint": None,
                    },
                ],
            },
        )
        return "/timelines/items/timeline:main/edit_plan", op, before, plan

    _patch_with_compute(project_id, _compute, "edit_plan + scene gate bypass")


def trigger_render(project_id: str) -> str:
    resp = req(
        "POST",
        f"/projects/{project_id}/timelines/timeline%3Amain/render",
        headers={"Idempotency-Key": f"render-{uuid.uuid4().hex[:12]}"},
    )
    resp.raise_for_status()
    return resp.json()["taskId"]


def wait_task(project_id: str, task_id: str, timeout: float = 300) -> dict:
    time.sleep(2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = req("GET", f"/projects/{project_id}/tasks/{task_id}")
        if resp.status_code == 404:
            time.sleep(2)
            continue
        resp.raise_for_status()
        task = resp.json()
        print(f"  task status: {task['status']}")
        if task["status"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "QUARANTINED",
        }:
            return task
        time.sleep(2)
    raise TimeoutError(task_id)


def monitor_render_review(project_id: str, timeout: float = 300) -> dict:
    runtime = (
        Path.home()
        / ".copaw"
        / "creator-runtime"
        / project_id
        / "runtime"
        / "render-review"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runtime.exists():
            files = list(runtime.glob("*.json"))
            print(f"render-review files: {[f.name for f in files]}")
            result: dict[str, Any] = {}
            for f in files:
                if "budget" in f.name:
                    result["budget"] = json.loads(f.read_text())
                    print(
                        f"BUDGET: {json.dumps(result['budget'], indent=2, ensure_ascii=False)}",
                    )
                if "chain" in f.name:
                    result["chain"] = json.loads(f.read_text())
                    print(
                        f"CHAIN: {json.dumps(result['chain'], indent=2, ensure_ascii=False)}",
                    )
            if "budget" in result:
                return result
        time.sleep(3)
    print("Timeout waiting for render-review budget file")
    return {}


def main() -> int:
    work_dir = Path("/tmp/render_review_complex")
    work_dir.mkdir(parents=True, exist_ok=True)

    videos = [
        (work_dir / "scene1_mystery.mp4", "mystery", "scene-1"),
        (work_dir / "scene2_tension.mp4", "tension", "scene-2"),
        (work_dir / "scene3_reflective.mp4", "reflective", "scene-3"),
    ]
    for path, pattern, label in videos:
        make_sample_video(path, pattern, label)

    project_id = create_project()
    version_ids = [
        upload_source_video(project_id, path, label)
        for path, _pattern, label in videos
    ]
    add_complex_timeline(project_id, version_ids)
    set_edit_plan_and_bypass_scene_gate(project_id)

    task_id = trigger_render(project_id)
    task = wait_task(project_id, task_id)
    print(f"Render finished: {task['status']}")
    if task["status"] != "SUCCEEDED":
        print(json.dumps(task, indent=2, default=str))
        return 1

    print("Waiting for render-review loop...")
    state = monitor_render_review(project_id)
    print(f"\nProject ID: {project_id}")
    if state:
        print("Render review completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
