#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Build a plugin-bundled inspiration example archive from a real Project.

The archive ships inside the plugin at ``backend/examples/`` and is
materialized on demand by ``POST /examples/{id}/open``.  The source Project is
re-identified under a deterministic example project id so it can never collide
with the Project it was built from, and runtime layers that store checksums
over historical ``project.json`` bytes (transactions, change rounds, reviews)
are pruned because they cannot survive re-identification.  ``state.json`` is
re-pointed at the recomputed ETag of the rewritten ``project.json`` so future
commits still see a consistent baseline.

Tutorial
========

What gets packaged
------------------
A full Project directory is far larger than an example needs to be, so the
build works in five steps:

1. **Copy & prune** - the source Project is copied to a staging directory
   while dropping regenerable or non-portable subtrees (see
   ``_PRUNED_SUBTREES``): media task scratch (``runtime/task-work``),
   observability logs/traces, and the transaction/review layers whose
   checksums are tied to the original project id.  What survives is
   ``project.json``, ``assets/`` (final cut, storyboards, reference images)
   and ``runtime/sessions`` + ``runs`` so the opened example still shows the
   whole Agent creation history.  This typically shrinks ~500 MB to ~20 MB.
2. **Rewrite the id** - every text file has the original project id replaced
   with ``example_project_id(example_id)``, a uuid5-derived id that is stable
   per example and can never collide with a user Project.
3. **Repoint the ETag** - ``runtime/state.json`` is updated to the recomputed
   ETag of the rewritten ``project.json`` so later commits stay consistent.
4. **Zip** - the staged directory is archived as
   ``backend/examples/<example-id>.zip`` with the project id as the single
   top-level folder (the same shape the import endpoint expects).
5. **Manifest** - ``backend/examples/manifest.json`` gains (or updates) the
   card entry: id, title, description, projectId and archive name.  The home
   page reads this catalogue through ``GET /examples``.

How to use it
-------------
Point ``--source`` at a Project directory inside ``CREATOR_DATA_ROOT`` (or at
an unzipped project export) and pick a stable ``--example-id``:

    python scripts/build_example_archive.py \
        --source ~/.qwenpaw-poc/creator-runtime/project-XXXX \
        --example-id crow-short-drama \
        --title 短剧制作 \
        --description "做一个乌鸦喝水的卡通短视频…"

Re-running with the same ``--example-id`` rebuilds the zip and replaces the
manifest entry in place.  Ship the plugin as usual afterwards - the archive
rides along in ``backend/examples/`` and users never import anything by hand:
clicking the inspiration card installs it (marked ``.builtin-example`` so it
stays out of "my projects") and opens the project page.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from services.project_files.serialization import (  # noqa: E402  # pylint: disable=wrong-import-position
    load_project_json,
    project_etag,
)

# Directories whose *contents* never ship with an example.  Scratch space and
# observability logs are regenerable; transactions/change-rounds/reviews store
# ETags over historical project.json bytes and cannot survive the id rewrite.
_PRUNED_SUBTREES = (
    Path("observability/logs"),
    Path("observability/traces"),
    Path("runtime/transactions"),
    Path("runtime/change-rounds"),
    Path("runtime/reviews"),
    Path("runtime/task-work"),
    Path("runtime/temp"),
    Path("runtime/locks"),
)

# Only plain-text runtime/document formats take part in the id rewrite.
_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md"}


def example_project_id(example_id: str) -> str:
    """Deterministic id mirroring api.project_routes._stable_id."""

    identity = f"qwenpaw-creator:project:example:{example_id}"
    return f"project-{uuid5(NAMESPACE_URL, identity).hex}"


def _copy_pruned(source: Path, staged: Path) -> None:
    pruned = {source / subtree for subtree in _PRUNED_SUBTREES}

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        return {
            name
            for name in names
            if current / name in pruned or (current / name).is_symlink()
        }

    shutil.copytree(source, staged, ignore=ignore)
    # Pruned directories stay present-but-empty so the layout matches a
    # freshly created Project.
    for subtree in _PRUNED_SUBTREES:
        (staged / subtree).mkdir(parents=True, exist_ok=True)


