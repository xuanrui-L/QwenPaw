#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=line-too-long,unused-argument,too-many-branches
"""Creator project health monitor: detect stuck projects, auto-fix, and nudge.

Detection rules (per project):
  R1 broken-output-binding  element's runtime-managed ``outputs`` lost its
                            slot binding while the artifact slot still exists
                            with a selected version (root cause of the
                            project-05580c2e deadlock).  L1: auto-fixable.
  R2 stuck-no-goal          work remains (work-graph nodes not done, or
                            elements missing render_source) but there is no
                            active goal and no running task for a while.
                            L2: nudge the agent via a conversation message.
  R3 orphan-quarantined     QUARANTINED task whose targetRef element no
                            longer exists in the timeline.  Report only.

Usage:
  # one-shot, read-only report
  python scripts/creator_health_monitor.py

  # continuous monitoring with auto-fix and nudging
  python scripts/creator_health_monitor.py --interval 300 --fix --nudge

  # single project
  python scripts/creator_health_monitor.py --project-id project-xxxx --fix
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "http://127.0.0.1:8088"
API = BASE_URL + "/api/qwenpaw-creator"
RUNTIME_ROOT = Path.home() / ".copaw" / "creator-runtime"

# A project is only "stuck" if it has been idle for at least this long.
DEFAULT_STALE_MINUTES = 10
# Do not nudge the same project more often than this.
NUDGE_COOLDOWN_MINUTES = 30

TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "QUARANTINED"}


def _json_hash(value: Any) -> str:
    def norm(o: Any) -> Any:
        if isinstance(o, float) and o.is_integer():
            return int(o)
        if isinstance(o, dict):
            return {k: norm(v) for k, v in o.items()}
        if isinstance(o, list):
            return [norm(v) for v in o]
        return o

    enc = json.dumps(
        norm(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(enc).hexdigest()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get(path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 30)
    return requests.get(f"{API}{path}", **kwargs)


class ProjectHealth:
    """Collects findings for one project."""

    def __init__(self, project_id: str, name: str = "") -> None:
        self.project_id = project_id
        self.name = name
        self.findings: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []

    def add(self, rule: str, level: str, detail: str, **extra: Any) -> None:
        self.findings.append(
            {"rule": rule, "level": level, "detail": detail, **extra},
        )

    def record_action(self, action: str, detail: str, ok: bool) -> None:
        self.actions.append({"action": action, "detail": detail, "ok": ok})

    @property
    def healthy(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "checked_at": _now().isoformat(),
            "healthy": self.healthy,
            "findings": self.findings,
            "actions": self.actions,
        }


def _active_goal_id(project_id: str) -> str | None:
    state_path = RUNTIME_ROOT / project_id / "runtime" / "state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text()).get("active_goal_id")
    except (json.JSONDecodeError, OSError):
        return None


def _default_conversation_id(project_id: str) -> str | None:
    resp = _get(f"/projects/{project_id}/conversations")
    if resp.status_code != 200:
        return None
    items = resp.json().get("items", [])
    for item in items:
        if item.get("isDefault"):
            return item["conversationId"]
    return items[0]["conversationId"] if items else None


def check_output_bindings(
    project_id: str,
    project: dict,
    health: ProjectHealth,
) -> list[dict[str, Any]]:
    """R1: find elements whose outputs binding was clobbered while the slot survives."""
    repairs: list[dict[str, Any]] = []
    slots = project.get("assets", {}).get("artifact_slots_by_id", {})
    timelines = project.get("timelines", {}).get("items", {})
    for timeline_id, timeline in timelines.items():
        for element_id, element in timeline.get("elements_by_id", {}).items():
            outputs = element.get("outputs") or {}
            for output_name in ("storyboard", "main"):
                slot_id = f"element:{element_id}:{output_name}"
                slot = slots.get(slot_id)
                if slot is None or not slot.get("selected_version_id"):
                    continue
                binding = outputs.get(output_name)
                if binding and binding.get("slot_id") == slot_id:
                    continue
                health.add(
                    "broken-output-binding",
                    "L1",
                    f"{element_id} 缺少 outputs['{output_name}'] 绑定，但 slot {slot_id} 已有 selected version",
                    timeline_id=timeline_id,
                    element_id=element_id,
                    output_name=output_name,
                    slot_id=slot_id,
                )
                repairs.append(
                    {
                        "timeline_id": timeline_id,
                        "element_id": element_id,
                        "output_name": output_name,
                        "slot_id": slot_id,
                    },
                )
    return repairs


def check_orphan_quarantined(
    project: dict,
    tasks: list[dict],
    health: ProjectHealth,
) -> None:
    """R3: QUARANTINED tasks whose target element no longer exists."""
    element_ids: set[str] = set()
    for timeline in project.get("timelines", {}).get("items", {}).values():
        element_ids.update(timeline.get("elements_by_id", {}).keys())
    for task in tasks:
        if task.get("status") != "QUARANTINED":
            continue
        target = task.get("targetRef") or ""
        if target.startswith("element:"):
            element_id = target.split(":", 1)[1]
            if element_id not in element_ids:
                health.add(
                    "orphan-quarantined",
                    "info",
                    f"task {task['id']} quarantined，但目标 {target} 已不存在（element 可能被改名/删除）",
                    task_id=task["id"],
                    target_ref=target,
                )


def check_stuck(
    project_id: str,
    project: dict,
    tasks: list[dict],
    stale_minutes: int,
    health: ProjectHealth,
) -> list[str]:
    """R2: work remains but nothing is running and no goal is active."""
    blockers: list[str] = []

    running = [t for t in tasks if t.get("status") not in TERMINAL_TASK_STATES]
    if running:
        return blockers
    if _active_goal_id(project_id):
        return blockers

    updated_at = _parse_ts(project.get("updated_at"))
    if (
        updated_at
        and (_now() - updated_at).total_seconds() < stale_minutes * 60
    ):
        return blockers

    # Incomplete elements: r2v/edit elements without a usable render_source.
    for timeline_id, timeline in (
        project.get("timelines", {}).get("items", {}).items()
    ):
        for element_id, element in timeline.get("elements_by_id", {}).items():
            creation_type = (element.get("creation") or {}).get("type")
            if creation_type not in ("r2v", "edit"):
                continue
            if element.get("render_source") is None:
                blockers.append(
                    f"{element_id}: render_source 为空（{timeline_id}）",
                )

    # Work-graph nodes that are not done and report an error / unmet deps.
    resp = _get(f"/projects/{project_id}/work-graph")
    if resp.status_code == 200:
        for node in resp.json().get("nodes", []):
            status = node.get("status")
            if status == "done":
                continue
            if status == "stale":
                # The scheduler only dispatches READY nodes; a STALE node
                # waits for an agent decision to regenerate.  With no
                # active goal that decision never comes, so a stale
                # backlog is a real blocker, not a finished-but-old node.
                blockers.append(
                    f"work-graph node {node.get('id')} 处于 stale，"
                    "需要决定重新生成或接受当前版本",
                )
                continue
            desc = f"work-graph node {node.get('id')} status={status}"
            if node.get("error"):
                desc += f" error={node['error']}"
            if node.get("missing"):
                desc += f" missing={node['missing']}"
            blockers.append(desc)

    if blockers:
        health.add(
            "stuck-no-goal",
            "L2",
            f"项目空转超过 {stale_minutes} 分钟：无 active goal、无运行中任务，但仍有未完成工作",
            blockers=blockers,
        )
    return blockers


def fix_output_binding(
    project_id: str,
    repair: dict[str, Any],
    health: ProjectHealth,
) -> bool:
    """L1 remediation: restore the missing outputs binding via CAS PATCH."""
    for _attempt in range(5):
        resp = _get(f"/projects/{project_id}/project")
        if resp.status_code != 200:
            health.record_action(
                "fix-output-binding",
                f"snapshot 失败 {resp.status_code}",
                False,
            )
            return False
        data = resp.json()
        timeline = data["project"]["timelines"]["items"][repair["timeline_id"]]
        element = timeline["elements_by_id"].get(repair["element_id"])
        if element is None:
            health.record_action(
                "fix-output-binding",
                f"{repair['element_id']} 已不存在",
                False,
            )
            return False
        before = element.get("outputs") or {}
        if (before.get(repair["output_name"]) or {}).get("slot_id") == repair[
            "slot_id"
        ]:
            health.record_action(
                "fix-output-binding",
                f"{repair['element_id']}.{repair['output_name']} 已恢复",
                True,
            )
            return True
        after = dict(before)
        after[repair["output_name"]] = {"slot_id": repair["slot_id"]}
        command_id = f"healthfix-{uuid.uuid4().hex[:12]}"
        patch = requests.patch(
            f"{API}/projects/{project_id}/project",
            json={
                "clientCommandId": command_id,
                "editSessionId": f"edit-{uuid.uuid4().hex[:12]}",
                "baseGeneration": data["generation"],
                "baseEtag": data["etag"],
                "operations": [
                    {
                        "op": "replace",
                        "path": f"/timelines/items/{repair['timeline_id']}/elements_by_id/{repair['element_id']}/outputs",
                        "value": after,
                        "expectedValueHash": _json_hash(before),
                    },
                ],
            },
            headers={"Idempotency-Key": command_id},
            timeout=30,
        )
        if patch.status_code == 409:
            time.sleep(0.5)
            continue
        ok = patch.status_code == 200
        health.record_action(
            "fix-output-binding",
            f"{repair['element_id']}.{repair['output_name']} -> {repair['slot_id']} ({patch.status_code})",
            ok,
        )
        return ok
    health.record_action("fix-output-binding", "CAS 冲突重试耗尽", False)
    return False


def nudge_project(
    project_id: str,
    blockers: list[str],
    state_dir: Path,
    health: ProjectHealth,
) -> bool:
    """L2 remediation: post a recovery message so the agent resumes."""
    marker = state_dir / f"nudge-{project_id}.json"
    if marker.exists():
        try:
            last = _parse_ts(json.loads(marker.read_text()).get("nudged_at"))
            if (
                last
                and (_now() - last).total_seconds()
                < NUDGE_COOLDOWN_MINUTES * 60
            ):
                health.record_action("nudge", "冷却中，跳过", True)
                return True
        except (json.JSONDecodeError, OSError):
            pass
    conversation_id = _default_conversation_id(project_id)
    if not conversation_id:
        health.record_action("nudge", "找不到 conversation", False)
        return False
    blocker_text = "\n".join(f"- {b}" for b in blockers[:10])
    message = (
        "【健康监控 · 自动提醒】检测到项目已停滞且没有活跃目标，以下环节仍未完成：\n"
        f"{blocker_text}\n"
        "请诊断并继续推进这些环节直至最终成片；如某环节确实无法完成，请在回复中说明原因。"
        "注意：修改 Timeline Element 时不要整体替换 element（会清空 runtime 管理的 outputs 绑定），只做最小修改。"
    )
    command_id = f"healthnudge-{uuid.uuid4().hex[:12]}"
    resp = requests.post(
        f"{API}/projects/{project_id}/messages",
        json={
            "clientMessageId": command_id,
            "conversationId": conversation_id,
            "message": message,
        },
        headers={"Idempotency-Key": command_id},
        timeout=30,
    )
    ok = resp.status_code == 202
    health.record_action("nudge", f"post message -> {resp.status_code}", ok)
    if ok:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {"nudged_at": _now().isoformat(), "blockers": blockers},
            ),
        )
    return ok


def check_project(
    project_id: str,
    name: str,
    args: argparse.Namespace,
    state_dir: Path,
) -> ProjectHealth:
    health = ProjectHealth(project_id, name)
    resp = _get(f"/projects/{project_id}/project")
    if resp.status_code != 200:
        health.add("unreachable", "warn", f"GET project -> {resp.status_code}")
        return health
    project = resp.json()["project"]

    tasks_resp = _get(f"/projects/{project_id}/tasks")
    tasks = (
        tasks_resp.json().get("items", [])
        if tasks_resp.status_code == 200
        else []
    )

    repairs = check_output_bindings(project_id, project, health)
    if repairs and args.fix:
        for repair in repairs:
            fix_output_binding(project_id, repair, health)

    check_orphan_quarantined(project, tasks, health)

    blockers = check_stuck(
        project_id,
        project,
        tasks,
        args.stale_minutes,
        health,
    )
    if blockers and args.nudge:
        # If we just repaired bindings, give the scheduler a chance first.
        if not (repairs and args.fix):
            nudge_project(project_id, blockers, state_dir, health)
        else:
            health.record_action("nudge", "本轮已执行 L1 修复，先观察下一轮", True)

    return health


def run_once(args: argparse.Namespace, state_dir: Path) -> list[ProjectHealth]:
    if args.project_id:
        targets = [(args.project_id, "")]
    else:
        resp = _get("/projects", params={"limit": 200})
        resp.raise_for_status()
        items = resp.json().get("items", [])
        cutoff_days = args.max_age_days
        targets = []
        for item in items:
            updated = _parse_ts(item.get("updatedAt"))
            if (
                cutoff_days
                and updated
                and (_now() - updated).days > cutoff_days
            ):
                continue
            targets.append((item["projectId"], item.get("name", "")))

    results = []
    for project_id, name in targets:
        try:
            health = check_project(project_id, name, args, state_dir)
        except requests.RequestException as error:
            health = ProjectHealth(project_id, name)
            health.add(
                "unreachable",
                "warn",
                f"{type(error).__name__}: {error}",
            )
        results.append(health)
    return results


def report(results: list[ProjectHealth], output: Path | None) -> None:
    unhealthy = [h for h in results if not h.healthy]
    print(
        f"\n[{_now().strftime('%H:%M:%S')}] checked {len(results)} projects, {len(unhealthy)} with findings",
    )
    for h in unhealthy:
        print(f"  {h.project_id} ({h.name})")
        for f in h.findings:
            print(f"    [{f['level']}] {f['rule']}: {f['detail']}")
            for blocker in f.get("blockers", [])[:5]:
                print(f"        - {blocker}")
        for a in h.actions:
            print(f"    -> {a['action']}: {a['detail']} ok={a['ok']}")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as fp:
            for h in results:
                if not h.healthy or h.actions:
                    fp.write(
                        json.dumps(h.to_dict(), ensure_ascii=False) + "\n",
                    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Creator project health monitor",
    )
    parser.add_argument("--project-id", help="Only check this project")
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Loop interval seconds (0 = run once)",
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=DEFAULT_STALE_MINUTES,
        help="Idle minutes before a project counts as stuck",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=3,
        help="Skip projects not updated within N days (0 = no filter)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply L1 auto-fixes (restore output bindings)",
    )
    parser.add_argument(
        "--nudge",
        action="store_true",
        help="Post L2 recovery messages to stuck projects",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/creator_health_monitor/findings.jsonl"),
    )
    args = parser.parse_args()

    state_dir = Path("/tmp/creator_health_monitor/state")

    while True:
        try:
            results = run_once(args, state_dir)
            report(results, args.output)
        except requests.RequestException as error:
            print(
                f"[{_now().strftime('%H:%M:%S')}] service unreachable: {error}",
            )
        if not args.interval:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
