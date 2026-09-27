"""SQLite 存储层。

版本、迁移结果、审阅依赖、完成记录与门禁策略修订均只追加（append-only）：
不提供任何 UPDATE/DELETE 路径，人工确认以新行追加到 confirmations 表，
同名门禁策略的每次提交都以递增 revision 追加到 policy_revisions 表。

人工确认在数据库层也有硬性不变量：``confirmations.location_id`` 带唯一索引
（每个 location 至多一条确认）。该约束在 :func:`init_db` 时以幂等迁移方式
建立——历史无重复库平滑建索引；历史有重复时**绝不静默删改**，而是抛出
:class:`DuplicateConfirmationsError` 并给出可定位的 location 与确认行 ID，
需人工核查后再启动。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List

logger = logging.getLogger(__name__)

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
-- 每个 location 至多一条确认：location_id 的唯一索引不由本建表脚本创建，
-- 而由 init_db() 的幂等迁移 _ensure_confirmation_unique() 建立——
-- 以便对旧库先做历史重复核查（有重复则拒绝启动并报告 location，不删改数据），
-- 无重复时再平滑建索引（唯一索引名为 idx_confirm_loc_unique）。
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
CREATE INDEX IF NOT EXISTS idx_dependencies_doc ON dependencies (document_id);
CREATE INDEX IF NOT EXISTS idx_completions_doc  ON completions (document_id);
CREATE INDEX IF NOT EXISTS idx_policy_name      ON policy_revisions (name, revision);
"""

# 每个 location 至多一条人工确认的唯一索引（由幂等迁移建立，见模块说明）
CONFIRMATION_UNIQUE_INDEX = "idx_confirm_loc_unique"
# 旧版本建表脚本创建的非唯一同名前缀索引，建唯一索引前需将其摘除
CONFIRMATION_LEGACY_INDEX = "idx_confirm_loc"


class DuplicateConfirmationsError(RuntimeError):
    """历史库中同一 location 已存在多条人工确认，无法建立唯一约束。

    ``duplicates`` 为每个存在重复的 location 给出可定位信息：

    - ``location_id`` / ``annotation_id`` / ``version_number`` / ``document_id``：
      重复确认所指歧义记录及其归属；
    - ``confirmation_ids`` 与 ``confirmations``：该 location 下全部确认行 ID
      （升序）及各行所选 ``[start, end)``、备注与写入时间，供人工核查首条归属。

    迁移**不删除、不合并**任何行；需人工核查解决后重新启动。
    """

    def __init__(self, duplicates: List[Dict]):
        self.duplicates = duplicates
        locations = ", ".join(str(item["location_id"]) for item in duplicates)
        super().__init__(
            "检测到历史人工确认重复，无法建立每个 location 至多一条确认的唯一约束；"
            f"涉及 location_id：{locations}（重复行未被删改，请人工核查后重启）"
        )


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
        _ensure_confirmation_unique(con)
    finally:
        con.close()


def _find_duplicate_confirmations(con: sqlite3.Connection) -> List[Dict]:
    """返回所有存在多条确认的 location 及其可定位明细（不做任何删改）。"""
    groups = con.execute(
        """
        SELECT location_id
        FROM confirmations
        GROUP BY location_id
        HAVING COUNT(*) > 1
        ORDER BY location_id
        """
    ).fetchall()
    if not groups:
        return []
    duplicates: List[Dict] = []
    for group in groups:
        location_id = group["location_id"]
        rows = con.execute(
            """
            SELECT id, start, "end", note, created_at
            FROM confirmations WHERE location_id = ? ORDER BY id
            """,
            (location_id,),
        ).fetchall()
        loc = con.execute(
            """
            SELECT l.annotation_id, l.version_id, a.document_id, v.number
            FROM locations l
            LEFT JOIN annotations a ON a.id = l.annotation_id
            LEFT JOIN versions v ON v.id = l.version_id
            WHERE l.id = ?
            """,
            (location_id,),
        ).fetchone()
        duplicates.append(
            {
                "location_id": location_id,
                "annotation_id": loc["annotation_id"] if loc else None,
                "version_number": loc["number"] if loc else None,
                "document_id": loc["document_id"] if loc else None,
                "confirmation_ids": [row["id"] for row in rows],
                "confirmations": [
                    {
                        "id": row["id"],
                        "start": row["start"],
                        "end": row["end"],
                        "note": row["note"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            }
        )
    return duplicates


def _ensure_confirmation_unique(con: sqlite3.Connection) -> None:
    """幂等迁移：保证 ``confirmations.location_id`` 上存在唯一索引。

    - 已有同名唯一索引：直接返回（重复启动无副作用）；
    - 旧库无该索引且数据无重复：摘除旧版非唯一索引（若有），建立唯一索引；
    - 旧库存在历史重复：抛出 :class:`DuplicateConfirmationsError`，
      不建立索引、不删除或合并任何确认行。

    新库（由 SCHEMA 新建、confirmations 为空）同样走本函数建立索引。
    """
    listing = con.execute("PRAGMA index_list(confirmations)").fetchall()
    for entry in listing:
        if entry["name"] == CONFIRMATION_UNIQUE_INDEX and entry["unique"]:
            return

    duplicates = _find_duplicate_confirmations(con)
    if duplicates:
        for item in duplicates:
            logger.error(
                "历史人工确认重复：document_id=%s annotation_id=%s "
                "version_number=%s location_id=%s confirmation_ids=%s；"
                "唯一索引未建立，重复行未被删改，请人工核查后重启",
                item["document_id"],
                item["annotation_id"],
                item["version_number"],
                item["location_id"],
                item["confirmation_ids"],
            )
        raise DuplicateConfirmationsError(duplicates)

    with transaction(con):
        # 旧版本建表脚本可能留有非唯一索引，摘除后以唯一索引替代
        con.execute(f"DROP INDEX IF EXISTS {CONFIRMATION_LEGACY_INDEX}")
        con.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {CONFIRMATION_UNIQUE_INDEX} "
            "ON confirmations (location_id)"
        )
