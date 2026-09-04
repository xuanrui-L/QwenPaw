-- IVB 放映端状态层。规范见 docs/er-diagram.md §3。
--
-- 契约:
-- * user_id 恒为 1(不做鉴权),但列保留 —— 将来补多用户只加回鉴权,不迁库。
-- * 不建 users / tokens(无鉴权)、不建 variables(内容层无字段消费它)。
-- * 清进度必须是真 DELETE。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS progress (
    user_id          INTEGER NOT NULL DEFAULT 1,
    bundle_id        TEXT    NOT NULL,
    current_timeline TEXT    NOT NULL DEFAULT '',
    started_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (user_id, bundle_id)
);

-- 走过的每一个节点。choice_edge 是"进入本节点所走的边",入口节点为 NULL。
CREATE TABLE IF NOT EXISTS visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL DEFAULT 1,
    bundle_id       TEXT    NOT NULL,
    timeline_id     TEXT    NOT NULL,
    choice_edge     TEXT,
    watched_seconds REAL    NOT NULL DEFAULT 0,
    entered_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_lookup
    ON visits (user_id, bundle_id, timeline_id);

-- 已解锁结局。timeline_id 必须是内容层 nodes 中 is_ending 为真的节点。
CREATE TABLE IF NOT EXISTS endings (
    user_id     INTEGER NOT NULL DEFAULT 1,
    bundle_id   TEXT    NOT NULL,
    timeline_id TEXT    NOT NULL,
    unlocked_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, bundle_id, timeline_id)
);

-- 抉择分布。interaction_source + edge_ref 定位"哪道题选了哪个"。
CREATE TABLE IF NOT EXISTS choice_stats (
    user_id          INTEGER NOT NULL DEFAULT 1,
    bundle_id        TEXT    NOT NULL,
    interaction_source TEXT NOT NULL,
    edge_ref         TEXT    NOT NULL,
    count            INTEGER NOT NULL DEFAULT 0,
    last_chosen_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, bundle_id, interaction_source, edge_ref)
);
