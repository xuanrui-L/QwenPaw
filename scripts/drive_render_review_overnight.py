#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=line-too-long,cell-var-from-loop
"""Overnight autonomous render-review dogfood runner.

Runs multiple render-review trial projects sequentially, records each trial
as a JSONL line, and continues on individual failures. Designed to be started
and left running unattended.

Example:
    nohup python scripts/drive_render_review_overnight.py \
        --trials 50 --mode 30s --rounds-per-project 3 \
        --output /tmp/render_review_overnight/results.jsonl \
        > /tmp/render_review_overnight/run.log 2>&1 &
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import requests

BASE_URL = "http://127.0.0.1:8088"
API_PREFIX = "/api/qwenpaw-creator"
API = BASE_URL + API_PREFIX


class _Missing:
    """Sentinel distinct from JSON null for 'path does not exist'."""


MISSING = _Missing()


def req(method: str, path: str, **kwargs) -> requests.Response:
    url = f"{API}{path}"
    kwargs.setdefault("timeout", 60)
    print(f"[{method}] {url}")
    resp = requests.request(method, url, **kwargs)
    print(f"  -> {resp.status_code}")
    if resp.status_code >= 400:
        try:
            body = resp.text[:500]
            print(f"  -> {body}")
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


def _missing_hash() -> str:
    return f"sha256:{hashlib.sha256(b'MISSING').hexdigest()}"


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def make_sample_video(
    path: Path,
    pattern: str,
    label: str,
    duration_s: int,
) -> None:
    """Generate a synthetic test video with a constant frame rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    d = duration_s
    if pattern == "mystery":
        source = f"color=c=000040:s=1280x720:r=30:d={d},drawgrid=w=40:h=40:c=blue@0.3:t={d}"
    elif pattern == "tension":
        source = f"testsrc=size=1280x720:rate=30:duration={d}"
    else:
        source = f"color=c=505050:s=1280x720:r=30:d={d}"
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
        f"sine=frequency=440:duration={d}",
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
        str(d),
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Generated {label}: {path} ({path.stat().st_size} bytes)")


