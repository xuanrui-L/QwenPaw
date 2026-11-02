# -*- coding: utf-8 -*-
# pylint: disable=protected-access,use-implicit-booleaness-not-comparison
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from services.project_files import (
    Project,
    ProjectAlreadyExists,
    ProjectConflict,
    ProjectIntegrityError,
    ProjectNotFound,
    ProjectStore,
    UnsafeProjectPath,
)
from services.project_files import store as store_module

pytestmark = pytest.mark.unit


def _project(project_id: str, name: str = "Project") -> Project:
    return Project.new(
        project_id=project_id,
        name=name,
        now=datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc),
    )


def _next(project: Project, *, name: str = "Updated") -> Project:
    raw = project.model_dump(mode="python")
    raw["generation"] = project.generation + 1
    raw["updated_at"] = project.updated_at + timedelta(seconds=1)
    raw["name"] = name
    return Project.model_validate(raw)


def test_create_read_list_replace_and_delete(tmp_path):
    store = ProjectStore(tmp_path.resolve())
    created = store.create(_project("project-b", "B"))
    store.create(_project("project-a", "A"))

    assert created.generation == 0
    assert created.etag.startswith("sha256:")
    assert store.project_path("project-b").is_file()
    assert (store.project_root("project-b") / "assets").is_dir()
    assert (
        store.project_root("project-b") / "observability" / "logs"
    ).is_dir()
    assert (
        store.project_root("project-b") / "observability" / "traces"
    ).is_dir()
    assert (store.project_root("project-b") / "runtime" / "temp").is_dir()
    assert store.read("project-b") == created
    assert [item.project_id for item in store.list()] == [
        "project-a",
        "project-b",
    ]

    replacement = _next(created.project)
    updated = store.replace(
        "project-b",
        replacement,
        expected_etag=created.etag,
    )
    assert updated.generation == 1
    assert updated.project.name == "Updated"
    assert updated.etag != created.etag

    store.delete("project-b", expected_etag=updated.etag)
    with pytest.raises(ProjectNotFound):
        store.read("project-b")
    assert [item.project_id for item in store.list()] == ["project-a"]


def test_delete_returns_after_atomic_hide_without_waiting_for_tree_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    store = ProjectStore(tmp_path.resolve())
    store.create(_project("project-fast-delete", "Delete"))
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    real_rmtree = store_module.shutil.rmtree

    def blocking_cleanup(path, *args, **kwargs):
        cleanup_started.set()
        release_cleanup.wait(timeout=5)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(store_module.shutil, "rmtree", blocking_cleanup)
    store.delete("project-fast-delete")

    assert cleanup_started.wait(timeout=1)
    assert not store.project_root("project-fast-delete").exists()
    release_cleanup.set()


def test_create_is_exclusive_and_does_not_overwrite(tmp_path):
    store = ProjectStore(tmp_path.resolve())
    first = store.create(_project("project-1", "First"))

    with pytest.raises(ProjectAlreadyExists):
        store.create(_project("project-1", "Second"))

    assert store.read("project-1") == first


def test_create_failure_never_exposes_a_partial_project(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path.resolve())
    original = store._atomic_write

    def fail_after_staging(project_root, payload):
        original(project_root, payload)
        raise OSError("injected staging failure")

    monkeypatch.setattr(store, "_atomic_write", fail_after_staging)
    with pytest.raises(OSError, match="injected staging failure"):
        store.create(_project("project-1"))

    assert not store.project_root("project-1").exists()
    assert list((tmp_path / ".staging").iterdir()) == []


def test_replace_and_delete_enforce_etag_and_generation(tmp_path):
    store = ProjectStore(tmp_path.resolve())
    current = store.create(_project("project-1"))
    next_project = _next(current.project)

    with pytest.raises(ProjectConflict, match="ETag"):
        store.replace(
            "project-1",
            next_project,
            expected_etag="sha256:" + "0" * 64,
        )
    with pytest.raises(ProjectConflict, match="generation"):
        store.replace(
            "project-1",
            current.project.model_copy(update={"name": "bad"}),
            expected_etag=current.etag,
        )
    with pytest.raises(ProjectConflict, match="ETag"):
        store.delete("project-1", expected_etag="sha256:" + "0" * 64)

    assert store.read("project-1") == current


@pytest.mark.parametrize(
    "project_id",
    [
        "../escape",
        "nested/project",
        "..",
        "has:colon",
    ],
)
def test_project_ids_cannot_escape_the_fixed_root(tmp_path, project_id):
    store = ProjectStore(tmp_path.resolve())

    with pytest.raises(UnsafeProjectPath):
        store.project_path(project_id)


