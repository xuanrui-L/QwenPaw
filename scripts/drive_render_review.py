#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive a real render review cycle via the Creator API.

Creates a project, uploads a short synthetic video, places it on the main
timeline as an edit element, triggers final-cut compose, and monitors the
runtime/render-review directory for budget/chain convergence.
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


def create_project() -> str:
    client_id = f"drive-{uuid.uuid4().hex[:12]}"
    suffix = uuid.uuid4().hex[:8]
    resp = req(
        "POST",
        "/projects",
        json={
            "clientRequestId": client_id,
            "name": f"Render Review Dogfood {suffix}",
            "description": "4-scene short drama render-review dogfood",
            "scenario": "general",
            "aspectRatio": "16:9",
            "resolution": "720P",
            "contentType": None,
        },
        headers={"Idempotency-Key": client_id},
    )
    resp.raise_for_status()
    payload = resp.json()
    project_id = payload["projectId"]
    print(f"Created project {project_id}")
    return project_id


def make_sample_video(path: Path) -> None:
    """Generate a 3-second 1280x720 test video with ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=30:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:duration=3",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-t",
        "3",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Generated sample video: {path} ({path.stat().st_size} bytes)")


def upload_source_video(project_id: str, video_path: Path) -> str:
    client_id = f"upload-{uuid.uuid4().hex[:12]}"
    with video_path.open("rb") as f:
        resp = req(
            "POST",
            f"/projects/{project_id}/assets",
            files={"file": ("scene1.mp4", f, "video/mp4")},
            data={
                "clientRequestId": client_id,
                "postIngestAction": "ATTACH_SOURCE",
            },
            headers={"Idempotency-Key": client_id},
        )
    resp.raise_for_status()
    payload = resp.json()
    version_id = payload["assetVersionId"]
    print(f"Uploaded source video version {version_id}")
    return version_id


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


def add_edit_element(project_id: str, version_id: str) -> None:
    # Read current project snapshot.
    resp = req("GET", f"/projects/{project_id}/project")
    resp.raise_for_status()
    snapshot = resp.json()
    generation = snapshot["generation"]
    etag = snapshot["etag"]
    timeline_id = "timeline:main"
    timeline = snapshot["project"]["timelines"]["items"][timeline_id]

    element_id = "edit-scene-1"
    before = timeline["elements_by_id"]
    elements = dict(before)
    elements[element_id] = {
        "element_id": element_id,
        "label": "Scene 1",
        "enabled": True,
        "span": {"start_tick": 0, "duration_tick": 3000},
        "location": {
            "coordinate_space": "normalized_canvas",
            "x": 0,
            "y": 0,
            "width": 1,
            "height": 1,
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "rotation_degrees": 0,
            "opacity": 1,
        },
        "z_index": 0,
        "creation": {"type": "edit", "intent": "开场镜头"},
        "outputs": {},
        "render_source": {
            "type": "source_asset_version",
            "version_id": version_id,
            "source_in_tick": 0,
            "source_out_tick": 3000,
            "playback_rate": 1.0,
            "loop": False,
        },
        "provenance_refs": [],
    }

    command_id = f"add-element-{uuid.uuid4().hex[:12]}"
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
                    "op": "replace",
                    "path": f"/timelines/items/{timeline_id}/elements_by_id",
                    "value": elements,
                    "expectedValueHash": _json_hash(before),
                },
            ],
        },
        headers={"Idempotency-Key": command_id},
    )
    print(resp.text[:500])
    resp.raise_for_status()
    print(f"Added edit element {element_id}")


def trigger_render(project_id: str) -> str:
    resp = req(
        "POST",
        f"/projects/{project_id}/timelines/timeline%3Amain/render",
        headers={"Idempotency-Key": f"render-{uuid.uuid4().hex[:12]}"},
    )
    print(resp.text[:500])
    resp.raise_for_status()
    payload = resp.json()
    task_id = payload["taskId"]
    print(f"Render dispatched task {task_id}")
    return task_id


def wait_task(project_id: str, task_id: str, timeout: float = 300) -> dict:
    # Give the async worker a moment to persist the task record.
    time.sleep(2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = req("GET", f"/projects/{project_id}/tasks/{task_id}")
        if resp.status_code == 404:
            print("  task record not yet visible, retrying...")
            time.sleep(2)
            continue
        resp.raise_for_status()
        task = resp.json()
        status = task["status"]
        print(f"  task status: {status}")
        if status in {"SUCCEEDED", "FAILED", "CANCELLED", "QUARANTINED"}:
            return task
        time.sleep(2)
    raise TimeoutError(f"Task {task_id} did not finish in {timeout}s")


def monitor_render_review(project_id: str, timeout: float = 300) -> None:
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
            for f in files:
                if "budget" in f.name:
                    data = json.loads(f.read_text())
                    print(f"BUDGET: {json.dumps(data, indent=2)}")
                if "chain" in f.name:
                    data = json.loads(f.read_text())
                    print(f"CHAIN: {json.dumps(data, indent=2)}")
            if any("budget" in f.name for f in files):
                return
        time.sleep(3)
    print("Timeout waiting for render-review budget file")


def main() -> int:
    video_path = Path("/tmp/render_review_dogfood.mp4")
    make_sample_video(video_path)

    project_id = create_project()
    version_id = upload_source_video(project_id, video_path)
    add_edit_element(project_id, version_id)
    task_id = trigger_render(project_id)
    task = wait_task(project_id, task_id)
    print(f"Render finished: {task['status']}")
    if task["status"] != "SUCCEEDED":
        print(json.dumps(task, indent=2, default=str))
        return 1

    print("Waiting for render-review loop...")
    monitor_render_review(project_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