def create_project(mode: str) -> str:
    client_id = f"overnight-{mode}-{uuid.uuid4().hex[:12]}"
    suffix = uuid.uuid4().hex[:8]
    descriptions = {
        "simple": "Single-scene smoke-test project for overnight validation.",
        "complex": "6-second 3-scene dramatic short with overlays for overnight validation.",
        "30s": "30-second 3-scene dramatic short with overlays for overnight validation.",
    }
    resp = req(
        "POST",
        "/projects",
        json={
            "clientRequestId": client_id,
            "name": f"Overnight {mode.title()} {suffix}",
            "description": descriptions[mode],
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


def add_timeline(project_id: str, version_ids: list[str], mode: str) -> None:
    if mode == "simple":
        scene_dur = _ms(2.5)
        spans = [(0, scene_dur)]
        labels = [("edit-scene-1", "测试场景")]
        title_dur = 0
        credits_start = 0
    elif mode == "complex":
        scene_dur = _ms(2.2)
        spans = [(0, scene_dur), (_ms(1.8), scene_dur), (_ms(3.6), scene_dur)]
        labels = [
            ("edit-scene-1", "开场悬疑"),
            ("edit-scene-2", "冲突升级"),
            ("edit-scene-3", "尾声回味"),
        ]
        title_dur = _ms(1.5)
        credits_start = _ms(4.8)
    else:  # 30s
        scene_dur = _ms(10)
        spans = [
            (0, scene_dur),
            (scene_dur, scene_dur),
            (2 * scene_dur, scene_dur),
        ]
        labels = [
            ("edit-scene-1", "开场悬疑"),
            ("edit-scene-2", "冲突升级"),
            ("edit-scene-3", "尾声回味"),
        ]
        title_dur = _ms(3)
        credits_start = _ms(27)

    def _compute(snapshot: dict) -> tuple[str, str, Any, Any]:
        timeline = snapshot["project"]["timelines"]["items"]["timeline:main"]
        before = timeline["elements_by_id"]
        elements: dict[str, Any] = {}
        for (element_id, label), (start, duration) in zip(labels, spans):
            elements[element_id] = _edit_element(
                element_id,
                label,
                start,
                duration,
                version_ids[len(elements)],
            )

        if title_dur:
            elements["overlay-title"] = {
                "element_id": "overlay-title",
                "label": "片名标题",
                "enabled": True,
                "span": {"start_tick": 0, "duration_tick": title_dur},
                "location": _base_location(width=0.8, height=0.3, y=0.35),
                "z_index": 10,
                "creation": {
                    "type": "overlay",
                    "text": "谜影 \n Mystery Shadow",
                    "vibe": "mystery",
                    "prompt": "电影感片名：衬线字体、暗角、微光",
                    "reference_version_ids": [],
                    "motion": None,
                },
                "outputs": {},
                "render_source": None,
                "provenance_refs": [],
            }
        if credits_start:
            elements["overlay-credits"] = {
                "element_id": "overlay-credits",
                "label": "片尾字幕",
                "enabled": True,
                "span": {
                    "start_tick": credits_start,
                    "duration_tick": spans[-1][0]
                    + spans[-1][1]
                    - credits_start,
                },
                "location": _base_location(width=0.8, height=0.5, y=0.6),
                "z_index": 10,
                "creation": {
                    "type": "overlay",
                    "text": "导演：Dogfood\n主演：Test Pattern",
                    "vibe": "chill",
                    "prompt": "简洁片尾字幕：无衬线、小字号、居下",
                    "reference_version_ids": [],
                    "motion": None,
                },
                "outputs": {},
                "render_source": None,
                "provenance_refs": [],
            }
        return (
            "/timelines/items/timeline:main/elements_by_id",
            "replace",
            before,
            elements,
        )

    _patch_with_compute(project_id, _compute, f"{mode} timeline elements")


def set_edit_plan(project_id: str, mode: str) -> None:
    concepts = {
        "simple": "2.5 秒单场景测试片",
        "complex": "三段式情绪短片：悬疑开场 -> 紧张升级 -> 回味收尾",
        "30s": (
            "30 秒三段式情绪短片《谜影》：一位主角在深夜都市中追寻某个模糊线索， "
            "从孤独悬疑的开场，到紧张追逐的中段，最终在一个空旷天台上留白收尾。"
        ),
    }
    pacings = {
        "simple": "单场景 2.5 秒",
        "complex": "每场景约 2 秒，总长约 6 秒，开场标题 1.5 秒，结尾字幕 1 秒",
        "30s": "总时长 30 秒；三幕各约 10 秒。第 0-3 秒片名；第 10、20 秒硬切；第 27-30 秒字幕",
    }

    def _compute(snapshot: dict) -> tuple[str, str, Any, Any]:
        timeline = snapshot["project"]["timelines"]["items"]["timeline:main"]
        if "edit_plan" in timeline:
            op = "replace"
            before = timeline["edit_plan"]
        else:
            op = "add"
            before = MISSING
        plan = dict(before) if before not in (MISSING, None) else {}
        scene_count = {"simple": 1, "complex": 3, "30s": 3}[mode]
        plan.update(
            {
                "concept": concepts[mode],
                "dials": {
                    "energy": "high" if mode == "30s" else "mid",
                    "density": "mid",
                    "decoration": "mid",
                },
                "signature_device": "hard_cut",
                "pacing": pacings[mode],
                "design_floor": {
                    "opening": "片名标题叠在首场景上",
                    "transitions": "硬切",
                    "body": f"{scene_count} 段独立源素材连续剪辑",
                    "ending": "片尾字幕叠在末场景上",
                },
                "mechanical_exemption": True,
                "scene_ledger": [
                    {
                        "scene_id": f"scene-{i+1}",
                        "label": label,
                        "element_ids": [element_id],
                        "status": "draft",
                        "review_round": 0,
                        "locked_fingerprint": None,
                    }
                    for i, (element_id, label) in enumerate(
                        [
                            ("edit-scene-1", "开场悬疑"),
                            ("edit-scene-2", "冲突升级"),
                            ("edit-scene-3", "尾声回味"),
                        ][:scene_count],
                    )
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


def wait_task(project_id: str, task_id: str, timeout: float = 600) -> dict:
    time.sleep(5)
    deadline = time.time() + timeout
    not_found_count = 0
    while time.time() < deadline:
        resp = req("GET", f"/projects/{project_id}/tasks/{task_id}")
        if resp.status_code == 404:
            not_found_count += 1
            time.sleep(min(2 + not_found_count, 10))
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


def wait_for_review_round(project_id: str, timeout: float = 600) -> dict:
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
            result: dict[str, Any] = {}
            for f in files:
                if "budget" in f.name:
                    result["budget"] = json.loads(f.read_text())
                if "chain" in f.name:
                    result["chain"] = json.loads(f.read_text())
            if (
                result.get("chain", {}).get("claim") is None
                and "budget" in result
            ):
                return result
        time.sleep(5)
    raise TimeoutError(f"review settle for {project_id}")


def drive_extra_rounds(
    project_id: str,
    element_id: str,
    rounds: int,
    durations: list[int],
) -> dict:
    """Drive additional render-review rounds by mutating one element."""
    results = []
    for i in range(rounds):
        print(f"\n=== Driving extra round {i+1}/{rounds} ===")
        duration_ms = durations[i] if i < len(durations) else durations[-1]

        def _compute(snapshot: dict) -> tuple[str, Any, Any]:
            timeline = snapshot["project"]["timelines"]["items"][
                "timeline:main"
            ]
            before = timeline["elements_by_id"]
            elements = dict(before)
            element = dict(elements[element_id])
            creation = dict(element["creation"])
            creation["intent"] = f"场景意图变动 {i+1}"
            element["creation"] = creation
            span = dict(element["span"])
            span["duration_tick"] = duration_ms
            element["span"] = span
            render_source = dict(element["render_source"])
            render_source["source_out_tick"] = duration_ms
            element["render_source"] = render_source
            elements[element_id] = element
            return (
                "/timelines/items/timeline:main/elements_by_id",
                before,
                elements,
            )

        _patch_with_compute(
            project_id,
            _compute,
            f"round {i+1} element mutation",
        )
        task_id = trigger_render(project_id)
        task = wait_task(project_id, task_id)
        if task["status"] != "SUCCEEDED":
            raise RuntimeError(f"round {i+1} render {task['status']}")
        state = wait_for_review_round(project_id)
        results.append(
            {
                "round": i + 1,
                "render_status": task["status"],
                "budget": state.get("budget"),
                "chain": state.get("chain"),
            },
        )
    return {"extra_rounds": results}


def run_one_trial(trial_num: int, mode: str, extra_rounds: int) -> dict:
    start = time.time()
    result: dict[str, Any] = {
        "trial": trial_num,
        "mode": mode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
    }
    project_id: str | None = None
    try:
        work_dir = (
            Path("/tmp/render_review_overnight")
            / mode
            / f"trial-{trial_num:04d}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)

        durations = {
            "simple": [2500, 2000, 1500],
            "complex": [2200, 2000, 1800],
            "30s": [10000, 9000, 8000],
        }[mode]
        patterns = ["mystery", "tension", "reflective"]
        scene_duration_s = {"simple": 3, "complex": 3, "30s": 10}[mode]

        videos = [
            (work_dir / f"scene{i+1}_{pat}.mp4", pat, f"scene-{i+1}")
            for i, pat in enumerate(
                patterns[: {"simple": 1, "complex": 3, "30s": 3}[mode]],
            )
        ]
        for path, pattern, label in videos:
            make_sample_video(path, pattern, label, scene_duration_s)

        project_id = create_project(mode)
        result["project_id"] = project_id
        version_ids = [
            upload_source_video(project_id, path, label)
            for path, _pat, label in videos
        ]
        add_timeline(project_id, version_ids, mode)
        set_edit_plan(project_id, mode)

        task_id = trigger_render(project_id)
        task = wait_task(project_id, task_id)
        result["initial_render_status"] = task["status"]
        if task["status"] != "SUCCEEDED":
            raise RuntimeError(f"initial render {task['status']}")

        state = wait_for_review_round(project_id)
        result["initial_review"] = {
            "budget": state.get("budget"),
            "chain": state.get("chain"),
        }

        if extra_rounds > 0:
            extra = drive_extra_rounds(
                project_id,
                "edit-scene-1",
                extra_rounds,
                durations,
            )
            result.update(extra)

        result["status"] = "success"
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
    finally:
        result["duration_seconds"] = round(time.time() - start, 2)
        if project_id:
            result["project_id"] = project_id
    return result


def summarize(output_path: Path) -> None:
    if not output_path.exists():
        print("No results to summarize")
        return
    results = [
        json.loads(line)
        for line in output_path.read_text().strip().splitlines()
        if line.strip()
    ]
    total = len(results)
    successes = [r for r in results if r.get("status") == "success"]
    errors = [r for r in results if r.get("status") != "success"]
    print(f"\n=== Overnight run summary ({total} trials) ===")
    print(f"Success: {len(successes)}  Error: {len(errors)}")
    if successes:
        avg_dur = sum(r["duration_seconds"] for r in successes) / len(
            successes,
        )
        print(f"Avg success duration: {avg_dur:.1f}s")
    for r in errors:
        print(
            f"  ERROR trial {r['trial']} ({r.get('project_id', 'no-project')}): {r['error'][:120]}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Overnight render-review dogfood runner",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=10,
        help="Number of trial projects to run",
    )
    parser.add_argument(
        "--mode",
        choices=["simple", "complex", "30s"],
        default="30s",
        help="Trial scenario",
    )
    parser.add_argument(
        "--rounds-per-project",
        type=int,
        default=1,
        help="Total render-review rounds per project (1 = only initial)",
    )
    parser.add_argument(
        "--pause-seconds",
        type=int,
        default=10,
        help="Pause between trials",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/render_review_overnight/results.jsonl"),
    )
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    extra_rounds = max(0, args.rounds_per_project - 1)

    print(
        f"Starting overnight run: {args.trials} trials, mode={args.mode}, rounds={args.rounds_per_project}",
    )
    print(f"Logging results to {args.output}")

    for i in range(1, args.trials + 1):
        print(f"\n{'='*60}")
        print(f"TRIAL {i}/{args.trials}")
        print(f"{'='*60}")
        result = run_one_trial(i, args.mode, extra_rounds)
        with args.output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(
            f"Trial {i} finished: {result['status']} in {result['duration_seconds']}s",
        )
        if i < args.trials:
            time.sleep(args.pause_seconds)

    summarize(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