def test_read_rejects_project_directory_links(tmp_path, directory_link):
    store = ProjectStore((tmp_path / "store").resolve())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "project.json").write_text("{}", encoding="utf-8")
    directory_link(outside, store.root / "project-link")

    with pytest.raises(UnsafeProjectPath):
        store.read("project-link")


def test_read_rejects_project_file_symlinks(tmp_path, file_symlink):
    store = ProjectStore((tmp_path / "store").resolve())
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    store.create(_project("project-real"))
    target = store.project_path("project-real")
    target.unlink()
    file_symlink(outside, target)
    with pytest.raises(ProjectIntegrityError, match="non-symlink"):
        store.read("project-real")


def test_read_rejects_indexed_asset_symlink_escape(tmp_path, directory_link):
    store = ProjectStore((tmp_path / "store").resolve())
    project = store.create(_project("project-1")).project
    project_root = store.project_root("project-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    directory_link(outside, project_root / "assets" / "escape")

    raw = project.model_dump(mode="json")
    raw["generation"] = 1
    raw["updated_at"] = "2026-07-15T08:00:01Z"
    raw["assets"]["files_by_id"] = {
        "file-1": {
            "file_id": "file-1",
            "kind": "other",
            "relative_uri": "assets/escape/payload.bin",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "application/octet-stream",
            "created_at": "2026-07-15T08:00:00Z",
        },
    }
    candidate = Project.model_validate(raw)

    with pytest.raises(UnsafeProjectPath, match="escapes"):
        store.replace(
            "project-1",
            candidate,
            expected_etag=store.read("project-1").etag,
        )


def test_atomic_replace_failure_preserves_old_project_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    store = ProjectStore(tmp_path.resolve())
    current = store.create(_project("project-1", "Before"))
    replacement = _next(current.project, name="After")
    real_replace = store_module.os.replace

    def fail_project_publish(source, destination):
        if str(destination).endswith("project.json"):
            raise OSError("simulated publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", fail_project_publish)
    with pytest.raises(OSError, match="simulated"):
        store.replace("project-1", replacement, expected_etag=current.etag)

    assert store.read("project-1") == current
    assert (
        list((store.project_root("project-1") / "runtime" / "temp").iterdir())
        == []
    )


def test_oversized_project_is_rejected_before_publication(tmp_path):
    store = ProjectStore(tmp_path.resolve(), max_project_json_bytes=256)

    with pytest.raises(ProjectIntegrityError, match="move large content"):
        store.create(_project("project-big", "x" * 512))
    assert not store.project_root("project-big").exists()


def test_corrupt_or_duplicate_key_project_is_not_silently_listed(tmp_path):
    store = ProjectStore(tmp_path.resolve())
    store.create(_project("project-1"))
    path = store.project_path("project-1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        '{"schema_version":1,"schema_version":1,"project_id":"project-1"}',
        encoding="utf-8",
    )

    with pytest.raises(ProjectIntegrityError):
        store.read("project-1")
    assert store.list() == []
    assert store.discover_project_ids() == ("project-1",)

    # Keep the variable used to make this test's intentional corruption clear.
    assert payload["project_id"] == "project-1"


def test_discovery_ignores_internal_incomplete_and_symlink_directories(
    tmp_path,
    directory_link,
):
    store = ProjectStore((tmp_path / "store").resolve())
    store.create(_project("project-b"))
    store.create(_project("project-a"))
    (store.root / "incomplete").mkdir()
    (store.root / ".staging" / "partial").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "project.json").write_text("{}", encoding="utf-8")
    directory_link(outside, store.root / "project-link")

    assert store.discover_project_ids() == ("project-a", "project-b")


def test_startup_gc_sweeps_legacy_locks_but_spares_project_assets(tmp_path):
    """The sweep must never reach a Project's asset tree.

    Users legitimately upload files named ``poetry.lock`` / ``Cargo.lock``
    as Project assets; only the directories the retired flock
    implementation used may be swept.
    """

    from services.project_files.facade import _startup_disk_gc

    root = tmp_path / "data"
    project = root / "project-1"
    (project / "assets" / "sub").mkdir(parents=True)
    (project / "runtime" / "locks").mkdir(parents=True)
    (root / ".locks").mkdir()
    assets = [
        project / "assets" / "poetry.lock",
        project / "assets" / "Cargo.lock",
        project / "assets" / "sub" / "yarn.lock",
    ]
    legacy = [
        root / ".locks" / "project-1.lock",
        root / ".locks" / "project-1.lock.gate",
        project / "runtime" / "locks" / "session-runtime.lock",
        project / "runtime" / ".session.json.lock",
    ]
    for path in (*assets, *legacy):
        path.write_text("x", encoding="utf-8")

    _startup_disk_gc(root)

    assert all(path.exists() for path in assets)
    assert not any(path.exists() for path in legacy)


