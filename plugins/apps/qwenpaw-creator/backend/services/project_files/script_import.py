# -*- coding: utf-8 -*-
"""剧本直登：把写好的剧本正文直接落成 timeline_script 版本，不经素材理解。

用户自带的剧本此前只被当作普通 Source：文档要逐页 VLM 观察，正文还要模型
把整份剧本重新抄进工具参数，一次导入能耗到小时级。本模块提供一条一等通
道——正文按 UTF-8 读取、归一化到约定块格式、以内容寻址写入该 Timeline 的
``timeline_script`` ArtifactVersion。两个入口共用同一实现：

- :func:`publish_script_import`：HTTP 直登（前端剧本页导入）；
- ``AgentProjectTools.import_script_asset``：主 Agent 把本轮已入库的文本
  或 DOCX/PDF 文档素材直接登记为剧本，不创建 ProjectSource，因此不会
  触发 ``source_intelligence_agent`` 委派。

DOCX/PDF 的正文抽取在 :mod:`services.project_files.script_document_text`：
复用文档渲染链路的文字层（不渲染页图），抽不到就显式失败，不静默登记
半份剧本。

直登写入走既有提交边界（``ProjectCommitBoundary``），因此剧本变更仍会按
``production_stage`` 的既有派生规则重新要求用户确认剧本，审阅策略也不会
被绕过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .assets import AssetAlreadyExists, AssetFileStore
from .commit import ProjectCommitBoundary
from .script_artifacts import (
    add_script_version,
    script_file_relative_uri,
    script_version_id,
    timeline_script_slot_id,
)
from .script_markdown import (
    parse_script_markdown,
    serialize_script_blocks,
)
from .store import ProjectSnapshot, ProjectStore

IMPORT_SCRIPT_TOOL_NAME = "import_script_asset"

# A hand-written script is text: 2 MiB of UTF-8 already exceeds any realistic
# feature-length draft, and the cap keeps a mis-uploaded blob out of the index.
SCRIPT_IMPORT_MAX_BYTES = 2 * 1024 * 1024

TEXT_MEDIA_KINDS = frozenset({"text"})
TEXT_MEDIA_TYPE_PREFIXES = ("text/",)
JSON_MEDIA_TYPES = frozenset({"application/json"})

_SOURCE_KIND = "script_import"
# ``auto_snapshot._SNAPSHOT_PREFIX``: rolled-back copies live in the same
# ``timelines.items`` map but never enter the work graph, so landing a script
# version on one would be invisible to the user.
_SNAPSHOT_TIMELINE_PREFIX = "snapshot:"


class ScriptImportError(RuntimeError):
    """A script import that must surface a readable reason to the caller."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "SCRIPT_IMPORT_INVALID",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(slots=True)
