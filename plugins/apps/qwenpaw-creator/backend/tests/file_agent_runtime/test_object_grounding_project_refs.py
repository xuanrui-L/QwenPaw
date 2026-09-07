# -*- coding: utf-8 -*-
"""Generated images retain indexed MIME types and exact project scope."""

import hashlib
from types import SimpleNamespace

import pytest

from services.file_agent_runtime.driver import (
    FileAgentRuntimeError,
    FileCreatorAgentRuntime,
)
from services.project_files.models import ArtifactVersion, IndexedFile, Project

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "image_ref",
    [
        "artifact-version:artifact-version-test",
        "artifact-version-test",
        "artifact://artifact-version-test",
        "artifact://slot-test@artifact-version-test",
    ],
)
def test_generated_image_refs_read_verified_indexed_payload(
    tmp_path,
    image_ref,
):
    content = b"test image payload"
    checksum = hashlib.sha256(content).hexdigest()
    image_file = tmp_path / "assets/artifacts/image.png"
    image_file.parent.mkdir(parents=True)
    image_file.write_bytes(content)
    project = Project.new(project_id="project-test", name="test")
    indexed = IndexedFile(
        file_id="file-test",
        kind="artifact_payload",
        media_type="image/png",
        relative_uri="assets/artifacts/image.png",
        sha256=checksum,
        size_bytes=len(content),
        created_at="2026-09-07T00:00:00Z",
    )
    project.assets.files_by_id[indexed.file_id] = indexed
    project.assets.artifact_versions_by_id[
        "artifact-version-test"
    ] = ArtifactVersion(
        version_id="artifact-version-test",
        name="generated image",
        slot_id="slot-test",
        kind="visual_asset_image",
        owner_ref="asset:character-test",
        file_id=indexed.file_id,
        checksum=checksum,
        input_fingerprint="sha256:" + "1" * 64,
        based_on_generation=1,
        created_at="2026-09-07T00:00:00Z",
    )
    runtime = SimpleNamespace(
        services=SimpleNamespace(
            projects=SimpleNamespace(
                read=lambda project_id: SimpleNamespace(project=project),
                project_root=lambda project_id: tmp_path,
            ),
        ),
    )

    def read(ref):
        # pylint: disable-next=protected-access
        return FileCreatorAgentRuntime._read_object_grounding_project_image(
            runtime,
            project.project_id,
            ref,
        )

    assert read(image_ref) == (content, None)
    # Parsing shorthand must not bypass lookup in the current Project.
    with pytest.raises(FileAgentRuntimeError, match="does not exist"):
        read("artifact-version-other-project")
    indexed.media_type = "video/mp4"
    with pytest.raises(FileAgentRuntimeError, match="not an image"):
        read(image_ref)
    indexed.media_type = "image/png"
    indexed.sha256 = "0" * 64
    with pytest.raises(FileAgentRuntimeError, match="checksum does not match"):
        read(image_ref)