def _publish_asset_index(
    store: ProjectStore,
    project_id: str,
    uris: list[str],
) -> None:
    """Publish one Project whose asset index points at *uris*.

    Both write and read run :meth:`ProjectStore._validate_asset_paths`, so this
    is the shape the containment rule has to accept or refuse.
    """

    current = store.read(project_id)
    raw = current.project.model_dump(mode="json")
    raw["generation"] = current.project.generation + 1
    raw["updated_at"] = "2026-07-15T08:00:01Z"
    raw["assets"]["files_by_id"] = {
        f"file-{index}": {
            "file_id": f"file-{index}",
            "kind": "other",
            "relative_uri": uri,
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "application/octet-stream",
            "created_at": "2026-07-15T08:00:00Z",
        }
        for index, uri in enumerate(uris)
    }
    store.replace(
        project_id,
        Project.model_validate(raw),
        expected_etag=current.etag,
    )


def test_asset_rule_accepts_everything_the_resolve_rule_accepted(tmp_path):
    store = ProjectStore((tmp_path / "store").resolve())
    store.create(_project("project-ok"))
    project_root = store.project_root("project-ok")
    artifacts = project_root / "assets" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "real.bin").write_bytes(b"x")

    _publish_asset_index(
        store,
        "project-ok",
        [
            "assets/artifacts/real.bin",
            "assets/artifacts/missing.bin",
            "assets/uploads/note.txt",
        ],
    )

    assert store.read("project-ok").project.generation == 1


def test_asset_rule_still_refuses_a_leaf_link_that_leaves_assets(
    tmp_path,
    file_symlink,
):
    store = ProjectStore((tmp_path / "store").resolve())
    store.create(_project("project-leaf"))
    project_root = store.project_root("project-leaf")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"y")
    link = project_root / "assets" / "leak.bin"
    file_symlink(outside, link)

    with pytest.raises(UnsafeProjectPath, match="escapes"):
        _publish_asset_index(store, "project-leaf", ["assets/leak.bin"])


def test_asset_rule_accepts_a_leaf_link_that_stays_inside_assets(
    tmp_path,
    file_symlink,
):
    store = ProjectStore((tmp_path / "store").resolve())
    store.create(_project("project-inner"))
    project_root = store.project_root("project-inner")
    artifacts = project_root / "assets" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    target = artifacts / "real.bin"
    target.write_bytes(b"x")
    file_symlink(target, project_root / "assets" / "alias.bin")

    _publish_asset_index(store, "project-inner", ["assets/alias.bin"])

    assert store.read("project-inner").project.generation == 1


def test_asset_rule_refuses_a_directory_link_above_the_leaf(
    tmp_path,
    directory_link,
):
    """The per-parent resolve must catch a redirect that is not the leaf.

    ``assets/uploads`` pointing outside the Project is exactly what the old
    per-file ``resolve()`` refused, so caching resolved parents may not soften
    it.
    """

    store = ProjectStore((tmp_path / "store").resolve())
    store.create(_project("project-dirlink"))
    project_root = store.project_root("project-dirlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.bin").write_bytes(b"y")
    directory_link(outside, project_root / "assets" / "uploads")

    with pytest.raises(UnsafeProjectPath, match="escapes"):
        _publish_asset_index(
            store,
            "project-dirlink",
            ["assets/uploads/real.bin"],
        )


@pytest.mark.parametrize(
    ("relative_uri", "expected"),
    (
        ("assets/artifacts/file.bin", ("assets", "artifacts", "file.bin")),
        (
            "assets/sub/../artifacts/file.bin",
            ("assets", "artifacts", "file.bin"),
        ),
        ("assets/./file.bin", ("assets", "file.bin")),
        ("assets", ("assets",)),
        ("x/../assets/file.bin", ("assets", "file.bin")),
    ),
)
def test_lexical_asset_parts_accepts_uris_that_return_inside(
    relative_uri,
    expected,
):
    assert store_module._lexical_asset_parts(relative_uri) == expected


@pytest.mark.parametrize(
    "relative_uri",
    ("", "/etc/passwd", "../outside", "assets/../../escape", "media/file.bin"),
)
def test_lexical_asset_parts_refuses_every_form_of_escape(relative_uri):
    with pytest.raises(UnsafeProjectPath, match="escapes"):
        store_module._lexical_asset_parts(relative_uri)
