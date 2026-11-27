# -*- coding: utf-8 -*-
"""One-shot continuity audit for a Creator project (read-only).

Usage (inside the creator container, or anywhere the runtime tree is
mounted at the same path):

    python3 scripts/verify/continuity-audit.py <project_dir_name> [--dump]

Prints, for every main-track visual element in play order:
  routing (mode / continuityShape), promoted last-frame anchor?,
  storyboard in the SENT reference stack?, bgm reference attached?,
  scene_ref / prop_refs, published duration, seam SSIM vs the
  predecessor's last frame, and the dispatched-task count (loop
  watchdog: more than one render without a user reroll is suspicious).

``--dump`` additionally writes first/middle/last keyframes of every
published segment under <project>/runtime/continuity-audit/ so scene
consistency (set drift, prop duplication) can be eyeballed per scene.

Read-only against project data; no provider calls, no paid work.
"""
import glob
import json
import os
import subprocess
import sys
import pathlib

# Pylint: the audit is one flat reporting pass by design (see docstring).
# pylint: disable=too-many-branches,too-many-statements

RUNTIME = "/app/working/creator-runtime"


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    pid = sys.argv[1]
    dump = "--dump" in sys.argv
    base = pathlib.Path(RUNTIME) / pid
    if not (base / "project.json").exists():
        matches = sorted(glob.glob(str(base) + "*"))
        if not matches:
            raise SystemExit(f"project dir not found: {base}")
        base = pathlib.Path(matches[0])
    proj = read_json(base / "project.json")
    a = proj["assets"]
    slots = a["artifact_slots_by_id"]
    vers = a["artifact_versions_by_id"]
    files = a["files_by_id"]
    tls = proj["timelines"]["items"]
    tid = "timeline:main" if "timeline:main" in tls else next(iter(tls))
    tl = tls[tid]
    els = tl["elements_by_id"]

    def norm(v):
        return str(v).replace("artifact-version:", "")

    def latest_tasks(eid):
        rows = []
        for tf in glob.glob(str(base / "runtime/tasks/*/task.json")):
            d = read_json(tf)
            if d.get("kind") != "r2v_generation":
                continue
            st = os.path.join(os.path.dirname(tf), "r2v-state.json")
            req = {}
            if os.path.exists(st):
                req = (read_json(st) or {}).get("request") or {}
            if req.get("elementId") == eid:
                rows.append((d.get("created_at", ""), d, req))
        rows.sort()
        return rows

    def vid_path(eid):
        slot = slots.get(f"element:{eid}:main") or {}
        sel = slot.get("selected_version_id")
        if not sel:
            return None
        v = vers.get(sel) or {}
        f = files.get(v.get("file_id") or "")
        return base / f["relative_uri"] if f else None

    def dur(p):
        # pylint: disable-next=subprocess-run-check
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(p),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            return float(r.stdout.strip())
        except ValueError:
            return 0.0

    def frame(v, t, out):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                str(t),
                "-i",
                str(v),
                "-frames:v",
                "1",
                out,
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

    def ssim(x, y):
        # pylint: disable-next=subprocess-run-check
        r = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "info",
                "-i",
                x,
                "-i",
                y,
                "-lavfi",
                "ssim",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in r.stderr.splitlines():
            if "All:" in line:
                return round(float(line.split("All:")[1].split("(")[0]), 3)
        return None

    order = sorted(
        [
            k
            for k, e in els.items()
            if (e.get("creation") or {}).get("type") in ("r2v", "i2v", "t2v")
        ],
        key=lambda k: els[k]["span"]["start_tick"],
    )
    transitions = {}
    for e in els.values():
        c = e.get("creation") or {}
        if c.get("type") == "transition":
            transitions[
                (c.get("from_element_id"), c.get("to_element_id"))
            ] = c.get("transition_kind")
    if dump:
        outdir = base / "runtime" / "continuity-audit"
        outdir.mkdir(parents=True, exist_ok=True)

    prev = None
    print(
        f"{'element':12} {'scene':10} {'props':8} {'trans':7} "
        f"{'mode':13} {'anchor?':7} {'sbSent?':7} {'bgm?':5} "
        f"{'dur':6} {'seam':7} {'renders':>7}",
    )
    for eid in order:
        el = els[eid]
        c = el["creation"]
        rows = latest_tasks(eid)
        mode = "—"
        anchor = sent_sb = bgm = "—"
        if rows:
            _tsk, _d, req = rows[-1]
            mode = str(req.get("mode") or "r2v")
            ids = [str(x) for x in req.get("referenceVersionIds") or []]
            anchor = (
                "YES" if ids and ids[0].startswith("continuity:") else "no"
            )
            sb = (slots.get(f"element:{eid}:storyboard") or {}).get(
                "selected_version_id",
            )
            sent_sb = (
                "yes"
                if sb and any(norm(i) == norm(sb) for i in ids)
                else ("no" if sb else "—")
            )
            bgm = "YES" if req.get("bgmReferenceUrl") else "no"
        tr = transitions.get((prev, eid), "") or "none" if prev else "-"
        path = vid_path(eid)
        d = dur(path) if path else 0.0
        seam = "—"
        if path and prev:
            ppath = vid_path(prev)
            if ppath:
                frame(ppath, max(dur(ppath) - 0.15, 0), "/tmp/_ap.png")
                frame(path, 0.05, "/tmp/_bp.png")
                seam = ssim("/tmp/_ap.png", "/tmp/_bp.png")
        if dump and path:
            dd = dur(path)
            for tag, t in (
                ("first", 0.05),
                ("mid", dd / 2),
                ("last", dd - 0.15),
            ):
                try:
                    frame(path, t, str(outdir / f"{eid}_{tag}.png"))
                except Exception:  # noqa: BLE001 - best-effort dump
                    pass
        props = (
            ",".join(
                str(x).rsplit(":", maxsplit=1)[-1]
                for x in (c.get("prop_refs") or [])
            )
            or "-"
        )
        print(
            f"{eid:12} {str(c.get('scene_ref'))[6:16]:10} "
            f"{props[:8]:8} {(tr or '-'):7} {mode:13} {str(anchor):7} "
            f"{str(sent_sb):7} {str(bgm):5} {d:<6.2f} {str(seam):7} "
            f"{len(rows):>7}",
        )
        prev = eid
    if dump:
        print(f"\nkeyframes dumped to {base}/runtime/continuity-audit/")


if __name__ == "__main__":
    main()