def _rewrite_text_files(staged: Path, old: str, new: str) -> int:
    rewritten = 0
    for path in sorted(staged.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old not in text:
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        rewritten += 1
    return rewritten


def _repoint_state_etag(staged: Path, old_etag: str, new_etag: str) -> None:
    state_path = staged / "runtime" / "state.json"
    if not state_path.is_file():
        return
    text = state_path.read_text(encoding="utf-8")
    state_path.write_text(text.replace(old_etag, new_etag), encoding="utf-8")


def _zip_directory(staged: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(
        archive_path,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(staged.rglob("*")):
            relative = f"{staged.name}/{path.relative_to(staged).as_posix()}"
            if path.is_dir():
                archive.writestr(f"{relative}/", b"")
            elif path.is_file():
                archive.write(path, relative)


def _update_manifest(
    examples_dir: Path,
    *,
    example_id: str,
    title: str,
    description: str,
    project_id: str,
    archive_name: str,
) -> None:
    manifest_path = examples_dir / "manifest.json"
    manifest: dict = {"examples": []}
    if manifest_path.is_file():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(
            loaded.get("examples"),
            list,
        ):
            manifest = loaded
    entry = {
        "id": example_id,
        "title": title,
        "description": description,
        "projectId": project_id,
        "archive": archive_name,
    }
    entries = [
        item
        for item in manifest["examples"]
        if not (isinstance(item, dict) and item.get("id") == example_id)
    ]
    entries.append(entry)
    manifest["examples"] = entries
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build(args: argparse.Namespace) -> Path:
    source = Path(args.source).expanduser().resolve()
    if not (source / "project.json").is_file():
        raise SystemExit(f"not a Project directory: {source}")
    old_id = source.name
    new_id = example_project_id(args.example_id)
    if old_id == new_id:
        raise SystemExit("source already uses the example project id")

    old_project = load_project_json((source / "project.json").read_bytes())
    if old_project.project_id != old_id:
        raise SystemExit("source project.json does not match its directory")
    old_etag = project_etag(old_project)

    examples_dir = Path(args.examples_dir).expanduser().resolve()
    examples_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as workdir:
        staged = Path(workdir) / new_id
        print(f"copying {source} -> {staged} (pruned)")
        _copy_pruned(source, staged)
        rewritten = _rewrite_text_files(staged, old_id, new_id)
        print(f"rewrote {old_id} -> {new_id} in {rewritten} text files")

        new_project = load_project_json(
            (staged / "project.json").read_bytes(),
        )
        if new_project.project_id != new_id:
            raise SystemExit("project.json rewrite failed")
        _repoint_state_etag(staged, old_etag, project_etag(new_project))

        archive_name = f"{args.example_id}.zip"
        archive_path = examples_dir / archive_name
        print(f"zipping -> {archive_path}")
        _zip_directory(staged, archive_path)

    _update_manifest(
        examples_dir,
        example_id=args.example_id,
        title=args.title,
        description=args.description,
        project_id=new_id,
        archive_name=archive_name,
    )
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"done: {archive_path} ({size_mb:.1f} MB), projectId={new_id}")
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="source Project directory (…/project-XXXX)",
    )
    parser.add_argument(
        "--example-id",
        required=True,
        help="stable example identifier, e.g. crow-short-drama",
    )
    parser.add_argument("--title", required=True, help="card title")
    parser.add_argument(
        "--description",
        required=True,
        help="card description",
    )
    parser.add_argument(
        "--examples-dir",
        default=str(BACKEND_ROOT / "examples"),
        help="output directory (default: backend/examples)",
    )
    build(parser.parse_args())


if __name__ == "__main__":
    main()
