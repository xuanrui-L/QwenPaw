# -*- coding: utf-8 -*-
# pylint: disable=confusing-with-statement
"""状态层:SQLite 持久化观看路径与结局。

一次操作一个连接:放映服务是单写者进程(见 docs/service-design.md §7),没有
长连接收益,反而避免 FastAPI 线程池共享连接导致的 ``sqlite3.ProgrammingError``。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

#: 开发期缺省用户(哨兵)。真实 user_id 由上游随请求传入,TEXT;本服务不鉴权。
ANONYMOUS_USER_ID = "anonymous"

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
#: 状态库自身的版本(与包格式版本无关)。改动即 +1 并在此处补迁移分支。
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Progress:
    project_id: str
    current_timeline: str
    started_at: int
    updated_at: int
    visited: tuple[str, ...]
    endings: tuple[str, ...]
    total_nodes: int = 0
    total_endings: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "current_timeline": self.current_timeline,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "visited": list(self.visited),
            "endings": list(self.endings),
            "total_nodes": self.total_nodes,
            "total_endings": self.total_endings,
        }


def _now() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """目录表 ``projects`` 的一行。上传时登记,库页列表读取。

    ``created_at`` / ``updated_at`` 是读出时由 DB 填充的服务端时间戳;写入
    (:meth:`ProgressStore.upsert_project`)时忽略这两个字段,一律取当前时间。
    """

    project_id: str
    owner_user_id: str
    title: str = ""
    synopsis: str = ""
    node_count: int = 0
    ending_count: int = 0
    interaction_count: int = 0
    storage_path: str = ""
    created_at: int = 0
    updated_at: int = 0

    def as_dict(self) -> dict[str, object]:
        """库页/详情端点的公开视图。刻意略去内部 ``storage_path``。"""

        return {
            "project_id": self.project_id,
            "owner_user_id": self.owner_user_id,
            "title": self.title,
            "synopsis": self.synopsis,
            "node_count": self.node_count,
            "ending_count": self.ending_count,
            "interaction_count": self.interaction_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        project_id=str(row["project_id"]),
        owner_user_id=str(row["owner_user_id"]),
        title=str(row["title"]),
        synopsis=str(row["synopsis"]),
        node_count=int(row["node_count"]),
        ending_count=int(row["ending_count"]),
        interaction_count=int(row["interaction_count"]),
        storage_path=str(row["storage_path"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


@runtime_checkable
class StateStore(Protocol):
    """状态/目录层的 DB 接缝(见 docs/service-design.md §6.6、§8)。

    所有 SQL 都关在实现类内部,上层(server/app.py)只调这里的方法。当前唯一
    实现是 :class:`ProgressStore`(SQLite);将来切 PostgreSQL 只需新增一个同样
    满足本协议的类,端点与前端不动。

    进度类方法绑定到构造时的 ``user_id``(单写者进程,一个请求一个 user 视图);
    目录类方法把 ``owner_user_id`` 作为显式参数,天然多用户就绪。
    """

    # -- 进度(绑定单个 user) ------------------------------------------
    def touch_progress(
        self,
        project_id: str,
        current_timeline: str = "",
    ) -> None:
        ...

    def record_visit(
        self,
        project_id: str,
        timeline_id: str,
        *,
        choice_edge: str | None = None,
        watched_seconds: float = 0.0,
        current: bool = True,
    ) -> None:
        ...

    def commit_watch_time(
        self,
        project_id: str,
        timeline_id: str,
        watched_seconds: float,
    ) -> int:
        ...

    def unlock_ending(self, project_id: str, timeline_id: str) -> bool:
        ...

    def record_choice(
        self,
        project_id: str,
        interaction_source: str,
        edge_ref: str,
    ) -> None:
        ...

    def clear(self, project_id: str) -> dict[str, object]:
        ...

    def visited(self, project_id: str) -> list[str]:
        ...

    def endings(self, project_id: str) -> list[str]:
        ...

    def current_timeline(self, project_id: str) -> str:
        ...

    def path_of(
        self,
        project_id: str,
        timeline_id: str,
    ) -> list[dict[str, object]]:
        ...

    def trail(self, project_id: str) -> list[dict[str, object]]:
        ...

    def choice_stats(self, project_id: str) -> list[dict[str, object]]:
        ...

    def progress(self, project_id: str) -> Progress:
        ...

    def stats(self, project_id: str) -> dict[str, object]:
        ...

    # -- 目录(owner_user_id 显式传入) --------------------------------
    def upsert_project(self, record: ProjectRecord) -> None:
        ...

    def get_project(self, project_id: str) -> ProjectRecord | None:
        ...

    def list_projects(
        self,
        owner_user_id: str | None = None,
    ) -> list[ProjectRecord]:
        ...


class ProgressStore:
    """``state.db`` 的唯一写入口。所有方法都按 (user_id, project_id) 隔离。

    这是 :class:`StateStore` 协议当前的 SQLite 实现。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        user_id: str = ANONYMOUS_USER_ID,
    ) -> None:
        self.db_path = Path(db_path)
        self.user_id = user_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # -- 连接管理 ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _bootstrap(self) -> None:
        script = _SCHEMA_PATH.read_text(encoding="utf-8")
        with closing(self._connect()) as conn, conn:
            conn.executescript(script)
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < STATE_SCHEMA_VERSION:
                conn.execute(
                    f"PRAGMA user_version = {STATE_SCHEMA_VERSION}",
                )

    # -- 写 ---------------------------------------------------------------

    @staticmethod
    def _upsert_progress(
        conn: sqlite3.Connection,
        user_id: str,
        project_id: str,
        current_timeline: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO progress
                (user_id, project_id, current_timeline, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, project_id) DO UPDATE SET
                current_timeline = excluded.current_timeline,
                updated_at       = excluded.updated_at
            """,
            (user_id, project_id, current_timeline, now, now),
        )

    def touch_progress(
        self,
        project_id: str,
        current_timeline: str = "",
    ) -> None:
        """登记/更新断点。不覆盖 ``started_at``。"""

        now = _now()
        with closing(self._connect()) as conn, conn:
            self._upsert_progress(
                conn,
                self.user_id,
                project_id,
                current_timeline,
                now,
            )

    def record_visit(
        self,
        project_id: str,
        timeline_id: str,
        *,
        choice_edge: str | None = None,
        watched_seconds: float = 0.0,
        current: bool = True,
    ) -> None:
        """记一次节点访问,并顺带把断点推到该节点。

        ``watched_seconds`` 由前端 ``timeupdate`` 累计后传入;demo 早期实现
        恒写 0,这个字段就此变死列 —— 这里要求调用方给出真实值。
        """

        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO visits
                    (user_id, project_id, timeline_id, choice_edge,
                     watched_seconds, entered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.user_id,
                    project_id,
                    timeline_id,
                    choice_edge,
                    float(watched_seconds or 0.0),
                    now,
                ),
            )
            if current:
                self._upsert_progress(
                    conn,
                    self.user_id,
                    project_id,
                    timeline_id,
                    now,
                )

    def commit_watch_time(
        self,
        project_id: str,
        timeline_id: str,
        watched_seconds: float,
    ) -> int:
        """把观看秒数回填到该节点**最近一次** visit 行。

        demo 早期实现只在进入时写 0、从不再写,导致 ``watched_seconds`` 恒空。
        累计而非覆盖:同一节点反复回访时保留真实总时长。
        """

        seconds = float(watched_seconds or 0.0)
        if seconds <= 0:
            return 0
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE visits SET watched_seconds = watched_seconds + ?
                WHERE id = (
                    SELECT id FROM visits
                    WHERE user_id = ? AND project_id = ? AND timeline_id = ?
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (seconds, self.user_id, project_id, timeline_id),
            )
            return max(cursor.rowcount, 0)

    def unlock_ending(self, project_id: str, timeline_id: str) -> bool:
        """返回 True 表示这是首次解锁(前端据此弹"新结局")。"""

        now = _now()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO endings
                    (user_id, project_id, timeline_id, unlocked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, project_id, timeline_id) DO NOTHING
                """,
                (self.user_id, project_id, timeline_id, now),
            )
            self._upsert_progress(
                conn,
                self.user_id,
                project_id,
                timeline_id,
                now,
            )
            return cursor.rowcount > 0

    def record_choice(
        self,
        project_id: str,
        interaction_source: str,
        edge_ref: str,
    ) -> None:
        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO choice_stats
                    (user_id, project_id, interaction_source, edge_ref,
                     count, last_chosen_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT (user_id, project_id, interaction_source, edge_ref)
                DO UPDATE SET
                    count = count + 1,
                    last_chosen_at = excluded.last_chosen_at
                """,
                (self.user_id, project_id, interaction_source, edge_ref, now),
            )

    def clear(self, project_id: str) -> dict[str, object]:
        """真删除。返回每张表被删掉的行数,便于前端确认确实清了。"""

        deleted: dict[str, object] = {}
        with closing(self._connect()) as conn, conn:
            for table in ("progress", "visits", "endings", "choice_stats"):
                cursor = conn.execute(
                    f"DELETE FROM {table} "
                    "WHERE user_id = ? AND project_id = ?",
                    (self.user_id, project_id),
                )
                deleted[table] = max(cursor.rowcount, 0)
        deleted["user_id"] = self.user_id
        deleted["project_id"] = project_id
        return deleted

    # -- 读 ---------------------------------------------------------------

    def visited(self, project_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id FROM visits
                WHERE user_id = ? AND project_id = ?
                GROUP BY timeline_id
                ORDER BY MIN(id)
                """,
                (self.user_id, project_id),
            ).fetchall()
        return [str(row["timeline_id"]) for row in rows]

    def endings(self, project_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id FROM endings
                WHERE user_id = ? AND project_id = ?
                ORDER BY unlocked_at
                """,
                (self.user_id, project_id),
            ).fetchall()
        return [str(row["timeline_id"]) for row in rows]

    def current_timeline(self, project_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT current_timeline FROM progress
                WHERE user_id = ? AND project_id = ?
                """,
                (self.user_id, project_id),
            ).fetchone()
        return str(row["current_timeline"]) if row else ""

    def path_of(
        self,
        project_id: str,
        timeline_id: str,
    ) -> list[dict[str, object]]:
        """进入某节点时走过的边序列 —— 结局页"回顾"用。"""

        return [
            row
            for row in self.trail(project_id)
            if str(row["timeline_id"]) == timeline_id
        ]

    def trail(self, project_id: str) -> list[dict[str, object]]:
        """完整参观顺序(含进入时所走的边)。"""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id, choice_edge, watched_seconds, entered_at
                FROM visits
                WHERE user_id = ? AND project_id = ?
                ORDER BY id
                """,
                (self.user_id, project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def choice_stats(self, project_id: str) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT interaction_source, edge_ref, count, last_chosen_at
                FROM choice_stats
                WHERE user_id = ? AND project_id = ?
                ORDER BY interaction_source, edge_ref
                """,
                (self.user_id, project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def progress(self, project_id: str) -> Progress:
        visited = self.visited(project_id)
        endings = self.endings(project_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT current_timeline, started_at, updated_at FROM progress
                WHERE user_id = ? AND project_id = ?
                """,
                (self.user_id, project_id),
            ).fetchone()
        return Progress(
            project_id=project_id,
            current_timeline=str(row["current_timeline"]) if row else "",
            started_at=int(row["started_at"]) if row else 0,
            updated_at=int(row["updated_at"]) if row else 0,
            visited=tuple(visited),
            endings=tuple(endings),
        )

    def stats(self, project_id: str) -> dict[str, object]:
        with closing(self._connect()) as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS visits,
                       COUNT(DISTINCT timeline_id) AS distinct_nodes,
                       COALESCE(SUM(watched_seconds), 0) AS watched
                FROM visits WHERE user_id = ? AND project_id = ?
                """,
                (self.user_id, project_id),
            ).fetchone()
            edges = conn.execute(
                """
                SELECT COALESCE(SUM(count), 0) AS choices FROM choice_stats
                WHERE user_id = ? AND project_id = ?
                """,
                (self.user_id, project_id),
            ).fetchone()
        return {
            "user_id": self.user_id,
            "project_id": project_id,
            "visits": int(totals["visits"]),
            "distinct_nodes": int(totals["distinct_nodes"]),
            "watched_seconds": float(totals["watched"]),
            "choices_made": int(edges["choices"]),
            "endings_unlocked": len(self.endings(project_id)),
            "state_schema_version": STATE_SCHEMA_VERSION,
        }

    # -- 目录(catalog) --------------------------------------------------

    def upsert_project(self, record: ProjectRecord) -> None:
        """登记/更新目录行。

        ``owner_user_id`` 只在首次插入时落库;重复上传(同 project_id)更新标题、
        计数、路径与 updated_at,但**保持首次上传者**(见 service-design §1.5)。
        created_at 同样只在插入时写,冲突时不动。
        """

        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO projects
                    (project_id, owner_user_id, title, synopsis,
                     node_count, ending_count, interaction_count,
                     storage_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (project_id) DO UPDATE SET
                    title             = excluded.title,
                    synopsis          = excluded.synopsis,
                    node_count        = excluded.node_count,
                    ending_count      = excluded.ending_count,
                    interaction_count = excluded.interaction_count,
                    storage_path      = excluded.storage_path,
                    updated_at        = excluded.updated_at
                """,
                (
                    record.project_id,
                    record.owner_user_id,
                    record.title,
                    record.synopsis,
                    record.node_count,
                    record.ending_count,
                    record.interaction_count,
                    record.storage_path,
                    now,
                    now,
                ),
            )

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return _row_to_project(row) if row else None

    def list_projects(
        self,
        owner_user_id: str | None = None,
    ) -> list[ProjectRecord]:
        """``owner_user_id`` 为 None 时返回全部;否则只返回该用户的(库页"我的")。"""

        with closing(self._connect()) as conn:
            if owner_user_id is None:
                rows = conn.execute(
                    "SELECT * FROM projects ORDER BY updated_at DESC",
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM projects
                    WHERE owner_user_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
        return [_row_to_project(row) for row in rows]


__all__ = [
    "ANONYMOUS_USER_ID",
    "STATE_SCHEMA_VERSION",
    "Progress",
    "ProgressStore",
    "ProjectRecord",
    "StateStore",
]
