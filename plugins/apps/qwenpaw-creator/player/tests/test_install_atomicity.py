# -*- coding: utf-8 -*-
"""Library replacement preserves ownership, playback and progress."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from ivb.server.library import ProjectLibrary
from ivb.testing import BundleSpec, write_bundle_dir
from ivb.format.reader import inspect_bundle


def test_non_owner_upload_is_rejected_without_changing_content(tmp_path):
    source = write_bundle_dir(tmp_path / "src", BundleSpec())
    library = ProjectLibrary(tmp_path / "data")
    original = library.install(source, owner_user_id="alice")
    with pytest.raises(PermissionError):
        library.install(source, owner_user_id="bob", title="hijacked")
    assert library.store.get_project(original.project_id) == original
    assert library.resolve(original.project_id).bundle.meta.title == "深夜便利店"


def test_mid_copy_failure_leaves_old_revision_and_progress(
    tmp_path,
    monkeypatch,
):
    import ivb.server.library as module

    source = write_bundle_dir(tmp_path / "src", BundleSpec())
    library = ProjectLibrary(tmp_path / "data")
    original = library.install(source, owner_user_id="alice")
    library.store.record_visit(original.project_id, "timeline:open")

    def broken(_source, dest):
        dest.mkdir()
        (dest / "partial").write_bytes(b"broken")
        raise OSError("disk full")

    monkeypatch.setattr(module, "_materialize", broken)
    with pytest.raises(OSError, match="disk full"):
        library.install(source, owner_user_id="alice")
    assert library.store.get_project(original.project_id) == original
    assert library.resolve(original.project_id).bundle is not None
    assert library.store.visited(original.project_id) == ["timeline:open"]
    assert not list((tmp_path / "data/bundles").glob(".stage-*"))


def test_new_revision_clears_progress_for_all_users_but_replay_preserves_it(
    tmp_path,
):
    source = write_bundle_dir(tmp_path / "src", BundleSpec())
    library = ProjectLibrary(tmp_path / "data")
    first = library.install(source, owner_user_id="alice")
    for user in ("alice", "bob"):
        library.store_for(user).record_visit(first.project_id, "timeline:open")
    same = library.install(source, owner_user_id="alice")
    assert same.storage_path == first.storage_path
    assert library.store_for("bob").visited(first.project_id)
    manifest = source / "manifest.json"
    content = json.loads(manifest.read_text())
    content["meta"]["title"] = "Second revision"
    manifest.write_text(json.dumps(content))
    second = library.install(source, owner_user_id="alice")
    assert second.storage_path != first.storage_path
    assert (library.data_dir / first.storage_path / "manifest.json").exists()
    assert (
        library.resolve(first.project_id).bundle.meta.title
        == "Second revision"
    )
    for user in ("alice", "bob"):
        assert not library.store_for(user).visited(first.project_id)
        assert (
            not library.store_for(user)
            .progress(first.project_id)
            .current_timeline
        )


def test_concurrent_first_install_has_one_owner(tmp_path):
    source = write_bundle_dir(tmp_path / "src", BundleSpec())
    libraries = [ProjectLibrary(tmp_path / "data") for _ in range(2)]

    def attempt(index):
        try:
            return (
                libraries[index]
                .install(source, owner_user_id=("alice", "bob")[index])
                .owner_user_id
            )
        except PermissionError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert len([result for result in results if result]) == 1


def test_duplicate_zip_and_segment_paths_are_fatal(tmp_path):
    import zipfile

    source = write_bundle_dir(tmp_path / "src", BundleSpec())
    target = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(target, "w") as archive:
        for file in source.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(source))
        archive.writestr(
            "manifest.json",
            (source / "manifest.json").read_bytes(),
        )
    result = inspect_bundle(target)
    assert any(item.code == "DUPLICATE_MEMBER" for item in result.fatal)
    path = source / "manifest.json"
    raw = json.loads(path.read_text())
    ids = list(raw["segments"])
    raw["segments"][ids[1]] = raw["segments"][ids[0]]
    path.write_text(json.dumps(raw))
    assert any(
        item.code == "DUPLICATE_MEMBER"
        for item in inspect_bundle(source).fatal
    )


@pytest.mark.parametrize(
    "name",
    ["interaction_html.py", "presentation_html.py"],
)
def test_html_validator_matches_creator_source(name):
    player = Path(__file__).resolve().parents[1]
    assert (player / "ivb/format" / name).read_bytes() == (
        player.parent / "backend/services/project_files" / name
    ).read_bytes()
