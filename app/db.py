"""SQLite 存储层。

版本、迁移结果、审阅依赖、完成记录与门禁策略修订均只追加（append-only）：
不提供任何 UPDATE/DELETE 路径，人工确认以新行追加到 confirmations 表，
同名门禁策略的每次提交都以递增 revision 追加到 policy_revisions 表。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    number      INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, number)
);

CREATE TABLE IF NOT EXISTS annotations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES documents (id),
    body               TEXT NOT NULL,
    selection          TEXT NOT NULL,
    left_context       TEXT NOT NULL DEFAULT '',
    right_context      TEXT NOT NULL DEFAULT '',
    orig_start         INTEGER NOT NULL,
    orig_end           INTEGER NOT NULL,
    created_version_id INTEGER NOT NULL REFERENCES versions (id),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每条批注在每个版本上的迁移结果（只追加）
CREATE TABLE IF NOT EXISTS locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id INTEGER NOT NULL REFERENCES annotations (id),
    version_id    INTEGER NOT NULL REFERENCES versions (id),
    status        TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved')),
    start         INTEGER,
    "end"         INTEGER,
    left_match    INTEGER,
    right_match   INTEGER,
    score         INTEGER,
    candidates    TEXT NOT NULL DEFAULT '[]',  -- JSON：全部候选范围及各自匹配依据
    carried_from  INTEGER REFERENCES locations (id),  -- 非空表示歧义未确认而顺延
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (annotation_id, version_id)
);

-- 人工确认（只追加；确认后对应 location 的生效状态变为 confirmed）
CREATE TABLE IF NOT EXISTS confirmations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations (id),
    start       INTEGER NOT NULL,
    "end"       INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 批注间的审阅依赖边：前置批注 → 后续批注（只追加，限同一文档内，无环）
CREATE TABLE IF NOT EXISTS dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents (id),
    prerequisite_id INTEGER NOT NULL REFERENCES annotations (id),
    dependent_id    INTEGER NOT NULL REFERENCES annotations (id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, prerequisite_id, dependent_id),
    CHECK (prerequisite_id <> dependent_id)
);

-- 批注完成记录（只追加；一条批注至多一条，重复提交幂等）
CREATE TABLE IF NOT EXISTS completions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents (id),
    annotation_id INTEGER NOT NULL REFERENCES annotations (id),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (annotation_id)
);

-- 文档发布门禁策略修订（按名称只追加：同名提交一律生成递增 revision，
-- 旧修订不可改、不可删；strategy_json 为提交时原样保留的策略树）
CREATE TABLE IF NOT EXISTS policy_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    strategy_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (name, revision)
);

CREATE INDEX IF NOT EXISTS idx_versions_doc    ON versions (document_id, number);
CREATE INDEX IF NOT EXISTS idx_locations_ann   ON locations (annotation_id);
CREATE INDEX IF NOT EXISTS idx_locations_ver   ON locations (version_id);
CREATE INDEX IF NOT EXISTS idx_confirm_loc     ON confirmations (location_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_doc ON dependencies (document_id);
CREATE INDEX IF NOT EXISTS idx_completions_doc  ON completions (document_id);
CREATE INDEX IF NOT EXISTS idx_policy_name      ON policy_revisions (name, revision);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None：自动提交；多语句写入用 transaction() 显式包裹
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def transaction(con: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：写操作串行化，保证版本号等自增逻辑无竞态。"""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


@contextmanager
def read_transaction(con: sqlite3.Connection):
    """BEGIN 只读事务：多次 SELECT 落在同一快照（WAL 下快照隔离）。

    供迁移归档导出使用：文档元数据、版本、批注、迁移记录与人工确认
    必须在同一个读事务中取出，避免导出过程中并发写入造成行间不一致。
    """
    con.execute("BEGIN")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = connect()
    try:
        con.executescript(SCHEMA)
    finally:
        con.close()
