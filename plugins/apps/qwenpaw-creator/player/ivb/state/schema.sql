-- IVB 放映端状态层。规范见 docs/er-diagram.md §3。
--
-- 契约:
-- * 隔离键是 (user_id, project_id)。project_id 即 Creator 的 meta.bundle_id,
--   全局唯一;状态层统一叫 project_id(包格式层仍叫 bundle_id,两层勿混)。
-- * user_id 由上游随请求传入(本服务不鉴权),TEXT 类型;开发期缺省落哨兵值。
-- * 不建 users / tokens(无鉴权)、不建 variables(内容层无字段消费它)。
-- * 清进度必须是真 DELETE。
-- * projects 是目录表(上传登记),与状态四表同库;它按 project_id 唯一,不分用户。
-- * 本次改名(bundle_id→project_id、user_id→TEXT)是破坏性的:开发期旧库直接
--   删除重建,不做迁移。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS progress (
    user_id          TEXT    NOT NULL,
    project_id       TEXT    NOT NULL,
    current_timeline TEXT    NOT NULL DEFAULT '',
    started_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (user_id, project_id)
);

-- 走过的每一个节点。choice_edge 是"进入本节点所走的边",入口节点为 NULL。
CREATE TABLE IF NOT EXISTS visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL,
    project_id      TEXT    NOT NULL,
    timeline_id     TEXT    NOT NULL,
    choice_edge     TEXT,
    watched_seconds REAL    NOT NULL DEFAULT 0,
    entered_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_lookup
    ON visits (user_id, project_id, timeline_id);

-- 已解锁结局。timeline_id 必须是内容层 nodes 中 is_ending 为真的节点。
CREATE TABLE IF NOT EXISTS endings (
    user_id     TEXT    NOT NULL,
    project_id  TEXT    NOT NULL,
    timeline_id TEXT    NOT NULL,
    unlocked_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, project_id, timeline_id)
);

-- 抉择分布。interaction_source + edge_ref 定位"哪道题选了哪个"。
CREATE TABLE IF NOT EXISTS choice_stats (
    user_id            TEXT    NOT NULL,
    project_id         TEXT    NOT NULL,
    interaction_source TEXT    NOT NULL,
    edge_ref           TEXT    NOT NULL,
    count              INTEGER NOT NULL DEFAULT 0,
    last_chosen_at     INTEGER NOT NULL,
    PRIMARY KEY (user_id, project_id, interaction_source, edge_ref)
);

-- 目录表:上传过的项目登记于此,供库页列表浏览。project_id 全局唯一
-- (= Creator meta.bundle_id);owner_user_id 是首次上传者,重复上传不改 owner
-- (见 docs/service-design.md §1.5)。时间戳用 INTEGER(SQLite 即 64 位 epoch)。
CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT    PRIMARY KEY,
    owner_user_id     TEXT    NOT NULL,
    title             TEXT    NOT NULL DEFAULT '',
    synopsis          TEXT    NOT NULL DEFAULT '',
    node_count        INTEGER NOT NULL DEFAULT 0,
    ending_count      INTEGER NOT NULL DEFAULT 0,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    storage_path      TEXT    NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_projects_owner
    ON projects (owner_user_id, updated_at);
