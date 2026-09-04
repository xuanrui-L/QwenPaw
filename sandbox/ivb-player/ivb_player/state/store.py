# -*- coding: utf-8 -*-
"""状态层:SQLite 持久化观看路径与结局。

一次操作一个连接:放映服务是本地单用户进程,没有长连接收益,反而避免
FastAPI 线程池共享连接导致的 ``sqlite3.ProgrammingError``。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

#: v1 不鉴权,所有写入都归属这个哨兵用户。列保留是为了将来加回鉴权不迁库。
ANONYMOUS_USER_ID = 1

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
#: 状态库自身的版本(与包格式版本无关)。改动即 +1 并在此处补迁移分支。
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Progress:
    bundle_id: str
    current_timeline: str
    started_at: int
    updated_at: int
    visited: tuple[str, ...]
    endings: tuple[str, ...]
    total_nodes: int = 0
    total_endings: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
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


class ProgressStore:
    """``state.db`` 的唯一写入口。所有方法都按 (user_id, bundle_id) 隔离。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        user_id: int = ANONYMOUS_USER_ID,
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
        user_id: int,
        bundle_id: str,
        current_timeline: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO progress
                (user_id, bundle_id, current_timeline, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, bundle_id) DO UPDATE SET
                current_timeline = excluded.current_timeline,
                updated_at       = excluded.updated_at
            """,
            (user_id, bundle_id, current_timeline, now, now),
        )

    def touch_progress(
        self, bundle_id: str, current_timeline: str = ""
    ) -> None:
        """登记/更新断点。不覆盖 ``started_at``。"""

        now = _now()
        with closing(self._connect()) as conn, conn:
            self._upsert_progress(
                conn, self.user_id, bundle_id, current_timeline, now
            )

    def record_visit(
        self,
        bundle_id: str,
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
                    (user_id, bundle_id, timeline_id, choice_edge,
                     watched_seconds, entered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.user_id,
                    bundle_id,
                    timeline_id,
                    choice_edge,
                    float(watched_seconds or 0.0),
                    now,
                ),
            )
            if current:
                self._upsert_progress(
                    conn, self.user_id, bundle_id, timeline_id, now
                )

    def commit_watch_time(
        self,
        bundle_id: str,
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
                    WHERE user_id = ? AND bundle_id = ? AND timeline_id = ?
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (seconds, self.user_id, bundle_id, timeline_id),
            )
            return max(cursor.rowcount, 0)

    def unlock_ending(self, bundle_id: str, timeline_id: str) -> bool:
        """返回 True 表示这是首次解锁(前端据此弹"新结局")。"""

        now = _now()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO endings
                    (user_id, bundle_id, timeline_id, unlocked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, bundle_id, timeline_id) DO NOTHING
                """,
                (self.user_id, bundle_id, timeline_id, now),
            )
            self._upsert_progress(
                conn, self.user_id, bundle_id, timeline_id, now
            )
            return cursor.rowcount > 0

    def record_choice(
        self,
        bundle_id: str,
        interaction_source: str,
        edge_ref: str,
    ) -> None:
        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO choice_stats
                    (user_id, bundle_id, interaction_source, edge_ref,
                     count, last_chosen_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT (user_id, bundle_id, interaction_source, edge_ref)
                DO UPDATE SET
                    count = count + 1,
                    last_chosen_at = excluded.last_chosen_at
                """,
                (self.user_id, bundle_id, interaction_source, edge_ref, now),
            )

    def clear(self, bundle_id: str) -> dict[str, int]:
        """真删除。返回每张表被删掉的行数,便于前端确认确实清了。"""

        deleted: dict[str, int] = {}
        with closing(self._connect()) as conn, conn:
            for table in ("progress", "visits", "endings", "choice_stats"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE user_id = ? AND bundle_id = ?",
                    (self.user_id, bundle_id),
                )
                deleted[table] = max(cursor.rowcount, 0)
        deleted["user_id"] = self.user_id
        deleted["bundle_id"] = bundle_id
        return deleted

    # -- 读 ---------------------------------------------------------------

    def visited(self, bundle_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id FROM visits
                WHERE user_id = ? AND bundle_id = ?
                GROUP BY timeline_id
                ORDER BY MIN(id)
                """,
                (self.user_id, bundle_id),
            ).fetchall()
        return [str(row["timeline_id"]) for row in rows]

    def endings(self, bundle_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id FROM endings
                WHERE user_id = ? AND bundle_id = ?
                ORDER BY unlocked_at
                """,
                (self.user_id, bundle_id),
            ).fetchall()
        return [str(row["timeline_id"]) for row in rows]

    def current_timeline(self, bundle_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT current_timeline FROM progress
                WHERE user_id = ? AND bundle_id = ?
                """,
                (self.user_id, bundle_id),
            ).fetchone()
        return str(row["current_timeline"]) if row else ""

    def path_of(
        self, bundle_id: str, timeline_id: str
    ) -> list[dict[str, object]]:
        """进入某节点时走过的边序列 —— 结局页"回顾"用。"""

        return [
            row
            for row in self.trail(bundle_id)
            if str(row["timeline_id"]) == timeline_id
        ]

    def trail(self, bundle_id: str) -> list[dict[str, object]]:
        """完整参观顺序(含进入时所走的边)。"""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timeline_id, choice_edge, watched_seconds, entered_at
                FROM visits
                WHERE user_id = ? AND bundle_id = ?
                ORDER BY id
                """,
                (self.user_id, bundle_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def choice_stats(self, bundle_id: str) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT interaction_source, edge_ref, count, last_chosen_at
                FROM choice_stats
                WHERE user_id = ? AND bundle_id = ?
                ORDER BY interaction_source, edge_ref
                """,
                (self.user_id, bundle_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def progress(self, bundle_id: str) -> Progress:
        visited = self.visited(bundle_id)
        endings = self.endings(bundle_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT current_timeline, started_at, updated_at FROM progress
                WHERE user_id = ? AND bundle_id = ?
                """,
                (self.user_id, bundle_id),
            ).fetchone()
        return Progress(
            bundle_id=bundle_id,
            current_timeline=str(row["current_timeline"]) if row else "",
            started_at=int(row["started_at"]) if row else 0,
            updated_at=int(row["updated_at"]) if row else 0,
            visited=tuple(visited),
            endings=tuple(endings),
        )

    def stats(self, bundle_id: str) -> dict[str, object]:
        with closing(self._connect()) as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS visits,
                       COUNT(DISTINCT timeline_id) AS distinct_nodes,
                       COALESCE(SUM(watched_seconds), 0) AS watched
                FROM visits WHERE user_id = ? AND bundle_id = ?
                """,
                (self.user_id, bundle_id),
            ).fetchone()
            edges = conn.execute(
                """
                SELECT COALESCE(SUM(count), 0) AS choices FROM choice_stats
                WHERE user_id = ? AND bundle_id = ?
                """,
                (self.user_id, bundle_id),
            ).fetchone()
        return {
            "user_id": self.user_id,
            "bundle_id": bundle_id,
            "visits": int(totals["visits"]),
            "distinct_nodes": int(totals["distinct_nodes"]),
            "watched_seconds": float(totals["watched"]),
            "choices_made": int(edges["choices"]),
            "endings_unlocked": len(self.endings(bundle_id)),
            "state_schema_version": STATE_SCHEMA_VERSION,
        }


__all__ = [
    "ANONYMOUS_USER_ID",
    "STATE_SCHEMA_VERSION",
    "Progress",
    "ProgressStore",
]