class ScriptImportOutcome:
    """One landed script version, plus the Project state it produced."""

    project_id: str
    timeline_id: str
    slot_id: str
    artifact_version_id: str
    file_id: str
    checksum: str
    block_count: int
    character_count: int
    generation: int
    etag: str
    replayed: bool
    review_id: str | None = None
    changed_pointers: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Camel-case payload shared by the tool result and the HTTP route."""

        return {
            "resultKind": "script_import",
            "projectId": self.project_id,
            "timelineId": self.timeline_id,
            "slotId": self.slot_id,
            "artifactVersionId": self.artifact_version_id,
            "fileId": self.file_id,
            "checksum": self.checksum,
            "blockCount": self.block_count,
            "characterCount": self.character_count,
            "generation": self.generation,
            "etag": self.etag,
            "replayed": self.replayed,
            "reviewId": self.review_id,
            "changedPointers": list(self.changed_pointers),
            "summary": self.summary_text(),
        }

    def summary_text(self) -> str:
        if self.replayed:
            return (
                f"该正文与当前已登记剧本内容一致，未产生新版本"
                f"（{self.block_count} 块 / {self.character_count} 字）。"
            )
        if self.review_id is not None:
            return (
                f"剧本正文（{self.block_count} 块 / {self.character_count} 字）"
                f"已提交待审阅，通过后成为该集剧本版本。"
            )
        return (
            f"剧本正文（{self.block_count} 块 / {self.character_count} 字）"
            f"已直接登记为该集剧本版本，未经过素材理解。"
        )


def normalize_script_text(raw_text: str) -> tuple[str, int]:
    """归一化到约定块格式并返回 ``(markdown, block_count)``。

    解析/序列化双向无损，所以前端块级渲染与块级 diff 得到与模型起草
    同一套基线；不做任何内容改写。
    """

    if not raw_text.strip():
        raise ScriptImportError(
            "剧本正文为空，无法登记为剧本版本。",
            code="SCRIPT_IMPORT_EMPTY",
        )
    markdown_text = serialize_script_blocks(parse_script_markdown(raw_text))
    if not markdown_text.strip():
        raise ScriptImportError(
            "剧本正文没有可解析的内容，无法登记为剧本版本。",
            code="SCRIPT_IMPORT_EMPTY",
        )
    blocks = parse_script_markdown(markdown_text)
    return markdown_text, len(blocks)


def script_import_file_id(project_id: str, checksum: str) -> str:
    """内容寻址的剧本文件 ID：同一内容重复导入不会新增文件。"""

    digest = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:script-import:{project_id}:{checksum}",
    ).hex
    return f"file-script-{digest}"


def is_importable_script_text(
    *,
    media_kind: str | None,
    media_type: str | None,
) -> bool:
    """Only readable UTF-8 text may land a script directly."""

    if media_kind in TEXT_MEDIA_KINDS:
        return True
    normalized = (media_type or "").split(";", 1)[0].strip().casefold()
    if not normalized:
        return False
    if normalized in JSON_MEDIA_TYPES:
        return False
    return normalized.startswith(TEXT_MEDIA_TYPE_PREFIXES)


def _script_import_identity(
    project_id: str,
    timeline_id: str,
    idempotency_key: str,
) -> str:
    """Deterministic edit-transaction identity for one explicit request key.

    Keyed on the caller-supplied idempotency key -- never on the script
    checksum -- so re-selecting previously imported content (A -> B -> A) is a
    *new* edit transaction, while a network retry carrying the same key maps
    back to the same transaction and deduplicates.
    """

    return uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:script-import-transaction:"
        f"{project_id}:{timeline_id}:{idempotency_key}",
    ).hex


def publish_script_import(
    *,
    store: ProjectStore,
    commits: ProjectCommitBoundary,
    project_id: str,
    timeline_id: str,
    raw_text: str,
    source_name: str = "",
    provenance_refs: Sequence[str] = (),
    commit_metadata: Mapping[str, Any],
    observe_snapshot: Callable[[ProjectSnapshot], None] | None = None,
    idempotency_key: str | None = None,
) -> ScriptImportOutcome:
    """Write one script markdown payload as a selected ``timeline_script``.

    ``commit_metadata`` is forwarded to :meth:`ProjectCommitBoundary.commit`
    so the caller keeps control of origin and review policy: the HTTP route
    commits as a frontend edit while the Agent tool passes its own request
    context (which may require review).

    ``idempotency_key`` identifies *this edit request*, not its content: when
    present it makes the commit transaction deterministic so a network retry
    deduplicates.  When absent every edit mints a fresh transaction, so
    re-selecting a previously imported script never reuses that script's
    already-committed transaction id.
    """

    markdown_text, block_count = normalize_script_text(raw_text)
    content = markdown_text.encode("utf-8")
    if len(content) > SCRIPT_IMPORT_MAX_BYTES:
        raise ScriptImportError(
            f"剧本正文 {len(content)} 字节超过直登上限 "
            f"{SCRIPT_IMPORT_MAX_BYTES} 字节；请按集或场次拆分后分别导入。",
            code="SCRIPT_IMPORT_TOO_LARGE",
        )
    checksum = sha256(content).hexdigest()
    version_id = script_version_id(timeline_id, checksum)
    file_id = script_import_file_id(project_id, checksum)
    relative_uri = script_file_relative_uri(file_id)
    now = datetime.now(UTC)
    file_store = AssetFileStore(store.project_root(project_id))

    with store.lifecycle_lock(project_id):
        base = store.read(project_id)
        project = base.project
        if timeline_id.startswith(_SNAPSHOT_TIMELINE_PREFIX):
            raise ScriptImportError(
                "snapshot: 开头的时间线是用户回滚留下的历史副本，不能把剧本"
                "直登写在它上面；请传当前正在创作的真实 Timeline ID。",
                code="SCRIPT_IMPORT_SNAPSHOT_TIMELINE",
            )
        timeline = project.timelines.items.get(timeline_id)
        if timeline is None:
            available = ", ".join(project.timelines.order[:12]) or "（无）"
            raise ScriptImportError(
                f"timeline 不存在: {timeline_id}；当前时间线：{available}",
                code="SCRIPT_IMPORT_TIMELINE_NOT_FOUND",
            )
        slot_id = timeline_script_slot_id(timeline_id)
        slot = project.assets.artifact_slots_by_id.get(slot_id)
        if (
            slot is not None
            and slot.selected_version_id == version_id
            and version_id in slot.version_ids
        ):
            # Same content already selected: replay without a no-op commit.
            # ``fileId`` must be the indexed file the selected version points
            # at, never the artifact-version id: callers key file reads off
            # ``assets.files_by_id``, whose keys are file ids, and a version
            # id is not one of them (it would also duplicate
            # ``artifactVersionId``).
            selected = project.assets.artifact_versions_by_id.get(version_id)
            if (
                selected is None
                or selected.file_id not in project.assets.files_by_id
            ):
                raise ScriptImportError(
                    "剧本槽位选中的版本或文件索引缺失，无法复放该正文；请重新导入。",
                    code="SCRIPT_IMPORT_SLOT_CONFLICT",
                )
            return ScriptImportOutcome(
                project_id=project_id,
                timeline_id=timeline_id,
                slot_id=slot.slot_id,
                artifact_version_id=version_id,
                file_id=selected.file_id,
                checksum=checksum,
                block_count=block_count,
                character_count=len(markdown_text),
                generation=base.generation,
                etag=base.etag,
                replayed=True,
            )

        working = project.model_copy(deep=True)
        try:
            version = add_script_version(
                working,
                timeline_id,
                markdown_text,
                file_id=file_id,
                checksum=checksum,
                name=_version_name(
                    timeline_title=timeline.title,
                    timeline_id=timeline_id,
                    source_name=source_name,
                ),
                now=now,
                based_on_generation=base.generation,
                provenance_refs=list(provenance_refs),
                input_fingerprint=None,
            )
        except ValueError as error:
            raise ScriptImportError(
                f"剧本槽位不可用：{error}",
                code="SCRIPT_IMPORT_SLOT_CONFLICT",
            ) from error
        version.metadata = {
            **version.metadata,
            "source": _SOURCE_KIND,
            "importedFromName": source_name[:160],
        }
        indexed = working.assets.files_by_id[version.file_id]

        if version.file_id == file_id:
            staged = file_store.stage_bytes(
                content,
                staging_id=f"script-import-{file_id[:48]}",
            )
            try:
                file_store.publish(
                    staged,
                    relative_uri,
                    expected_sha256=checksum,
                    expected_size_bytes=len(content),
                )
            except AssetAlreadyExists:
                file_store.abandon(staged)
                if not file_store.inspect(indexed).available:
                    raise

        metadata: dict[str, Any] = {
            **dict(commit_metadata),
            "_lifecycle_lock_held": True,
        }
        # Transaction identity is per *edit*, not per content.  Re-selecting a
        # previously imported script (A -> B -> A) is a new edit and must not
        # reuse A's already-COMMITTED transaction id, or the commit boundary
        # rejects it as a duplicate.  Only an explicit request idempotency key
        # makes the transaction deterministic (so a network retry dedupes);
        # without one the boundary mints a fresh transaction per edit.
        if idempotency_key:
            operation_id = _script_import_identity(
                project_id,
                timeline_id,
                idempotency_key,
            )
            metadata.setdefault(
                "transaction_id",
                f"transaction-{operation_id}",
            )
            metadata.setdefault(
                "round_id",
                f"round-script-import-{operation_id}",
            )
        commit = commits.commit(
            base=base,
            candidate=working.model_dump(mode="json"),
            **metadata,
        )
        if observe_snapshot is not None:
            observe_snapshot(commit.snapshot)
        outcome = ScriptImportOutcome(
            project_id=project_id,
            timeline_id=timeline_id,
            slot_id=version.slot_id,
            artifact_version_id=version.version_id,
            file_id=version.file_id,
            checksum=checksum,
            block_count=block_count,
            character_count=len(markdown_text),
            generation=commit.snapshot.generation,
            etag=commit.snapshot.etag,
            replayed=False,
            review_id=(
                commit.review.review_id if commit.review is not None else None
            ),
            changed_pointers=[
                change.json_pointer
                for change in commit.changeset.changes
                if change.json_pointer is not None
            ],
        )
    return outcome


def _version_name(
    *,
    timeline_title: str,
    timeline_id: str,
    source_name: str,
) -> str:
    title = (timeline_title or "").strip() or timeline_id
    origin = (source_name or "").strip()
    label = f"剧本 · {title} · {origin}" if origin else f"剧本 · {title}"
    return label[:160]


def import_script_tool_manifest() -> dict[str, Any]:
    """Mainline-only tool: land an uploaded script text as this turn's 剧本."""

    return {
        "type": "function",
        "function": {
            "name": IMPORT_SCRIPT_TOOL_NAME,
            "description": (
                "把本轮已入库的**剧本素材**（.md/.txt 等 text 素材、用户"
                "粘贴的长文本附件，或 DOCX/PDF 文档剧本）直接登记为指定 "
                "Timeline 的 timeline_script 正文。服务端按文件索引读取并"
                "落版本（文档由服务端抽出文字层），你不需要、也不要把剧本"
                "正文重抄进参数。用于用户自带剧本/文稿：调用本工具即完成"
                "剧本登记，不需要为该素材创建 ProjectSource，也不要委派 "
                "source_intelligence_agent 理解它，更不要重新起草覆盖它。"
                "图片/视频/音频素材不适用本工具；DOCX/PDF 抽不出文字层"
                "（扫描件）时本工具会报错，那时才按文档规则处理。返回落"
                "版本的时间线、ArtifactVersion 与块数/字数，并在剧本待"
                "审阅时给出审阅 ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "projectId": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "timelineId": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "目标 Timeline 的真实 ID（如 timeline:main），"
                            "不要用 targetRef 形式或名称。"
                        ),
                    },
                    "assetVersionId": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "已入库剧本素材（文本或 DOCX/PDF）的 source "
                            "asset version ID。"
                        ),
                    },
                },
                "required": ["projectId", "timelineId", "assetVersionId"],
                "additionalProperties": False,
            },
        },
    }


__all__ = [
    "IMPORT_SCRIPT_TOOL_NAME",
    "SCRIPT_IMPORT_MAX_BYTES",
    "ScriptImportError",
    "ScriptImportOutcome",
    "import_script_tool_manifest",
    "is_importable_script_text",
    "normalize_script_text",
    "publish_script_import",
    "script_import_file_id",
]
